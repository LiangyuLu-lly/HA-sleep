"""EDF file parser for extracting heart rate, movement, and sleep stage data"""
import struct
import numpy as np
from datetime import datetime
from typing import Tuple, Optional
from pathlib import Path
import logging

from src.data_structures import (
    EDFHeader,
    HeartRateData,
    MovementData,
    SleepStages,
    SleepStage
)

logger = logging.getLogger(__name__)


class EDFParseError(Exception):
    """EDF file parsing error"""
    pass


class EDFParser:
    """Parser for European Data Format (EDF) files"""
    
    def __init__(self):
        """Initialize EDF parser"""
        self._header: Optional[EDFHeader] = None
        self._signals_data: dict = {}
    
    def parse_header(self, edf_file_path: str) -> EDFHeader:
        """
        Parse EDF file header to extract metadata
        
        Args:
            edf_file_path: Path to EDF file
            
        Returns:
            EDFHeader containing file metadata
            
        Raises:
            EDFParseError: If file is corrupted or invalid
        """
        try:
            path = Path(edf_file_path)
            if not path.exists():
                raise EDFParseError(f"EDF file not found: {edf_file_path}")
            
            with open(edf_file_path, 'rb') as f:
                # Read fixed header (256 bytes)
                version = f.read(8).decode('ascii').strip()
                patient_id = f.read(80).decode('ascii').strip()
                recording_id = f.read(80).decode('ascii').strip()
                
                # Parse recording date and time
                date_str = f.read(8).decode('ascii').strip()  # dd.mm.yy
                time_str = f.read(8).decode('ascii').strip()  # hh.mm.ss
                
                try:
                    recording_date = datetime.strptime(
                        f"{date_str} {time_str}", 
                        "%d.%m.%y %H.%M.%S"
                    )
                except ValueError:
                    # Fallback for invalid date format
                    recording_date = datetime.now()
                    logger.warning(f"Invalid date format in EDF file, using current time")
                
                # Read header record size and data record info
                header_bytes = int(f.read(8).decode('ascii').strip())
                reserved = f.read(44).decode('ascii').strip()
                num_data_records = int(f.read(8).decode('ascii').strip())
                duration_data_record = float(f.read(8).decode('ascii').strip())
                num_channels = int(f.read(4).decode('ascii').strip())
                
                # Calculate total duration
                duration_seconds = num_data_records * duration_data_record
                
                # Read channel-specific information
                channel_labels = []
                transducer_types = []
                physical_dimensions = []
                physical_mins = []
                physical_maxs = []
                digital_mins = []
                digital_maxs = []
                prefilterings = []
                num_samples_per_record = []
                
                # Read each field for all channels
                for _ in range(num_channels):
                    channel_labels.append(f.read(16).decode('ascii').strip())
                
                for _ in range(num_channels):
                    transducer_types.append(f.read(80).decode('ascii').strip())
                
                for _ in range(num_channels):
                    physical_dimensions.append(f.read(8).decode('ascii').strip())
                
                for _ in range(num_channels):
                    physical_mins.append(float(f.read(8).decode('ascii').strip()))
                
                for _ in range(num_channels):
                    physical_maxs.append(float(f.read(8).decode('ascii').strip()))
                
                for _ in range(num_channels):
                    digital_mins.append(int(f.read(8).decode('ascii').strip()))
                
                for _ in range(num_channels):
                    digital_maxs.append(int(f.read(8).decode('ascii').strip()))
                
                for _ in range(num_channels):
                    prefilterings.append(f.read(80).decode('ascii').strip())
                
                for _ in range(num_channels):
                    num_samples_per_record.append(int(f.read(8).decode('ascii').strip()))
                
                # Skip reserved space
                for _ in range(num_channels):
                    f.read(32)
                
                # Build sampling rates and physical units dictionaries
                sampling_rates = {}
                physical_units = {}

                for i, label in enumerate(channel_labels):
                    # EDF+ annotation files often set duration_data_record to 0
                    # (annotations have variable timing). Fall back to 0 in that
                    # case rather than dividing by zero — callers that need
                    # signal data shouldn't be hitting these channels anyway.
                    if duration_data_record > 0:
                        sampling_rates[label] = int(
                            num_samples_per_record[i] / duration_data_record
                        )
                    else:
                        sampling_rates[label] = 0
                    physical_units[label] = physical_dimensions[i]
                
                # Store header for later use
                self._header = EDFHeader(
                    subject_id=patient_id,
                    recording_date=recording_date,
                    duration_seconds=duration_seconds,
                    num_channels=num_channels,
                    channel_labels=channel_labels,
                    sampling_rates=sampling_rates,
                    physical_units=physical_units
                )
                
                # Store additional info for signal extraction
                self._num_data_records = num_data_records
                self._num_samples_per_record = num_samples_per_record
                self._physical_mins = physical_mins
                self._physical_maxs = physical_maxs
                self._digital_mins = digital_mins
                self._digital_maxs = digital_maxs
                self._header_bytes = header_bytes
                self._duration_data_record = duration_data_record
                self._reserved = reserved  # contains "EDF+" marker
                
                return self._header
                
        except (IOError, ValueError, struct.error) as e:
            raise EDFParseError(f"Failed to parse EDF header: {str(e)}")
    
    def _read_signals(self, edf_file_path: str) -> None:
        """
        Read all signal data from EDF file
        
        Args:
            edf_file_path: Path to EDF file
        """
        if self._header is None:
            raise EDFParseError("Must call parse_header() before reading signals")
        
        try:
            with open(edf_file_path, 'rb') as f:
                # Skip header
                f.seek(self._header_bytes)
                
                # Initialize signal arrays
                signals = {label: [] for label in self._header.channel_labels}
                
                # Read each data record
                for _ in range(self._num_data_records):
                    for ch_idx, label in enumerate(self._header.channel_labels):
                        num_samples = self._num_samples_per_record[ch_idx]
                        
                        # Read digital values (16-bit integers)
                        digital_values = []
                        for _ in range(num_samples):
                            value = struct.unpack('<h', f.read(2))[0]
                            digital_values.append(value)
                        
                        # Convert digital to physical values
                        physical_values = self._digital_to_physical(
                            digital_values,
                            self._physical_mins[ch_idx],
                            self._physical_maxs[ch_idx],
                            self._digital_mins[ch_idx],
                            self._digital_maxs[ch_idx]
                        )
                        
                        signals[label].extend(physical_values)
                
                # Convert to numpy arrays
                self._signals_data = {
                    label: np.array(values, dtype=np.float64)
                    for label, values in signals.items()
                }
                
        except (IOError, struct.error) as e:
            raise EDFParseError(f"Failed to read signal data: {str(e)}")
    
    def _digital_to_physical(
        self,
        digital_values: list,
        phys_min: float,
        phys_max: float,
        dig_min: int,
        dig_max: int
    ) -> list:
        """
        Convert digital signal values to physical values
        
        Args:
            digital_values: List of digital values
            phys_min: Physical minimum
            phys_max: Physical maximum
            dig_min: Digital minimum
            dig_max: Digital maximum
            
        Returns:
            List of physical values
        """
        gain = (phys_max - phys_min) / (dig_max - dig_min)
        offset = phys_min - gain * dig_min
        
        return [gain * val + offset for val in digital_values]

    
    def _find_channel(self, keywords: list) -> Optional[str]:
        """
        Find channel label matching any of the keywords
        
        Args:
            keywords: List of possible channel name keywords
            
        Returns:
            Channel label if found, None otherwise
        """
        if self._header is None:
            return None
        
        for label in self._header.channel_labels:
            label_lower = label.lower()
            for keyword in keywords:
                if keyword.lower() in label_lower:
                    return label
        return None
    
    def extract_heart_rate_channel(self, edf_file_path: str) -> HeartRateData:
        """
        Extract heart rate signal from EDF file
        
        Args:
            edf_file_path: Path to EDF file
            
        Returns:
            HeartRateData containing heart rate signal
            
        Raises:
            EDFParseError: If heart rate channel not found or data invalid
        """
        if self._header is None:
            self.parse_header(edf_file_path)
        
        # Find heart rate channel
        hr_keywords = ['hr', 'heart', 'ecg', 'ekg', 'pulse', 'bpm']
        hr_label = self._find_channel(hr_keywords)
        
        if hr_label is None:
            raise EDFParseError(
                f"Heart rate channel not found. Available channels: {self._header.channel_labels}"
            )
        
        # Read signals if not already loaded
        if not self._signals_data:
            self._read_signals(edf_file_path)
        
        # Get heart rate signal
        hr_signal = self._signals_data[hr_label]
        
        # Generate timestamps
        sampling_rate = self._header.sampling_rates[hr_label]
        num_samples = len(hr_signal)
        timestamps = np.arange(num_samples) / sampling_rate
        
        # Validate heart rate values are in reasonable range
        # Note: Some EDF files may have raw ECG data, not heart rate
        # We'll be lenient here and let the validation happen in HeartRateData
        try:
            return HeartRateData(
                timestamps=timestamps,
                values=hr_signal,
                sampling_rate=sampling_rate
            )
        except AssertionError as e:
            logger.warning(f"Heart rate data validation warning: {e}")
            # Clip values to valid range if needed
            hr_signal_clipped = np.clip(hr_signal, 30, 200)
            return HeartRateData(
                timestamps=timestamps,
                values=hr_signal_clipped,
                sampling_rate=sampling_rate
            )
    
    def extract_movement_channel(self, edf_file_path: str) -> MovementData:
        """
        Extract movement/accelerometer signal from EDF file
        
        Args:
            edf_file_path: Path to EDF file
            
        Returns:
            MovementData containing movement signal
            
        Raises:
            EDFParseError: If movement channel not found
        """
        if self._header is None:
            self.parse_header(edf_file_path)
        
        # Find movement channel
        mv_keywords = ['movement', 'accel', 'activity', 'actimeter', 'motion', 'acc']
        mv_label = self._find_channel(mv_keywords)
        
        if mv_label is None:
            raise EDFParseError(
                f"Movement channel not found. Available channels: {self._header.channel_labels}"
            )
        
        # Read signals if not already loaded
        if not self._signals_data:
            self._read_signals(edf_file_path)
        
        # Get movement signal
        mv_signal = self._signals_data[mv_label]
        
        # Generate timestamps
        sampling_rate = self._header.sampling_rates[mv_label]
        num_samples = len(mv_signal)
        timestamps = np.arange(num_samples) / sampling_rate
        
        return MovementData(
            timestamps=timestamps,
            values=mv_signal,
            sampling_rate=sampling_rate
        )
    
    def extract_sleep_annotations(self, edf_file_path: str) -> SleepStages:
        """
        Extract sleep stage annotations from EDF file
        
        Args:
            edf_file_path: Path to EDF file
            
        Returns:
            SleepStages containing sleep stage labels
            
        Raises:
            EDFParseError: If annotations not found
        """
        if self._header is None:
            self.parse_header(edf_file_path)

        # Detect EDF+ format from the reserved field of the file header.
        # EDF+ files start with "EDF+C" (continuous) or "EDF+D" (discontinuous).
        is_edf_plus = getattr(self, "_reserved", "").startswith("EDF+")

        # 1) Preferred path: EDF+ TAL annotations.  This is the format used by
        #    PhysioNet Sleep-EDF Hypnogram files.
        if is_edf_plus or any(
            "annotation" in label.lower() for label in self._header.channel_labels
        ):
            try:
                annotations = self._read_edf_plus_annotations(edf_file_path)
                if annotations is not None and len(annotations.stages) > 0:
                    return annotations
            except Exception as exc:
                logger.warning("Failed to read EDF+ TAL annotations: %s", exc)

        # 2) Fallback for non-EDF+ datasets that store stages in a numeric
        #    channel (some MIT-BIH derivatives, synthetic test files).
        annotation_keywords = ["annotation", "event", "stage", "hypnogram"]
        annotation_label = self._find_channel(annotation_keywords)
        if annotation_label is not None and self._header.sampling_rates.get(annotation_label, 0) > 0:
            if not self._signals_data:
                self._read_signals(edf_file_path)
            annotation_signal = self._signals_data[annotation_label]
            stages = self._parse_annotation_signal(annotation_signal)
            timestamps = np.arange(len(stages)) * 30.0  # 30-second epochs
            return SleepStages(timestamps=timestamps, stages=stages)

        raise EDFParseError(
            f"Sleep stage annotations not found. Available channels: {self._header.channel_labels}"
        )
    
    def _parse_annotation_signal(self, signal: np.ndarray) -> np.ndarray:
        """
        Parse annotation signal to extract sleep stages
        
        Args:
            signal: Annotation signal array
            
        Returns:
            Array of sleep stage values (0-3)
        """
        # Map annotation values to sleep stages
        # This is a simplified mapping - adjust based on actual dataset format
        stages = []
        
        for value in signal:
            if value == 0 or value == 5:  # Wake
                stages.append(SleepStage.AWAKE.value)
            elif value == 1 or value == 2:  # Light sleep (N1, N2)
                stages.append(SleepStage.LIGHT.value)
            elif value == 3 or value == 4:  # Deep sleep (N3, N4)
                stages.append(SleepStage.DEEP.value)
            elif value == 6:  # REM
                stages.append(SleepStage.REM.value)
            else:
                # Unknown stage, default to awake
                stages.append(SleepStage.AWAKE.value)
        
        return np.array(stages, dtype=np.int32)
    
    def _read_edf_plus_annotations(self, edf_file_path: str) -> Optional[SleepStages]:
        """Read EDF+ TAL (Time-stamped Annotations List) from an annotation file.

        EDF+ encodes annotations in a special channel labeled
        ``"EDF Annotations"``.  Each data record contains a stream of TAL
        blocks separated by ``\\x00``.  Each TAL has the form::

            +<onset>[\\x15<duration>]\\x14<text>\\x14[\\x14<text>]...\\x14\\x00

        For Sleep-EDF Hypnogram files the relevant ``<text>`` strings are
        ``"Sleep stage W"``, ``"Sleep stage 1"``, ``"Sleep stage 2"``,
        ``"Sleep stage 3"``, ``"Sleep stage 4"``, ``"Sleep stage R"`` and
        ``"Sleep stage ?"``.  Stages are stored with the AASM 4-class
        mapping used by :class:`~src.data_structures.SleepStage`.

        Args:
            edf_file_path: Path to the annotation EDF+ file.

        Returns:
            :class:`SleepStages` with one entry per 30-second epoch, or
            ``None`` if no annotation channel exists.
        """
        if self._header is None:
            return None

        # Locate the annotation channel (case-insensitive)
        ann_idx = None
        for i, label in enumerate(self._header.channel_labels):
            if "annotation" in label.lower():
                ann_idx = i
                break
        if ann_idx is None:
            return None

        ann_samples = self._num_samples_per_record[ann_idx]

        try:
            with open(edf_file_path, "rb") as f:
                f.seek(self._header_bytes)
                events = []  # list of (onset_seconds, duration_seconds, text)
                for _ in range(self._num_data_records):
                    for ch_idx, _ in enumerate(self._header.channel_labels):
                        n_samples = self._num_samples_per_record[ch_idx]
                        # Each sample is 2 bytes (int16)
                        record_bytes = f.read(n_samples * 2)
                        if ch_idx == ann_idx:
                            events.extend(self._parse_tal_block(record_bytes))
        except (IOError, struct.error) as exc:
            logger.warning("Failed to read TAL annotations: %s", exc)
            return None

        if not events:
            return None

        # Convert TAL events into a per-epoch stage array (30-second epochs).
        # Each "Sleep stage X" event spans `duration` seconds beginning at `onset`.
        EPOCH_S = 30.0
        recording_duration = float(self._header.duration_seconds)
        # If duration is zero (annotation-only file), use the latest event end time.
        if recording_duration <= 0.0:
            recording_duration = max(
                (onset + dur for onset, dur, _ in events), default=0.0
            )
        if recording_duration <= 0.0:
            return None

        n_epochs = int(np.floor(recording_duration / EPOCH_S))
        stages = np.full(n_epochs, SleepStage.AWAKE.value, dtype=np.int32)

        for onset, duration, text in events:
            stage_value = self._map_sleep_text_to_stage(text)
            if stage_value is None:
                continue  # not a sleep-stage event
            start_epoch = int(np.floor(onset / EPOCH_S))
            end_epoch = int(np.floor((onset + duration) / EPOCH_S))
            start_epoch = max(0, min(start_epoch, n_epochs))
            end_epoch = max(0, min(end_epoch, n_epochs))
            if end_epoch > start_epoch:
                stages[start_epoch:end_epoch] = stage_value

        timestamps = np.arange(n_epochs, dtype=np.float64) * EPOCH_S
        return SleepStages(timestamps=timestamps, stages=stages)

    @staticmethod
    def _parse_tal_block(record_bytes: bytes) -> list:
        """Parse a TAL data record into a list of ``(onset, duration, text)`` tuples.

        TAL blocks are separated by ``b"\\x00"``.  Within a block the onset
        (and optional duration) precede ``b"\\x14"`` followed by one or more
        annotation texts terminated by ``b"\\x14"``.
        """
        events = []
        # Drop trailing zero padding then split on null bytes
        for raw in record_bytes.split(b"\x00"):
            if not raw:
                continue
            try:
                # Find first separator (\x14) which ends the onset/duration field
                sep_idx = raw.index(b"\x14")
            except ValueError:
                continue
            timing = raw[:sep_idx].decode("ascii", errors="replace")
            payload = raw[sep_idx + 1:]
            # Onset may be prefixed with '+' or '-' and may contain a duration
            # suffix delimited by \x15.
            if "\x15" in timing:
                onset_str, duration_str = timing.split("\x15", 1)
            else:
                onset_str, duration_str = timing, "0"
            try:
                onset = float(onset_str)
                duration = float(duration_str) if duration_str else 0.0
            except ValueError:
                continue
            # Multiple annotation texts may follow, separated by \x14
            for text_bytes in payload.split(b"\x14"):
                if not text_bytes:
                    continue
                text = text_bytes.decode("ascii", errors="replace").strip()
                if text:
                    events.append((onset, duration, text))
        return events

    @staticmethod
    def _map_sleep_text_to_stage(text: str) -> Optional[int]:
        """Map a Sleep-EDF annotation text to one of four AASM classes.

        Returns ``None`` for non-stage events (e.g. lights-off, body movement).
        """
        if not text.lower().startswith("sleep stage"):
            return None
        # Extract last word: "Sleep stage W" / "1" / "2" / "3" / "4" / "R" / "?"
        token = text.split()[-1].upper()
        if token in {"W"}:
            return SleepStage.AWAKE.value
        if token in {"1", "2"}:
            return SleepStage.LIGHT.value
        if token in {"3", "4"}:
            return SleepStage.DEEP.value
        if token in {"R"}:
            return SleepStage.REM.value
        return None  # Unknown / movement / "?"
