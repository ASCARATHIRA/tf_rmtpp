import tensorflow as tf
import numpy as np
import decorated_options as Deco

def_opts = Deco.Options(
    hidden_layer_size=64,   # 64, 128, 256, 512, 1024
    batch_size=64,          # 16, 32, 64
    learning_rate=0.1,      # 0.1, 0.01, 0.001
    momentum=0.9,
    l2_penalty=0.001,
    embed_size=64,
    float_type=tf.float32,
    seed=42,
    scope="RMTPP",

    bptt=10
)


class RMTPP:
    """Class implementing the Recurrent Marked Temporal Point Process model."""

    @Deco.optioned()
    def __init__(self, sess, num_categories, hidden_layer_size, batch_size,
                 learning_rate, momentum, l2_penalty, embed_size,
                 float_type, bptt, seed, scope):
        self.HIDDEN_LAYER_SIZE = hidden_layer_size
        self.BATCH_SIZE = batch_size
        self.LEARNING_RATE = learning_rate
        self.MOMENTUM = momentum
        self.L2_PENALTY = l2_penalty
        self.EMBED_SIZE = embed_size
        self.BPTT = bptt

        self.NUM_CATEGORIES = num_categories
        self.FLOAT_TYPE = float_type

        self.sess = sess
        self.seed = seed
        self.last_epoch = 0

        with tf.variable_scope(scope):
            with tf.device('/gpu:0'):
                # Make input variables
                self.events_in = tf.placeholder(tf.int32, [None, self.BPTT])
                self.times_in = tf.placeholder(self.FLOAT_TYPE, [None, self.BPTT])

                self.events_out = tf.placeholder(tf.int32, [None, self.BPTT])
                self.times_out = tf.placeholder(self.FLOAT_TYPE, [None, self.BPTT])

                self.inf_batch_size = tf.shape(self.events_in)[0]

                # Make variables
                with tf.variable_scope('hidden_state'):
                    self.Wt = tf.get_variable(name='Wt', shape=(1, self.HIDDEN_LAYER_SIZE),
                                              dtype=self.FLOAT_TYPE)
                    # The first row of Wem is merely a placeholder (will not be trained).
                    self.Wem = tf.get_variable(name='Wem', shape=(self.NUM_CATEGORIES + 1, self.EMBED_SIZE),
                                               dtype=self.FLOAT_TYPE)
                    self.Wh = tf.get_variable(name='Wh', shape=(self.HIDDEN_LAYER_SIZE, self.HIDDEN_LAYER_SIZE),
                                              dtype=self.FLOAT_TYPE)
                    self.bh = tf.get_variable(name='bh', shape=(1, self.HIDDEN_LAYER_SIZE),
                                              dtype=self.FLOAT_TYPE)

                with tf.variable_scope('output'):
                    self.wt = tf.get_variable(name='wt', shape=(1, 1),
                                              dtype=self.FLOAT_TYPE)

                    self.Wy = tf.get_variable(name='Wy', shape=(self.EMBED_SIZE, self.HIDDEN_LAYER_SIZE),
                                              dtype=self.FLOAT_TYPE)

                    # The first column of Vy is merely a placeholder (will not be trained).
                    self.Vy = tf.get_variable(name='Vy', shape=(self.HIDDEN_LAYER_SIZE, self.NUM_CATEGORIES + 1),
                                              dtype=self.FLOAT_TYPE)
                    self.Vt = tf.get_variable(name='Vt', shape=(self.HIDDEN_LAYER_SIZE, 1),
                                              dtype=self.FLOAT_TYPE,
                                              initializer=tf.uniform_unit_scaling_initializer())
                    self.bt = tf.get_variable(name='bt', shape=(1, 1),
                                              dtype=self.FLOAT_TYPE)
                    self.bk = tf.get_variable(name='bk', shape=(1, self.NUM_CATEGORIES + 1),
                                              dtype=self.FLOAT_TYPE)

                # Make graph
                # RNNcell = RNN_CELL_TYPE(HIDDEN_LAYER_SIZE)

                # Initial state for GRU cells
                self.initial_state = state = tf.zeros([self.inf_batch_size, self.HIDDEN_LAYER_SIZE], dtype=self.FLOAT_TYPE, name='hidden_state')

                self.loss = 0.0
                batch_ones = tf.ones((self.inf_batch_size, 1), dtype=self.FLOAT_TYPE)
                for i in range(self.BPTT):
                    events_embedded = tf.nn.embedding_lookup(self.Wem, self.events_in[:, i])
                    time = tf.expand_dims(self.times_in[:, i], axis=-1)

                    # output, state = RNNcell(events_embedded, state)
                    # TODO Does TF automatically broadcast? Then we'll not need multiplication
                    # with tf.ones

                    self.state = tf.clip_by_value(
                        tf.matmul(state, self.Wh) +
                        tf.matmul(events_embedded, self.Wy) +
                        tf.matmul(time, self.Wt) +
                        tf.matmul(batch_ones, self.bh),
                        0.0, 1e6,
                        name='h_t')

                    base_intensity = tf.matmul(batch_ones, self.bt)
                    delta_t = tf.expand_dims(self.times_out[:, i] - self.times_in[:, i], axis=-1)
                    log_lambda_ = (tf.matmul(state, self.Vt) +
                                   delta_t * self.wt +
                                   base_intensity)

                    lambda_ = tf.exp(tf.minimum(50.0, log_lambda_), name='lambda_')
                    wt_non_zero = tf.sign(self.wt) * tf.maximum(1e-6, tf.abs(self.wt))
                    log_f_star = (log_lambda_ +
                                  (1.0 / wt_non_zero) * tf.exp(tf.minimum(50.0, tf.matmul(state, self.Vt) + base_intensity)) -
                                  (1.0 / wt_non_zero) * lambda_)

                    events_pred = tf.nn.softmax(
                        tf.minimum(50.0,
                                   tf.matmul(state, self.Vy) + batch_ones * self.bk),
                        name='Pr_events'
                    )

                    time_loss = log_f_star
                    mark_loss = tf.expand_dims(
                        tf.log(
                            tf.maximum(
                                1e-6,
                                tf.gather_nd(
                                    events_pred,
                                    tf.concat([
                                        tf.expand_dims(tf.range(self.inf_batch_size), -1),
                                        tf.expand_dims(self.events_out[:, i], -1)
                                    ], axis=1, name='Pr_next_event'
                                    )
                                )
                            )
                        ), axis=-1, name='log_Pr_next_event'
                    )
                    step_loss = time_loss + mark_loss

                    # In the batch some of the sequences may have ended before we get to the
                    # end of the seq. In such cases, the events will be zero.
                    # TODO Figure out how to do this with RNNCell, LSTM, etc.
                    num_events = tf.reduce_sum(tf.where(self.events_in[:, i] > 0,
                                               tf.ones(shape=(self.inf_batch_size,), dtype=self.FLOAT_TYPE),
                                               tf.zeros(shape=(self.inf_batch_size,), dtype=self.FLOAT_TYPE)),
                                               name='num_events')
                    self.loss -= tf.cond(num_events > 0,
                                         lambda: tf.reduce_sum(
                                             tf.where(self.events_in[:, i] > 0,
                                                      tf.squeeze(step_loss) / num_events,
                                                      tf.zeros(shape=(self.inf_batch_size,))),
                                             name='batch_bptt_loss'),
                                         lambda: 0.0)

                self.final_state = state

                with tf.device('/cpu:0'):
                    # Global step needs to be on the CPU (Why?)
                    self.global_step = tf.Variable(0, name='global_step', trainable=False)

                self.learning_rate = tf.train.inverse_time_decay(self.LEARNING_RATE, global_step=self.global_step,
                                                                 decay_steps=10.0, decay_rate=.001)
                # self.global_step is incremented automatically by the
                # optimizer.

                # self.increment_global_step = tf.assign(
                #     self.global_step,
                #     self.global_step + 1,
                #     name='update_global_step'
                # )

                # self.optimizer = tf.train.GradientDescentOptimizer(learning_rate=self.learning_rate)

                self.optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate,
                                                          beta1=self.MOMENTUM)

                # Capping the gradient before minimizing.
                # update = optimizer.minimize(loss)

                # Performing manual gradient clipping.
                self.gvs = self.optimizer.compute_gradients(self.loss)
                # update = optimizer.apply_gradients(gvs)

                # capped_gvs = [(tf.clip_by_norm(grad, 100.0), var) for grad, var in gvs]
                grads, vars_ = list(zip(*self.gvs))
                self.norm_grads, self.global_norm = tf.clip_by_global_norm(grads, 100.0)
                capped_gvs = list(zip(self.norm_grads, vars_))

                self.update = self.optimizer.apply_gradients(capped_gvs,
                                                             global_step=self.global_step)

                self.tf_init = tf.global_variables_initializer()
                # self.check_nan = tf.add_check_numerics_ops()

    def initialize(self, finalize=False):
        """Initialize the global trainable variables."""
        self.sess.run(self.tf_init)

        if finalize:
            # This prevents memory leaks by disallowing changes to the graph
            # after initialization.
            self.sess.graph.finalize()

    def train(self, training_data, num_epochs, check_nans=False):
        """Train the model given the training data."""
        rs = np.random.RandomState(seed=self.seed)

        train_event_in_seq = training_data['train_event_in_seq']
        train_time_in_seq = training_data['train_time_in_seq']
        train_event_out_seq = training_data['train_event_out_seq']
        train_time_out_seq = training_data['train_event_out_seq']

        idxes = list(range(len(train_event_in_seq)))
        n_batches = len(idxes) // self.BATCH_SIZE

        for epoch in range(self.last_epoch, self.last_epoch + num_epochs):
            rs.shuffle(idxes)

            print("Starting epoch...", epoch)
            total_loss = 0.0

            for batch_idx in range(n_batches):
                batch_idxes = idxes[batch_idx * self.BATCH_SIZE:(batch_idx + 1) * self.BATCH_SIZE]
                batch_event_train_in = train_event_in_seq[batch_idxes, :]
                batch_event_train_out = train_event_out_seq[batch_idxes, :]
                batch_time_train_in = train_time_in_seq[batch_idxes, :]
                batch_time_train_out = train_time_out_seq[batch_idxes, :]

                cur_state = np.zeros((self.BATCH_SIZE, self.HIDDEN_LAYER_SIZE))
                batch_loss = 0.0

                for bptt_idx in range(0, len(batch_event_train_in[0]) - self.BPTT, self.BPTT):
                    bptt_range = range(bptt_idx, (bptt_idx + self.BPTT))
                    bptt_event_in = batch_event_train_in[:, bptt_range]
                    bptt_event_out = batch_event_train_out[:, bptt_range]
                    bptt_time_in = batch_time_train_in[:, bptt_range]
                    bptt_time_out = batch_time_train_out[:, bptt_range]

                    feed_dict = {
                        self.initial_state: cur_state,
                        self.events_in: bptt_event_in,
                        self.events_out: bptt_event_out,
                        self.times_in: bptt_time_in,
                        self.times_out: bptt_time_out
                    }

                    if check_nans:
                        raise NotImplemented('tf.add_check_numerics_ops is '
                                             'incompatible with tf.cond and '
                                             'tf.while_loop.')
                        # _, _, cur_state, loss_ = \
                        #     self.sess.run([self.check_nan, self.update,
                        #                    self.final_state, self.loss],
                        #                   feed_dict=feed_dict)
                    else:
                        _, cur_state, loss_ = \
                            self.sess.run([self.update,
                                           self.final_state, self.loss],
                                          feed_dict=feed_dict)
                    batch_loss += loss_

                total_loss += batch_loss
                if batch_idx % 10 == 0:
                    print('Loss during batch {} last BPTT = {}, lr = {}'
                          .format(batch_idx, batch_loss, self.sess.run(self.learning_rate)))

            # self.sess.run(self.increment_global_step)
            print('Loss on last epoch = {}, new lr = {}, global_step = {}'
                  .format(total_loss / n_batches,
                          self.sess.run(self.learning_rate),
                          self.sess.run(self.global_step)))

        self.last_epoch += self.num_epochs

    def predict(self, test_data):
        pass
