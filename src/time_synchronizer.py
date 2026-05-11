"""Time synchronizer for aligning dual-sensor (heart rate + movement) data streams."""
import logging
import numpy as np
from typing import Tuple

from src.data_structures import HeartRateData, MovementData

logger = logging.getLogger(__name__)

OFFSET_WARNING_THRESHOLD = 1.0  # seconds


class TimeSynchronizer:
    """Synchronizes heart rate and movement sensor timestamps via linear interpolation."""

    def calculate_time_offset(
        self,
        hr_timestamps: np.ndarray,
        mv_timestamps: np.ndarray,
    ) -> float:
        """Calculate time offset between two sensors.

        The offset is defined as the difference between the start times of the
        two timestamp arrays: hr_start - mv_start.

        Args:
            hr_timestamps: Unix timestamps (seconds) for heart-rate sensor.
            mv_timestamps: Unix timestamps (seconds) for movement sensor.

        Returns:
            Time offset in seconds (positive means HR sensor started later).
        """
        if len(hr_timestamps) == 0 or len(mv_timestamps) == 0:
            raise ValueError("Timestamp arrays must not be empty")

        offset = float(hr_timestamps[0] - mv_timestamps[0])

        if abs(offset) > OFFSET_WARNING_THRESHOLD:
            logger.warning(
                "Time offset between sensors is %.3f s (>1 s). "
                "Data may be unreliable.",
                offset,
            )

        return offset

    def align_data(
        self,
        hr_data: HeartRateData,
        mv_data: MovementData,
    ) -> Tuple[HeartRateData, MovementData]:
        """Align dual-channel data using linear interpolation.

        A common time grid is built from the *shorter* sensor's timestamps so
        that the returned arrays both have length == min(len(hr_data), len(mv_data)).
        Each sensor's values are then re-sampled onto that grid with np.interp.

        Args:
            hr_data: Heart-rate sensor data.
            mv_data: Movement sensor data.

        Returns:
            Tuple of (aligned_hr_data, aligned_mv_data) with identical timestamps
            and length == min(len(hr_data), len(mv_data)).
        """
        # Log (and optionally warn about) the offset before aligning
        self.calculate_time_offset(hr_data.timestamps, mv_data.timestamps)

        n_hr = len(hr_data.timestamps)
        n_mv = len(mv_data.timestamps)
        n_out = min(n_hr, n_mv)

        # Determine the overlapping time range
        t_start = max(hr_data.timestamps[0], mv_data.timestamps[0])
        t_end = min(hr_data.timestamps[-1], mv_data.timestamps[-1])

        if t_start >= t_end:
            # No overlap – fall back to the shorter array's timestamps
            if n_hr <= n_mv:
                common_timestamps = hr_data.timestamps[:n_out].copy()
            else:
                common_timestamps = mv_data.timestamps[:n_out].copy()
        else:
            # Build a uniform grid over the overlap with n_out points
            common_timestamps = np.linspace(t_start, t_end, n_out)

        # Interpolate both signals onto the common grid
        aligned_hr_values = np.interp(
            common_timestamps, hr_data.timestamps, hr_data.values
        )
        aligned_mv_values = np.interp(
            common_timestamps, mv_data.timestamps, mv_data.values
        )

        aligned_hr = HeartRateData(
            timestamps=common_timestamps,
            values=aligned_hr_values,
            sampling_rate=hr_data.sampling_rate,
        )
        aligned_mv = MovementData(
            timestamps=common_timestamps,
            values=aligned_mv_values,
            sampling_rate=mv_data.sampling_rate,
        )

        return aligned_hr, aligned_mv
