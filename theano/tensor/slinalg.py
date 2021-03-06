import logging

logger = logging.getLogger(__name__)
import numpy

from theano.gof import Op, Apply

from theano.tensor import as_tensor_variable, dot, DimShuffle, Dot
from theano.tensor.blas import Dot22
from theano import tensor
import theano.tensor
from theano.tensor.opt import (register_stabilize,
        register_specialize, register_canonicalize)
from theano.gof import local_optimizer
from theano.gof.opt import Optimizer
from theano.gradient import DisconnectedType

try:
    import scipy.linalg
    imported_scipy = True
except ImportError:
    # some ops (e.g. Cholesky, Solve, A_Xinv_b) won't work
    imported_scipy = False

MATRIX_STRUCTURES = (
        'general',
        'symmetric',
        'lower_triangular',
        'upper_triangular',
        'hermitian',
        'banded',
        'diagonal',
        'toeplitz',
        )

class Cholesky(Op):
    """
    Return a triangular matrix square root of positive semi-definite `x`

    L = cholesky(X, lower=True) implies dot(L, L.T) == X
    """
    #TODO: inplace
    #TODO: for specific dtypes
    #TODO: LAPACK wrapper with in-place behavior, for solve also

    __props__ = ('lower', 'destructive')

    def __init__(self, lower=True):
        self.lower = lower
        self.destructive = False

    def infer_shape(self, node, shapes):
        return [shapes[0]]

    def make_node(self, x):
        assert imported_scipy, (
            "Scipy not available. Scipy is needed for the Cholesky op")
        x = as_tensor_variable(x)
        assert x.ndim == 2
        return Apply(self, [x], [x.type()])

    def perform(self, node, inputs, outputs):
        x = inputs[0]
        z = outputs[0]
        z[0] = scipy.linalg.cholesky(x, lower=self.lower).astype(x.dtype)

    def grad(self, inputs, gradients):
        return [CholeskyGrad(self.lower)(inputs[0], self(inputs[0]),
                                         gradients[0])]

cholesky = Cholesky()


class CholeskyGrad(Op):
    """
    """

    __props__ = ('lower', 'destructive')

    def __init__(self, lower=True):
        self.lower = lower
        self.destructive = False

    def make_node(self, x, l, dz):
        x = as_tensor_variable(x)
        l = as_tensor_variable(l)
        dz = as_tensor_variable(dz)
        assert x.ndim == 2
        assert l.ndim == 2
        assert dz.ndim == 2
        assert l.owner.op.lower == self.lower, (
            "lower/upper mismatch between Cholesky op and CholeskyGrad op"
        )
        return Apply(self, [x, l, dz], [x.type()])

    def perform(self, node, inputs, outputs):
        """Implements the "reverse-mode" gradient [1]_ for the
        Cholesky factorization of a positive-definite matrix.

        .. [1] S. P. Smith. "Differentiation of the Cholesky Algorithm".
               Journal of Computational and Graphical Statistics,
               Vol. 4, No. 2 (Jun.,1995), pp. 134-147
               http://www.jstor.org/stable/1390762

        """
        x = inputs[0]
        L = inputs[1]
        dz = inputs[2]
        dx = outputs[0]
        N = x.shape[0]
        if self.lower:
            F = numpy.tril(dz)
            for k in xrange(N - 1, -1, -1):
                for j in xrange(k + 1, N):
                    for i in xrange(j, N):
                        F[i, k] -= F[i, j] * L[j, k]
                        F[j, k] -= F[i, j] * L[i, k]
                for j in xrange(k + 1, N):
                    F[j, k] /= L[k, k]
                    F[k, k] -= L[j, k] * F[j, k]
                F[k, k] /= (2 * L[k, k])
        else:
            F = numpy.triu(dz)
            M = N - 1
            for k in xrange(N - 1, -1, -1):
                for j in xrange(k + 1, N):
                    for i in xrange(j, N):
                        F[k, i] -= F[j, i] * L[k, j]
                        F[k, j] -= F[j, i] * L[k, i]
                for j in xrange(k + 1, N):
                    F[k, j] /= L[k, k]
                    F[k, k] -= L[k, j] * F[k, j]
                F[k, k] /= (2 * L[k, k])
        dx[0] = F

    def infer_shape(self, node, shapes):
        return [shapes[0]]


class Solve(Op):
    """Solve a system of linear equations"""

    __props__ = ('A_structure', 'lower', 'overwrite_A', 'overwrite_b')

    def __init__(self,
                 A_structure='general',
                 lower=False,
                 overwrite_A=False,
                 overwrite_b=False):
        if A_structure not in MATRIX_STRUCTURES:
            raise ValueError('Invalid matrix structure argument', A_structure)
        self.A_structure = A_structure
        self.lower = lower
        self.overwrite_A = overwrite_A
        self.overwrite_b = overwrite_b

    def __repr__(self):
        return 'Solve{%s}' % str(self.props())

    def make_node(self, A, b):
        assert imported_scipy, (
            "Scipy not available. Scipy is needed for the Solve op")
        A = as_tensor_variable(A)
        b = as_tensor_variable(b)
        assert A.ndim == 2
        assert b.ndim in [1, 2]
        otype = tensor.tensor(
                broadcastable=b.broadcastable,
                dtype=(A * b).dtype)
        return Apply(self, [A, b], [otype])

    def perform(self, node, inputs, output_storage):
        A, b = inputs
        #TODO: use the A_structure to go faster
        output_storage[0][0] = scipy.linalg.solve(A, b)

    # computes shape of x where x = inv(A) * b
    def infer_shape(self, node, shapes):
        Ashape, Bshape = shapes
        rows = Ashape[1]
        if len(Bshape) == 1:  # b is a Vector
            return [(rows,)]
        else:
            cols = Bshape[1]  # b is a Matrix
            return [(rows, cols)]

solve = Solve()  # general solve

class SolveCholesky(Op):
    """Solve a system of linear equations, represented by a triangular Cholesky
    factor."""

    __props__ = ('A_structure', 'lower')

    def __init__(self, A_structure='lower_triangular', lower=True):
        if A_structure not in MATRIX_STRUCTURES:
            raise ValueError('Invalid matrix structure argument', A_structure)
        self.A_structure = A_structure
        self.lower = lower

    def __repr__(self):
        return 'SolveCholesky{%s}' % str(self.props())

    def make_node(self, A, b):
        assert imported_scipy, (
            "Scipy not available. Scipy is needed for the SolveCholesky op")
        A = as_tensor_variable(A)
        b = as_tensor_variable(b)
        assert A.ndim == 2
        assert b.ndim in [1, 2]
        otype = tensor.tensor(
                broadcastable=b.broadcastable,
                dtype=(A * b).dtype)
        return Apply(self, [A, b], [otype])

    def perform(self, node, inputs, output_storage):
        A, b = inputs
        output_storage[0][0] = scipy.linalg.cho_solve((A, self.lower), b)

    # computes shape of x where x = inv(A) * b
    def infer_shape(self, node, shapes):
        Ashape, Bshape = shapes
        rows = Ashape[1]
        if len(Bshape) == 1:  # b is a Vector
            return [(rows,)]
        else:
            cols = Bshape[1]  # b is a Matrix
            return [(rows, cols)]

solve_cholesky = SolveCholesky()  # Cholesky solve

#TODO : SolveTriangular

#TODO: Optimizations to replace multiplication by matrix inverse
#      with solve() Op (still unwritten)


class Eigvalsh(Op):
    """Generalized eigenvalues of a Hermetian positive definite Eigensystem
    """

    __props__ = ('lower',)

    def __init__(self, lower=True):
        assert lower in [True, False]
        self.lower = lower

    def make_node(self, a, b):
        assert imported_scipy, (
            "Scipy not  available. Scipy is needed for the Eigvalsh op")

        if b == theano.tensor.NoneConst:
            a = as_tensor_variable(a)  
            assert a.ndim == 2

            out_dtype = theano.scalar.upcast(a.dtype)
            w = theano.tensor.vector(dtype=out_dtype)
            return Apply(self, [a], [w])
        else:
            a = as_tensor_variable(a)
            b = as_tensor_variable(b)
            assert a.ndim == 2
            assert b.ndim == 2

            out_dtype = theano.scalar.upcast(a.dtype, b.dtype)
            w = theano.tensor.vector(dtype=out_dtype)
            return Apply(self, [a, b], [w])

    def perform(self, node, inputs, (w,)):
        if len(inputs) == 2:
            w[0] = scipy.linalg.eigvalsh(a=inputs[0], b=inputs[1], lower=self.lower)
        else:
            w[0] = scipy.linalg.eigvalsh(a=inputs[0], b=None, lower=self.lower)

    def grad(self, inputs, g_outputs):
        a, b = inputs
        gw, = g_outputs
        return EigvalshGrad(self.lower)(a, b, gw)

    def infer_shape(self, node, shapes):
        n = shapes[0][0]
        return [(n,)]


class EigvalshGrad(Op):
    """Gradient of generalized eigenvalues of a Hermetian positive definite
    eigensystem
    """

    # Note: This Op (EigvalshGrad), should be removed and replaced with a graph
    # of theano ops that is constructed directly in Eigvalsh.grad.
    # But this can only be done once scipy.linalg.eigh is available as an Op
    # (currently the Eigh uses numpy.linalg.eigh, which doesn't let you
    # pass the right-hand-side matrix for a generalized eigenproblem.) See the
    # discussion on github at
    # https://github.com/Theano/Theano/pull/1846#discussion-diff-12486764

    __props__ = ('lower',)

    def __init__(self, lower=True):
        assert lower in [True, False]
        self.lower = lower
        if lower:
            self.tri0 = numpy.tril
            self.tri1 = lambda a: numpy.triu(a, 1)
        else:
            self.tri0 = numpy.triu
            self.tri1 = lambda a: numpy.tril(a, -1)

    def make_node(self, a, b, gw):
        assert imported_scipy, (
            "Scipy not available. Scipy is needed for the GEigvalsh op")
        a = as_tensor_variable(a)
        b = as_tensor_variable(b)
        gw = as_tensor_variable(gw)  
        assert a.ndim == 2
        assert b.ndim == 2
        assert gw.ndim == 1

        out_dtype = theano.scalar.upcast(a.dtype, b.dtype, gw.dtype)
        out1 = theano.tensor.matrix(dtype=out_dtype)
        out2 = theano.tensor.matrix(dtype=out_dtype)
        return Apply(self, [a, b, gw], [out1, out2])

    def perform(self, node, (a, b, gw), outputs):
        w, v = scipy.linalg.eigh(a, b, lower=self.lower)
        gA = v.dot(numpy.diag(gw).dot(v.T))
        gB = - v.dot(numpy.diag(gw*w).dot(v.T))

        # See EighGrad comments for an explanation of these lines
        out1 = self.tri0(gA) + self.tri1(gA).T
        out2 = self.tri0(gB) + self.tri1(gB).T
        outputs[0][0] = numpy.asarray(out1, dtype=node.outputs[0].dtype)
        outputs[1][0] = numpy.asarray(out2, dtype=node.outputs[1].dtype)

    def infer_shape(self, node, shapes):
        return [shapes[0], shapes[1]]


def eigvalsh(a, b, lower=True):
    return Eigvalsh(lower)(a, b)


def kron(a, b):
    """ Kronecker product

    Same as scipy.linalg.kron(a, b).

    :note: numpy.kron(a, b) != scipy.linalg.kron(a, b)!
        They don't have the same shape and order when
        a.ndim != b.ndim != 2.

    :param a: array_like
    :param b: array_like
    :return: array_like with a.ndim + b.ndim - 2 dimensions.

    """
    a = tensor.as_tensor_variable(a)
    b = tensor.as_tensor_variable(b)
    if (a.ndim + b.ndim <= 2):
        raise TypeError('kron: inputs dimensions must sum to 3 or more. '
                        'You passed %d and %d.' % (a.ndim, b.ndim))
    o = tensor.outer(a, b)
    o = o.reshape(tensor.concatenate((a.shape, b.shape)),
                  a.ndim + b.ndim)
    shf = o.dimshuffle(0, 2, 1, * range(3, o.ndim))
    if shf.ndim == 3:
        shf = o.dimshuffle(1, 0, 2)
        o = shf.flatten()
    else:
        o = shf.reshape((o.shape[0] * o.shape[2],
                         o.shape[1] * o.shape[3]) +
                        tuple([o.shape[i] for i in range(4, o.ndim)]))
    return o
