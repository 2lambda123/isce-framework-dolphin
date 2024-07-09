from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from dolphin._types import Index

__all__ = [
    "get_gtiff_options",
    "repack_raster",
    "repack_rasters",
]


def _ensure_slices(rows: Index, cols: Index) -> tuple[slice, slice]:
    def _parse(key: Index):
        if isinstance(key, int):
            return slice(key, key + 1)
        elif key is ...:
            return slice(None)
        else:
            return key

    return _parse(rows), _parse(cols)


def _unpack_3d_slices(key: tuple[Index, ...]) -> tuple[Index, slice, slice]:
    # Check that it's a tuple of slices
    if not isinstance(key, tuple):
        msg = "Index must be a tuple of slices."
        raise TypeError(msg)
    if len(key) not in (1, 3):
        msg = "Index must be a tuple of 1 or 3 slices."
        raise TypeError(msg)
    # If only the band is passed (e.g. stack[0]), convert to (0, :, :)
    if len(key) == 1:
        key = (key[0], slice(None), slice(None))
    # unpack the slices
    bands, rows, cols = key
    # convert the rows/cols to slices
    r_slice, c_slice = _ensure_slices(rows, cols)
    return bands, r_slice, c_slice


def get_gtiff_options(
    max_error: float | None = None,
    compression_type: str = "lzw",
    chunk_size: int = 256,
    predictor: int | None = None,
    zlevel: int | None = 1,
) -> dict[str, str]:
    """Generate GTiff creation options for GDAL translate.

    Parameters
    ----------
    max_error : float
        Maximum compression error.
    compression_type : str, optional
        Compression type to use (default is "lzw").
    chunk_size : int, optional
        Size of the chunks for blockxsize and blockysize (default is 256).
    predictor : int or None, optional
        Predictor type to use (default is 3). Use None to omit the predictor.
    zlevel : int or None, optional
        Compression level for the 'deflate' and 'zstd' compression types (default is 1).
        Use None to omit the zlevel.
    gdal_format: bool, default = True

    Returns
    -------
    dict[str, str] | list[str]
        List of GTiff creation options formatted for GDAL (if `gdal_format=True`)
        Otherwise, a dict mapping option to value for rasterio.

    """
    options = {
        "bigtiff": "yes",
        "tiled": "yes",
        "blockxsize": str(chunk_size),
        "blockysize": str(chunk_size),
        "compress": compression_type,
    }
    if zlevel is not None:
        options["zlevel"] = str(zlevel)
    if predictor is not None:
        options["predictor"] = str(predictor)
    if compression_type.lower().startswith("lerc") and max_error is not None:
        options["max_z_error"] = str(max_error)

    return options


def _format_for_gdal(options: dict[str, str]) -> list[str]:
    """Output creation options as a list of -co strings for GDAL.

    Parameters
    ----------
    options : dict[str, str]
        Dict of creation options usable in Rasterio.

    Returns
    -------
    list[str]
        List -co options for GDAL.

    """
    return [f"{k.upper()}={v}" for k, v in options.items()]


def repack_raster(
    raster_path: Path,
    output_dir: Path | None = None,
    significant_bits: int | None = None,
    **output_options,
) -> Path:
    """Repack a single raster file with GDAL Translate using provided options.

    Parameters
    ----------
    raster_path : Path
        Path to the input raster file.
    output_dir : Path, optional
        Directory to save the repacked rasters or None for in-place repacking.
    significant_bits : int, optional
        Number of bits to preserve in mantissa. Defaults to None.
        Lower numbers will truncate the mantissa more and enable more compression.
    **output_options
        Keyword args passed to `get_gtiff_options`

    Returns
    -------
    output_path : Path
        Path to newly created file.
        If `output_dir` is None, this will be the same filename as `raster_path`

    """
    import rasterio as rio

    if output_dir is None:
        output_file = tempfile.NamedTemporaryFile(
            suffix=raster_path.suffix, dir=output_dir, delete=False
        )
        output_path = Path(output_file.name)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / raster_path.name

    options = get_gtiff_options(**output_options)
    with rio.open(raster_path) as src:
        profile = src.profile
        profile.update(**options)
        with rio.open(output_path, "w", **profile) as dst:
            for i in range(1, src.count + 1):
                data = src.read(i)
                if significant_bits is not None:
                    round_mantissa(data, significant_bits)
                dst.write(data, i)

    if output_dir is None:
        # Overwrite the original
        shutil.move(output_path, raster_path)
        output_path = raster_path
    return output_path


def repack_rasters(
    raster_files: list[Path],
    output_dir: Path | None = None,
    num_threads: int = 4,
    significant_bits: int | None = None,
    **output_options,
):
    """Recreate and compress a list of raster files.

    Useful for rasters which were created in block and lost
    the full effect of compression.

    Parameters
    ----------
    raster_files : List[Path]
        List of paths to the input raster files.
    output_dir : Path, optional
        Directory to save the processed rasters or None for in-place processing.
    num_threads : int, optional
        Number of threads to use (default is 4).
    significant_bits : int, optional
        Number of bits to preserve in mantissa. Defaults to None.
        Lower numbers will truncate the mantissa more and enable more compression
    **output_options
        Creation options to pass to `get_gtiff_options`

    Returns
    -------
    output_path : Path
        Path to newly created file.
        If `output_dir` is None, this will be the same as `raster_paths`

    """
    from tqdm.contrib.concurrent import thread_map

    thread_map(
        lambda raster: repack_raster(
            raster, output_dir, significant_bits=significant_bits, **output_options
        ),
        raster_files,
        max_workers=num_threads,
        desc="Processing Rasters",
    )


def round_mantissa(z: NDArray, significant_bits=10, truncate: bool = False):
    """Zero out bits in mantissa of elements of array in place.

    Attempts to round the floating point numbers zeroing.

    Parameters
    ----------
    z : numpy.array
        Real or complex array whose mantissas are to be zeroed out
    significant_bits : int, optional
        Number of bits to preserve in mantissa. Defaults to 10.
        Lower numbers will truncate the mantissa more and enable
        more compression.
    truncate : bool, optional
        Instead of attempting to round, simply truncate the mantissa.
        Default = False

    """
    # recurse for complex data
    if np.iscomplexobj(z):
        round_mantissa(z.real, significant_bits)
        round_mantissa(z.imag, significant_bits)
        return

    if not issubclass(z.dtype.type, np.floating):
        err_str = "argument z is not complex float or float type"
        raise TypeError(err_str)

    mant_bits = np.finfo(z.dtype).nmant
    float_bytes = z.dtype.itemsize

    if significant_bits == mant_bits:
        return

    if not 0 < significant_bits <= mant_bits:
        err_str = f"Require 0 < {significant_bits=} <= {mant_bits}"
        raise ValueError(err_str)

    # create integer value whose binary representation is one for all bits in
    # the floating point type.
    allbits = (1 << (float_bytes * 8)) - 1

    # Construct bit mask by left shifting by nzero_bits and then truncate.
    # This works because IEEE 754 specifies that bit order is sign, then
    # exponent, then mantissa.  So we zero out the least significant mantissa
    # bits when we AND with this mask.
    nzero_bits = mant_bits - significant_bits
    bitmask = (allbits << nzero_bits) & allbits

    utype = np.dtype(f"u{float_bytes}")
    # view as uint type (can not mask against float)
    u = z.view(utype)

    if truncate is False:
        round_mask = 1 << (nzero_bits - 1)
        u += round_mask  # Add the rounding mask before applying the bitmask
    # bitwise-and in-place to mask
    u &= bitmask
