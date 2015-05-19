# -*- coding: utf-8 -*-

r'''Activation functions for network layers.

Activation functions are normally constructed using the :func:`build` function.
Common keys are:

- "tanh"
- "logistic" (or "sigmoid")
- "softmax" (typically used for :class:`classifier <theanets.feedforward.Classifier>` output layers)
- "linear"
- "softplus" (continuous approximation of "relu")
- "relu" (or "rect:max")
- "rect:min"
- "rect:minmax"
- "norm:mean": mean subtractive batch normalization
- "norm:max": max divisive batch normalization
- "norm:std": standard deviation divisive batch normalization
- "norm:z": z-score batch normalization

Additionally, the names of all classes defined in this module can be used as
keys for specifying an activation function.
'''

import functools
import numpy as np
import theano
import theano.tensor as TT

from . import util

FLOAT = theano.config.floatX


def build(name, layer, **kwargs):
    '''Construct an activation function by name.

    Parameters
    ----------
    name : str or :class:`Activation`
        The name of the type of activation function to build, or an
        already-created instance of an activation function.
    layer : :class:`theanets.layers.Layer`
        The layer to which this activation will be applied.
    kwargs : dict
        Additional named arguments to pass to the activation constructor.

    Returns
    -------
    activation : :class:`Activation`
        A neural network activation function instance.
    '''
    if isinstance(name, Activation):
        return name
    def compose(a, b):
        c = lambda z: b(a(z))
        c.name = ['%s(%s)' % (b.name, a.name)]
        return c
    if '+' in name:
        return functools.reduce(compose, (build(n) for n in name.split('+')))
    act = {
        # s-shaped
        'tanh':        TT.tanh,
        'logistic':    TT.nnet.sigmoid,
        'sigmoid':     TT.nnet.sigmoid,

        # softmax (typically for classification)
        'softmax':     softmax,

        # linear variants
        'linear':      lambda x: x,
        'softplus':    TT.nnet.softplus,
        'relu':        lambda x: (x + abs(x)) / 2,
        'rect:max':    lambda x: (1 + x - abs(x - 1)) / 2,
        'rect:minmax': lambda x: (1 + abs(x) - abs(x - 1)) / 2,

        # batch normalization
        'norm:mean':   lambda x: x - x.mean(axis=-1, keepdims=True),
        'norm:max':    lambda x: x / (
            abs(x).max(axis=-1, keepdims=True) + TT.cast(1e-6, FLOAT)),
        'norm:std':    lambda x: x / (
            x.std(axis=-1, keepdims=True) + TT.cast(1e-6, FLOAT)),
        'norm:z':      lambda x: (x - x.mean(axis=-1, keepdims=True)) / (
            x.std(axis=-1, keepdims=True) + TT.cast(1e-6, FLOAT)),
    }.get(name)
    if act is not None:
        act.name = name
        act.params = []
        return act
    return Activation.build(name, name, layer, **kwargs)


def softmax(x):
    z = TT.exp(x - x.max(axis=-1, keepdims=True))
    return z / z.sum(axis=-1, keepdims=True)


class Activation(util.Registrar(str('Base'), (), {})):
    '''An activation function for a neural network layer.

    Parameters
    ----------
    name : str
        Name of this activation function.
    layer : :class:`Layer`
        The layer to which this function is applied.

    Attributes
    ----------
    name : str
        Name of this activation function.
    layer : :class:`Layer`
        The layer to which this function is applied.
    '''

    def __init__(self, name, layer, **kwargs):
        self.name = name
        self.layer = layer
        self.kwargs = kwargs
        self.params = []

    def __call__(self, x):
        '''Compute a symbolic expression for this activation function.

        Parameters
        ----------
        x : Theano expression
            A Theano expression representing the input to this activation
            function.

        Returns
        -------
        y : Theano expression
            A Theano expression representing the output from this activation
            function.
        '''
        raise NotImplementedError


class Prelu(Activation):
    r'''Parametric rectified linear activation with learnable leak rate.

    This activation is characterized by two linear pieces joined at the origin.
    For negative inputs, the unit response is a linear function of the input
    with slope :math:`r` (the "leak rate"). For positive inputs, the unit
    response is the identity function:

    .. math::
       f(x) = \left\{ \begin{eqnarray*} rx &\qquad& \mbox{if } x < 0 \\ x &\qquad& \mbox{otherwise} \end{eqnarray*} \right.

    References
    ----------
    K He, X Zhang, S Ren, J Sun (2015), "Delving Deep into Rectifiers:
    Surpassing Human-Level Performance on ImageNet Classification"
    http://arxiv.org/abs/1502.01852
    '''

    __extra_registration_keys__ = ['leaky-relu']

    def __init__(self, *args, **kwargs):
        super(Prelu, self).__init__(*args, **kwargs)
        self.leak = theano.shared(
            np.ones((self.layer.size, ), FLOAT) * 0.1,
            name=self.layer._fmt('leak'))
        self.params.append(self.leak)

    def __call__(self, x):
        return (x + abs(x)) / 2 + self.leak * (x - abs(x)) / 2


class LGrelu(Activation):
    r'''Rectified linear activation with learnable leak rate and gain.

    This activation is characterized by two linear pieces joined at the origin.
    For negative inputs, the unit response is a linear function of the input
    with slope :math:`r` (the "leak rate"). For positive inputs, the unit
    response is a different linear function of the input with slope :math:`g`
    (the "gain"):

    .. math::
       f(x) = \left\{ \begin{eqnarray*} rx &\qquad& \mbox{if } x < 0 \\ gx &\qquad& \mbox{otherwise} \end{eqnarray*} \right.
    '''

    __extra_registration_keys__ = ['leaky-gain-relu']

    def __init__(self, *args, **kwargs):
        super(LGrelu, self).__init__(*args, **kwargs)
        self.gain = theano.shared(
            np.ones((self.layer.size, ), FLOAT),
            name=self.layer._fmt('gain'))
        self.params.append(self.gain)
        self.leak = theano.shared(
            np.ones((self.layer.size, ), FLOAT) * 0.1,
            name=self.layer._fmt('leak'))
        self.params.append(self.leak)

    def __call__(self, x):
        return self.gain * (x + abs(x)) / 2 + self.leak * (x - abs(x)) / 2


class Maxout(Activation):
    r'''Arbitrary piecewise linear activation.

    This activation is unusual in that it requires a parameter at initialization
    time: the number of linear pieces to use. Consider a layer for the moment
    with just one unit. A maxout activation with :math:`k` pieces uses a slope
    :math:`m_k` and an intercept :math:`b_k` for each linear piece. It then
    transfers the input activation as the maximum of all of the pieces:

    .. math::
       f(x) = \max_k m_k x + b_k

    The parameters :math:`m_k` and :math:`b_k` are learnable.

    For layers with more than one unit, the maxout activation allocates a slope
    :math:`m_{ki}` and intercept :math:`b_{ki}` for each unit :math:`i` and each
    piece :math:`k`. The activation for unit :math:`x_i` is:

    .. math::
       f(x_i) = \max_k m_{ki} x_i + b_{ki}

    Again, the slope and intercept parameters are learnable.

    This activation is actually a generalization of the rectified linear
    activations; to see how, just allocate 2 pieces and set the intercepts to 0.
    The slopes of the ``relu`` activation are given by :math:`m = (0, 1)`, those
    of the :class:`Prelu` function are given by :math:`m = (r, 1)`, and those of
    the :class:`LGrelu` are given by :math:`m = (r, g)` where :math:`r` is the
    leak rate parameter and `g` is a gain parameter.

    Parameters
    ----------
    pieces : int
        Number of linear pieces to use in the activation.
    '''

    def __init__(self, *args, **kwargs):
        super(Maxout, self).__init__(*args, **kwargs)

        self.pieces = kwargs['pieces']

        m = np.ones((self.layer.size, self.pieces), FLOAT)
        self.slope = theano.shared(m, name=self.layer._fmt('slope'))
        self.params.append(self.slope)

        b = np.ones((self.pieces, ), FLOAT)
        self.intercept = theano.shared(b, name=self.layer._fmt('intercept'))
        self.params.append(self.intercept)

    def __call__(self, x):
        return (x[..., None] * self.slope + self.intercept).max(axis=-1)
