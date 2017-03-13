import numpy as np
import tensorflow as tf
import argparse
from sklearn import metrics
from tqdm import tqdm

from dataset import process_stateful, load_music_styles, get_all_files, compute_beat, compute_completion
from music import *
from midi_util import *
from util import chunk
from constants import NUM_STYLES, styles
from keras.layers.recurrent import GRU

NUM_NOTES = MAX_NOTE - MIN_NOTE
BATCH_SIZE = 32
TIME_STEPS = 16
model_file = 'out/saves/model'

class Model:
    def __init__(self, batch_size=BATCH_SIZE, time_steps=TIME_STEPS, training=True, dropout=0.5, activation=tf.nn.tanh, rnn_layers=1):
        dropout_keep_prob = 0.5 if training else 1

        self.init_states = []
        self.final_states = []

        def repeat(x):
            return np.reshape(np.repeat(x, batch_size * time_steps), [batch_size, time_steps, -1])

        def rnn(units):
            """
            Recurrent layer
            """
            def f(x):
                cell = tf.contrib.rnn.GRUCell(units, activation=activation)
                # cell = tf.contrib.rnn.MultiRNNCell([cell] * rnn_layers)
                # Initial state of the memory.
                init_state = cell.zero_state(batch_size, tf.float32)
                rnn_out, final_state = tf.nn.dynamic_rnn(cell, x, initial_state=init_state)
                rnn_out = tf.layers.dropout(inputs=rnn_out, rate=dropout, training=training)
                self.init_states.append(init_state)
                self.final_states.append(final_state)
                return rnn_out
            return f

        def rnn_conv(name, units, filter_size, stride=1, include_note_pitch=False):
            """
            Recurrent convolution Layer.
            Given a tensor of shape [batch_size, time_steps, features, channels],
            outputs a tensor of shape [batch_size, time_steps, features, channels]
            """
            def f(x, contexts):
                num_features = int(x.get_shape()[2])
                # num_channels = int(x.get_shape()[3])

                outs = []

                if num_features % stride != 0:
                    print('Warning! Stride not divisible.', num_features, stride)

                print('Layer {}: units={} len={} filter={} stride={}'.format(name, units, num_features, filter_size, stride))

                # Convolve every channel independently
                for i in range(0, num_features, stride):
                    with tf.variable_scope(name, reuse=len(outs) > 0):
                        inv_input = [x[:, :, i:i+filter_size], contexts]

                        # Include the context of how high the current input is.
                        if include_note_pitch:
                            # Position of note and pitch class of note
                            inv_input += [
                                tf.constant(repeat(i / (num_features - 1)), dtype='float'),
                                tf.constant(repeat(one_hot(i % OCTAVE, OCTAVE)), dtype='float')
                            ]

                        inv_input = tf.concat(inv_input, 2)
                        outs.append(rnn(units)(inv_input))
                        if i + filter_size == num_features:
                            break
                out = tf.concat(outs, 2)
                # Perform max pooling
                # out = tf.nn.pool(out, [2], strides=[2], pooling_type='MAX', padding='VALID', data_format='NCW')
                assert out.get_shape()[0] == batch_size
                assert out.get_shape()[1] == time_steps
                return out
            return f

        def time_axis_block(name, num_units=128):
            """
            Recurrent convolution Layer.
            Given a tensor of shape [batch_size, time_steps, features, channels],
            outputs a tensor of shape [batch_size, time_steps, features, channels]
            """
            def f(x, contexts):
                outs = []

                pitch_class_bins = tf.reduce_sum([x[:, :, i*OCTAVE:i*OCTAVE+OCTAVE] for i in range(NUM_OCTAVES)], axis=0)
                print('Pitch class bins', pitch_class_bins)

                # Pad by one octave
                x = tf.pad(x, [[0, 0], [0, 0], [OCTAVE, OCTAVE]])
                print('Padded note input by octave', x)

                # Process every note independently
                for i in range(OCTAVE, NUM_NOTES + OCTAVE):
                    with tf.variable_scope(name, reuse=len(outs) > 0):
                        inv_input = tf.concat([
                            x[:, :, i - OCTAVE:i + OCTAVE + 1],
                            contexts,
                            # Position of note
                            tf.constant(repeat(i / (NUM_NOTES - 1)), dtype='float'),
                            # Pitch class of current note
                            tf.constant(repeat(one_hot(i % OCTAVE, OCTAVE)), dtype='float'),
                            pitch_class_bins
                        ], 2)

                        outs.append(rnn(num_units)(inv_input))

                # Stack all outputs into a new dimension
                out = tf.stack(outs, axis=2)

                print(name, out)
                assert out.get_shape()[0] == batch_size
                assert out.get_shape()[1] == time_steps
                assert out.get_shape()[2] == NUM_NOTES
                assert out.get_shape()[3] == num_units

                return out
            return f

        def note_axis_block(name, num_units=64):
            """
            The pitch block that conditions each note's generation on the
            previous note within one time step.
            """
            def f(x, target):
                """
                Parameters:
                    x - The output of the time axis layer.
                        [batch, time_steps, notes, features]
                    target - The target output for training.
                              [batch, time_steps, notes]
                """
                # TODO: Could try using non-recurrent network.

                num_time_steps = x.get_shape()[1]
                outs = []

                # Every time slice has a note-axis RNN
                for t in range(num_time_steps):
                    # [batch, notes, features]
                    input_for_time = x[:, t, :, :]
                    # [batch, notes, 1]
                    target_for_time = tf.expand_dims(target[:, t, :], -1)
                    # Shift target vector for prediction
                    target_for_time = tf.pad(target_for_time, [[0, 0], [1, 0], [0, 0]])
                    # Remove last note
                    target_for_time = target_for_time[:, :-1, :]

                    assert target_for_time.get_shape()[0] == input_for_time.get_shape()[0]
                    assert target_for_time.get_shape()[1] == NUM_NOTES
                    assert target_for_time.get_shape()[2] == 1

                    rnn_input = tf.concat([
                        # Features for each note
                        input_for_time,
                        # Conditioned on the previously generated note
                        target_for_time
                    ], 2)

                    with tf.variable_scope(name, reuse=len(outs) > 0):
                        rnn_out = rnn(num_units)(rnn_input)

                        # Dense prediction layer
                        rnn_out = tf.layers.dense(inputs=rnn_out, units=1)
                        rnn_out = tf.squeeze(rnn_out)
                        outs.append(rnn_out)

                # Merge note-axis outputs for each time step.
                out = tf.stack(outs, axis=1)

                print(name, out)
                assert out.get_shape()[0] == batch_size
                assert out.get_shape()[1] == time_steps
                assert out.get_shape()[2] == NUM_NOTES

                return out
            return f

        """
        Input
        """
        # Input note (multi-hot vector)
        note_in = tf.placeholder(tf.float32, [batch_size, time_steps, NUM_NOTES], name='note_in')
        # Input beat (clock representation)
        beat_in = tf.placeholder(tf.float32, [batch_size, time_steps, 2], name='beat_in')
        # Input progress (scalar representation)
        progress_in = tf.placeholder(tf.float32, [batch_size, time_steps, 1], name='progress_in')
        # Style bias (one-hot representation)
        style_in = tf.placeholder(tf.float32, [batch_size, time_steps, NUM_STYLES], name='style_in')

        # Target note to predict
        note_target = tf.placeholder(tf.float32, [batch_size, time_steps, NUM_NOTES], name='target_in')

        # Context to help generation
        contexts = tf.concat([beat_in, progress_in], 2)

        # Note input
        out = note_in
        print('note_in', out)

        """
        Pitch class binning:
        Count the number of pitch classes occurences
        """

        out = time_axis_block('time_axis_block')(out, contexts)
        out = note_axis_block('note_axis_block')(out, note_target)

        """
        Sigmoid Layer
        """
        # Next note predictions
        logits = out
        self.prob = tf.nn.sigmoid(logits)
        # Classification prediction for f1 score
        self.pred = tf.round(self.prob)

        """
        Loss
        """
        total_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=logits, labels=note_target))
        train_step = tf.train.AdamOptimizer().minimize(total_loss)

        """
        Set instance vars
        """
        self.note_in = note_in
        self.beat_in = beat_in
        self.progress_in = progress_in
        self.style_in = style_in
        self.note_target = note_target

        self.loss = total_loss
        self.train_step = train_step

        # Saver
        with tf.device('/cpu:0'):
            self.saver = tf.train.Saver()

        """
        Statistics
        """
        tf.summary.scalar('loss', total_loss)
        self.merged_summaries = tf.summary.merge_all()

    def train(self, sess, train_seqs, num_epochs, verbose=True):
        writer = tf.summary.FileWriter('out/summary', sess.graph, flush_secs=3)
        total_steps = 0
        patience = 10
        no_improvement = 0
        best_fscore = 0

        for epoch in range(num_epochs):
            # Metrics
            training_loss = 0
            f1_score = 0
            step = 0

            # Shuffle sequence orders.
            order = np.random.permutation(len(train_seqs))
            t = tqdm(order)
            t.set_description('{}/{}'.format(epoch + 1, num_epochs))

            # Train every single sequence
            for i in t:
                seq = train_seqs[i]
                # Reset state
                states = [None for _ in self.init_states]

                for note_in, beat_in, progress_in, label in tqdm(seq):
                    # Build feed-dict
                    feed_dict = {
                        self.note_in: note_in,
                        self.beat_in: beat_in,
                        self.progress_in: progress_in,
                        # self.style_in: X[3],
                        self.note_target: label
                    }

                    for tf_s, s in zip(self.init_states, states):
                        if s is not None:
                            feed_dict[tf_s] = s

                    pred, summary, t_loss, _, *states = sess.run([
                            self.pred,
                            self.merged_summaries,
                            self.loss,
                            self.train_step
                        ] + self.final_states,
                        feed_dict
                    )

                    # Add summary to Tensorboard
                    writer.add_summary(summary, total_steps)

                    training_loss += t_loss
                    step += 1
                    # Compute F-1 score of all timesteps and batches
                    # For every single sample in the batch
                    f1_score += np.mean([metrics.f1_score(y, p, average='weighted') for y, p in zip(label, pred)])
                    t.set_postfix(loss=training_loss / step, f1=f1_score / step)

                    if total_steps % 1000 == 0:
                        # Early stopping
                        if f1_score > best_fscore:
                            self.saver.save(sess, model_file)
                            best_fscore = f1_score
                            no_improvement = 0
                        else:
                            no_improvement += 1

                            if no_improvement > patience:
                                break

                    total_steps += 1

        # Save the last epoch
        self.saver.save(sess, model_file)

    def generate(self, sess, inspiration=None, length=NOTES_PER_BAR * 16):
        total_len = length + (len(inspiration) if inspiration is not None else 0)
        # Resulting generation
        results = []
        # Reset state
        states = [None for _ in self.init_states]

        # Current note
        current_note = np.zeros(NUM_NOTES)
        current_beat = compute_beat(0, NOTES_PER_BAR)
        current_progress = compute_completion(0, total_len)

        for i in range(total_len):
            # Build feed dict
            feed_dict = {
                self.note_in: [[current_note]],
                self.beat_in: [[current_beat]],
                self.progress_in: [[current_progress]]
            }

            for tf_s, s in zip(self.init_states, states):
                if s is not None:
                    feed_dict[tf_s] = s

            prob, *states = sess.run([self.prob] + self.final_states, feed_dict)

            if inspiration is not None and i < len(inspiration):
                # Priming notes
                current_note = inspiration[i]
            else:
                prob = prob[0][0]
                # Randomly choose classes for each class
                current_note = np.zeros(NUM_NOTES)

                for n in range(NUM_NOTES):
                    current_note[n] = 1 if np.random.random() <= prob[n] else 0

                results.append(current_note)
        return results

def reset_graph():
    if 'sess' in globals() and sess:
        sess.close()
    tf.reset_default_graph()

def stagger(data, time_steps):
    dataX, dataY = [], []

    # First note prediction
    data = [np.zeros_like(data[0])] + list(data)

    for i in range(len(data) - time_steps - 1):
        dataX.append(data[i:(i + time_steps)])
        dataY.append(data[i + 1:(i + time_steps + 1)])
    return dataX, dataY

def process(sequences):
    train_seqs = []

    for seq in sequences:
        train_data, label_data = stagger(seq, TIME_STEPS)

        beat_data = [compute_beat(i, NOTES_PER_BAR) for i in range(len(seq))]
        beat_data, _ = stagger(beat_data, TIME_STEPS)

        progress_data = [compute_completion(i, len(seq)) for i in range(len(seq))]
        progress_data, _ = stagger(progress_data, TIME_STEPS)

        # Chunk into batches
        train_data = chunk(train_data, BATCH_SIZE)
        beat_data = chunk(beat_data, BATCH_SIZE)
        progress_data = chunk(progress_data, BATCH_SIZE)
        label_data = chunk(label_data, BATCH_SIZE)
        train_seqs.append(list(zip(train_data, beat_data, progress_data, label_data)))
    return train_seqs

def main():
    parser = argparse.ArgumentParser(description='Generates music.')
    parser.add_argument('--train', default=False, action='store_true', help='Train model?')
    parser.add_argument('--load', default=False, action='store_true', help='Load model?')
    args = parser.parse_args()

    print('Preparing training data')

    # Load training data
    # TODO: Cirriculum training. Increasing complexity. Increasing timestep details?
    # TODO: Random transpoe?
    # TODO: Random slices of subsequence?
    sequences = [load_midi(f) for f in get_all_files(['data/classical/bach'])]
    sequences = [np.minimum(np.ceil(m[:, MIN_NOTE:MAX_NOTE]), 1) for m in sequences]
    train_seqs = process(sequences)

    if args.train:
        with tf.Session() as sess:
            print('Training batch_size={} time_steps={}'.format(BATCH_SIZE, TIME_STEPS))
            train_model = Model()
            sess.run(tf.global_variables_initializer())
            if args.load:
                train_model.saver.restore(sess, model_file)
            else:
                sess.run(tf.global_variables_initializer())
            train_model.train(sess, train_seqs, 1000)

    reset_graph()

    with tf.Session() as sess:
        print('Generating...')
        gen_model = Model(1, 1, training=False)
        gen_model.saver.restore(sess, model_file)

        for s in range(5):
            print('s={}'.format(s))
            composition = gen_model.generate(sess)#,np.random.choice(sequences)[:NOTES_PER_BAR])
            composition = np.concatenate((np.zeros((len(composition), MIN_NOTE)), composition), axis=1)
            midi.write_midifile('out/result_{}.mid'.format(s), midi_encode(composition))

if __name__ == '__main__':
    main()
