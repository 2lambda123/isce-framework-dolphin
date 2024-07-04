import warnings

import numpy as np
from numpy.typing import ArrayLike

from dolphin.utils import upsample_nearest


def compress(
    slc_stack: ArrayLike, pl_cpx_phase: ArrayLike, slc_mean: ArrayLike | None = None
):
    """Compress the stack of SLC data using the estimated phase.

    Parameters
    ----------
    slc_stack : ArrayLike
        The stack of complex SLC data, shape (nslc, rows, cols)
    pl_cpx_phase : ArrayLike
        The estimated complex phase from phase linking.
        shape  = (nslc, rows // strides.y, cols // strides.x)
    slc_mean : ArrayLike, optional
        The mean SLC magnitude, shape (rows, cols), to use as output pixel magnitudes.
        If None, the mean is computed from the input SLC stack.

    Returns
    -------
    np.array
        The compressed SLC data, shape (rows, cols)

    """
    # If the output is downsampled, we need to make `pl_cpx_phase` the same shape
    # as the output
    pl_estimate_upsampled = upsample_nearest(pl_cpx_phase, slc_stack.shape[1:])
    # For each pixel, project the SLCs onto the (normalized) estimated phase
    # by performing a pixel-wise complex dot product
    pl_norm = np.linalg.norm(pl_estimate_upsampled, axis=0)
    # Avoid divide by zero (there may be 0s at the upsampled boundary)
    pl_norm[pl_norm == 0] = np.nan
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="invalid value encountered")
        phase = np.angle(
            np.nansum(slc_stack * np.conjugate(pl_estimate_upsampled), axis=0) / pl_norm
        )
    if slc_mean is None:
        slc_mean = np.nanmean(np.abs(slc_stack), axis=0)
    return slc_mean * np.exp(1j * phase)
