"""Unit tests for Dataset_Loader class"""
import pytest
import numpy as np
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import tempfile
import os

from src.dataset_loader import DatasetLoader, DatasetLoadError
from src.data_structures import (
    Dataset,
    HeartRateData,
    MovementData,
    SleepStages,
    EDFHeader,
    TrainingSet,
    TestSet
)
from src.edf_parser import EDFParseError
from datetime import datetime


class TestDatasetLoader:
    """Test suite for DatasetLoader class"""
    
    @pytest.fixture
    def loader(self):
        """Create DatasetLoader instance"""
        return DatasetLoader()
    
    @pytest.fixture
    def mock_dataset(self):
        """Create mock dataset for testing"""
        num_samples = 1000
        timestamps = np.arange(num_samples, dtype=np.float64)
        
        heart_rate = HeartRateData(
            timestamps=timestamps,
            values=np.random.uniform(60, 100, num_samples),
            sampling_rate=100
        )
        
        movement = MovementData(
            timestamps=timestamps,
            values=np.random.uniform(0, 5, num_samples),
            sampling_rate=100
        )
        
        sleep_stages = SleepStages(
            timestamps=timestamps,
            stages=np.random.randint(0, 4, num_samples)
        )
        
        return Dataset(
            heart_rate=heart_rate,
            movement=movement,
            sleep_stages=sleep_stages,
            subject_ids=['subject_001'] * num_samples
        )
    
    def test_load_sleep_edf_invalid_path(self, loader):
        """Test loading Sleep-EDF with invalid path"""
        with pytest.raises(DatasetLoadError) as exc_info:
            loader.load_sleep_edf("/nonexistent/path")
        
        assert "does not exist" in str(exc_info.value)
    
    def test_load_sleep_edf_directory_no_files(self, loader):
        """Test loading Sleep-EDF from directory with no EDF files"""
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(DatasetLoadError) as exc_info:
                loader.load_sleep_edf(tmpdir)
            
            assert "No EDF files found" in str(exc_info.value)
    
    @patch('src.dataset_loader.EDFParser')
    def test_load_sleep_edf_missing_heart_rate_channel(self, mock_parser_class, loader):
        """Test loading Sleep-EDF with missing heart rate channel"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a dummy EDF file
            edf_file = Path(tmpdir) / "test.edf"
            edf_file.touch()
            
            # Mock parser to raise error on heart rate extraction
            mock_parser = Mock()
            mock_parser.parse_header.return_value = Mock(
                subject_id='test',
                recording_date=datetime.now(),
                duration_seconds=100.0,
                num_channels=2,
                channel_labels=['channel1', 'channel2'],
                sampling_rates={'channel1': 100},
                physical_units={'channel1': 'mV'}
            )
            mock_parser.extract_heart_rate_channel.side_effect = EDFParseError(
                "Heart rate channel not found"
            )
            
            loader.parser = mock_parser
            
            with pytest.raises(DatasetLoadError) as exc_info:
                loader.load_sleep_edf(str(edf_file))
            
            assert "heart rate channel" in str(exc_info.value).lower()
    
    @patch('src.dataset_loader.EDFParser')
    def test_load_sleep_edf_missing_movement_channel(self, mock_parser_class, loader):
        """Test loading Sleep-EDF with missing movement channel"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a dummy EDF file
            edf_file = Path(tmpdir) / "test.edf"
            edf_file.touch()
            
            # Mock parser
            mock_parser = Mock()
            mock_parser.parse_header.return_value = Mock(
                subject_id='test',
                recording_date=datetime.now(),
                duration_seconds=100.0,
                num_channels=2
            )
            mock_parser.extract_heart_rate_channel.return_value = HeartRateData(
                timestamps=np.arange(100),
                values=np.full(100, 70.0),
                sampling_rate=100
            )
            mock_parser.extract_movement_channel.side_effect = EDFParseError(
                "Movement channel not found"
            )
            
            loader.parser = mock_parser
            
            with pytest.raises(DatasetLoadError) as exc_info:
                loader.load_sleep_edf(str(edf_file))
            
            assert "movement channel" in str(exc_info.value).lower()
    
    @patch('src.dataset_loader.EDFParser')
    def test_load_sleep_edf_success(self, mock_parser_class, loader):
        """Test successful Sleep-EDF loading"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a dummy EDF file
            edf_file = Path(tmpdir) / "test.edf"
            edf_file.touch()
            
            # Mock parser
            mock_parser = Mock()
            mock_parser.parse_header.return_value = Mock(
                subject_id='test_subject',
                recording_date=datetime.now(),
                duration_seconds=100.0,
                num_channels=3
            )
            
            timestamps = np.arange(1000, dtype=np.float64)
            mock_parser.extract_heart_rate_channel.return_value = HeartRateData(
                timestamps=timestamps,
                values=np.full(1000, 70.0),
                sampling_rate=100
            )
            mock_parser.extract_movement_channel.return_value = MovementData(
                timestamps=timestamps,
                values=np.full(1000, 1.0),
                sampling_rate=100
            )
            mock_parser.extract_sleep_annotations.return_value = SleepStages(
                timestamps=timestamps,
                stages=np.zeros(1000, dtype=np.int32)
            )
            
            loader.parser = mock_parser
            
            dataset = loader.load_sleep_edf(str(edf_file))
            
            assert isinstance(dataset, Dataset)
            assert len(dataset.heart_rate.values) == 1000
            assert len(dataset.movement.values) == 1000
            assert len(dataset.sleep_stages.stages) == 1000
            assert dataset.subject_ids == ['test_subject']
    
    def test_load_mit_bih_invalid_path(self, loader):
        """Test loading MIT-BIH with invalid path"""
        with pytest.raises(DatasetLoadError) as exc_info:
            loader.load_mit_bih("/nonexistent/path")
        
        assert "does not exist" in str(exc_info.value)
    
    @patch('src.dataset_loader.EDFParser')
    def test_load_mit_bih_success(self, mock_parser_class, loader):
        """Test successful MIT-BIH loading"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a dummy EDF file
            edf_file = Path(tmpdir) / "mitbih.edf"
            edf_file.touch()
            
            # Mock parser
            mock_parser = Mock()
            mock_parser.parse_header.return_value = Mock(
                subject_id='mitbih_001',
                recording_date=datetime.now(),
                duration_seconds=200.0,
                num_channels=3
            )
            
            timestamps = np.arange(2000, dtype=np.float64)
            mock_parser.extract_heart_rate_channel.return_value = HeartRateData(
                timestamps=timestamps,
                values=np.full(2000, 75.0),
                sampling_rate=100
            )
            mock_parser.extract_movement_channel.return_value = MovementData(
                timestamps=timestamps,
                values=np.full(2000, 0.5),
                sampling_rate=100
            )
            mock_parser.extract_sleep_annotations.return_value = SleepStages(
                timestamps=timestamps,
                stages=np.ones(2000, dtype=np.int32)
            )
            
            loader.parser = mock_parser
            
            dataset = loader.load_mit_bih(str(edf_file))
            
            assert isinstance(dataset, Dataset)
            assert len(dataset.heart_rate.values) == 2000
            assert dataset.subject_ids == ['mitbih_001']
    
    def test_split_train_test_invalid_ratio(self, loader, mock_dataset):
        """Test split_train_test with invalid ratio"""
        with pytest.raises(ValueError):
            loader.split_train_test(mock_dataset, test_ratio=0.0)
        
        with pytest.raises(ValueError):
            loader.split_train_test(mock_dataset, test_ratio=1.0)
        
        with pytest.raises(ValueError):
            loader.split_train_test(mock_dataset, test_ratio=-0.1)
    
    def test_split_train_test_default_ratio(self, loader, mock_dataset):
        """Test split_train_test with default 80:20 ratio"""
        train_set, test_set = loader.split_train_test(mock_dataset)
        
        assert isinstance(train_set, TrainingSet)
        assert isinstance(test_set, TestSet)
        
        # Check normalization parameters exist
        assert 'heart_rate' in train_set.normalization_params
        assert 'movement' in train_set.normalization_params
        
        # Check data split
        total_samples = len(mock_dataset.heart_rate.values)
        train_samples = len(train_set.dataset.heart_rate.values)
        test_samples = len(test_set.dataset.heart_rate.values)
        
        assert train_samples + test_samples == total_samples
        assert test_samples / total_samples == pytest.approx(0.2, abs=0.05)
    
    def test_split_train_test_custom_ratio(self, loader, mock_dataset):
        """Test split_train_test with custom ratio"""
        train_set, test_set = loader.split_train_test(mock_dataset, test_ratio=0.3)
        
        total_samples = len(mock_dataset.heart_rate.values)
        test_samples = len(test_set.dataset.heart_rate.values)
        
        assert test_samples / total_samples == pytest.approx(0.3, abs=0.05)
    
    def test_split_train_test_no_overlap(self, loader):
        """Test that same subject doesn't appear in both train and test sets"""
        # Create dataset with multiple subjects
        num_samples_per_subject = 100
        num_subjects = 5
        
        timestamps = np.arange(num_samples_per_subject * num_subjects, dtype=np.float64)
        subject_ids = []
        for i in range(num_subjects):
            subject_ids.extend([f'subject_{i:03d}'] * num_samples_per_subject)
        
        dataset = Dataset(
            heart_rate=HeartRateData(
                timestamps=timestamps,
                values=np.random.uniform(60, 100, len(timestamps)),
                sampling_rate=100
            ),
            movement=MovementData(
                timestamps=timestamps,
                values=np.random.uniform(0, 5, len(timestamps)),
                sampling_rate=100
            ),
            sleep_stages=SleepStages(
                timestamps=timestamps,
                stages=np.random.randint(0, 4, len(timestamps))
            ),
            subject_ids=subject_ids
        )
        
        train_set, test_set = loader.split_train_test(dataset, test_ratio=0.2)
        
        train_subjects = set(train_set.dataset.subject_ids)
        test_subjects = set(test_set.dataset.subject_ids)
        
        # Ensure no overlap
        assert len(train_subjects & test_subjects) == 0
    
    def test_k_fold_split_invalid_k(self, loader, mock_dataset):
        """Test k_fold_split with invalid k value"""
        with pytest.raises(ValueError):
            loader.k_fold_split(mock_dataset, k=1)
        
        with pytest.raises(ValueError):
            loader.k_fold_split(mock_dataset, k=0)
    
    def test_k_fold_split_default_k(self, loader, mock_dataset):
        """Test k_fold_split with default k=5"""
        folds = loader.k_fold_split(mock_dataset, k=5)
        
        assert len(folds) == 5
        
        for train_set, test_set in folds:
            assert isinstance(train_set, TrainingSet)
            assert isinstance(test_set, TestSet)
            
            # Check normalization parameters
            assert 'heart_rate' in train_set.normalization_params
            assert 'movement' in train_set.normalization_params
    
    def test_k_fold_split_balanced(self, loader, mock_dataset):
        """Test that k-fold splits are roughly balanced"""
        folds = loader.k_fold_split(mock_dataset, k=5)
        
        test_sizes = [len(test_set.dataset.heart_rate.values) 
                     for _, test_set in folds]
        
        # Check that all test sets are roughly the same size (within 10%)
        mean_size = np.mean(test_sizes)
        for size in test_sizes:
            assert abs(size - mean_size) / mean_size <= 0.1
    
    def test_k_fold_split_coverage(self, loader, mock_dataset):
        """Test that k-fold splits cover all data"""
        folds = loader.k_fold_split(mock_dataset, k=5)
        
        total_samples = len(mock_dataset.heart_rate.values)
        
        # Sum of all test set sizes should equal total samples
        total_test_samples = sum(
            len(test_set.dataset.heart_rate.values)
            for _, test_set in folds
        )
        
        assert total_test_samples == total_samples
    
    def test_normalization_params_calculation(self, loader, mock_dataset):
        """Test that normalization parameters are correctly calculated"""
        train_set, _ = loader.split_train_test(mock_dataset)
        
        hr_mean, hr_std = train_set.normalization_params['heart_rate']
        mv_mean, mv_std = train_set.normalization_params['movement']
        
        # Verify parameters match actual data statistics
        actual_hr_mean = np.mean(train_set.dataset.heart_rate.values)
        actual_hr_std = np.std(train_set.dataset.heart_rate.values)
        actual_mv_mean = np.mean(train_set.dataset.movement.values)
        actual_mv_std = np.std(train_set.dataset.movement.values)
        
        assert hr_mean == pytest.approx(actual_hr_mean, rel=1e-6)
        assert hr_std == pytest.approx(actual_hr_std, rel=1e-6)
        assert mv_mean == pytest.approx(actual_mv_mean, rel=1e-6)
        assert mv_std == pytest.approx(actual_mv_std, rel=1e-6)


# Feature: cnn-bilstm-sleep-algorithm, Property 1: Dataset loading completeness
# Validates: Requirements 1.9

from hypothesis import given, settings, assume
import hypothesis.strategies as st
from unittest.mock import patch, Mock


def _make_dataset(num_samples: int, sampling_rate: int) -> Dataset:
    """Helper to build a Dataset with the given parameters."""
    timestamps = np.arange(num_samples, dtype=np.float64) / sampling_rate
    heart_rate = HeartRateData(
        timestamps=timestamps,
        values=np.full(num_samples, 70.0),
        sampling_rate=sampling_rate,
    )
    movement = MovementData(
        timestamps=timestamps,
        values=np.full(num_samples, 1.0),
        sampling_rate=sampling_rate,
    )
    sleep_stages = SleepStages(
        timestamps=timestamps,
        stages=np.zeros(num_samples, dtype=np.int32),
    )
    return Dataset(
        heart_rate=heart_rate,
        movement=movement,
        sleep_stages=sleep_stages,
        subject_ids=["subject_001"] * num_samples,
    )


# Strategy: generate valid dataset parameters
dataset_params = st.fixed_dictionaries(
    {
        "num_samples": st.integers(min_value=1, max_value=500),
        "sampling_rate": st.integers(min_value=1, max_value=256),
    }
)


@given(params=dataset_params, dataset_type=st.sampled_from(["sleep_edf", "mit_bih"]))
@settings(max_examples=50)
def test_dataset_loading_completeness(params, dataset_type):
    """
    **Validates: Requirements 1.9**

    For ALL supported datasets (Sleep-EDF or MIT-BIH), the loaded Dataset SHALL
    contain heart_rate, movement, and sleep_stages, each with non-empty data.
    """
    num_samples = params["num_samples"]
    sampling_rate = params["sampling_rate"]

    expected_dataset = _make_dataset(num_samples, sampling_rate)

    loader = DatasetLoader()

    with tempfile.TemporaryDirectory() as tmpdir:
        edf_file = Path(tmpdir) / "test.edf"
        edf_file.touch()

        mock_parser = Mock()
        mock_parser.parse_header.return_value = Mock(
            subject_id="subject_001",
            recording_date=datetime.now(),
            duration_seconds=float(num_samples) / sampling_rate,
            num_channels=3,
        )
        mock_parser.extract_heart_rate_channel.return_value = expected_dataset.heart_rate
        mock_parser.extract_movement_channel.return_value = expected_dataset.movement
        mock_parser.extract_sleep_annotations.return_value = expected_dataset.sleep_stages

        loader.parser = mock_parser

        if dataset_type == "sleep_edf":
            dataset = loader.load_sleep_edf(str(edf_file))
        else:
            dataset = loader.load_mit_bih(str(edf_file))

        # Property: loaded dataset must contain all three required components
        assert dataset.heart_rate is not None, "heart_rate must be present"
        assert dataset.movement is not None, "movement must be present"
        assert dataset.sleep_stages is not None, "sleep_stages must be present"

        # Property: each component must be non-empty
        assert len(dataset.heart_rate.values) > 0, "heart_rate.values must be non-empty"
        assert len(dataset.movement.values) > 0, "movement.values must be non-empty"
        assert len(dataset.sleep_stages.stages) > 0, "sleep_stages.stages must be non-empty"


# Feature: cnn-bilstm-sleep-algorithm, Property 5: K-fold split balance
# Validates: Requirements 4.6

def _make_dataset_multi_subject(num_samples: int, num_subjects: int) -> Dataset:
    """Helper to build a Dataset with multiple subjects."""
    samples_per_subject = num_samples // num_subjects
    total_samples = samples_per_subject * num_subjects

    timestamps = np.arange(total_samples, dtype=np.float64)
    subject_ids = []
    for i in range(num_subjects):
        subject_ids.extend([f"subject_{i:03d}"] * samples_per_subject)

    heart_rate = HeartRateData(
        timestamps=timestamps,
        values=np.full(total_samples, 70.0),
        sampling_rate=100,
    )
    movement = MovementData(
        timestamps=timestamps,
        values=np.full(total_samples, 1.0),
        sampling_rate=100,
    )
    sleep_stages = SleepStages(
        timestamps=timestamps,
        stages=np.zeros(total_samples, dtype=np.int32),
    )
    return Dataset(
        heart_rate=heart_rate,
        movement=movement,
        sleep_stages=sleep_stages,
        subject_ids=subject_ids,
    )


# Strategy: k values from 2 to 10, datasets with enough samples to split meaningfully
k_fold_params = st.fixed_dictionaries(
    {
        "k": st.integers(min_value=2, max_value=10),
        "num_subjects": st.integers(min_value=10, max_value=50),
        "samples_per_subject": st.integers(min_value=10, max_value=50),
    }
)


@given(params=k_fold_params)
@settings(max_examples=50)
def test_k_fold_split_balance(params):
    """
    **Validates: Requirements 4.6**

    For ALL K-fold splits, each subset SHALL contain approximately equal number
    of samples (difference within ±10% of the mean test-set size).
    """
    k = params["k"]
    num_subjects = params["num_subjects"]
    samples_per_subject = params["samples_per_subject"]

    # Skip cases where integer subject distribution makes ±10% balance
    # mathematically impossible. With remainder subjects distributed one-per-fold,
    # fold sizes are either base or base+1 subjects. Check both deviations.
    base_subjects = num_subjects // k
    remainder = num_subjects % k
    if remainder > 0:
        mean_subjects = num_subjects / k
        # Folds with base+1 subjects deviate upward; folds with base deviate downward
        max_up_deviation = (base_subjects + 1 - mean_subjects) / mean_subjects
        max_down_deviation = (mean_subjects - base_subjects) / mean_subjects
        assume(max(max_up_deviation, max_down_deviation) <= 0.10)

    dataset = _make_dataset_multi_subject(
        num_samples=num_subjects * samples_per_subject,
        num_subjects=num_subjects,
    )

    loader = DatasetLoader()
    folds = loader.k_fold_split(dataset, k=k)

    # Property: exactly k folds are returned
    assert len(folds) == k, f"Expected {k} folds, got {len(folds)}"

    # Collect test-set sizes across all folds
    test_sizes = [
        len(test_set.dataset.heart_rate.values)
        for _, test_set in folds
    ]

    mean_size = np.mean(test_sizes)

    # Property: each fold's test set is within ±10% of the mean
    for fold_idx, size in enumerate(test_sizes):
        deviation = abs(size - mean_size) / mean_size
        assert deviation <= 0.10 + 1e-9, (
            f"Fold {fold_idx} test size {size} deviates {deviation:.1%} "
            f"from mean {mean_size:.1f} (k={k}, num_subjects={num_subjects})"
        )
