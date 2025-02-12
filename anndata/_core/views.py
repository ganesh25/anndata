from contextlib import contextmanager
from copy import deepcopy
from functools import reduce, singledispatch, wraps
from typing import Any, KeysView, Optional, Sequence, Tuple
import warnings

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype
from scipy import sparse

from anndata._warnings import ImplicitModificationWarning
from .access import ElementRef
from ..compat import ZappyArray


class _SetItemMixin:
    """\
    Class which (when values are being set) lets their parent AnnData view know,
    so it can make a copy of itself.
    This implements copy-on-modify semantics for views of AnnData objects.
    """

    def __setitem__(self, idx: Any, value: Any):
        if self._view_args is None:
            super().__setitem__(idx, value)
        else:
            warnings.warn(
                f"Trying to modify attribute `.{self._view_args.attrname}` of view, "
                "initializing view as actual.",
                ImplicitModificationWarning,
                stacklevel=2,
            )
            with self._update() as container:
                container[idx] = value

    @contextmanager
    def _update(self):
        adata_view, attr_name, keys = self._view_args
        new = adata_view.copy()
        attr = getattr(new, attr_name)
        container = reduce(lambda d, k: d[k], keys, attr)
        yield container
        adata_view._init_as_actual(new)


class _ViewMixin(_SetItemMixin):
    def __init__(
        self,
        *args,
        view_args: Tuple["anndata.AnnData", str, Tuple[str, ...]] = None,
        **kwargs,
    ):
        if view_args is not None:
            view_args = ElementRef(*view_args)
        self._view_args = view_args
        super().__init__(*args, **kwargs)

    # TODO: This makes `deepcopy(obj)` return `obj._view_args.parent._adata_ref`, fix it
    def __deepcopy__(self, memo):
        parent, attrname, keys = self._view_args
        return deepcopy(getattr(parent._adata_ref, attrname))


class ArrayView(_SetItemMixin, np.ndarray):
    def __new__(
        cls,
        input_array: Sequence[Any],
        view_args: Tuple["anndata.AnnData", str, Tuple[str, ...]] = None,
    ):
        arr = np.asanyarray(input_array).view(cls)

        if view_args is not None:
            view_args = ElementRef(*view_args)
        arr._view_args = view_args
        return arr

    def __array_finalize__(self, obj: Optional[np.ndarray]):
        if obj is not None:
            self._view_args = getattr(obj, "_view_args", None)

    def keys(self) -> KeysView[str]:
        # it’s a structured array
        return self.dtype.names

    def copy(self, order: str = "C") -> np.ndarray:
        # we want a conventional array
        return np.array(self)

    def toarray(self) -> np.ndarray:
        return self.copy()


# Unlike array views, SparseCSRView and SparseCSCView
# do not propagate through subsetting
class SparseCSRView(_ViewMixin, sparse.csr_matrix):
    # https://github.com/scverse/anndata/issues/656
    def copy(self) -> sparse.csr_matrix:
        return sparse.csr_matrix(self).copy()


class SparseCSCView(_ViewMixin, sparse.csc_matrix):
    # https://github.com/scverse/anndata/issues/656
    def copy(self) -> sparse.csc_matrix:
        return sparse.csc_matrix(self).copy()


class DictView(_ViewMixin, dict):
    pass


class DataFrameView(_ViewMixin, pd.DataFrame):
    _metadata = ["_view_args"]

    @wraps(pd.DataFrame.drop)
    def drop(self, *args, inplace: bool = False, **kw):
        if not inplace:
            return self.copy().drop(*args, **kw)
        with self._update() as df:
            df.drop(*args, inplace=True, **kw)


@singledispatch
def as_view(obj, view_args):
    raise NotImplementedError(f"No view type has been registered for {type(obj)}")


@as_view.register(np.ndarray)
def as_view_array(array, view_args):
    return ArrayView(array, view_args=view_args)


@as_view.register(pd.DataFrame)
def as_view_df(df, view_args):
    return DataFrameView(df, view_args=view_args)


@as_view.register(sparse.csr_matrix)
def as_view_csr(mtx, view_args):
    return SparseCSRView(mtx, view_args=view_args)


@as_view.register(sparse.csc_matrix)
def as_view_csc(mtx, view_args):
    return SparseCSCView(mtx, view_args=view_args)


@as_view.register(dict)
def as_view_dict(d, view_args):
    return DictView(d, view_args=view_args)


@as_view.register(ZappyArray)
def as_view_zappy(z, view_args):
    # Previous code says ZappyArray works as view,
    # but as far as I can tell they’re immutable.
    return z


def _resolve_idxs(old, new, adata):
    t = tuple(_resolve_idx(old[i], new[i], adata.shape[i]) for i in (0, 1))
    return t


@singledispatch
def _resolve_idx(old, new, l):
    return old[new]


@_resolve_idx.register(np.ndarray)
def _resolve_idx_ndarray(old, new, l):
    if is_bool_dtype(old):
        old = np.where(old)[0]
    return old[new]


@_resolve_idx.register(np.integer)
@_resolve_idx.register(int)
def _resolve_idx_scalar(old, new, l):
    return np.array([old])[new]


@_resolve_idx.register(slice)
def _resolve_idx_slice(old, new, l):
    if isinstance(new, slice):
        return _resolve_idx_slice_slice(old, new, l)
    else:
        return np.arange(*old.indices(l))[new]


def _resolve_idx_slice_slice(old, new, l):
    r = range(*old.indices(l))[new]
    # Convert back to slice
    start, stop, step = r.start, r.stop, r.step
    if len(r) == 0:
        stop = start
    elif stop < 0:
        stop = None
    return slice(start, stop, step)
