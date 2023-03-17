#!/usr/bin/env python
from pathlib import Path
from pprint import pformat
from typing import Dict, List, Optional

from dolphin._log import get_log, log_runtime
from dolphin.interferogram import VRTInterferogram

from . import OutputFormat, _product, stitch_and_unwrap, wrapped_phase
from ._utils import group_by_burst
from .config import Workflow


@log_runtime
def run(cfg: Workflow, debug: bool = False, log_file: Optional[str] = None):
    """Run the displacement workflow on a stack of SLCs.

    Parameters
    ----------
    cfg : Workflow
        [Workflow][dolphin.workflows.config.Workflow] object with workflow parameters
    debug : bool, optional
        Enable debug logging, by default False.
    log_file : str, optional
        If provided, will log to this file in addition to stderr.
    """
    logger = get_log(debug=debug, filename=log_file)
    logger.debug(pformat(cfg.dict()))

    try:
        grouped_slc_files = group_by_burst(cfg.inputs.cslc_file_list)
    except ValueError as e:
        # Make sure it's not some other ValueError
        if "Could not parse burst id" not in str(e):
            raise e
        # Otherwise, we have SLC files which are not OPERA burst files
        grouped_slc_files = {"": cfg.inputs.cslc_file_list}

    if len(grouped_slc_files) > 1:
        logger.info(f"Found SLC files from {len(grouped_slc_files)} bursts")
        wrapped_phase_cfgs = [
            # Include the burst for logging purposes
            (burst, _create_burst_cfg(cfg, burst, grouped_slc_files))
            for burst in grouped_slc_files
        ]
        for _, burst_cfg in wrapped_phase_cfgs:
            burst_cfg.create_dir_tree()
    else:
        wrapped_phase_cfgs = [("", cfg)]
    # ###########################
    # 1. Wrapped phase estimation
    # ###########################
    ifg_list: List[VRTInterferogram] = []
    tcorr_list: List[Path] = []
    # Now for each burst, run the wrapped phase estimation
    for burst, burst_cfg in wrapped_phase_cfgs:
        msg = "Running wrapped phase estimation"
        if burst:
            msg += f" for burst {burst}"
        logger.info(msg)
        logger.debug(pformat(burst_cfg.dict()))
        cur_ifg_list, comp_slc, tcorr = wrapped_phase.run(burst_cfg, debug=debug)
        ifg_list.extend(cur_ifg_list)
        tcorr_list.append(tcorr)

    # TODO: store the compressed SLCs somewhere
    # if cfg.outputs.store_compressed_slcs:
    #     pass

    # ###################################
    # 2. Stitch and unwrap interferograms
    # ###################################
    unwrapped_paths, conncomp_paths = stitch_and_unwrap.run(
        ifg_list=ifg_list, tcorr_file_list=tcorr_list, cfg=cfg, debug=debug
    )

    # ######################################
    # 3. Finalize the output as an HDF5 product
    # ######################################
    logger.info(
        f"Creating {len(unwrapped_paths), len(conncomp_paths)} outputs in"
        f" {cfg.outputs.output_directory}"
    )
    if cfg.outputs.output_format == OutputFormat.NETCDF:
        for unw_p, cc_p in zip(unwrapped_paths, conncomp_paths):
            output_name = cfg.outputs.output_directory / unw_p.with_suffix(".nc").name
            _product.create_output_product(
                unw_filename=unw_p,
                conncomp_filename=cc_p,
                # TODO: How am i going to create the output name?
                # output_name=cfg.outputs.output_name,
                output_name=output_name,
                corrections={},
            )
    else:
        _product._move_files_to_output_folder(
            unwrapped_paths,
            conncomp_paths,
            cfg.outputs.output_directory,
        )


def _create_burst_cfg(
    cfg: Workflow, burst_id: str, grouped_slc_files: Dict[str, List[Path]]
) -> Workflow:
    excludes = {
        "inputs": {"cslc_file_list"},
        "ps_options": {
            "directory",
            "output_file",
            "amp_dispersion_file",
            "amp_mean_file",
        },
        "phase_linking": {"directory"},
        "interferogram_network": {"directory"},
    }
    cfg_temp_dict = cfg.copy(deep=True, exclude=excludes).dict()

    top_level_scratch = cfg_temp_dict["outputs"]["scratch_directory"]
    new_input_dict = dict(
        inputs={"cslc_file_list": grouped_slc_files[burst_id]},
        outputs={"scratch_directory": top_level_scratch / burst_id},
    )
    # Just update the inputs and the scratch directory
    cfg_temp_dict["inputs"].update(new_input_dict["inputs"])
    cfg_temp_dict["outputs"].update(new_input_dict["outputs"])
    return Workflow(**cfg_temp_dict)
