
from __future__ import print_function

import numpy as np
import tensorflow as tf
from sklearn.base import BaseEstimator, RegressorMixin
from tqdm import tqdm, trange
from scipy.sparse import coo_matrix, csr_matrix
import json


class MatrixFactorizer(BaseEstimator):
    
    """Matrix Factorizer
    
    Factorize the matrix R (n, k) into P (n, n_components) and Q (n_components, k) weights
    matrices:
    
        R[i,j] = P[i,:] * Q[:,j]
    
    Additional intercepts mu, bi, bj can be included, leading to the following model:
    
        R[i,j] = mu + bi[i] + bj[j] + P[i,:] * Q[:,j]
        
    The model is commonly used for collaborative filtering in recommender systems, where
    the matrix R contains of ratings by n users of k products. When users rate products
    using some kind of rating system (e.g. "likes", 1 to 5 stars), we are talking about
    explicit ratings (Koren et al, 2009). When ratings are not available and instead we
    use indirect measures of preferences (e.g. clicks, purchases), we are talking about
    implicit ratings (Hu et al, 2008). For implicit ratings we use modified model, where
    we model the indicator variable:
    
        D[i,j] = 1 if R[i,j] > 0 else 0
        
    and define additional weights:
    
        C[i,j] = 1 + alpha * R[i, j]
        
    or log weights:
    
        C[i,j] = 1 + alpha * log(1 + R[i, j])
        
    The model is defined in terms of minimizing the loss function (squared, logistic) between
    D[i,j] indicators and the values predicted using matrix factorization, where the loss is
    weighted using the C[i,j] weights (see Hu et al, 2008 for details). When using logistic
    loss, the predictions are passed through the sigmoid function to squeze them into the
    (0, 1) range.
    
    Parameters
    ----------
    
    n_components : int, default : 5
        Number of latent components to be estimated. The estimated latent matrices P and Q
        have (n, n_components) and (n_components, m) shapes subsequently.
    
    n_iter : int, default : 500
        Number of training epochs, the actual number of iterations is n_samples * n_epoch.
        
    batch_size : int, default : 500
        Size of the random batch to be used during training. The batch_size is the number of
        cells that are randomly sampled from the factorized matrix.
    
    learning_rate : float, default : 0.01
        Learning rate parameter.
    
    regularization_rate : float, default : 0.02
        Regularization parameter.
        
    alpha : float, default : 1.0
        Weighting parameter in matrix factorization with implicit ratings.
    
    implicit : bool, default : False
        Use matrix factorization with explicit (default) or implicit ratings. 
    
    loss : 'squared', 'logistic', default: 'squared'
        Loss function to be used. For implicit=True 'logistic' loss may be preferable.
        
    log_weights : bool, default : None
        Used only when implicit=True, then it defaults to log_weights=True, so log weighting
        is used in the loss function instead of standard weights (log_weights=False).
        
    fit_intercepts : bool, default : True
        When set to True, the mu, bi, bj intercepts are fitted, otherwise
        only the P and Q latent matrices are fitted.
        
    warm_start : bool, optional
        When set to True, reuse the solution of the previous call to fit as initialization,
        otherwise, just erase the previous solution.
        
    optimizer : 'Adam', 'Ftrl', default : 'Adam'
        Optimizer to be used, see TensorFlow documentation for more details.
        
    random_state : int, or None, default : None
        The seed of the pseudo random number generator to use when shuffling the data. If int,
        random_state is the seed used by the random number generator.
        
    show_progress : bool, default : False
        Show the progress bar.
    
    Examples
    --------
    
    >>> import numpy as np
    >>> import pandas as pd
    >>> from tfmf import MatrixFactorizer, sparse_matrix
    >>> user_id = [0,0,1,1,2,2]
    >>> movie_id = [0,1,2,0,1,2]
    >>> rating = [1,1,2,2,3,3]
    >>> X = sparse_matrix(user_id, movie_id, rating)
    >>> mf = MatrixFactorizer(n_components=2, n_iter=100, batch_size=6, random_state=42, show_progress=False)
    >>> mf.partial_fit(X)
    MatrixFactorizer(alpha=1.0, batch_size=6, fit_intercepts=True, implicit=False,
             learning_rate=0.01, log_weights=False, loss='squared',
             n_components=2, n_iter=100, random_state=42,
             regularization_rate=0.02, show_progress=False, warm_start=False)
    >>> X_full = np.array([[i,j] for i in range(3) for j in range(3)])
    >>> np.reshape(mf.predict(X_full[:,0], X_full[:,1]), (3,3))
    array([[1.1241099 , 0.4444648 , 0.5635694 ],
           [1.6370661 , 1.1460071 , 1.2965162 ],
           [0.55132747, 2.502296  , 2.5540314 ]], dtype=float32)
    
    References
    ----------
    
    Koren, Y., Bell, R., & Volinsky, C. (2009).
    Matrix factorization techniques for recommender systems. Computer, 42(8).
    
    Yu, H. F., Hsieh, C. J., Si, S., & Dhillon, I. (2012, December).
    Scalable coordinate descent approaches to parallel matrix factorization for recommender systems.
    In Data Mining (ICDM), 2012 IEEE 12th International Conference on (pp. 765-774). IEEE.
    
    Hu, Y., Koren, Y., & Volinsky, C. (2008, December).
    Collaborative filtering for implicit feedback datasets.
    In Data Mining, 2008. ICDM'08. Eighth IEEE International Conference on (pp. 263-272). IEEE.
    
    """ 
    
    class TFModel(object):
        # Define and initialize the TensorFlow model, its weights, initialize session and saver

        def __init__(self, shape, learning_rate, alpha, regularization_rate,
                     implicit, loss, log_weights, fit_intercepts, optimizer,
                     random_state=None):

            self.shape = shape
            self.learning_rate = learning_rate
            self.implicit = implicit
            self.loss = loss
            self.log_weights = log_weights
            self.fit_intercepts = fit_intercepts
            self.optimizer = optimizer
            self.random_state = random_state

            # the R (n, k) matrix is factorized to P (n, d) and Q (k, d) matrices
            n, k, d = self.shape

            self.graph = tf.Graph()

            with self.graph.as_default():

                tf.set_random_seed(self.random_state)

                with tf.name_scope('constants'):
                    self.alpha = tf.constant(alpha, dtype=tf.float32)
                    self.regularization_rate = tf.constant(regularization_rate, dtype=tf.float32,
                                                           name='regularization_rate')

                with tf.name_scope('inputs'):
                    self.row_ids = tf.placeholder(tf.int32, shape=[None], name='row_ids')
                    self.col_ids = tf.placeholder(tf.int32, shape=[None], name='col_ids')
                    self.values = tf.placeholder(tf.float32, shape=[None], name='values')

                    if self.implicit:
                        # D[i,j] = 1 if R[i,j] > 0 else 0
                        targets = tf.clip_by_value(self.values, 0, 1, name='targets')
                        
                        if self.log_weights:
                            data_weights = tf.add(1.0, self.alpha * tf.log1p(self.values), name='data_weights')
                        else:
                            data_weights = tf.add(1.0, self.alpha * self.values, name='data_weights')
                    else:
                        targets = tf.identity(self.values, name='targets')
                        data_weights = tf.constant(1.0, name='data_weights')

                with tf.name_scope('parameters'):
                    
                    if self.fit_intercepts:
                        # mu
                        self.global_bias = tf.get_variable('global_bias', shape=[], dtype=tf.float32,
                                                           initializer=tf.zeros_initializer())
                        # bi
                        self.row_biases = tf.get_variable('row_biases', shape=[n], dtype=tf.float32,
                                                           initializer=tf.zeros_initializer())
                        # bj
                        self.col_biases = tf.get_variable('col_biases', shape=[k], dtype=tf.float32,
                                                           initializer=tf.zeros_initializer())

                    # P (n, d) matrix
                    self.row_weights = tf.get_variable('row_weights', shape=[n, d], dtype=tf.float32,
                                                        initializer = tf.random_normal_initializer(mean=0, stddev=0.01))

                    # Q (k, d) matrix
                    self.col_weights = tf.get_variable('col_weights', shape=[k, d], dtype=tf.float32,
                                                        initializer = tf.random_normal_initializer(mean=0, stddev=0.01))

                with tf.name_scope('prediction'):
                    
                    if self.fit_intercepts:
                        batch_row_biases = tf.nn.embedding_lookup(self.row_biases, self.row_ids, name='row_bias')
                        batch_col_biases = tf.nn.embedding_lookup(self.col_biases, self.col_ids, name='col_bias')

                    batch_row_weights = tf.nn.embedding_lookup(self.row_weights, self.row_ids, name='row_weights')
                    batch_col_weights = tf.nn.embedding_lookup(self.col_weights, self.col_ids, name='col_weights')

                    # P[i,:] * Q[j,:]
                    weights = tf.reduce_sum(tf.multiply(batch_row_weights, batch_col_weights), axis=1, name='weights')

                    if self.fit_intercepts:
                        biases = tf.add(batch_row_biases, batch_col_biases)
                        biases = tf.add(self.global_bias, biases, name='biases')
                        linear_predictor = tf.add(biases, weights, name='linear_predictor')
                    else:
                        linear_predictor = tf.identity(weights, name='linear_predictor')

                    if self.loss == 'logistic':
                        self.pred = tf.sigmoid(linear_predictor, name='predictions')
                    else:
                        self.pred = tf.identity(linear_predictor, name='predictions')

                with tf.name_scope('loss'):
                    
                    l2_weights = tf.add(tf.nn.l2_loss(self.row_weights),
                                        tf.nn.l2_loss(self.col_weights), name='l2_weights')
                    
                    if self.fit_intercepts:
                        l2_biases = tf.add(tf.nn.l2_loss(batch_row_biases),
                                            tf.nn.l2_loss(batch_col_biases), name='l2_biases')
                        l2_term = tf.add(l2_weights, l2_biases)
                    else:
                        l2_term = l2_weights
                    
                    l2_term = tf.multiply(self.regularization_rate, l2_term, name='regularization')

                    if self.loss == 'logistic':
                        loss_raw = tf.losses.log_loss(predictions=self.pred, labels=targets,
                                                      weights=data_weights)
                    else:
                        loss_raw = tf.losses.mean_squared_error(predictions=self.pred, labels=targets,
                                                                weights=data_weights)            

                    self.cost = tf.add(loss_raw, l2_term, name='loss')

                if self.optimizer == 'Ftrl':
                    self.train_step = tf.train.FtrlOptimizer(self.learning_rate).minimize(self.cost)
                else:
                    self.train_step = tf.train.AdamOptimizer(self.learning_rate).minimize(self.cost)

                self.saver = tf.train.Saver()

                init = tf.global_variables_initializer()

            # initialize TF session
            self.sess = tf.Session(graph=self.graph)
            self.sess.run(init)
            
            
        def train(self, rows, cols, values):             
            batch = {
                self.row_ids : rows,
                self.col_ids : cols,
                self.values : values
            }
            _, loss_value = self.sess.run(fetches=[self.train_step, self.cost], feed_dict=batch)
            return loss_value
        
        
        def predict(self, rows, cols):
            batch = {
                self.row_ids : rows,
                self.col_ids : cols
            }
            return self.pred.eval(feed_dict=batch, session=self.sess)
        
        
        def coef(self):
            if self.fit_intercepts:
                return self.sess.run(fetches={
                    'global_bias' : self.global_bias,
                    'row_bias' : self.row_biases,
                    'col_bias' : self.col_biases,
                    'row_weights' : self.row_weights,
                    'col_weights' : self.col_weights
                })
            else:
                return self.sess.run(fetches={
                    'row_weights' : self.row_weights,
                    'col_weights' : self.col_weights
                })
        
        
        def save(self, path):
            self.saver.save(self.sess, path)
        
        
        def restore(self, path):
            self.saver.restore(self.sess, path)
    
        
    def __init__(self, n_components=5, n_iter=500, batch_size=500, learning_rate=0.01,
                 regularization_rate=0.02, alpha=1.0, implicit=False, loss='squared',
                 log_weights=None, fit_intercepts=True, warm_start=False, optimizer='Adam',
                 random_state=None, show_progress=True):
        
        self.n_components = n_components
        self.shape = (None, None, self.n_components)
        self._data = None
        self.n_iter = n_iter
        self.batch_size = batch_size
        self.learning_rate = float(learning_rate)
        self.alpha = float(alpha)
        self.regularization_rate = float(regularization_rate)
        self.implicit = implicit
        self.loss = loss

        if implicit and log_weights is None:
            self.log_weights = True
        else:
            self.log_weights = log_weights
        
        self.fit_intercepts = fit_intercepts
        self.optimizer = optimizer
        self.random_state = random_state
        self.warm_start = warm_start
        self.show_progress = show_progress
        
        np.random.seed(self.random_state)
        self._fresh_session()
    
        
    def _fresh_session(self):
        # reset the session, to start from the scratch        
        self._tf = None
        self.history = []
    
    
    def _tf_init(self, shape=None):
        # define the TensorFlow model and initialize variables, session, saver
        if shape is None:
            shape = self.shape
        self._tf = self.TFModel(shape=self.shape, learning_rate=self.learning_rate,
                                alpha=self.alpha, regularization_rate=self.regularization_rate,
                                implicit=self.implicit, loss=self.loss, log_weights=self.log_weights,
                                fit_intercepts=self.fit_intercepts, optimizer=self.optimizer,
                                random_state=self.random_state)
    
            
    def _get_batch(self, data, batch_size=1):
        # create single batch for training
        
        batch_rows = np.random.randint(self.shape[0], size=batch_size)
        batch_cols = np.random.randint(self.shape[1], size=batch_size)

        # extract elements from scipy.sparse matrix
        batch_vals = data[batch_rows, batch_cols].A.flatten()
        
        return batch_rows, batch_cols, batch_vals
    
      
    def init_with_shape(self, n, k):
        '''Manually initialize model for given shape of factorized matrix

        n, k : int
            Shape of the factorized matrix.
        '''
        self.shape = (int(n), int(k), int(self.n_components))
        self._tf_init(self.shape)
    
    
    def fit(self, sparse_matrix):
        '''Fit the model
        
        Parameters
        ----------
        
        sparse_matrix : sparse-matrix, shape (n_users, n_items)
            Sparse matrix in scipy.sparse format, can be created using sparse_matrix
            function from this package.
        '''
        if not self.warm_start:
            self._fresh_session()
        return self.partial_fit(sparse_matrix)
    
    
    def partial_fit(self, sparse_matrix):
        '''Fit the model
        
        Parameters
        ----------
        
        sparse_matrix : sparse-matrix, shape (n_users, n_items)
            Sparse matrix in scipy.sparse format, can be created using sparse_matrix
            function from this package.
        '''
                    
        if self._tf is None:
            self.init_with_shape(*sparse_matrix.shape)
        
        for _ in trange(self.n_iter, disable=not self.show_progress, desc='training'):
            batch_X0, batch_X1, batch_y = self._get_batch(sparse_matrix, self.batch_size)
            loss_value = self._tf.train(batch_X0, batch_X1, batch_y)
            self.history.append(loss_value)
                
        return self
    
    
    def predict(self, rows, cols):
        '''Predict using the model
        
        Parameters
        ----------
        
        rows : array, shape (n_samples,)
            Row indexes.

        cols : array, shape (n_samples,)
            Column indexes.
        '''
        
        return self._tf.predict(rows, cols)
