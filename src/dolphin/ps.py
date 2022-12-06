"""Find the persistent scatterers in a stack of SLCS."""
from os import fspath
from pathlib import Path

import numpy as np
from osgeo import gdal
from osgeo_utils import gdal_calc

gdal.UseExceptions()

from dolphin.io import copy_projection
from dolphin.utils import Filename


def create_amp_dispersion(
    *,
    slc_vrt_file: Filename,
    output_file: Filename,
    amp_mean_file: Filename,
    reference_band: int,
    lines_per_block: int = 1000,
    ram: int = 1024,
):
    """Create the amplitude dispersion file using FRInGE."""
    import ampdispersionlib

    aa = ampdispersionlib.Ampdispersion()

    aa.inputDS = fspath(slc_vrt_file)
    aa.outputDS = fspath(output_file)
    aa.meanampDS = fspath(amp_mean_file)

    aa.blocksize = lines_per_block
    aa.memsize = ram
    aa.refband = reference_band

    aa.run()
    copy_projection(slc_vrt_file, output_file)


def create_amp_dispersion_py(
    *,
    slc_vrt_file: Filename,
    output_file: Filename,
    amp_mean_file: Filename,
    reference_band: int,
):
    """Create the amplitude dispersion file using Python.

    Parameters
    ----------
    slc_vrt_file : Filename
        The VRT file pointing to the stack of SLCs.
    output_file : Filename
        The output amplitude dispersion file.
    amp_mean_file : Filename
        The mean amplitude file.
    reference_band : int
        The band number of the reference SLC.
    """
    pass


def create_ps(
    *,
    output_file: Filename,
    amp_disp_file: Filename,
    amp_dispersion_threshold: float = 0.42,
):
    """Create the PS file using the existing amplitude dispersion file."""
    gdal_calc.Calc(
        [f"a<{amp_dispersion_threshold}"],
        a=fspath(amp_disp_file),
        outfile=fspath(output_file),
        format="ENVI",
        type="Byte",
        overwrite=True,
        quiet=True,
    )
    copy_projection(amp_disp_file, output_file)


def update_amp_disp(
    amp_mean_file: Filename,
    amp_disp_file: Filename,
    slc_vrt_file: Filename,
    output_directory: Filename = "",
):
    r"""Update the amplitude dispersion for the new SLC.

    Uses Welford's method to update the mean and variance.

    \[
    \begin{align}
    \mu_{n+1} &= \mu_n + (x_{n+1} - \mu_n) / (n+1)  \\
    \text{var}_{n+1} &= \text{var}_n + ((x_{n+1} - \mu_n) * (x_{n+1} - \mu_{n+1}) - \text{var}_n) / (n+1) \\
    v1 &= v0 + (x1 - m0) * (x1 - m1)
    \end{align}
    \]


    See <https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Welford's_online_algorithm>
    or <https://changyaochen.github.io/welford/> for derivation.


    Parameters
    ----------
    amp_mean_file : Filename
        The existing mean amplitude file.
    amp_disp_file : Filename
        The existing amplitude dispersion file.
    slc_vrt_file : Filename
        The VRT file pointing to the stack of SLCs.
        Assumes that the final band is the new SLC to be added.
    output_directory : Filename, optional
        The output directory for the updated files, current directory by default.

    References
    ----------
    Welford, B. P. "Note on a method for calculating corrected sums of squares and
    products." Technometrics 4.3 (1962): 419-420.
    """  # noqa: E501
    output_directory = Path(output_directory)
    if not output_directory.exists():
        output_directory.mkdir(parents=True, exist_ok=True)
    output_mean_file = output_directory / Path(amp_mean_file).name
    output_disp_file = output_directory / Path(amp_disp_file).name

    _check_output_files(output_mean_file, output_disp_file)

    ds_mean = gdal.Open(fspath(amp_mean_file), gdal.GA_ReadOnly)
    ds_ampdisp = gdal.Open(fspath(amp_disp_file), gdal.GA_ReadOnly)
    # Get the number of SLCs used to create the mean amplitude
    try:
        # Use the ENVI metadata domain for ENVI files
        md_domain = "ENVI" if ds_mean.GetDriver().ShortName == "ENVI" else ""
        N = int(ds_mean.GetMetadataItem("N", md_domain))
    except KeyError:
        ds_mean = ds_ampdisp = None  # Close files before raising error
        raise ValueError("Cannot find N in metadata of mean amplitude file")

    driver = ds_mean.GetDriver()
    mean_n = ds_mean.GetRasterBand(1).ReadAsArray()
    ampdisp = ds_ampdisp.GetRasterBand(1).ReadAsArray()

    # Get the new data amplitude
    ds_slc_stack = gdal.Open(fspath(slc_vrt_file))
    nbands = ds_slc_stack.RasterCount
    # The last band should be the new SLC
    bnd_new_slc = ds_slc_stack.GetRasterBand(nbands)
    new_amp = np.abs(bnd_new_slc.ReadAsArray())
    bnd_new_slc = ds_slc_stack = None

    # Make the output files
    ds_mean_out = driver.CreateCopy(fspath(output_mean_file), ds_mean)
    ds_ampdisp_out = driver.CreateCopy(fspath(output_disp_file), ds_ampdisp)
    bnd_mean = ds_mean_out.GetRasterBand(1)
    bnd_ampdisp = ds_ampdisp_out.GetRasterBand(1)

    # Get the variance from the amplitude dispersion
    # d = sigma / mu, so sigma^2 = d^2 * mu^2
    var_n = ampdisp**2 * mean_n**2

    # Update the mean
    mean_n1 = mean_n + (new_amp - mean_n) / (N + 1)
    # Update the variance
    var_n1 = var_n + ((new_amp - mean_n) * (new_amp - mean_n1) - var_n) / (N + 1)

    # Update both files with the new values
    bnd_mean.WriteArray(mean_n1)
    bnd_ampdisp.WriteArray(np.sqrt(var_n1 / mean_n1**2))

    # Update the metadata with the new N
    ds_ampdisp.SetMetadataItem("N", str(N + 1), md_domain)
    ds_mean.SetMetadataItem("N", str(N + 1), md_domain)

    # Close the files to save the changes
    bnd_mean = bnd_ampdisp = ds_mean = ds_ampdisp = None
    ds_mean = ds_ampdisp = None


def _check_output_files(*files):
    """Check if the output files already exist."""
    err_msg = "Output file {} already exists. Please delete before running."
    for f in files:
        if f.exists():
            raise FileExistsError(err_msg.format(f))
