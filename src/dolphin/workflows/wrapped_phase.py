from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional, Sequence, cast

import numpy as np
from opera_utils import make_nodata_mask

from dolphin import _readers, interferogram, ps, stack
from dolphin._dates import get_dates
from dolphin._log import get_log, log_runtime

from . import InterferogramNetworkType, sequential
from .config import DisplacementWorkflow


@log_runtime
def run(
    cfg: DisplacementWorkflow, debug: bool = False
) -> tuple[list[Path], Path, Path, Path]:
    """Run the displacement workflow on a stack of SLCs.

    Parameters
    ----------
    cfg : DisplacementWorkflow
        [`DisplacementWorkflow`][dolphin.workflows.config.DisplacementWorkflow] object
        for controlling the workflow.
    debug : bool, optional
        Enable debug logging, by default False.

    Returns
    -------
    list[Path]
        list of Paths to virtual interferograms created.
    Path
        Path the final compressed SLC file created.
    Path
        Path to temporal correlation file created.
        In the case of a single phase linking step, this is the one tcorr file.
        In the case of sequential phase linking, this is the average tcorr file.
    """
    logger = get_log(debug=debug)
    work_dir = cfg.work_directory
    logger.info("Running wrapped phase estimation in %s", work_dir)

    input_file_list = cfg.cslc_file_list

    # #############################################
    # Make a VRT pointing to the input SLC files
    # #############################################
    subdataset = cfg.input_options.subdataset
    vrt_stack = _readers.VRTStack(
        input_file_list,
        subdataset=subdataset,
        outfile=cfg.work_directory / "slc_stack.vrt",
    )

    # Make the nodata mask from the polygons, if we're using OPERA CSLCs
    try:
        nodata_mask_file = cfg.work_directory / "nodata_mask.tif"
        make_nodata_mask(
            vrt_stack.file_list, out_file=nodata_mask_file, buffer_pixels=200
        )
    except Exception as e:
        logger.warning(f"Could not make nodata mask: {e}")
        nodata_mask_file = None

    # ###############
    # PS selection
    # ###############
    ps_output = cfg.ps_options._output_file
    if ps_output.exists():
        logger.info(f"Skipping making existing PS file {ps_output}")
    else:
        logger.info(f"Creating persistent scatterer file {ps_output}")
        try:
            existing_amp: Optional[Path] = cfg.amplitude_mean_files[0]
            existing_disp: Optional[Path] = cfg.amplitude_dispersion_files[0]
        except IndexError:
            existing_amp = existing_disp = None

        ps.create_ps(
            slc_vrt_file=vrt_stack.outfile,
            output_file=ps_output,
            output_amp_mean_file=cfg.ps_options._amp_mean_file,
            output_amp_dispersion_file=cfg.ps_options._amp_dispersion_file,
            amp_dispersion_threshold=cfg.ps_options.amp_dispersion_threshold,
            existing_amp_dispersion_file=existing_disp,
            existing_amp_mean_file=existing_amp,
            block_shape=cfg.worker_settings.block_shape,
        )

    # Save a looked version of the PS mask too
    strides = cfg.output_options.strides
    ps_looked_file = ps.multilook_ps_mask(
        strides=strides, ps_mask_file=cfg.ps_options._output_file
    )

    # #########################
    # phase linking/EVD step
    # #########################
    pl_path = cfg.phase_linking._directory
    pl_path.mkdir(parents=True, exist_ok=True)

    # Mark any files beinning with "compressed" as compressed
    is_compressed = [f.name.startswith("compressed") for f in input_file_list]
    input_dates = _get_input_dates(
        input_file_list, is_compressed, cfg.input_options.cslc_date_fmt
    )
    reference_date, reference_idx = _get_reference_date_idx(
        input_file_list, is_compressed, input_dates
    )

    ministack_planner = stack.MiniStackPlanner(
        file_list=input_file_list,
        dates=input_dates,
        is_compressed=is_compressed,
        output_folder=pl_path,
        max_num_compressed=cfg.phase_linking.max_num_compressed,
        reference_date=reference_date,
        reference_idx=reference_idx,
    )

    phase_linked_slcs = list(pl_path.glob("2*.tif"))
    if len(phase_linked_slcs) > 0:
        logger.info(f"Skipping EVD step, {len(phase_linked_slcs)} files already exist")
        comp_slc_file = sorted(pl_path.glob("compressed*tif"))[-1]
        tcorr_file = next(pl_path.glob("tcorr*tif"))
    else:
        logger.info(f"Running sequential EMI step in {pl_path}")

        # TODO: Need a good way to store the nslc attribute in the PS file...
        # If we pre-compute it from some big stack, we need to use that for SHP
        # finding, not use the size of `slc_vrt_file`
        shp_nslc = None
        (
            phase_linked_slcs,
            comp_slcs,
            tcorr_file,
        ) = sequential.run_wrapped_phase_sequential(
            slc_vrt_file=vrt_stack.outfile,
            ministack_planner=ministack_planner,
            ministack_size=cfg.phase_linking.ministack_size,
            half_window=cfg.phase_linking.half_window.model_dump(),
            strides=strides,
            use_evd=cfg.phase_linking.use_evd,
            beta=cfg.phase_linking.beta,
            mask_file=nodata_mask_file,
            ps_mask_file=ps_output,
            amp_mean_file=cfg.ps_options._amp_mean_file,
            amp_dispersion_file=cfg.ps_options._amp_dispersion_file,
            shp_method=cfg.phase_linking.shp_method,
            shp_alpha=cfg.phase_linking.shp_alpha,
            shp_nslc=shp_nslc,
            block_shape=cfg.worker_settings.block_shape,
            n_workers=cfg.worker_settings.n_workers,
            gpu_enabled=cfg.worker_settings.gpu_enabled,
        )
        comp_slc_file = comp_slcs[-1]

    # ###################################################
    # Form interferograms from estimated wrapped phase
    # ###################################################

    ifg_dir = cfg.interferogram_network._directory
    existing_ifgs = list(ifg_dir.glob("*.int.*"))
    if len(existing_ifgs) > 0:
        logger.info(f"Skipping interferogram step, {len(existing_ifgs)} exists")
        return existing_ifgs, comp_slc_file, tcorr_file, ps_looked_file
    logger.info(f"Creating virtual interferograms from {len(phase_linked_slcs)} files")

    ifg_file_list = _create_ifgs(cfg, phase_linked_slcs, is_compressed, reference_date)
    return ifg_file_list, comp_slc_file, tcorr_file, ps_looked_file


def _create_ifgs(
    cfg: DisplacementWorkflow,
    phase_linked_slcs: Sequence[Path],
    is_compressed: Sequence[bool],
    reference_date: datetime.datetime,
) -> list[Path]:
    ifg_dir = cfg.interferogram_network._directory
    ifg_file_list: list[Path] = []
    if not any(is_compressed):
        # When no compressed SLCs were passed in to the config, we can direclty pass
        # options to `Network` and get the ifg list
        network = interferogram.Network(
            slc_list=phase_linked_slcs,
            reference_idx=cfg.interferogram_network.reference_idx,
            max_bandwidth=cfg.interferogram_network.max_bandwidth,
            max_temporal_baseline=cfg.interferogram_network.max_temporal_baseline,
            indexes=cfg.interferogram_network.indexes,
            outdir=ifg_dir,
        )
        if len(network.ifg_list) == 0:
            raise ValueError("No interferograms were created")
        ifg_file_list = [ifg.path for ifg in network.ifg_list]  # type: ignore
        assert all(p is not None for p in ifg_file_list)

        return ifg_file_list

    # When we started with compressed SLCs, we need to do some extra work to get the
    # interferograms we want.
    # The total SLC phases we have to work with are
    # 1. reference date (might be before any dates in the filenames)
    # 2. the secondary of all phase-linked SLCs (which are the names of the files)

    # To get the ifgs from the reference date to secondary(conj), this involves doing
    # a `.conj()` on the phase-linked SLCs (which are currently `day1.conj() * day2`)
    network_type = cfg.interferogram_network.network_type
    for f in phase_linked_slcs:
        p = interferogram.convert_pl_to_ifg(
            f, reference_date=reference_date, output_dir=ifg_dir
        )
        ifg_file_list.append(p)

    # If we're only wanting single-reference day-(reference) to day-k interferograms,
    # these are all we need
    if network_type == InterferogramNetworkType.SINGLE_REFERENCE:
        return ifg_file_list

    # For other networks, we have to combine other ones formed from the `Network`
    if network_type == InterferogramNetworkType.MAX_BANDWIDTH:
        max_b = cfg.interferogram_network.max_bandwidth
        # Max bandwidth is easier because we just take the first `max_b` from `phase_linked_slcs`
        # (which are the (ref_date, ...) interferograms),...
        ifgs_ref_date = ifg_file_list[:max_b]
        # ...then combine it with the results from the `Network`
        # Manually specify the dates, which come from the names of `phase_linked_slcs`
        secondary_dates = [get_dates(f)[0] for f in phase_linked_slcs]
        network_rest = interferogram.Network(
            slc_list=phase_linked_slcs,
            max_bandwidth=max_b,
            outdir=ifg_dir,
            dates=secondary_dates,
        )
        ifgs_others = cast(list[Path], [ifg.path for ifg in network_rest.ifg_list])

        return ifgs_ref_date + ifgs_others

    # Other types: TODO
    raise NotImplementedError(
        "Only single-reference interferograms are supported when"
        " starting with compressed SLCs"
    )
    # Say we had inputs like:
    #  compressed_2_3 , slc_4, slc_5, slc_6
    # but the compressed one was referenced to "1"
    # There will be 3 PL outputs for days 4, 5, 6, referenced to day "1":
    # (1, 4), (1, 5), (1, 6)
    # If we requested max-bw-2 interferograms, we want
    # (1, 4), (1, 5), (4, 5), (4, 6), (5, 6)
    # (the same as though we had normal SLCs (1, 4, 5, 6) )
    #
    # return ifg_file_list


def _get_reference_date_idx(
    input_file_list: Sequence[Path],
    is_compressed: Sequence[bool],
    input_dates: Sequence[Sequence[datetime.datetime]],
) -> tuple[datetime.datetime, int]:
    is_compressed = [f.name.startswith("compressed") for f in input_file_list]
    if not is_compressed[0]:
        return input_dates[0][0], 0

    # Otherwise use the last Compressed SLC as reference
    reference_idx = np.where(is_compressed)[0][-1]
    # read the Compressed SLC metadata to find it's reference date
    comp_slc = stack.CompressedSlcInfo.from_file_metadata(
        input_file_list[reference_idx]
    )
    return comp_slc.reference_date, reference_idx


def _get_input_dates(
    input_file_list: Sequence[Path], is_compressed: Sequence[bool], date_fmt: str
) -> list[list[datetime.datetime]]:
    input_dates = [get_dates(f, fmt=date_fmt) for f in input_file_list]
    # For any that aren't compressed, take the first date.
    # this is because the official product name of OPERA/Sentinel1 has both
    # "acquisition_date" ... "generation_date" in the filename
    # TODO: this is a bit hacky, perhaps we can make this some input option
    # so that the user can specify how to get dates from their files (or even
    # directly pass in dates?)
    input_dates = [
        dates[:1] if not is_comp else dates
        for dates, is_comp in zip(input_dates, is_compressed)
    ]
    return input_dates
