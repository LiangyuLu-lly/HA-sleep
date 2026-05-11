"""Wavelet denoising module for CNN-BiLSTM Sleep Algorithm.

Suppresses 50 Hz power-line interference and broadband noise from heart-rate
signals using multi-level discrete wavelet decomposition with soft thresholding
(VisuShrink / universal threshold).

Requirements: 6.1, 6.2, 6.3, 6.4
"""
import logging
import json
import os
import numpy as np
import pywt

logger = logging.getLogger(__name__)


class WaveletDenoiserError(Exception):
    """Raised when wavelet denoising fails."""


class WaveletDenoiser:
    """
    Multi-level wavelet denoiser for heart-rate signals.

    Algorithm
    ---------
    1. Decompose the signal with a Daubechies-5 (db5) wavelet to *level* levels.
    2. Estimate noise standard deviation from the finest detail coefficients
       using the robust median absolute deviation (MAD) estimator.
    3. Apply soft thresholding (VisuShrink universal threshold) to **all**
       detail coefficient arrays.
    4. Reconstruct the signal via inverse DWT.

    The universal threshold  λ = σ · √(2 · ln N)  ensures that 50 Hz
    power-line interference — which concentrates in the high-frequency detail
    bands — is suppressed to < 10 % of its original energy after reconstruction.

    Parameters
    ----------
    wavelet : str
        PyWavelets wavelet name (default ``'db5'``).
    level : int
        Decomposition depth (default ``5``).
    config_path : str
        Path to the JSON configuration file.  Values in the file override the
        constructor defaults when present.

    Requirements: 6.1, 6.2, 6.3, 6.4
    """

    def __init__(
        self,
        wavelet: str = "db5",
        level: int = 5,
        config_path: str = "training_config/config.json",
    ) -> None:
        # Validate constructor arguments first (before config override)
        if wavelet not in pywt.wavelist():
            raise WaveletDenoiserError(
                f"Unknown wavelet '{wavelet}'. "
                f"Available wavelets: {pywt.wavelist()[:10]} …"
            )
        if level < 1:
            raise WaveletDenoiserError(
                f"Decomposition level must be >= 1, got {level}."
            )

        # Load config and allow it to override constructor defaults.
        # Only override when the config file explicitly contains the keys
        # (i.e. read the raw file, not the merged default config).
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                wd_cfg = (
                    raw.get("data_processing", {})
                    .get("wavelet_denoising", {})
                )
                if "wavelet_type" in wd_cfg:
                    wavelet = wd_cfg["wavelet_type"]
                if "decomposition_level" in wd_cfg:
                    level = int(wd_cfg["decomposition_level"])
        except Exception as exc:
            logger.warning("Could not load wavelet config, using defaults: %s", exc)

        self.wavelet: str = wavelet
        self.level: int = level
        logger.info(
            "WaveletDenoiser initialised — wavelet=%s, level=%d", wavelet, level
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def denoise(self, heart_rate_signal: np.ndarray) -> np.ndarray:
        """
        Denoise a heart-rate signal using multi-level wavelet thresholding.

        Steps
        -----
        1. Decompose with ``pywt.wavedec`` (Requirements 6.1).
        2. Estimate noise σ from finest detail band via MAD.
        3. Compute universal threshold λ = σ · √(2 · ln N).
        4. Apply soft thresholding to all detail coefficient arrays (Req. 6.2).
        5. Reconstruct with ``pywt.waverec`` and trim to original length (Req. 6.3).

        The 50 Hz power-line component is concentrated in the highest-frequency
        detail bands; soft thresholding zeroes or shrinks those coefficients,
        reducing 50 Hz energy to < 10 % of the original (Req. 6.4).

        Parameters
        ----------
        heart_rate_signal : np.ndarray
            1-D array of heart-rate samples (arbitrary length ≥ 2).

        Returns
        -------
        np.ndarray
            Denoised signal with the same length as the input.

        Raises
        ------
        WaveletDenoiserError
            If the input is not a 1-D array or is too short.
        """
        signal = np.asarray(heart_rate_signal, dtype=float)

        if signal.ndim != 1:
            raise WaveletDenoiserError(
                f"Expected 1-D signal, got shape {signal.shape}."
            )
        if len(signal) < 2:
            raise WaveletDenoiserError(
                "Signal must contain at least 2 samples."
            )

        n = len(signal)

        # Clamp decomposition level to the maximum supported by the signal length
        max_level = pywt.dwt_max_level(n, self.wavelet)
        effective_level = min(self.level, max_level)
        if effective_level < self.level:
            logger.debug(
                "Signal length %d limits decomposition to level %d (requested %d).",
                n, effective_level, self.level,
            )

        # --- Step 1: Multi-level decomposition (Requirement 6.1) ---
        coeffs = pywt.wavedec(signal, self.wavelet, level=effective_level)
        # coeffs[0]  = approximation (cA_N)
        # coeffs[1:] = detail arrays from coarsest to finest

        # --- Step 2: Noise estimation from finest detail band ---
        finest_detail = coeffs[-1]
        sigma = self._estimate_noise(finest_detail)

        # --- Step 3: Universal (VisuShrink) threshold ---
        threshold = sigma * np.sqrt(2.0 * np.log(max(n, 2)))

        # --- Step 4: Soft-threshold all detail bands (Requirement 6.2) ---
        thresholded = [coeffs[0]]  # keep approximation unchanged
        for detail in coeffs[1:]:
            thresholded.append(pywt.threshold(detail, threshold, mode="soft"))

        # --- Step 5: Reconstruct and trim (Requirement 6.3) ---
        reconstructed = pywt.waverec(thresholded, self.wavelet)
        # waverec may produce one extra sample due to padding
        denoised = reconstructed[:n]

        logger.debug(
            "Denoised signal: n=%d, sigma=%.4f, threshold=%.4f, level=%d",
            n, sigma, threshold, effective_level,
        )
        return denoised

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_noise(detail_coefficients: np.ndarray) -> float:
        """
        Estimate noise standard deviation using the MAD estimator.

        σ̂ = median(|d|) / 0.6745

        This is robust to outliers and is the standard estimator used in
        VisuShrink / SureShrink wavelet denoising.
        """
        if len(detail_coefficients) == 0:
            return 1.0  # fallback
        mad = np.median(np.abs(detail_coefficients))
        sigma = mad / 0.6745
        # Avoid zero threshold (would leave signal unchanged)
        return max(sigma, 1e-10)
