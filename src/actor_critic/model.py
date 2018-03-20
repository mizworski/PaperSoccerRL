import os
import re
from multiprocessing import Pipe, Queue

import tensorflow as tf

from src.actor_critic.policy_network import CnnPolicy
from src.actor_critic.utils import mse, Scheduler

log_dir = 'models/logs/'


class ParallelModel(object):
    def __init__(self, ob_space, ac_space, batch_size, vf_coef=0.5, max_grad_norm=0.5, lr=1e-8,
                 lrschedule='linear', training_timesteps=int(1e6),
                 model_dir='models/actor_critic', momentum=0.9):
        self.model = Model(ob_space, ac_space, batch_size, vf_coef, max_grad_norm, lr, lrschedule, training_timesteps,
                           model_dir, momentum)

        self.training_timestep = self.model.training_timestep
        self.train = self.model.train
        self.train_model = self.model.train_model
        self.step_model = self.model.step_model
        self.value = self.model.step_model.value
        self.save = self.model.save
        self.update_best_player = self.model.update_best_player

        self.queue = Queue()
        self.initial_checkpoint_number = self.model.initial_checkpoint_number

        def step(state):
            parent_conn, child_conn = Pipe()
            self.queue.put((child_conn, state))

            return parent_conn.recv()

        self.step = step


class Model(object):
    def __init__(self, ob_space, ac_space, batch_size, vf_coef=0.5, max_grad_norm=0.5, lr=1e-8,
                 lrschedule='linear', training_timesteps=int(1e6),
                 model_dir='models/actor_critic', momentum=0.9):

        training_player_scope = 'training_player'
        best_player_scope = 'best_player'

        config = tf.ConfigProto(allow_soft_placement=True)
        config.gpu_options.allow_growth = True

        sess = tf.Session(config=config)
        n_act = ac_space.n

        PI = tf.placeholder(tf.float32, [batch_size, n_act], name='pi')
        R = tf.placeholder(tf.float32, [batch_size], name='reward')
        LR = tf.placeholder(tf.float32, [], name='learning_rate')

        step_model = CnnPolicy(sess, ob_space, n_act, best_player_scope, reuse=False)
        train_model = CnnPolicy(sess, ob_space, n_act, training_player_scope, reuse=False)
        with tf.variable_scope('loss'):
            logits = train_model.logits

            with tf.variable_scope('actor_loss'):
                cross_entropy = tf.nn.sigmoid_cross_entropy_with_logits(logits=logits, labels=PI)
                pg_loss = tf.reduce_mean(cross_entropy)
                tf.summary.scalar('actor cross_entropy', cross_entropy)

            with tf.variable_scope('critic_loss'):
                vf_loss = tf.reduce_mean(mse(tf.squeeze(train_model.vf), R))
                tf.summary.scalar('critic mse', vf_loss)
            with tf.variable_scope('regularization_loss'):
                # entropy = tf.reduce_mean(cat_entropy(train_model.logits))
                reg_loss = tf.losses.get_regularization_losses()
                tf.summary.scalar('reg_loss', reg_loss)

            loss = pg_loss + vf_loss * vf_coef + reg_loss

        params = tf.trainable_variables(scope=training_player_scope)
        grads = tf.gradients(loss, params)
        if max_grad_norm is not None:
            grads, grad_norm = tf.clip_by_global_norm(grads, max_grad_norm)
        grads = list(zip(grads, params))

        trainer = tf.train.MomentumOptimizer(learning_rate=LR, momentum=momentum)
        # trainer = tf.train.AdamOptimizer(learning_rate=LR)
        # trainer = tf.train.GradientDescentOptimizer(learning_rate=LR)
        _train = trainer.apply_gradients(grads)

        lr = Scheduler(v=lr, n_values=training_timesteps, schedule=lrschedule)

        saver = tf.train.Saver()

        self.training_timestep = 0

        def train(state, pi, rewards):
            cur_lr = lr.value()
            td_map = {train_model.X: state, PI: pi, R: rewards, LR: cur_lr}

            policy_loss, value_loss, _ = sess.run(
                [pg_loss, vf_loss, _train],
                td_map
            )

            return policy_loss, value_loss

        def save_model(step=0):
            model_path = os.path.join(model_dir, 'model.ckpt')
            saver.save(sess, model_path, global_step=step)
            print('Successfully saved step={}'.format(step))

        def update_best_player():
            best_player_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=best_player_scope)
            train_player_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=training_player_scope)

            copy_vars = []

            for best_model_var, train_player_var in zip(best_player_vars, train_player_vars):
                copy_var = best_model_var.assign(train_player_var)
                copy_vars.append(copy_var)

            sess.run(copy_vars)

        # writer = tf.summary.FileWriter(logdir='models/logs', graph=tf.Graph())
        # writer.flush()

        self.train = train
        self.train_model = train_model
        self.step_model = step_model
        self.step = step_model.step
        self.value = step_model.value
        self.save = save_model
        self.update_best_player = update_best_player

        latest_checkpoint = tf.train.latest_checkpoint(model_dir)

        if latest_checkpoint is None:
            tf.global_variables_initializer().run(session=sess)
            self.initial_checkpoint_number = 1
            print('No checkpoint found. Starting new model.')
        else:
            saver.restore(sess, save_path=latest_checkpoint)
            self.initial_checkpoint_number = int(re.findall(r'\d+', latest_checkpoint)[-1])
            print('Loaded checkpoint {}'.format(latest_checkpoint))

        # initialize training player with current best player
        self.update_best_player()
