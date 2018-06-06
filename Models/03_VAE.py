import matplotlib.pyplot as plt
import tensorflow as tf
import numpy as np
import sys
import os

# Import MNIST loader and utility functions from 'utils.py' file
from utils import write_mnist_tfrecords, checkFolders, show_variables, add_suffix

# Import layer definitions from 'layers.py' file
from layers import dense, conv2d, conv2d_transpose, batch_norm

# Import parse function for tfrecords features  and EarlyStoppingHook from 'misc.py' file
from misc import _parse_mnist_image, EarlyStoppingHook

# Import Flags specifying model hyperparameters and training options
from flags import getFlags_VAE


# Class representation of network model
class Model(object):
    
    # Initialize model
    def __init__(self, data_count, flags):
        self.data_count = data_count

        # Read keys/values from flags and assign to self
        for key, val in flags.__dict__.items():
            if key not in self.__dict__.keys():
                self.__dict__[key] = val
                                        
        # Create tfrecords if file does not exist
        if not os.path.exists(os.path.join(self.data_dir,'training.tfrecords')):
            print("\n [ Creating tfrecords files ]\n")
            write_mnist_tfrecords(self.data_dir)

        # Initialize datasets for training, validation, and early stopping checks
        self.initialize_datasets()
        
        # Define tensor for updating global step
        self.global_step = tf.train.get_or_create_global_step()

        # Build graph for network model
        self.build_model()

    # Initialize datasets
    def initialize_datasets(self, stopping_size=14000):

        # Define iterator for training dataset
        self.dataset = tf.data.TFRecordDataset(os.path.join(self.data_dir, 'training.tfrecords'))
        self.dataset = self.dataset.map(_parse_mnist_image)
        self.dataset = self.dataset.apply(tf.contrib.data.shuffle_and_repeat(self.batch_size*5))
        self.dataset = self.dataset.batch(self.batch_size)
        self.dataset = self.dataset.prefetch(self.batch_size*5)
        self.dataset = self.dataset.make_one_shot_iterator()
        
        # Define iterator for training dataset
        self.vdataset = tf.data.TFRecordDataset(os.path.join(self.data_dir, 'validation.tfrecords'))
        self.vdataset = self.vdataset.map(_parse_mnist_image)
        self.vdataset = self.vdataset.apply(tf.contrib.data.shuffle_and_repeat(self.batch_size*5))
        self.vdataset = self.vdataset.batch(self.batch_size)
        self.vdataset = self.vdataset.prefetch(self.batch_size*5)
        self.vdataset = self.vdataset.make_one_shot_iterator()

        # Create early stopping batch from validation dataset
        self.edataset = tf.data.TFRecordDataset(os.path.join(self.data_dir, 'validation.tfrecords'))
        self.edataset = self.edataset.map(_parse_mnist_image)
        self.edataset = self.edataset.apply(tf.contrib.data.shuffle_and_repeat(stopping_size))
        self.edataset = self.edataset.batch(stopping_size)
        self.edataset = self.edataset.make_one_shot_iterator()

    # Specify session for model evaluations
    def set_session(self, sess):
        self.sess = sess

    # Reinitialize handles for datasets when restoring from checkpoint
    def reinitialize_handles(self):
        self.training_handle = self.sess.run(self.dataset.string_handle())
        self.validation_handle = self.sess.run(self.vdataset.string_handle())

    # Encoder component of VAE model
    def encoder(self, x, training=True, reuse=None, name=None):

        # [None, 28, 28, 1]  -->  [None, 14, 14, 64]
        h = conv2d(x, 64, kernel_size=4, strides=2, activation=tf.nn.leaky_relu, reuse=reuse, name='e_conv_1')

        # [None, 14, 14, 64] -->  [None, 7, 7, 128]
        h = conv2d(h, 128, kernel_size=4, strides=2, reuse=reuse, name='e_conv_2')
        h = batch_norm(h, training=training, reuse=reuse, name='e_bn_1')
        h = tf.nn.leaky_relu(h)

        # [None, 7, 7, 128]  -->  [None, 7*7*128]
        h = tf.reshape(h, [-1, 7*7*128])

        # [None, 7*7*128] -->  [None, 1024]
        h = dense(h, 1024, reuse=reuse, name='e_dense_1')
        h = batch_norm(h, training=training, reuse=reuse, name='e_bn_2')
        h = tf.nn.leaky_relu(h)

        # [None, 1024] -->  [None, 2*self.z_dim]
        h = dense(h, 2*self.z_dim, reuse=reuse, name='e_dense_2')

        # Assign names to final outputs
        mean = tf.identity(h[:,:self.z_dim], name=name+"_mean")
        log_sigma = tf.identity(h[:,self.z_dim:], name=name+"_log_sigma")
        return mean, log_sigma

    # Decoder component of VAE model
    def decoder(self, z, training=True, reuse=None, name=None):

        # [None, z_dim]  -->  [None, 1024]
        h = dense(z, 1024, reuse=reuse, name='d_dense_1')
        h = batch_norm(h, training=training, reuse=reuse, name='d_bn_1')
        h = tf.nn.relu(h)
        
        # [None, 1024]  -->  [None, 7*7*128]
        h = dense(h, self.min_res*self.min_res*self.min_chans, reuse=reuse, name='d_dense_2')
        h = batch_norm(h, training=training, reuse=reuse, name='d_bn_2')
        h = tf.nn.relu(h)

        # [None, 7*7*128]  -->  [None, 7, 7, 128]
        h = tf.reshape(h, [-1, self.min_res, self.min_res, self.min_chans])

        # [None, 7, 7, 128]  -->  [None, 14, 14, 64]
        h = conv2d_transpose(h, 64, kernel_size=4, strides=2, reuse=reuse, name='d_tconv_1')
        h = batch_norm(h, training=training, reuse=reuse, name='d_bn_3')
        h = tf.nn.relu(h)
                        
        # [None, 14, 14, 64]  -->  [None, 28, 28, 1]
        h = conv2d_transpose(h, 1, kernel_size=4, strides=2, activation=tf.nn.sigmoid, reuse=reuse, name='d_tconv_2')
                        
        # Assign name to final output
        return tf.identity(h, name=name)

    # Sample from multivariate Gaussian
    def sampleGaussian(self, mean, log_sigma, name=None):
        epsilon = tf.random_normal(tf.shape(log_sigma))
        return tf.identity(mean + epsilon * tf.exp(log_sigma), name=name)

    # Define sampler for generating self.z values
    def sample_z(self, batch_size):
        return np.random.normal(size=(batch_size, self.z_dim))

    # Compute marginal likelihood loss
    def compute_ml_loss(self, data, pred, name=None):
        ml_loss = -tf.reduce_mean(tf.reduce_sum(data*tf.log(pred) + \
                                                (1 - data)*tf.log(1 - pred), [1, 2, 3]), name=name)
        return ml_loss

    # Compute Kullback–Leibler (KL) divergence
    def compute_kl_loss(self, mean, log_sigma, name=None):
        kl_loss = 0.5*tf.reduce_mean(tf.reduce_sum(tf.square(mean) + \
                                                   tf.square(tf.exp(log_sigma)) - \
                                                   2.*log_sigma - 1., axis=[-1]), name=name)
        return kl_loss

    # Evaluate model on specified batch of data
    def evaluate_model(self, data, reuse=None, training=True, suffix=None):
        
        # Encode input images
        mean, log_sigma = self.encoder(data, training=training, reuse=reuse, name=add_suffix("encoder", suffix))

        # Sample latent vector
        z_sample = self.sampleGaussian(mean, log_sigma, name=add_suffix("latent_vector", suffix))

        # Decode latent vector back to original image
        pred = self.decoder(z_sample, training=training, reuse=reuse, name=add_suffix("pred", suffix))

        # Compute marginal likelihood loss
        ml_loss = self.compute_ml_loss(data, pred, name=add_suffix("ml_loss", suffix))
        
        # Compute Kullback–Leibler (KL) divergence
        kl_loss = self.compute_kl_loss(mean, log_sigma, name=add_suffix("kl_loss", suffix))
                
        # Define loss according to the evidence lower bound objective (ELBO)
        loss = tf.add(ml_loss, kl_loss, name=add_suffix("loss", suffix))

        return pred, loss, ml_loss, kl_loss
        
    # Define graph for model
    def build_model(self):
        """
        Network model adapted from VAE.py file in GitHub repo by 'hwalsuklee':
        https://github.com/hwalsuklee/tensorflow-generative-model-collections/blob/master/VAE.py
        """
        # Define placeholder for noise vector
        self.z = tf.placeholder(tf.float32, [None, self.z_dim], name='z')

        # Define placeholder for dataset handle (to select training or validation)
        self.dataset_handle = tf.placeholder(tf.string, shape=[], name='dataset_handle')
        self.iterator = tf.data.Iterator.from_string_handle(self.dataset_handle, self.dataset.output_types, self.dataset.output_shapes)
        self.data = self.iterator.get_next()

        # Define learning rate with exponential decay
        self.learning_rt = tf.train.exponential_decay(self.learning_rate, self.global_step,
                                                      self.lr_decay_step, self.lr_decay_rate)

        # Define placeholder for training status
        self.training = tf.placeholder(tf.bool, name='training')

        # Compute predictions and loss for training/validation datasets
        self.pred, self.loss, self.ml_loss, self.kl_loss = self.evaluate_model(self.data, training=self.training)

        # Compute predictions and loss for early stopping checks
        self.epred, self.eloss, _, __ = self.evaluate_model(self.edataset.get_next(), reuse=True,
                                                            training=False, suffix="_stopping")

        # Define optimizer for training
        with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):
            self.optim = tf.train.AdamOptimizer(self.learning_rt, beta1=self.adam_beta1) \
                                 .minimize(self.loss, global_step=self.global_step)
        
        # Define summary operations
        loss_sum = tf.summary.scalar("loss", self.loss)
        kl_loss_sum = tf.summary.scalar("kl_loss", self.kl_loss)
        ml_loss_sum = tf.summary.scalar("ml_loss", self.ml_loss)
        self.merged_summaries = tf.summary.merge([loss_sum, kl_loss_sum, ml_loss_sum])

        
        # Compute predictions from random samples in latent space
        self.pred_sample = self.decoder(self.z, training=False, reuse=True, name="sampling_decoder")

        # Resize original images and predictions for plotting
        self.resized_data = tf.image.resize_images(self.data, [self.plot_res, self.plot_res])
        self.resized_pred = tf.image.resize_images(self.pred, [self.plot_res, self.plot_res])
        self.resized_imgs = tf.image.resize_images(self.pred_sample, [self.plot_res, self.plot_res])

        
    # Train model
    def train(self):

        # Define summary writer for saving log files (for training and validation)
        self.writer = tf.summary.FileWriter(os.path.join(self.log_dir, 'training/'), graph=tf.get_default_graph())
        self.vwriter = tf.summary.FileWriter(os.path.join(self.log_dir, 'validation/'), graph=tf.get_default_graph())

        # Show list of all variables and total parameter count
        show_variables()
        print("\n[ Initializing Variables ]\n")
        
        # Get handles for training and validation datasets
        self.training_handle, self.validation_handle = self.sess.run([self.dataset.string_handle(),
                                                                      self.vdataset.string_handle()])

        # Iterate through training steps
        while not self.sess.should_stop():

            # Update global step            
            step = tf.train.global_step(self.sess, self.global_step)

            # Break if early stopping hook requests stop after sess.run()
            if self.sess.should_stop():
                break

            # Specify feed dictionary
            fd = {self.dataset_handle: self.training_handle, self.training: True,
                  self.z: np.zeros([self.batch_size, self.z_dim])}


            # Save summaries, display progress, and update model
            if (step % self.summary_step == 0) and (step % self.display_step == 0):
                summary, kl_loss, ml_loss, loss, _ = self.sess.run([self.merged_summaries, self.kl_loss, self.ml_loss,
                                                                        self.loss, self.optim], feed_dict=fd)
                print("Step %d:  %.10f [kl_loss]   %.10f [ml_loss]   %.10f [loss] " %(step,kl_loss,ml_loss,loss))
                self.writer.add_summary(summary, step); self.writer.flush()
            # Save summaries and update model
            elif step % self.summary_step == 0:
                summary, _ = self.sess.run([self.merged_summaries, self.optim], feed_dict=fd)
                self.writer.add_summary(summary, step); self.writer.flush()
            # Display progress and update model
            elif step % self.display_step == 0:
                kl_loss, ml_loss, loss, _ = self.sess.run([self.kl_loss, self.ml_loss,
                                                           self.loss, self.optim], feed_dict=fd)
                print("Step %d:  %.10f [kl_loss]   %.10f [ml_loss]   %.10f [loss] " %(step,kl_loss,ml_loss,loss))
            # Update model
            else:
                self.sess.run([self.optim], feed_dict=fd)

            # Plot predictions
            if step % self.plot_step == 0:
                self.plot_predictions(step)
                self.plot_comparisons(step)

            # Break if early stopping hook requests stop after sess.run()
            if self.sess.should_stop():
                break

            # Save validation summaries
            if step % self.summary_step == 0:
                fd = {self.dataset_handle: self.validation_handle, self.z: np.zeros([self.batch_size, self.z_dim]),
                      self.training: False}
                vsummary = self.sess.run(self.merged_summaries, feed_dict=fd)
                self.vwriter.add_summary(vsummary, step); self.vwriter.flush()
            
    # Define method for computing model predictions
    def predict(self, random_samples=False):
        if random_samples:
            fd = {self.z: self.sample_z(self.batch_size), self.training: False}
            return self.sess.run(self.resized_imgs, feed_dict=fd)
        else:
            fd = {self.dataset_handle: self.validation_handle, self.z: np.zeros([self.batch_size, self.z_dim]),
                  self.training: False}
            data, pred =  self.sess.run([self.resized_data, self.resized_pred], feed_dict=fd)
            return data, pred

    # Plot generated images for qualitative evaluation
    def plot_predictions(self, step):
        plot_subdir = self.plot_dir + str(step) + "/"
        checkFolders([self.plot_dir, plot_subdir])
        resized_imgs = self.predict(random_samples=True)
        for n in range(0, self.batch_size):
            plot_name = 'plot_' + str(n) + '.png'
            plt.imsave(os.path.join(plot_subdir, plot_name), resized_imgs[n,:,:,0], cmap='gray')
        
    # Plot true and predicted images
    def plot_comparisons(self, step):
        plot_subdir = self.plot_dir + str(step) + "/"
        checkFolders([self.plot_dir, plot_subdir])
        resized_data, resized_pred = self.predict()
        for n in range(0, self.batch_size):
            data_name = 'data_' + str(n) + '.png'; pred_name = 'pred_' + str(n) + '.png'
            plt.imsave(os.path.join(plot_subdir, data_name), resized_data[n,:,:,0], cmap='gray')
            plt.imsave(os.path.join(plot_subdir, pred_name), resized_pred[n,:,:,0], cmap='gray')

    # Compute cumulative loss over multiple batches
    def compute_cumulative_loss(self, loss, loss_ops, dataset_handle, batches):
        for n in range(0, batches):
            fd = {self.dataset_handle: dataset_handle, self.training: False}
            current_loss = self.sess.run(loss_ops, feed_dict=fd)
            loss = np.add(loss, current_loss)
            sys.stdout.write('Batch {0} of {1}\r'.format(n+1,batches))
            sys.stdout.flush()
        return loss
            
    # Evaluate model
    def evaluate(self):
        t_batches = int(np.floor(0.8 * self.data_count/self.batch_size))
        v_batches = int(np.floor(0.2 * self.data_count/self.batch_size))
        print("\nTraining dataset:")
        training_loss = self.compute_cumulative_loss([0.], [self.loss], self.training_handle, t_batches)
        print("\n\nValidation dataset:")
        validation_loss = self.compute_cumulative_loss([0.], [self.loss], self.validation_handle, v_batches)
        training_loss = training_loss/t_batches
        validation_loss = validation_loss/v_batches
        return training_loss, validation_loss
        
                    
# Initialize and train model 
def main():

    # Define model parameters and options in dictionary of flags
    FLAGS = getFlags_VAE()

    # Initialize model
    model = Model(70000, FLAGS)

    # Specify number of training steps
    training_steps = FLAGS.__dict__['training_steps']

    # Define feed dictionary and loss name for EarlyStoppingHook
    loss_name = "loss_stopping:0"
    start_step = FLAGS.__dict__['early_stopping_start']
    stopping_step = FLAGS.__dict__['early_stopping_step']
    tolerance = FLAGS.__dict__['early_stopping_tol']
    
    # Define saver which only keeps previous 3 checkpoints (default=10)
    scaffold = tf.train.Scaffold(saver=tf.train.Saver(max_to_keep=3))
    
    # Initialize TensorFlow monitored training session
    with tf.train.MonitoredTrainingSession(
            checkpoint_dir = FLAGS.__dict__['checkpoint_dir'],
            hooks = [tf.train.StopAtStepHook(last_step=training_steps),
                     EarlyStoppingHook(loss_name, tolerance=tolerance, stopping_step=stopping_step, start_step=start_step)],
            save_summaries_steps = None, save_summaries_secs = None, save_checkpoint_secs = None,
            save_checkpoint_steps = FLAGS.__dict__['checkpoint_step'], scaffold=scaffold) as sess:

        # Set model session
        model.set_session(sess)
        
        # Train model
        model.train()

    print("\n[ TRAINING COMPLETE ]\n")

    # Create new session for model evaluation
    with tf.Session() as sess:

        # Restore network parameters from latest checkpoint
        saver = tf.train.Saver()
        saver.restore(sess, tf.train.latest_checkpoint(FLAGS.__dict__['checkpoint_dir']))
            
        # Set model session using restored sess
        model.set_session(sess)

        # Plot final predictions
        model.plot_predictions("final")

        # Reinitialize dataset handles
        model.reinitialize_handles()

        # Evaluate model
        print("[ Evaluating Model ]")
        t_loss, v_loss = model.evaluate()

        print("\n\n[ Final Evaluations ]")
        print("Training loss: %.5f" %(t_loss))
        print("Validation loss: %.5f\n" %(v_loss))
        

# Run main() function when called directly
if __name__ == '__main__':
    main()
