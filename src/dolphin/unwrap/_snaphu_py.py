from __future__ import annotations

from pathlib import Path

from dolphin._log import get_log
from dolphin._types import Filename
from dolphin.io._core import DEFAULT_TIFF_OPTIONS_RIO
from dolphin.utils import full_suffix

from ._constants import CONNCOMP_SUFFIX, DEFAULT_CCL_NODATA, DEFAULT_UNW_NODATA
from ._utils import _zero_from_mask

logger = get_log(__name__)


def unwrap_snaphu_py(
    ifg_filename: Filename,
    corr_filename: Filename,
    unw_filename: Filename,
    nlooks: float,
    ntiles: tuple[int, int] = (1, 1),
    tile_overlap: tuple[int, int] = (0, 0),
    nproc: int = 1,
    mask_file: Filename | None = None,
    zero_where_masked: bool = False,
    unw_nodata: float | None = DEFAULT_UNW_NODATA,
    ccl_nodata: int | None = DEFAULT_CCL_NODATA,
    init_method: str = "mst",
    cost: str = "smooth",
    scratchdir: Filename | None = None,
) -> tuple[Path, Path]:
    """Unwrap an interferogram using at multiple scales using `tophu`.

    Parameters
    ----------
    ifg_filename : Filename
        Path to input interferogram.
    corr_filename : Filename
        Path to input correlation file.
    unw_filename : Filename
        Path to output unwrapped phase file.
    downsample_factor : tuple[int, int]
        Downsample the interferograms by this factor to unwrap faster, then upsample
    nlooks : float
        Effective number of looks used to form the input correlation data.
    ntiles : tuple[int, int], optional
        Number of (row, column) tiles to split for full image into.
        If `ntiles` is an int, will use `(ntiles, ntiles)`
    tile_overlap : tuple[int, int], optional
        Number of pixels to overlap in the (row, col) direction.
        Default = (0, 0)
    nproc : int, optional
        If specifying `ntiles`, number of processes to spawn to unwrap the
        tiles in parallel.
        Default = 1, which unwraps each tile in serial.
    mask_file : Filename, optional
        Path to binary byte mask file, by default None.
        Assumes that 1s are valid pixels and 0s are invalid.
    zero_where_masked : bool, optional
        Set wrapped phase/correlation to 0 where mask is 0 before unwrapping.
        If not mask is provided, this is ignored.
        By default True.
    unw_nodata : float , optional.
        If providing `unwrap_callback`, provide the nodata value for your
        unwrapping function.
    ccl_nodata : float, optional
        Nodata value for the connected component labels.
    init_method : str, choices = {"mcf", "mst"}
        initialization method, by default "mst"
    cost : str
        Statistical cost mode.
        Default = "smooth"
    scratchdir : Filename, optional
        If provided, uses a scratch directory to save the intermediate files
        during unwrapping.

    Returns
    -------
    unw_path : Path
        Path to output unwrapped phase file.
    conncomp_path : Path
        Path to output connected component label file.

    """
    import snaphu

    unw_suffix = full_suffix(unw_filename)
    cc_filename = str(unw_filename).replace(unw_suffix, CONNCOMP_SUFFIX)

    if zero_where_masked and mask_file is not None:
        logger.info(f"Zeroing phase/corr of pixels masked in {mask_file}")
        zeroed_ifg_file, zeroed_corr_file = _zero_from_mask(
            ifg_filename, corr_filename, mask_file
        )
        igram = snaphu.io.Raster(zeroed_ifg_file)
        corr = snaphu.io.Raster(zeroed_corr_file)
    else:
        igram = snaphu.io.Raster(ifg_filename)
        corr = snaphu.io.Raster(corr_filename)

    mask = None if mask_file is None else snaphu.io.Raster(mask_file)
    try:
        with (
            snaphu.io.Raster.create(
                unw_filename,
                like=igram,
                nodata=unw_nodata,
                dtype="f4",
                **DEFAULT_TIFF_OPTIONS_RIO,
            ) as unw,
            snaphu.io.Raster.create(
                cc_filename,
                like=igram,
                nodata=ccl_nodata,
                dtype="u2",
                **DEFAULT_TIFF_OPTIONS_RIO,
            ) as conncomp,
        ):
            # Unwrap and store the results in the `unw` and `conncomp` rasters.
            snaphu.unwrap(
                igram,
                corr,
                nlooks=nlooks,
                init=init_method,
                cost=cost,
                mask=mask,
                unw=unw,
                conncomp=conncomp,
                ntiles=ntiles,
                tile_overlap=tile_overlap,
                nproc=nproc,
                scratchdir=scratchdir,
                # https://github.com/isce-framework/snaphu-py/commit/a77cbe1ff115d96164985523987b1db3278970ed
                # On frame-sized ifgs, especially with decorrelation, defaults of
                # (500, 100) for (tile_cost_thresh, min_region_size) lead to
                # "Exceeded maximum number of secondary arcs"
                # "Decrease TILECOSTTHRESH and/or increase MINREGIONSIZE"
                tile_cost_thresh=200,
                # ... "and/or increase MINREGIONSIZE"
                min_region_size=300,
            )
    finally:
        igram.close()
        corr.close()
        if mask is not None:
            mask.close()
    if zero_where_masked and mask_file is not None:
        logger.info("Zeroing unw/conncomp of pixels masked in " f"{mask_file}")

        return _zero_from_mask(unw_filename, cc_filename, mask_file)

    return Path(unw_filename), Path(cc_filename)
