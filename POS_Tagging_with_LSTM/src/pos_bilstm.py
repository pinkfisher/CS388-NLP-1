import glob
import sys
import tensorflow as tf
import numpy as np
import time
import math
import os
import cPickle as pickle
import shutil
import itertools
from datetime import datetime
from random import shuffle
from preprocess import PreprocessData

MAX_LENGTH = 100
BATCH_SIZE = 128
VALIDATION_FREQUENCY = 10
CHECKPOINT_FREQUENCY = 600
NO_OF_EPOCHS = 6


## Model class is adatepd from model.py found here
## https://github.com/monikkinom/ner-lstm/
class Model:
	def __init__(self, input_dim, sequence_len, output_dim, num_orth_feat,
				 hidden_state_size=300, orth_feat_concat_type='none'):
		self._input_dim = input_dim 		# vocab_size + 2
		self._sequence_len = sequence_len
		self._output_dim = output_dim
		self._num_orth_feat = num_orth_feat
		self._hidden_state_size = hidden_state_size
		self._orth_feat_concat_type = orth_feat_concat_type
		self._optimizer = tf.train.AdamOptimizer(0.0005)

	# Adapted from https://github.com/monikkinom/ner-lstm/blob/master/model.py __init__ function
	def create_placeholders(self):
		self._input_words = tf.placeholder(tf.int32, [BATCH_SIZE, self._sequence_len])
		self._input_orth_feat = tf.placeholder(tf.int32, [BATCH_SIZE, self._sequence_len, self._num_orth_feat])
		self._output_tags = tf.placeholder(tf.int32, [BATCH_SIZE, self._sequence_len])

	def set_input_output(self, input_, input_orth_feat, output):
		self._input_words = input_
		self._input_orth_feat = input_orth_feat
		self._output_tags = output
	
	## Returns the mask that is 1 for the actual words
	## and 0 for the padded part
	# Adapted from https://github.com/monikkinom/ner-lstm/blob/master/model.py __init__ function
	def get_mask(self, t):
		mask = tf.cast(tf.not_equal(t, -1), tf.int32)
		lengths = tf.reduce_sum(mask, reduction_indices=1)
		return mask, lengths

	## Returns the mask that is 1 for the OOV words (with id vocab_size)
	## and 0 for other parts
	def get_OOV_mask(self, w):
		"""
		w: (batch_size, seq_length)
		"""
		vocab_size = self._input_dim - 2
		mask = tf.cast(tf.equal(w, vocab_size), tf.int32)
		lengths = tf.reduce_sum(mask, reduction_indices=1)
		return mask, lengths

	## Embed the large one hot input vector into a smaller space
	## to make the lstm learning tractable
	def get_embedding(self, input_):
		embedding = tf.get_variable("embedding", 
							[self._input_dim,self._hidden_state_size ], dtype=tf.float32)
		return tf.nn.embedding_lookup(embedding,tf.cast(input_, tf.int32))

	# Adapted from https://github.com/monikkinom/ner-lstm/blob/master/model.py __init__ function
	def create_graph(self):
		self.create_placeholders()

		## Create forward and backward cell
		forward_cell = tf.contrib.rnn.LSTMCell(self._hidden_state_size, state_is_tuple=True)
		backward_cell = tf.contrib.rnn.LSTMCell(self._hidden_state_size, state_is_tuple=True)

		## Since we are padding the input, we need to give
		## the actual length of every instance in the batch
		## so that the backward lstm works properly
		self._mask, self._lengths = self.get_mask(self._output_tags)
		self._total_length = tf.reduce_sum(self._lengths)

		self._OOV_mask, self._OOV_lengths = self.get_OOV_mask(self._input_words)
		self._OOV_total_length = tf.reduce_sum(self._OOV_lengths)


		## Embedd the very large input vector into a smaller dimension
		## This is for computational tractability
		with tf.variable_scope("lstm_input"):
			lstm_input = self.get_embedding(self._input_words) 		# (batch_size, seq_length, hidden_state_size)

			# concat orthographic features with embedding
			if self._orth_feat_concat_type == 'input':
				lstm_input = tf.concat((lstm_input, tf.cast(self._input_orth_feat, tf.float32)), 2) # (batch_size, seq_length, hidden_state_size + num_orth_feat)
				print 'Concat orthographic features to input layers'

		
		## Apply bidrectional dyamic rnn to get a tuple of forward
		## and backward outputs. Using dynamic rnn instead of just 
		## an rnn avoids the task of breaking the input into 
		## into a list of tensors (one per time step)
		with tf.variable_scope("lstm"):
			outputs, _ = tf.nn.bidirectional_dynamic_rnn(forward_cell, backward_cell,
			                                       		   lstm_input,dtype=tf.float32,
			                                       		   sequence_length=self._lengths)

		with tf.variable_scope("lstm_output"):
			## concat forward and backward states
			outputs = tf.concat(outputs, 2) 	# (batch_size, seq_length, 2*hidden_state_size)

			# concat orthographic features with outputs
			if self._orth_feat_concat_type == 'output':
				outputs = tf.concat((outputs, tf.cast(self._input_orth_feat, tf.float32)), 2) # (batch_size, seq_length, 2*hidden_state_size + num_orth_feat)
				print 'Concat orthographic features to output layers'
			
			## Apply linear transformation to get logits(unnormalized scores)
			logits = self.compute_logits(outputs) 		# (batch_size, seq_length, num_pos)

			## Get the normalized probabilities
			## Note that this a rank 3 tensor
			## It contains the probabilities of 
			## different POS tags for each batch 
			## example at each time step
			self._probabilities = tf.nn.softmax(logits)

		self._loss = self.cost( self._output_tags, self._probabilities)
		self._accuracy = self.compute_accuracy( self._output_tags, self._probabilities, self._mask)
		self._OOV_accuracy = self.compute_accuracy( self._output_tags, self._probabilities, self._OOV_mask)

		self._average_accuracy = self._accuracy/tf.cast(self._total_length, tf.float32)
		self._average_loss = self._loss/tf.cast(self._total_length, tf.float32)

	# Taken from https://github.com/monikkinom/ner-lstm/blob/master/model.py weight_and_bias function
	## Creates a fully connected layer with the given dimensions and parameters
	def initialize_fc_layer(self, row_dim, col_dim, stddev=0.01, bias=0.1):
		weight = tf.truncated_normal([row_dim, col_dim], stddev=stddev)
		bias = tf.constant(bias, shape=[col_dim])
		return tf.Variable(weight, name='weight'), tf.Variable(bias, name='bias')

	# Taken from https://github.com/monikkinom/ner-lstm/blob/master/model.py __init__ function
	def compute_logits(self, outputs):
		softmax_input_size = int(outputs.get_shape()[2])
		outputs = tf.reshape(outputs, [-1, softmax_input_size])
		
		W, b = self.initialize_fc_layer(softmax_input_size, self._output_dim)
		
		logits = tf.matmul(outputs, W) + b
		logits = tf.reshape(logits, [-1, self._sequence_len, self._output_dim]) 	# (batch_size, seq_length, num_pos)
		return logits

	def add_loss_summary(self):
		tf.summary.scalar('Loss', self._average_loss)

	def add_accuracy_summary(self):
		tf.summary.scalar('Accuracy', self._average_accuracy)

	# Taken from https://github.com/monikkinom/ner-lstm/blob/master/model.py __init__ function
	def get_train_op(self, loss, global_step):
		training_vars = tf.trainable_variables()
		grads, _ = tf.clip_by_global_norm(tf.gradients(loss, training_vars), 10)
		apply_gradient_op = self._optimizer.apply_gradients(zip(grads, training_vars),
														    global_step)
		return apply_gradient_op

    # Adapted from https://github.com/monikkinom/ner-lstm/blob/master/model.py cost function
	def compute_accuracy(self, pos_classes, probabilities, mask):
		predicted_classes = tf.cast(tf.argmax(probabilities, dimension=2), tf.int32)
		correct_predictions = tf.cast(tf.equal(predicted_classes, pos_classes), tf.int32)
		correct_predictions = tf.multiply(correct_predictions, mask)
		return tf.cast(tf.reduce_sum(correct_predictions), tf.float32)


	def get_total_length():
		return self.total_length

	# Adapted from https://github.com/monikkinom/ner-lstm/blob/master/model.py cost function
	def cost(self, pos_classes, probabilities):
		pos_classes = tf.cast(pos_classes, tf.int32)
		pos_one_hot = tf.one_hot(pos_classes, self._output_dim)
		pos_one_hot = tf.cast(pos_one_hot, tf.float32)
		## masking not needed since pos class vector will be zero for 
		## padded time steps
		cross_entropy = pos_one_hot*tf.log(probabilities)
		return -tf.reduce_sum(cross_entropy)

	@property
	def input_words(self):
		return self._input_words

	@property
	def input_orth_feat(self):
		return self._input_orth_feat

	@property
	def output_tags(self):
		return self._output_tags

	@property
	def loss(self):
		return self._loss

	@property
	def accuracy(self):
		return self._accuracy

	@property
	def total_length(self):
		return self._total_length

	@property
	def OOV_accuracy(self):
		return self._OOV_accuracy

	@property
	def OOV_total_length(self):
		return self._OOV_total_length

# Adapted from http://r2rt.com/recurrent-neural-networks-in-tensorflow-i.html
def generate_batch(X, orth_feat, y):
	for i in xrange(0, len(X), BATCH_SIZE):
		yield X[i:i+BATCH_SIZE], orth_feat[i:i+BATCH_SIZE], y[i:i+BATCH_SIZE]

def shuffle_data(X, orth_feat, y):
	ran = range(len(X))
	shuffle(ran)
	return [X[num] for num in ran], [orth_feat[num] for num in ran], [y[num] for num in ran]

# Adapted from http://r2rt.com/recurrent-neural-networks-in-tensorflow-i.html
def generate_epochs(X, orth_feat, y, no_of_epochs):
	lx = len(X)
	lx = (lx//BATCH_SIZE)*BATCH_SIZE
	X = X[:lx]
	y = y[:lx]
	orth_feat = orth_feat[:lx]
	for i in range(no_of_epochs):
		shuffle_data(X, orth_feat, y)
		yield generate_batch(X, orth_feat, y)

## Compute overall loss and accuracy on dev/test data
def compute_summary_metrics(sess, m, sentence_words_val, orth_feat_val, sentence_tags_val):
	loss, accuracy, total_len = 0.0, 0.0, 0
	OOV_accuracy, OOV_total_len = 0.0, 0
	for i, epoch in enumerate(generate_epochs(sentence_words_val, orth_feat_val, sentence_tags_val, 1)):
		for step, (X, orth_feat, y) in enumerate(epoch):
			batch_loss, batch_accuracy, batch_len, batch_OOV_accuracy, batch_OOV_len = \
			sess.run([m.loss, m.accuracy, m.total_length, m.OOV_accuracy, m.OOV_total_length], \
					feed_dict={m.input_words:X, m.input_orth_feat:orth_feat, m.output_tags:y})
			loss += batch_loss
			accuracy += batch_accuracy
			total_len += batch_len
			OOV_accuracy += batch_OOV_accuracy
			OOV_total_len += batch_OOV_len
	loss = loss/total_len if total_len != 0 else 0
	accuracy = accuracy/total_len if total_len != 0 else 1
	OOV_accuracy = OOV_accuracy/OOV_total_len if OOV_total_len != 0 else 1
	return loss, accuracy, OOV_accuracy

## train and test adapted from https://github.com/tensorflow/tensorflow/blob/master/tensorflow/
## models/image/cifar10/cifar10_train.py and cifar10_eval.py
def train(sentence_words_train, orth_feat_train, sentence_tags_train, sentence_words_val,
		  orth_feat_val, sentence_tags_val, vocab_size, no_pos_classes, train_dir, orth_feat_concat_type):
	num_orth_feat = len(orth_feat_train[0][0])
	m = Model(vocab_size, MAX_LENGTH, no_pos_classes, num_orth_feat, orth_feat_concat_type=orth_feat_concat_type)
	with tf.Graph().as_default():
	    global_step = tf.Variable(0, trainable=False)
	    
	    ## Add input/output placeholders
	    m.create_placeholders()
	    ## create the model graph
	    m.create_graph()
	    ## create training op
	    train_op = m.get_train_op(m.loss, global_step)

	    ## create saver object which helps in checkpointing
	    ## the model
	    saver = tf.train.Saver(tf.global_variables()+tf.local_variables())

	    ## add scalar summaries for loss, accuracy
	    m.add_accuracy_summary()
	    m.add_loss_summary()
	    summary_op = tf.summary.merge_all()

	    ## Initialize all the variables
	    init = tf.global_variables_initializer()
	    sess = tf.Session(config=tf.ConfigProto())
	    sess.run(init)

	    summary_writer = tf.summary.FileWriter(train_dir, sess.graph)
	    j = 0
	    for i, epoch in enumerate(generate_epochs(sentence_words_train, orth_feat_train, sentence_tags_train, NO_OF_EPOCHS)):
	        start_time = time.time()
	        for step, (X, orth_feat, y) in enumerate(epoch):
				_, summary_value = sess.run([train_op, summary_op], feed_dict=
										 {m.input_words:X, m.input_orth_feat:orth_feat, m.output_tags:y})
				duration = time.time() - start_time
				j += 1
				if j % VALIDATION_FREQUENCY == 0:
					val_loss, val_accuracy, val_OOV_accuracy = compute_summary_metrics(sess, m, sentence_words_val, orth_feat_val, sentence_tags_val)
					summary = tf.Summary()
					summary.ParseFromString(summary_value)
					summary.value.add(tag='Validation Loss', simple_value=val_loss)
					summary.value.add(tag='Validation Accuracy', simple_value=val_accuracy)
					summary.value.add(tag='Validation OOV Accuracy', simple_value=val_OOV_accuracy)
					summary_writer.add_summary(summary, j)
					log_string = '{} batches ====> Validation Accuracy {:.3f}, Validation Loss {:.3f}, Validation OOV Accuracy {:.3f}'
					print log_string.format(j, val_accuracy, val_loss, val_OOV_accuracy)
				else:
					summary_writer.add_summary(summary_value, j)

				if j % CHECKPOINT_FREQUENCY == 0:
					checkpoint_path = os.path.join(train_dir, 'model.ckpt')
					saver.save(sess, checkpoint_path, global_step=j)
					print 'saved to', checkpoint_path

## Check performance on held out test data
## Loads most recent model from train_dir
## and applies it on test data
def test(sentence_words_test, orth_feat_test, sentence_tags_test,
		 vocab_size, no_pos_classes, train_dir, orth_feat_concat_type):
	num_orth_feat = len(orth_feat_train[0][0])
	m = Model(vocab_size, MAX_LENGTH, no_pos_classes, num_orth_feat, orth_feat_concat_type=orth_feat_concat_type)
	with tf.Graph().as_default():
		global_step = tf.Variable(0, trainable=False)
		m.create_placeholders()
		m.create_graph()
		saver = tf.train.Saver(tf.global_variables())
		with tf.Session() as sess:
			ckpt = tf.train.get_checkpoint_state(train_dir)
			if ckpt and ckpt.model_checkpoint_path:
				saver.restore(sess, ckpt.model_checkpoint_path)

				global_step = ckpt.model_checkpoint_path.split('/')[-1].split('-')[-1]
			test_loss, test_accuracy, test_OOV_accuracy = compute_summary_metrics(sess, m, sentence_words_test,
															   orth_feat_test, sentence_tags_test)
			print 'Test Accuracy: {:.3f}'.format(test_accuracy)
			print 'Test Loss: {:.3f}'.format(test_loss)
			print 'Test OOV Accuracy: {:.3f}'.format(test_OOV_accuracy)


if __name__ == '__main__':
	dataset_path = sys.argv[1]
	train_dir = sys.argv[2]
	split_type = sys.argv[3]
	experiment_type = sys.argv[4]
	orth_feat_concat_type = sys.argv[5] 	# 'none', 'input' or 'output'

	p = PreprocessData(dataset_type='wsj')

	files = p.preProcessDirectory(dataset_path)
	
	if split_type == 'standard':
		train_files, val_files, test_files = p.get_standard_split(files)
	else:
		shuffle(files)
		train_files, test_val_files = p.split_data(files, 0.8)
		test_files, val_files = p.split_data(test_val_files, 0.5)

	train_mat = p.get_raw_data(train_files, 'train')
	val_mat = p.get_raw_data(val_files, 'validation')
	test_mat = p.get_raw_data(test_files, 'test')

	print 'train size:', len(train_mat)
	print 'val size:', len(val_mat)
	print 'test size:', len(test_mat)

	X_train, orth_feat_train, y_train, _ = p.get_processed_data(train_mat, MAX_LENGTH)
	X_val, orth_feat_val, y_val, _ = p.get_processed_data(val_mat, MAX_LENGTH)
	X_test, orth_feat_test, y_test, _ = p.get_processed_data(test_mat, MAX_LENGTH)

	if experiment_type == 'train':
		if os.path.exists(train_dir):
			shutil.rmtree(train_dir)
		os.mkdir(train_dir)
		start = time.time()
		train(X_train, orth_feat_train, y_train, X_val, orth_feat_val, y_val, len(p.vocabulary)+2, len(p.pos_tags)+1, train_dir, orth_feat_concat_type)
		print 'Time used for training:', time.time() - start
	else:
		start = time.time()
		test(X_test, orth_feat_test, y_test, len(p.vocabulary)+2, len(p.pos_tags)+1, train_dir, orth_feat_concat_type)
		print 'Time used for testing:', time.time() - start
