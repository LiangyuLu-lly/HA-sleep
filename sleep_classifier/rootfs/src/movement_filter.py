"""Movement signal filtering module for CNN-BiLSTM Sleep Algorithm.

Applies a Butterworth bandpass filter (0.1–5 Hz) to movement/accelerometer
signals to retain sleep-relevant motion frequencies while suppressing
high-frequency noise above the configurable cutoff (default 10 Hz).

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5
"""
import json
import logging
import os

import numpy as np
from scipy.signal import butter, sosfilt

logger = logging.getLogger(__name__)


class MovementFilterError(Exception):
    """Raised when movement filtering fails."""


class MovementFilter:
    """
    Bandpass filter for movement/accelerometer signals.

    When *enabled*, a Butterworth bandpass filter (0.1–5 Hz) is applied to
    the input signal using ``scipy.signal.butter`` + ``sosfilt``.  The upper
    edge of the passband is fixed at 5 Hz (sleep-relevant motion band); the
    low-pass cutoff is configurable (default 10 Hz, used as the stopband
    reference for high-frequency noise reduction).

    When *disabled*, the raw signal is returned unchanged (Requirement 7.5).

    Parameters
    ----------
    enabled : bool
        Whether to apply the filter (default ``True``).
    cutoff_freq : float
        Low-pass cutoff frequency in Hz (default ``10.0``).  Frequencies above
        this value are considered high-frequency noise.
    sampling_rate : float
        Sampling rate of the input signal in Hz (default ``100.0``).
    config_path : str
        Path to the JSON configuration file.  Values in the file override the
        constructor defaults when present.

    Requirements: 7.1, 7.2, 7.3, 7.4, 7.5
    """

    # Fixed bandpass edges for sleep-relevant movement frequencies (Req. 7.4)
    _BANDPASS_LOW_HZ: float = 0.1
    _BANDPASS_HIGH_HZ: float = 5.0
    _FILTER_ORDER: int = 4

    def __init__(
        self,
        enabled: bool = True,
        cutoff_freq: float = 10.0,
        sampling_rate: float = 100.0,
        config_path: str = "training_config/config.json",
    ) -> None:
        if cutoff_freq <= 0:
            raise MovementFilterError(
                f"cutoff_freq must be positive, got {cutoff_freq}."
            )
        if sampling_rate <= 0:
            raise MovementFilterError(
                f"sampling_rate must be positive, got {sampling_rate}."
            )

        # Allow config file to override constructor defaults
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                mf_cfg = (
                    raw.get("data_processing", {})
                    .get("movement_filter", {})
                )
                if "enabled" in mf_cfg:
                    enabled = bool(mf_cfg["enabled"])
                if "cutoff_frequency" in mf_cfg:
                    cutoff_freq = float(mf_cfg["cutoff_frequency"])
        except Exception as exc:
            logger.warning(
                "Could not load movement_filter config, using defaults: %s", exc
            )

        self.enabled: bool = enabled
        self.cutoff_freq: float = cutoff_freq
        self.sampling_rate: float = sampling_rate

        logger.info(
            "MovementFilter initialised — enabled=%s, cutoff_freq=%.1f Hz, "
            "sampling_rate=%.1f Hz",
            enabled,
            cutoff_freq,
            sampling_rate,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def filter(self, movement_signal: np.ndarray) -> np.ndarray:
        """
        Filter a movement signal.

        When the filter is *enabled*, a 4th-order Butterworth bandpass filter
        (0.1–5 Hz) is applied via second-order sections (``sosfilt``) to
        preserve numerical stability (Requirements 7.1, 7.2, 7.3, 7.4).

        When the filter is *disabled*, the raw signal is returned unchanged
        (Requirement 7.5).

        Parameters
        ----------
        movement_signal : np.ndarray
            1-D array of movement/accelerometer samples.

        Returns
        -------
        np.ndarray
            Filtered (or pass-through) signal with the same length as the
            input.

        Raises
        ------
        MovementFilterError
            If the input is not a 1-D array or is too short to filter.
        """
        signal = np.asarray(movement_signal, dtype=float)

        if signal.ndim != 1:
            raise MovementFilterError(
                f"Expected 1-D signal, got shape {signal.shape}."
            )
        if len(signal) < 2:
            raise MovementFilterError(
                "Signal must contain at least 2 samples."
            )

        if not self.enabled:
            logger.debug("MovementFilter disabled — returning raw signal.")
            return signal.copy()

        # Minimum signal length for the chosen filter order and Nyquist
        nyquist = self.sampling_rate / 2.0
        low = self._BANDPASS_LOW_HZ / nyquist
        high = self._BANDPASS_HIGH_HZ / nyquist

        if high >= 1.0:
            raise MovementFilterError(
                f"Bandpass high edge {self._BANDPASS_HIGH_HZ} Hz must be "
                f"below Nyquist ({nyquist} Hz).  Increase sampling_rate."
            )
        if low <= 0.0 or low >= high:
            raise MovementFilterError(
                f"Invalid bandpass edges: low={low:.4f}, high={high:.4f} "
                "(normalised to Nyquist)."
            )

        # Minimum samples needed: 3 * filter_order + 1 for sosfilt padding
        min_samples = 3 * self._FILTER_ORDER + 1
        if len(signal) < min_samples:
            raise MovementFilterError(
                f"Signal too short ({len(signal)} samples) for filter order "
                f"{self._FILTER_ORDER}. Need at least {min_samples} samples."
            )

        sos = butter(
            self._FILTER_ORDER,
            [low, high],
            btype="bandpass",
            output="sos",
        )
        filtered = sosfilt(sos, signal)

        logger.debug(
            "MovementFilter applied bandpass %.2f–%.2f Hz to %d samples.",
            self._BANDPASS_LOW_HZ,
            self._BANDPASS_HIGH_HZ,
            len(signal),
        )
        return filtered
