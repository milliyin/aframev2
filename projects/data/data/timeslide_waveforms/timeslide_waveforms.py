import logging
from collections import defaultdict
from pathlib import Path
from typing import Callable, List, Optional

import data.timeslide_waveforms.utils as utils
import numpy as np
import torch
from data.waveforms.injection import (
    WaveformGenerator,
    convert_to_detector_frame,
)
from ledger.injections import InjectionParameterSet, LigoResponseSet

from ml4gw.gw import (
    compute_network_snr,
    compute_observed_strain,
    get_ifo_geometry,
)


def main(
    start: float,
    stop: float,
    ifos: List[str],
    shifts: List[float],
    background_dir: Path,
    spacing: float,
    buffer: float,
    prior: Callable,
    minimum_frequency: float,
    reference_frequency: float,
    sample_rate: float,
    waveform_duration: float,
    waveform_approximant: str,
    highpass: float,
    snr_threshold: float,
    output_dir: Path,
    log_file: Optional[Path] = None,
    verbose: bool = False,
    seed: Optional[int] = None,
):
    """
    Generates the waveforms for a single segment.

    Args:
        start:
            GPS time of the beginning of the testing segment
        stop:
            GPS time of the end of the testing segment
        ifos:
            List of interferometers to query data from. Expected to be given
            by prefix; e.g. "H1" for Hanford. Should be the same length as
            `shifts`
        shifts:
            The length of time in seconds by which each interferometer's
            timeseries will be shifted
        background_dir:
            Directory containing background data to use for PSD
            calculation. Should have data for each interferometer in
            the format generated by `background.py`
        spacing:
            The amount of time, in seconds, to leave between the end
            of one signal and the start of the next
        buffer:
            The amount of time, in seconds, on either side of the
            segment within which injection times will not be
            generated
        prior:
            A function that returns a Bilby PriorDict when called
        minimum_frequency:
            Minimum frequency of the gravitational wave. The part
            of the gravitational wave at lower frequencies will
            not be generated. Specified in Hz.
        reference_frequency:
            Frequency of the gravitational wave at the state of
            the merger that other quantities are defined with
            reference to
        sample_rate:
            Sample rate of timeseries data, specified in Hz
        waveform_duration:
            Duration of waveform in seconds
        waveform_approximant:
            Name of the waveform approximant to use.
        highpass:
            The frequency to use for a highpass filter, specified
            in Hz
        snr_threshold:
            Minimum SNR of generated waveforms. Sampled parameters
            that result in an SNR below this threshold will be rejected,
            but saved for later use
        output_dir:
            Directory to which the waveform file and rejected parameter
            file will be written
        log_file:
            File containing the logged information
        verbose:
            If True, log at `DEBUG` verbosity, otherwise log at
            `INFO` verbosity.

    Returns:
        The name of the waveform file and the name of the file containing the
        rejected parameters
    """

    if seed is not None:
        utils.seed_worker(start, stop, shifts, seed)

    prior, detector_frame_prior = prior()

    injection_times = utils.calc_segment_injection_times(
        start,
        stop - max(shifts),  # TODO: should account for uneven last batch too
        spacing,
        buffer,
        waveform_duration,
    )
    n_samples = len(injection_times)
    waveform_size = int(sample_rate * waveform_duration)

    zeros = np.zeros((n_samples,))
    parameters = defaultdict(lambda: zeros.copy())
    parameters["gps_time"] = injection_times
    parameters["shift"] = np.array([shifts for _ in range(n_samples)])

    for ifo in ifos:
        empty = np.zeros((n_samples, waveform_size))
        parameters[ifo.lower()] = empty

    tensors, vertices = get_ifo_geometry(*ifos)
    df = 1 / waveform_duration
    try:
        background_path = sorted(background_dir.iterdir())[0]
    except StopIteration:
        raise ValueError(
            f"No files in background data directory {background_dir}"
        )
    logging.info(
        f"Using background file {background_path} for psd calculation"
    )
    psds = utils.load_psds(background_path, ifos, df=df)

    generator = WaveformGenerator(
        waveform_duration,
        sample_rate,
        minimum_frequency,
        reference_frequency,
        waveform_approximant,
    )

    # loop until we've generated enough signals
    # with large enough snr to fill the segment,
    # keeping track of the number of signals rejected
    num_injections, idx = 0, 0
    rejected_params = InjectionParameterSet()
    while n_samples > 0:
        params = prior.sample(n_samples)
        if not detector_frame_prior:
            params = convert_to_detector_frame(params)
        # If a Bilby PriorDict has a conversion function, any
        # extra keys generated by the conversion function will
        # be added into the sampled parameters, but only if
        # we're sampling exactly 1 time. This removes those
        # extra keys
        # TODO: If https://git.ligo.org/lscsoft/bilby/-/merge_requests/1286
        # is merged, remove this
        if n_samples == 1:
            params = {k: params[k] for k in parameters if k in params}

        waveforms = generator(params)
        polarizations = {
            "cross": torch.Tensor(waveforms[:, 0, :]),
            "plus": torch.Tensor(waveforms[:, 1, :]),
        }

        projected = compute_observed_strain(
            torch.Tensor(params["dec"]),
            torch.Tensor(params["psi"]),
            torch.Tensor(params["ra"]),
            tensors,
            vertices,
            sample_rate,
            **polarizations,
        )
        # TODO: compute individual ifo snr so we can store that data
        snrs = compute_network_snr(projected, psds, sample_rate, highpass)
        snrs = snrs.numpy()

        # add all snrs: masking will take place in for loop below
        params["snr"] = snrs
        num_injections += len(snrs)
        mask = snrs >= snr_threshold

        # first record any parameters that were
        # rejected during sampling to a separate object
        rejected = {}
        for key in InjectionParameterSet.__dataclass_fields__:
            rejected[key] = params[key][~mask]
        rejected = InjectionParameterSet(**rejected)
        rejected_params.append(rejected)

        # if nothing got accepted, try again
        num_accepted = mask.sum()
        if num_accepted == 0:
            continue

        # insert our accepted parameters into the output array
        start, stop = idx, idx + num_accepted
        for key, value in params.items():
            parameters[key][start:stop] = value[mask]

        # do the same for our accepted projected waveforms
        projected = projected[mask].numpy()
        for i, ifo in enumerate(ifos):
            key = ifo.lower()
            parameters[key][start:stop] = projected[:, i]

        # subtract off the number of samples we accepted
        # from the number we'll need to sample next time,
        # that way we never overshoot our number of desired
        # accepted samples and therefore risk overestimating
        # our total number of injections
        idx += num_accepted
        n_samples -= num_accepted

    parameters["sample_rate"] = sample_rate
    parameters["duration"] = waveform_duration
    parameters["num_injections"] = num_injections

    response_set = LigoResponseSet(**parameters)
    waveform_fname = output_dir / "waveforms.h5"
    utils.io_with_blocking(response_set.write, waveform_fname)

    rejected_fname = output_dir / "rejected-parameters.h5"
    utils.io_with_blocking(rejected_params.write, rejected_fname)

    # TODO: compute probability of all parameters against
    # source and all target priors here then save them somehow
    return waveform_fname, rejected_fname
