"""Dataset loader for Sleep-EDF and MIT-BIH Polysomnographic datasets"""
import logging
from pathlib import Path
from typing import Tuple, List, Optional
import numpy as np

from src.data_structures import (
    Dataset,
    HeartRateData,
    MovementData,
    SleepStages,
    TrainingSet,
    TestSet
)
from src.edf_parser import EDFParser, EDFParseError

logger = logging.getLogger(__name__)


class DatasetLoadError(Exception):
    """Dataset loading error"""
    pass


class DatasetLoader:
    """Loader for public sleep datasets (Sleep-EDF, MIT-BIH)"""
    
    def __init__(self):
        """Initialize dataset loader"""
        self.parser = EDFParser()
    
    def load_sleep_edf(self, dataset_path: str) -> Dataset:
        """
        Load Sleep-EDF dataset
        
        Args:
            dataset_path: Path to Sleep-EDF dataset directory or EDF file
            
        Returns:
            Dataset containing heart rate, movement, and sleep stage data
            
        Raises:
            DatasetLoadError: If dataset path is invalid or data is missing
        """
        # Validate path
        path = Path(dataset_path)
        if not path.exists():
            error_msg = f"Dataset path does not exist: {dataset_path}"
            logger.error(error_msg)
            raise DatasetLoadError(error_msg)
        
        try:
            # If path is a directory, find EDF files
            if path.is_dir():
                edf_files = list(path.glob("*.edf")) + list(path.glob("*.EDF"))
                if not edf_files:
                    error_msg = f"No EDF files found in directory: {dataset_path}"
                    logger.error(error_msg)
                    raise DatasetLoadError(error_msg)
                
                # Load first EDF file (can be extended to load multiple files)
                edf_file_path = str(edf_files[0])
                logger.info(f"Loading Sleep-EDF file: {edf_file_path}")
            else:
                edf_file_path = str(path)
            
            # Parse metadata
            header = self.parser.parse_header(edf_file_path)
            logger.info(f"Loaded metadata - Subject: {header.subject_id}, "
                       f"Duration: {header.duration_seconds}s, "
                       f"Channels: {header.num_channels}")
            
            # Extract heart rate channel
            try:
                heart_rate = self.parser.extract_heart_rate_channel(edf_file_path)
                logger.info(f"Extracted heart rate channel: {len(heart_rate.values)} samples "
                           f"at {heart_rate.sampling_rate}Hz")
            except EDFParseError as e:
                error_msg = f"Failed to extract heart rate channel: {str(e)}"
                logger.error(error_msg)
                raise DatasetLoadError(error_msg)
            
            # Extract movement channel
            try:
                movement = self.parser.extract_movement_channel(edf_file_path)
                logger.info(f"Extracted movement channel: {len(movement.values)} samples "
                           f"at {movement.sampling_rate}Hz")
            except EDFParseError as e:
                error_msg = f"Failed to extract movement channel: {str(e)}"
                logger.error(error_msg)
                raise DatasetLoadError(error_msg)
            
            # Extract sleep stage annotations
            try:
                sleep_stages = self.parser.extract_sleep_annotations(edf_file_path)
                logger.info(f"Extracted sleep stages: {len(sleep_stages.stages)} annotations")
            except EDFParseError as e:
                error_msg = f"Failed to extract sleep stage annotations: {str(e)}"
                logger.error(error_msg)
                raise DatasetLoadError(error_msg)
            
            # Align sleep stages to match HR/movement sample count
            sleep_stages = self._align_sleep_stages(sleep_stages, len(heart_rate.timestamps))

            # Create dataset
            dataset = Dataset(
                heart_rate=heart_rate,
                movement=movement,
                sleep_stages=sleep_stages,
                subject_ids=[header.subject_id]
            )
            
            logger.info(f"Successfully loaded Sleep-EDF dataset for subject {header.subject_id}")
            return dataset
            
        except EDFParseError as e:
            error_msg = f"EDF parsing error: {str(e)}"
            logger.error(error_msg)
            raise DatasetLoadError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error loading Sleep-EDF dataset: {str(e)}"
            logger.error(error_msg)
            raise DatasetLoadError(error_msg)
    
    def load_mit_bih(self, dataset_path: str) -> Dataset:
        """
        Load MIT-BIH Polysomnographic dataset
        
        Args:
            dataset_path: Path to MIT-BIH dataset directory or EDF file
            
        Returns:
            Dataset containing heart rate, movement, and sleep stage data
            
        Raises:
            DatasetLoadError: If dataset path is invalid or data is missing
        """
        # Validate path
        path = Path(dataset_path)
        if not path.exists():
            error_msg = f"Dataset path does not exist: {dataset_path}"
            logger.error(error_msg)
            raise DatasetLoadError(error_msg)
        
        try:
            # If path is a directory, find EDF files
            if path.is_dir():
                edf_files = list(path.glob("*.edf")) + list(path.glob("*.EDF"))
                if not edf_files:
                    error_msg = f"No EDF files found in directory: {dataset_path}"
                    logger.error(error_msg)
                    raise DatasetLoadError(error_msg)
                
                # Load first EDF file (can be extended to load multiple files)
                edf_file_path = str(edf_files[0])
                logger.info(f"Loading MIT-BIH file: {edf_file_path}")
            else:
                edf_file_path = str(path)
            
            # Parse metadata
            header = self.parser.parse_header(edf_file_path)
            logger.info(f"Loaded metadata - Subject: {header.subject_id}, "
                       f"Duration: {header.duration_seconds}s, "
                       f"Channels: {header.num_channels}")
            
            # Extract heart rate channel
            try:
                heart_rate = self.parser.extract_heart_rate_channel(edf_file_path)
                logger.info(f"Extracted heart rate channel: {len(heart_rate.values)} samples "
                           f"at {heart_rate.sampling_rate}Hz")
            except EDFParseError as e:
                error_msg = f"Failed to extract heart rate channel: {str(e)}"
                logger.error(error_msg)
                raise DatasetLoadError(error_msg)
            
            # Extract movement channel
            try:
                movement = self.parser.extract_movement_channel(edf_file_path)
                logger.info(f"Extracted movement channel: {len(movement.values)} samples "
                           f"at {movement.sampling_rate}Hz")
            except EDFParseError as e:
                error_msg = f"Failed to extract movement channel: {str(e)}"
                logger.error(error_msg)
                raise DatasetLoadError(error_msg)
            
            # Extract sleep stage annotations
            try:
                sleep_stages = self.parser.extract_sleep_annotations(edf_file_path)
                logger.info(f"Extracted sleep stages: {len(sleep_stages.stages)} annotations")
            except EDFParseError as e:
                error_msg = f"Failed to extract sleep stage annotations: {str(e)}"
                logger.error(error_msg)
                raise DatasetLoadError(error_msg)
            
            # Align sleep stages to match HR/movement sample count
            sleep_stages = self._align_sleep_stages(sleep_stages, len(heart_rate.timestamps))

            # Create dataset
            dataset = Dataset(
                heart_rate=heart_rate,
                movement=movement,
                sleep_stages=sleep_stages,
                subject_ids=[header.subject_id]
            )
            
            logger.info(f"Successfully loaded MIT-BIH dataset for subject {header.subject_id}")
            return dataset
            
        except EDFParseError as e:
            error_msg = f"EDF parsing error: {str(e)}"
            logger.error(error_msg)
            raise DatasetLoadError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error loading MIT-BIH dataset: {str(e)}"
            logger.error(error_msg)
            raise DatasetLoadError(error_msg)
    
    def _align_sleep_stages(self, sleep_stages: SleepStages, target_length: int) -> SleepStages:
        """
        Upsample or downsample sleep stage annotations to match target_length.

        Each annotation epoch is repeated (or the array is truncated) so that
        the returned SleepStages has exactly *target_length* entries, matching
        the HR/movement sample count.

        Args:
            sleep_stages: Original sleep stage annotations.
            target_length: Desired number of samples.

        Returns:
            SleepStages with len == target_length.
        """
        n_stages = len(sleep_stages.stages)
        if n_stages == target_length:
            return sleep_stages

        # Use nearest-neighbour resampling (repeat each epoch proportionally)
        indices = np.round(np.linspace(0, n_stages - 1, target_length)).astype(int)
        new_stages = sleep_stages.stages[indices]
        new_timestamps = np.linspace(
            sleep_stages.timestamps[0] if n_stages > 0 else 0.0,
            sleep_stages.timestamps[-1] if n_stages > 0 else float(target_length - 1),
            target_length,
        )
        return SleepStages(timestamps=new_timestamps, stages=new_stages)

    def split_train_test(
        self,
        data: Dataset,
        test_ratio: float = 0.2
    ) -> Tuple[TrainingSet, TestSet]:
        """
        Split dataset into training and test sets by subject ID
        
        Args:
            data: Dataset to split
            test_ratio: Ratio of test set (default 0.2 for 80:20 split)
            
        Returns:
            Tuple of (TrainingSet, TestSet)
            
        Raises:
            ValueError: If test_ratio is not in (0, 1) range
        """
        if not 0 < test_ratio < 1:
            raise ValueError(f"test_ratio must be in (0, 1) range, got {test_ratio}")
        
        # Get unique subject IDs
        unique_subjects = list(set(data.subject_ids))
        num_subjects = len(unique_subjects)
        
        # Calculate split point
        num_test_subjects = max(1, int(num_subjects * test_ratio))
        
        # Randomly shuffle subjects
        np.random.shuffle(unique_subjects)
        
        # Split subjects
        test_subjects = set(unique_subjects[:num_test_subjects])
        train_subjects = set(unique_subjects[num_test_subjects:])
        
        logger.info(f"Split {num_subjects} subjects: {len(train_subjects)} train, "
                   f"{len(test_subjects)} test")
        
        # For single-subject datasets, split by time
        if num_subjects == 1:
            return self._split_by_time(data, test_ratio)
        
        # Split data by subject
        train_indices = [i for i, subj in enumerate(data.subject_ids) if subj in train_subjects]
        test_indices = [i for i, subj in enumerate(data.subject_ids) if subj in test_subjects]
        
        # Create training set
        train_dataset = Dataset(
            heart_rate=HeartRateData(
                timestamps=data.heart_rate.timestamps[train_indices],
                values=data.heart_rate.values[train_indices],
                sampling_rate=data.heart_rate.sampling_rate
            ),
            movement=MovementData(
                timestamps=data.movement.timestamps[train_indices],
                values=data.movement.values[train_indices],
                sampling_rate=data.movement.sampling_rate
            ),
            sleep_stages=SleepStages(
                timestamps=data.sleep_stages.timestamps[train_indices],
                stages=data.sleep_stages.stages[train_indices]
            ),
            subject_ids=[data.subject_ids[i] for i in train_indices]
        )
        
        # Create test set
        test_dataset = Dataset(
            heart_rate=HeartRateData(
                timestamps=data.heart_rate.timestamps[test_indices],
                values=data.heart_rate.values[test_indices],
                sampling_rate=data.heart_rate.sampling_rate
            ),
            movement=MovementData(
                timestamps=data.movement.timestamps[test_indices],
                values=data.movement.values[test_indices],
                sampling_rate=data.movement.sampling_rate
            ),
            sleep_stages=SleepStages(
                timestamps=data.sleep_stages.timestamps[test_indices],
                stages=data.sleep_stages.stages[test_indices]
            ),
            subject_ids=[data.subject_ids[i] for i in test_indices]
        )
        
        # Calculate normalization parameters from training set
        normalization_params = {
            'heart_rate': (
                float(np.mean(train_dataset.heart_rate.values)),
                float(np.std(train_dataset.heart_rate.values))
            ),
            'movement': (
                float(np.mean(train_dataset.movement.values)),
                float(np.std(train_dataset.movement.values))
            )
        }
        
        training_set = TrainingSet(
            dataset=train_dataset,
            normalization_params=normalization_params
        )
        
        test_set = TestSet(dataset=test_dataset)
        
        return training_set, test_set
    
    def _split_by_time(
        self,
        data: Dataset,
        test_ratio: float
    ) -> Tuple[TrainingSet, TestSet]:
        """
        Split single-subject dataset by time
        
        Args:
            data: Dataset to split
            test_ratio: Ratio of test set
            
        Returns:
            Tuple of (TrainingSet, TestSet)
        """
        num_samples = len(data.heart_rate.timestamps)
        split_point = int(num_samples * (1 - test_ratio))
        
        # Create training set
        train_dataset = Dataset(
            heart_rate=HeartRateData(
                timestamps=data.heart_rate.timestamps[:split_point],
                values=data.heart_rate.values[:split_point],
                sampling_rate=data.heart_rate.sampling_rate
            ),
            movement=MovementData(
                timestamps=data.movement.timestamps[:split_point],
                values=data.movement.values[:split_point],
                sampling_rate=data.movement.sampling_rate
            ),
            sleep_stages=SleepStages(
                timestamps=data.sleep_stages.timestamps[:split_point],
                stages=data.sleep_stages.stages[:split_point]
            ),
            subject_ids=data.subject_ids
        )
        
        # Create test set
        test_dataset = Dataset(
            heart_rate=HeartRateData(
                timestamps=data.heart_rate.timestamps[split_point:],
                values=data.heart_rate.values[split_point:],
                sampling_rate=data.heart_rate.sampling_rate
            ),
            movement=MovementData(
                timestamps=data.movement.timestamps[split_point:],
                values=data.movement.values[split_point:],
                sampling_rate=data.movement.sampling_rate
            ),
            sleep_stages=SleepStages(
                timestamps=data.sleep_stages.timestamps[split_point:],
                stages=data.sleep_stages.stages[split_point:]
            ),
            subject_ids=data.subject_ids
        )
        
        # Calculate normalization parameters from training set
        normalization_params = {
            'heart_rate': (
                float(np.mean(train_dataset.heart_rate.values)),
                float(np.std(train_dataset.heart_rate.values))
            ),
            'movement': (
                float(np.mean(train_dataset.movement.values)),
                float(np.std(train_dataset.movement.values))
            )
        }
        
        training_set = TrainingSet(
            dataset=train_dataset,
            normalization_params=normalization_params
        )
        
        test_set = TestSet(dataset=test_dataset)
        
        logger.info(f"Split by time: {split_point} train samples, "
                   f"{num_samples - split_point} test samples")
        
        return training_set, test_set
    
    def k_fold_split(
        self,
        data: Dataset,
        k: int = 5
    ) -> List[Tuple[TrainingSet, TestSet]]:
        """
        K-fold cross-validation data split
        
        Args:
            data: Dataset to split
            k: Number of folds (default 5)
            
        Returns:
            List of (TrainingSet, TestSet) tuples for each fold
            
        Raises:
            ValueError: If k < 2
        """
        if k < 2:
            raise ValueError(f"k must be >= 2, got {k}")
        
        # Get unique subject IDs
        unique_subjects = list(set(data.subject_ids))
        num_subjects = len(unique_subjects)
        
        if num_subjects < k:
            logger.warning(f"Number of subjects ({num_subjects}) < k ({k}), "
                          f"using time-based splitting")
            return self._k_fold_split_by_time(data, k)
        
        # Shuffle subjects
        np.random.shuffle(unique_subjects)
        
        # Distribute subjects as evenly as possible across folds
        # (remainder subjects are spread one-per-fold from the front)
        base_size = num_subjects // k
        remainder = num_subjects % k
        fold_sizes = [base_size + (1 if i < remainder else 0) for i in range(k)]
        
        folds = []
        
        for fold_idx in range(k):
            # Determine test subjects for this fold
            start_idx = sum(fold_sizes[:fold_idx])
            end_idx = start_idx + fold_sizes[fold_idx]
            test_subjects = set(unique_subjects[start_idx:end_idx])
            train_subjects = set(unique_subjects) - test_subjects
            
            # Split data by subject
            train_indices = [i for i, subj in enumerate(data.subject_ids) 
                           if subj in train_subjects]
            test_indices = [i for i, subj in enumerate(data.subject_ids) 
                          if subj in test_subjects]
            
            # Create training set
            train_dataset = Dataset(
                heart_rate=HeartRateData(
                    timestamps=data.heart_rate.timestamps[train_indices],
                    values=data.heart_rate.values[train_indices],
                    sampling_rate=data.heart_rate.sampling_rate
                ),
                movement=MovementData(
                    timestamps=data.movement.timestamps[train_indices],
                    values=data.movement.values[train_indices],
                    sampling_rate=data.movement.sampling_rate
                ),
                sleep_stages=SleepStages(
                    timestamps=data.sleep_stages.timestamps[train_indices],
                    stages=data.sleep_stages.stages[train_indices]
                ),
                subject_ids=[data.subject_ids[i] for i in train_indices]
            )
            
            # Create test set
            test_dataset = Dataset(
                heart_rate=HeartRateData(
                    timestamps=data.heart_rate.timestamps[test_indices],
                    values=data.heart_rate.values[test_indices],
                    sampling_rate=data.heart_rate.sampling_rate
                ),
                movement=MovementData(
                    timestamps=data.movement.timestamps[test_indices],
                    values=data.movement.values[test_indices],
                    sampling_rate=data.movement.sampling_rate
                ),
                sleep_stages=SleepStages(
                    timestamps=data.sleep_stages.timestamps[test_indices],
                    stages=data.sleep_stages.stages[test_indices]
                ),
                subject_ids=[data.subject_ids[i] for i in test_indices]
            )
            
            # Calculate normalization parameters from training set
            normalization_params = {
                'heart_rate': (
                    float(np.mean(train_dataset.heart_rate.values)),
                    float(np.std(train_dataset.heart_rate.values))
                ),
                'movement': (
                    float(np.mean(train_dataset.movement.values)),
                    float(np.std(train_dataset.movement.values))
                )
            }
            
            training_set = TrainingSet(
                dataset=train_dataset,
                normalization_params=normalization_params
            )
            
            test_set = TestSet(dataset=test_dataset)
            
            folds.append((training_set, test_set))
            
            logger.info(f"Fold {fold_idx + 1}/{k}: {len(train_subjects)} train subjects, "
                       f"{len(test_subjects)} test subjects")
        
        return folds
    
    def _k_fold_split_by_time(
        self,
        data: Dataset,
        k: int
    ) -> List[Tuple[TrainingSet, TestSet]]:
        """
        K-fold split by time for single or few subjects
        
        Args:
            data: Dataset to split
            k: Number of folds
            
        Returns:
            List of (TrainingSet, TestSet) tuples for each fold
        """
        num_samples = len(data.heart_rate.timestamps)
        base_size = num_samples // k
        remainder = num_samples % k
        fold_sizes = [base_size + (1 if i < remainder else 0) for i in range(k)]
        
        folds = []
        
        for fold_idx in range(k):
            # Determine test indices for this fold
            start_idx = sum(fold_sizes[:fold_idx])
            end_idx = start_idx + fold_sizes[fold_idx]
            
            test_indices = list(range(start_idx, end_idx))
            train_indices = list(range(0, start_idx)) + list(range(end_idx, num_samples))
            
            # Create training set
            train_dataset = Dataset(
                heart_rate=HeartRateData(
                    timestamps=data.heart_rate.timestamps[train_indices],
                    values=data.heart_rate.values[train_indices],
                    sampling_rate=data.heart_rate.sampling_rate
                ),
                movement=MovementData(
                    timestamps=data.movement.timestamps[train_indices],
                    values=data.movement.values[train_indices],
                    sampling_rate=data.movement.sampling_rate
                ),
                sleep_stages=SleepStages(
                    timestamps=data.sleep_stages.timestamps[train_indices],
                    stages=data.sleep_stages.stages[train_indices]
                ),
                subject_ids=data.subject_ids
            )
            
            # Create test set
            test_dataset = Dataset(
                heart_rate=HeartRateData(
                    timestamps=data.heart_rate.timestamps[test_indices],
                    values=data.heart_rate.values[test_indices],
                    sampling_rate=data.heart_rate.sampling_rate
                ),
                movement=MovementData(
                    timestamps=data.movement.timestamps[test_indices],
                    values=data.movement.values[test_indices],
                    sampling_rate=data.movement.sampling_rate
                ),
                sleep_stages=SleepStages(
                    timestamps=data.sleep_stages.timestamps[test_indices],
                    stages=data.sleep_stages.stages[test_indices]
                ),
                subject_ids=data.subject_ids
            )
            
            # Calculate normalization parameters from training set
            normalization_params = {
                'heart_rate': (
                    float(np.mean(train_dataset.heart_rate.values)),
                    float(np.std(train_dataset.heart_rate.values))
                ),
                'movement': (
                    float(np.mean(train_dataset.movement.values)),
                    float(np.std(train_dataset.movement.values))
                )
            }
            
            training_set = TrainingSet(
                dataset=train_dataset,
                normalization_params=normalization_params
            )
            
            test_set = TestSet(dataset=test_dataset)
            
            folds.append((training_set, test_set))
            
            logger.info(f"Fold {fold_idx + 1}/{k} (time-based): {len(train_indices)} train samples, "
                       f"{len(test_indices)} test samples")
        
        return folds

    # =====================================================================
    # Sleep-EDF Telemetry adapter (real PhysioNet data)
    # =====================================================================

    def load_sleep_edf_telemetry(
        self,
        dataset_dir: str,
        subjects: Optional[List[str]] = None,
        target_sampling_rate: int = 10,
    ) -> Dataset:
        """Load the PhysioNet Sleep-EDF Telemetry corpus into a :class:`Dataset`.

        This dataset does not contain dedicated heart-rate or accelerometer
        channels — it provides EEG, EOG and EMG.  To fit the project's
        dual-sensor schema we use the following channel mapping:

        * **Heart-rate proxy** (``Dataset.heart_rate``) – z-scored
          ``EOG horizontal`` signal mapped to the physiological range
          ``[60, 100] bpm``.  This satisfies the [30, 200] validation while
          preserving the slow eye-movement waveform that correlates with
          sleep depth (REM bursts, sleep onset, etc.).
        * **Movement proxy** (``Dataset.movement``) – RMS envelope of the
          ``EMG submental`` signal.  Submental EMG amplitude tracks gross
          body movement and decreases with sleep depth, then nearly
          vanishes during REM (atonia).
        * **Sleep stages** (``Dataset.sleep_stages``) – real expert-scored
          AASM stages from the paired Hypnogram file (W → AWAKE,
          N1+N2 → LIGHT, N3+N4 → DEEP, R → REM).

        All signals are downsampled to ``target_sampling_rate`` (default
        10 Hz) so that 9-hour recordings fit comfortably in memory while
        preserving the slow-wave content relevant for staging.

        Args:
            dataset_dir: Directory containing ``ST7XXX*-PSG.edf`` and
                ``ST7XXX*-Hypnogram.edf`` files (e.g. the output of
                ``scripts/download_data.py``).
            subjects: Optional list of subject IDs (e.g. ``["ST7011"]``) to
                include.  If ``None``, every subject in the directory is
                loaded.
            target_sampling_rate: Resampling rate for the proxy signals.

        Returns:
            A :class:`Dataset` whose ``heart_rate``, ``movement`` and
            ``sleep_stages`` arrays all share the same length and timestamps.

        Raises:
            DatasetLoadError: If the directory is empty, files cannot be
                paired, or no usable subjects remain after filtering.
        """
        path = Path(dataset_dir)
        if not path.exists() or not path.is_dir():
            raise DatasetLoadError(
                f"Sleep-EDF Telemetry directory does not exist: {dataset_dir}"
            )

        # Pair each PSG with its hypnogram by 6-character subject prefix
        psg_files = sorted(path.glob("ST7*PSG.edf"))
        hyp_files = sorted(path.glob("ST7*Hypnogram.edf"))
        pairs: List[Tuple[str, Path, Path]] = []
        for psg in psg_files:
            subject_id = psg.name[:6]  # e.g. "ST7011"
            if subjects is not None and subject_id not in subjects:
                continue
            matching_hyp = [h for h in hyp_files if h.name.startswith(subject_id)]
            if not matching_hyp:
                logger.warning("No hypnogram for %s — skipping", subject_id)
                continue
            pairs.append((subject_id, psg, matching_hyp[0]))

        if not pairs:
            raise DatasetLoadError(
                f"No usable PSG/Hypnogram pairs found in {dataset_dir}"
                + (f" for subjects {subjects}" if subjects else "")
            )

        logger.info("Loading %d Sleep-EDF Telemetry subject(s) from %s",
                    len(pairs), dataset_dir)

        # Per-subject buffers to be concatenated at the end
        all_hr: List[np.ndarray] = []
        all_mv: List[np.ndarray] = []
        all_stages: List[np.ndarray] = []
        all_subjects: List[str] = []

        for subject_id, psg_path, hyp_path in pairs:
            try:
                hr_seg, mv_seg, st_seg = self._load_telemetry_subject(
                    str(psg_path), str(hyp_path), target_sampling_rate
                )
            except Exception as exc:
                logger.error("Failed to load %s: %s", subject_id, exc)
                continue

            n = min(len(hr_seg), len(mv_seg), len(st_seg))
            if n == 0:
                logger.warning("Subject %s yielded zero samples — skipping", subject_id)
                continue
            all_hr.append(hr_seg[:n])
            all_mv.append(mv_seg[:n])
            all_stages.append(st_seg[:n])
            all_subjects.extend([subject_id] * n)
            logger.info("  %s: %d samples (%.1f minutes)",
                        subject_id, n, n / target_sampling_rate / 60.0)

        if not all_hr:
            raise DatasetLoadError("No subjects could be loaded successfully")

        hr_concat = np.concatenate(all_hr)
        mv_concat = np.concatenate(all_mv)
        stage_concat = np.concatenate(all_stages)
        timestamps = np.arange(len(hr_concat), dtype=np.float64) / target_sampling_rate

        dataset = Dataset(
            heart_rate=HeartRateData(
                timestamps=timestamps.copy(),
                values=hr_concat,
                sampling_rate=target_sampling_rate,
            ),
            movement=MovementData(
                timestamps=timestamps.copy(),
                values=mv_concat,
                sampling_rate=target_sampling_rate,
            ),
            sleep_stages=SleepStages(
                timestamps=timestamps.copy(),
                stages=stage_concat,
            ),
            subject_ids=all_subjects,
        )
        # Stage-distribution summary for sanity checking
        unique, counts = np.unique(stage_concat, return_counts=True)
        stage_names = {0: "AWAKE", 1: "LIGHT", 2: "DEEP", 3: "REM"}
        summary = ", ".join(
            f"{stage_names.get(int(u), '?')}={c}({100*c/len(stage_concat):.1f}%)"
            for u, c in zip(unique, counts)
        )
        logger.info(
            "Sleep-EDF Telemetry dataset built: %d samples @ %d Hz, stages: %s",
            len(hr_concat), target_sampling_rate, summary,
        )
        return dataset

    def _load_telemetry_subject(
        self,
        psg_path: str,
        hyp_path: str,
        target_sampling_rate: int,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Load and resample one Sleep-EDF Telemetry subject.

        Returns:
            ``(hr_proxy, mv_proxy, stages)`` arrays at ``target_sampling_rate``.
        """
        # 1) Hypnogram → per-epoch sleep stage labels (30 s each)
        hyp_parser = EDFParser()
        sleep_stages = hyp_parser.extract_sleep_annotations(hyp_path)
        if len(sleep_stages.stages) == 0:
            raise DatasetLoadError(f"No sleep stages found in {hyp_path}")

        # 2) PSG → EOG and EMG signals
        psg_parser = EDFParser()
        psg_parser.parse_header(psg_path)
        psg_parser._read_signals(psg_path)

        eog_label = next(
            (lbl for lbl in psg_parser._header.channel_labels if "eog" in lbl.lower()),
            None,
        )
        emg_label = next(
            (lbl for lbl in psg_parser._header.channel_labels if "emg" in lbl.lower()),
            None,
        )
        if eog_label is None or emg_label is None:
            raise DatasetLoadError(
                f"Expected EOG and EMG channels in {psg_path}; "
                f"got {psg_parser._header.channel_labels}"
            )

        eog = psg_parser._signals_data[eog_label].astype(np.float64)
        emg = psg_parser._signals_data[emg_label].astype(np.float64)

        psg_fs = int(psg_parser._header.sampling_rates[eog_label])
        if psg_fs <= 0:
            raise DatasetLoadError(f"Invalid sampling rate for {eog_label}")

        # 3) Build movement proxy: RMS envelope of EMG over 1-second windows
        mv_envelope = self._rms_envelope(emg, window_size=psg_fs)

        # 4) Resample EOG and EMG envelope to target_sampling_rate
        decimation = max(1, psg_fs // target_sampling_rate)
        eog_ds = eog[::decimation]
        mv_ds = mv_envelope[::decimation]

        # 5) Map EOG to "heart-rate" proxy in [60, 100] bpm via z-score
        hr_proxy = self._zscore_to_range(eog_ds, lo=60.0, hi=100.0)

        # 6) Expand epoch-level stages to sample-level
        epoch_seconds = 30.0
        samples_per_epoch = int(round(epoch_seconds * target_sampling_rate))
        n_epochs = len(sleep_stages.stages)
        stages_expanded = np.repeat(sleep_stages.stages, samples_per_epoch).astype(np.int32)

        # Truncate everything to the shortest length so all arrays match
        n = min(len(hr_proxy), len(mv_ds), len(stages_expanded))
        return hr_proxy[:n].astype(np.float64), mv_ds[:n].astype(np.float64), stages_expanded[:n]

    @staticmethod
    def _rms_envelope(signal: np.ndarray, window_size: int) -> np.ndarray:
        """Compute a moving root-mean-square envelope of ``signal``.

        Used to reduce raw EMG into a slowly-varying movement proxy.
        """
        if window_size <= 1:
            return np.abs(signal)
        squared = signal.astype(np.float64) ** 2
        # Cumulative sum trick for fast moving average
        cumsum = np.concatenate([[0.0], np.cumsum(squared)])
        ma = (cumsum[window_size:] - cumsum[:-window_size]) / window_size
        envelope = np.sqrt(np.maximum(ma, 0.0))
        # Pad the front so the output length matches the input length
        if len(envelope) < len(signal):
            pad = np.full(len(signal) - len(envelope), envelope[0] if len(envelope) else 0.0)
            envelope = np.concatenate([pad, envelope])
        return envelope

    @staticmethod
    def _zscore_to_range(signal: np.ndarray, lo: float, hi: float) -> np.ndarray:
        """Z-score normalise ``signal`` then linearly map to ``[lo, hi]``.

        Outputs are clipped to ``[lo, hi]`` so that downstream
        :class:`HeartRateData` validation always succeeds.  A logistic
        squashing through ``tanh`` is used so that extreme amplitudes do
        not dominate the mapping.
        """
        std = np.std(signal)
        if std < 1e-12:
            mid = 0.5 * (lo + hi)
            return np.full_like(signal, mid, dtype=np.float64)
        z = (signal - np.mean(signal)) / std
        squashed = np.tanh(z / 3.0)            # → (-1, 1) softly
        mid, half = 0.5 * (lo + hi), 0.5 * (hi - lo)
        mapped = mid + half * squashed
        return np.clip(mapped, lo, hi)
