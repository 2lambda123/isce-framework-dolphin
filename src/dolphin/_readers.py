from __future__ import annotations

import mmap
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from os import fspath
from pathlib import Path
from typing import Generator, Optional, Protocol, Sequence, runtime_checkable

import numpy as np
from numpy.typing import ArrayLike
from osgeo import gdal

from dolphin import io, utils
from dolphin._dates import get_dates, sort_files_by_date
from dolphin._types import Filename
from dolphin.stack import logger

__all__ = ["VRTStack", "DatasetReader", "BinaryFile", "StackReader", "BinaryStack"]


@runtime_checkable
class DatasetReader(Protocol):
    """An array-like interface for reading input datasets.

    `DatasetReader` defines the abstract interface that types must conform to in order
    to be read by functions which iterate in blocks over the input data.
    Such objects must export NumPy-like `dtype`, `shape`, and `ndim` attributes,
    and must support NumPy-style slice-based indexing.

    Note that this protol allows objects to be passed to `dask.array.from_array`
    which needs `.shape`, `.ndim`, `.dtype` and support numpy-style slicing.
    """

    dtype: np.dtype
    """numpy.dtype : Data-type of the array's elements."""  # noqa: D403

    shape: tuple[int, ...]
    """tuple of int : Tuple of array dimensions."""  # noqa: D403

    ndim: int
    """int : Number of array dimensions."""  # noqa: D403

    def __getitem__(self, key: tuple[slice, ...], /) -> ArrayLike:
        """Read a block of data."""
        ...


@runtime_checkable
class StackReader(DatasetReader, Protocol):
    """An array-like interface for reading a 3D stack of input datasets.

    `StackReader` defines the abstract interface that types must conform to in order
    to be valid inputs to be read in functions like [dolphin.ps.create_ps][].
    It is a specialization of [DatasetReader][] that requires a 3D shape.
    """

    ndim: int = 3
    """int : Number of array dimensions."""  # noqa: D403

    shape: tuple[int, int, int]
    """tuple of int : Tuple of array dimensions."""


@dataclass
class BinaryFile(DatasetReader):
    """A flat binary file for storing array data.

    See Also
    --------
    HDF5Dataset
    RasterBand

    Notes
    -----
    This class does not store an open file object. Instead, the file is opened on-demand
    for reading or writing and closed immediately after each read/write operation. This
    allows multiple spawned processes to write to the file in coordination (as long as a
    suitable mutex is used to guard file access.)
    """

    filepath: Path
    """pathlib.Path : The file path."""  # noqa: D403

    shape: tuple[int, ...]
    """tuple of int : Tuple of array dimensions."""  # noqa: D403

    dtype: np.dtype
    """numpy.dtype : Data-type of the array's elements."""  # noqa: D403

    def __post_init__(self):
        self.filepath = Path(self.filepath)
        if not self.filepath.exists():
            raise FileNotFoundError(f"File {self.filepath} does not exist.")
        self.dtype = np.dtype(self.dtype)
        self.filepath = Path(self.filepath)

    @property
    def ndim(self) -> int:  # type: ignore[override]
        """int : Number of array dimensions."""  # noqa: D403
        return len(self.shape)

    def __getitem__(self, key: tuple[slice, ...], /) -> np.ndarray:
        with self.filepath.open("rb") as f:
            # Memory-map the entire file.
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                # In order to safely close the memory-map, there can't be any dangling
                # references to it, so we return a copy (not a view) of the requested
                # data and decref the array object.
                arr = np.frombuffer(mm, dtype=self.dtype).reshape(self.shape)
                data = arr[key].copy()
                del arr
            return data

    def __array__(self) -> np.ndarray:
        return self[:,]

    @classmethod
    def from_gdal(cls, filename: Filename, band: int = 1) -> BinaryFile:
        """Create a BinaryFile from a GDAL-readable file.

        Parameters
        ----------
        filename : Filename
            Path to the file to read.
        band : int, optional
            Band to read from the file, by default 1

        Returns
        -------
        BinaryFile
            The BinaryFile object.
        """
        import rasterio as rio

        with rio.open(filename) as src:
            dtype = src.dtypes[band - 1]
            shape = src.shape
        return cls(Path(filename), shape=shape, dtype=dtype)


@dataclass
class RasterStack(StackReader):
    """Base class for a stack of separate raster image files.

    Parameters
    ----------
    file_list : list[Filename]
        List of paths to files to read.
    shape : tuple[int, int]
        Shape of each file.
    dtype : np.dtype
        Data type of each file.
    """

    file_list: Sequence[Filename]
    readers: Sequence[DatasetReader]
    num_threads: int = 1

    def __getitem__(self, key: tuple[slice, ...], /) -> np.ndarray:
        # Check that it's a tuple of slices
        if not isinstance(key, tuple):
            raise ValueError("Index must be a tuple of slices.")
        if len(key) not in (1, 3):
            raise ValueError("Index must be a tuple of 1 or 3 slices.")
        # If only the band is passed (e.g. stack[0]), convert to (0, :, :)
        if len(key) == 1:
            key = (key[0], slice(None), slice(None))

        # unpack the slices
        bands, rows, cols = key
        if isinstance(bands, slice):
            # convert the bands to -1-indexed list
            band_idxs = list(range(*bands.indices(len(self))))
        elif isinstance(bands, int):
            band_idxs = [bands]
        else:
            raise ValueError("Band index must be an integer or slice.")

        # Get only the bands we need
        if self.num_threads == 0:
            return np.stack([self.readers[i][rows, cols] for i in band_idxs], axis=-1)
        else:
            # test read 0
            with ThreadPoolExecutor(max_workers=self.num_threads) as executor:
                results = executor.map(lambda i: self.readers[i][rows, cols], band_idxs)
            return np.stack(list(results), axis=-1)

    def __len__(self):
        return len(self.file_list)

    @property
    def shape(self):
        return (len(self), *self.shape_2d)


@dataclass
class BinaryStack(StackReader):
    shape_2d: tuple[int, int]

    def __post_init__(self):
        self.readers = [
            BinaryFile(f, shape=self.shape_2d, dtype=self.dtype) for f in self.file_list
        ]


class VRTStack(DatasetReader):
    """Class for creating a virtual stack of raster files.

    Attributes
    ----------
    file_list : list[Filename]
        Paths or GDAL-compatible strings (NETCDF:...) for paths to files.
    outfile : pathlib.Path, optional (default = Path("slc_stack.vrt"))
        Name of output file to write
    dates : list[list[DateOrDatetime]]
        list, where each entry is all dates matched from the corresponding file
        in `file_list`. This is used to sort the files by date.
        Each entry is a list because some files (compressed SLCs) may have
        multiple dates in the filename.
    use_abs_path : bool, optional (default = True)
        Write the filepaths of the SLCs in the VRT as "relative=0"
    subdataset : str, optional
        Subdataset to use from the files in `file_list`, if using NetCDF files.
    sort_files : bool, optional (default = True)
        Sort the files in `file_list`. Assumes that the naming convention
        will sort the files in increasing time order.
    nodata_mask_file : pathlib.Path, optional
        Path to file containing a mask of pixels containing with nodata
        in every images. Used for skipping the loading of these pixels.
    file_date_fmt : str, optional (default = "%Y%m%d")
        Format string for parsing the dates from the filenames.
        Passed to [dolphin._dates.get_dates][].
    """

    def __init__(
        self,
        file_list: Sequence[Filename],
        outfile: Filename = "slc_stack.vrt",
        use_abs_path: bool = True,
        subdataset: Optional[str] = None,
        sort_files: bool = True,
        file_date_fmt: str = "%Y%m%d",
        write_file: bool = True,
        fail_on_overwrite: bool = False,
        skip_size_check: bool = False,
    ):
        if Path(outfile).exists() and write_file:
            if fail_on_overwrite:
                raise FileExistsError(
                    f"Output file {outfile} already exists. "
                    "Please delete or specify a different output file. "
                    "To create from an existing VRT, use the `from_vrt_file` method."
                )
            else:
                logger.info(f"Overwriting {outfile}")

        files: list[Filename] = [Path(f) for f in file_list]
        self._use_abs_path = use_abs_path
        if use_abs_path:
            files = [utils._resolve_gdal_path(p) for p in files]
        # Extract the date/datetimes from the filenames
        dates = [get_dates(f, fmt=file_date_fmt) for f in files]
        if sort_files:
            files, dates = sort_files_by_date(  # type: ignore
                files, file_date_fmt=file_date_fmt
            )

        # Save the attributes
        self.file_list = files
        self.dates = dates

        self.outfile = Path(outfile).resolve()
        # Assumes that all files use the same subdataset (if NetCDF)
        self.subdataset = subdataset

        if not skip_size_check:
            io._assert_images_same_size(self._gdal_file_strings)

        # Use the first file in the stack to get size, transform info
        ds = gdal.Open(fspath(self._gdal_file_strings[0]))
        self.xsize = ds.RasterXSize
        self.ysize = ds.RasterYSize
        # Should be CFloat32
        self.gdal_dtype = gdal.GetDataTypeName(ds.GetRasterBand(1).DataType)
        # Save these for setting at the end
        self.gt = ds.GetGeoTransform()
        self.proj = ds.GetProjection()
        self.srs = ds.GetSpatialRef()
        ds = None
        # Save the subset info

        self.xoff, self.yoff = 0, 0
        self.xsize_sub, self.ysize_sub = self.xsize, self.ysize

        if write_file:
            self._write()

    def _write(self):
        """Write out the VRT file pointing to the stack of SLCs, erroring if exists."""
        with open(self.outfile, "w") as fid:
            fid.write(
                f'<VRTDataset rasterXSize="{self.xsize_sub}"'
                f' rasterYSize="{self.ysize_sub}">\n'
            )

            for idx, filename in enumerate(self._gdal_file_strings, start=1):
                chunk_size = io.get_raster_chunk_size(filename)
                # chunks in a vrt have a min of 16, max of 2**14=16384
                # https://github.com/OSGeo/gdal/blob/2530defa1e0052827bc98696e7806037a6fec86e/frmts/vrt/vrtrasterband.cpp#L339
                if any([b < 16 for b in chunk_size]) or any(
                    [b > 16384 for b in chunk_size]
                ):
                    chunk_str = ""
                else:
                    chunk_str = (
                        f'blockXSize="{chunk_size[0]}" blockYSize="{chunk_size[1]}"'
                    )
                outstr = f"""  <VRTRasterBand dataType="{self.gdal_dtype}" band="{idx}" {chunk_str}>
    <SimpleSource>
      <SourceFilename>{filename}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SrcRect xOff="{self.xoff}" yOff="{self.yoff}" xSize="{self.xsize_sub}" ySize="{self.ysize_sub}"/>
      <DstRect xOff="0" yOff="0" xSize="{self.xsize_sub}" ySize="{self.ysize_sub}"/>
    </SimpleSource>
  </VRTRasterBand>\n"""  # noqa: E501
                fid.write(outstr)

            fid.write("</VRTDataset>")

        # Set the georeferencing metadata
        ds = gdal.Open(fspath(self.outfile), gdal.GA_Update)
        ds.SetGeoTransform(self.gt)
        ds.SetProjection(self.proj)
        ds.SetSpatialRef(self.srs)
        ds = None

    @property
    def _gdal_file_strings(self):
        """Get the GDAL-compatible paths to write to the VRT.

        If we're not using .h5 or .nc, this will just be the file_list as is.
        """
        return [io.format_nc_filename(f, self.subdataset) for f in self.file_list]

    def read_stack(
        self,
        band: Optional[int] = None,
        subsample_factor: int = 1,
        rows: Optional[slice] = None,
        cols: Optional[slice] = None,
        masked: bool = False,
    ):
        """Read in the SLC stack."""
        return io.load_gdal(
            self.outfile,
            band=band,
            subsample_factor=subsample_factor,
            rows=rows,
            cols=cols,
            masked=masked,
        )

    def __fspath__(self):
        # Allows os.fspath() to work on the object, enabling rasterio.open()
        return fspath(self.outfile)

    @classmethod
    def from_vrt_file(cls, vrt_file, new_outfile=None, **kwargs):
        """Create a new VRTStack using an existing VRT file."""
        file_list, subdataset = _parse_vrt_file(vrt_file)
        if new_outfile is None:
            # Point to the same, if none provided
            new_outfile = vrt_file

        return cls(
            file_list,
            outfile=new_outfile,
            subdataset=subdataset,
            write_file=False,
            **kwargs,
        )

    def iter_blocks(
        self,
        overlaps: tuple[int, int] = (0, 0),
        block_shape: tuple[int, int] = (512, 512),
        skip_empty: bool = True,
        nodata_mask: Optional[np.ndarray] = None,
        show_progress: bool = True,
    ) -> Generator[tuple[np.ndarray, tuple[slice, slice]], None, None]:
        """Iterate over blocks of the stack.

        Loads all images for one window at a time into memory.

        Parameters
        ----------
        overlaps : tuple[int, int], optional
            Pixels to overlap each block by (rows, cols)
            By default (0, 0)
        block_shape : tuple[int, int], optional
            2D shape of blocks to load at a time.
            Loads all dates/bands at a time with this shape.
        skip_empty : bool, optional (default True)
            Skip blocks that are entirely empty (all NaNs)
        nodata_mask : bool, optional
            Optional mask indicating nodata values. If provided, will skip
            blocks that are entirely nodata.
            1s are the nodata values, 0s are valid data.
        show_progress : bool, default=True
            If true, displays a `rich` ProgressBar.

        Yields
        ------
        tuple[np.ndarray, tuple[slice, slice]]
            Iterator of (data, (slice(row_start, row_stop), slice(col_start, col_stop))

        """
        self._loader = io.EagerLoader(
            self.outfile,
            block_shape=block_shape,
            overlaps=overlaps,
            nodata_mask=nodata_mask,
            skip_empty=skip_empty,
            show_progress=show_progress,
        )
        yield from self._loader.iter_blocks()

    @property
    def shape(self):
        """Get the 3D shape of the stack."""
        xsize, ysize = io.get_raster_xysize(self._gdal_file_strings[0])
        return (len(self.file_list), ysize, xsize)

    def __len__(self):
        return len(self.file_list)

    def __repr__(self):
        outname = fspath(self.outfile) if self.outfile else "(not written)"
        return f"VRTStack({len(self.file_list)} bands, outfile={outname})"

    def __eq__(self, other):
        if not isinstance(other, VRTStack):
            return False
        return (
            self._gdal_file_strings == other._gdal_file_strings
            and self.outfile == other.outfile
        )

    @property
    def ndim(self):
        return 3

    def __getitem__(self, index):
        if isinstance(index, int):
            if index < 0:
                index = len(self) + index
            return self.read_stack(band=index + 1)

        # TODO: raise an error if they try to skip like [::2, ::2]
        # or pass it to read_stack... but I dont think I need to support it.
        n, rows, cols = index
        if isinstance(rows, int):
            rows = slice(rows, rows + 1)
        if isinstance(cols, int):
            cols = slice(cols, cols + 1)
        if isinstance(n, int):
            if n < 0:
                n = len(self) + n
            return self.read_stack(band=n + 1, rows=rows, cols=cols)

        bands = list(range(1, 1 + len(self)))[n]
        if len(bands) == len(self):
            # This will use gdal's ds.ReadAsRaster, no iteration needed
            data = self.read_stack(band=None, rows=rows, cols=cols)
        else:
            data = np.stack(
                [self.read_stack(band=i, rows=rows, cols=cols) for i in bands], axis=0
            )
        return data.squeeze()

    @property
    def dtype(self):
        return io.get_raster_dtype(self._gdal_file_strings[0])


def _parse_vrt_file(vrt_file):
    """Extract the filenames, and possible subdatasets, from a .vrt file.

    Assumes, if using HDFS/NetCDF files, that the subdataset is the same.

    Note that we are parsing the XML, not using `GetFilelist`, because the
    function does not seem to work when using HDF5 files. E.g.

        <SourceFilename ="1">NETCDF:20220111.nc:SLC/VV</SourceFilename>

    This would not get added to the result of `GetFilelist`

    Parameters
    ----------
    vrt_file : Filename
        Path to the VRT file to read.

    Returns
    -------
    filepaths
        List of filepaths to the SLCs
    sds
        Subdataset name, if using NetCDF/HDF5 files
    """
    file_strings = []
    with open(vrt_file) as f:
        for line in f:
            if "<SourceFilename" not in line:
                continue
            # Get the middle part of < >filename</ >
            fn = line.split(">")[1].strip().split("<")[0]
            file_strings.append(fn)

    sds = ""
    filepaths = []
    for name in file_strings:
        if name.upper().startswith("HDF5:") or name.upper().startswith("NETCDF:"):
            prefix, filepath, subdataset = name.split(":")
            # Clean up subdataset
            sds = subdataset.replace('"', "").replace("'", "").lstrip("/")
            # Remove quoting if it was present
            filepaths.append(filepath.replace('"', "").replace("'", ""))
        else:
            filepaths.append(name)

    return filepaths, sds
