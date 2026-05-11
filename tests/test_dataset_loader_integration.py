"""Integration tests for Dataset_Loader with EDF_Parser"""
import pytest
import numpy as np
from pathlib import Path
import tempfile
import struct
from datetime import datetime

from src.dataset_loader import DatasetLoader, DatasetLoadError
from src.data_structures import Dataset


class TestDatasetLoaderIntegration:
    """Integration test suite for DatasetLoader with real EDF parsing"""
    
    @pytest.fixture
    def loader(self):
        """Create DatasetLoader instance"""
        return DatasetLoader()
    
    def create_minimal_edf_file(self, filepath: str, duration_seconds: float = 10.0):
        """
        Create a minimal valid EDF file for testing
        
        Args:
            filepath: Path to create EDF file
            duration_seconds: Duration of recording in seconds
        """
        with open(filepath, 'wb') as f:
            # Fixed header (256 bytes)
            f.write(b'0       ')  # Version (8 bytes)
            f.write(b'TestPatient'.ljust(80))  # Patient ID (80 bytes)
            f.write(b'TestRecording'.ljust(80))  # Recording ID (80 bytes)
            f.write(b'01.01.24')  # Date (8 bytes)
            f.write(b'12.00.00')  # Time (8 bytes)
            
            num_channels = 3
            header_bytes = 256 + num_channels * 256
            
            f.write(str(header_bytes).ljust(8).encode('ascii'))  # Header bytes (8 bytes)
            f.write(b' '.ljust(44))  # Reserved (44 bytes)
            f.write(b'10      ')  # Number of data records (8 bytes)
            f.write(b'1       ')  # Duration of data record (8 bytes)
            f.write(str(num_channels).ljust(4).encode('ascii'))  # Number of channels (4 bytes)
            
            # Channel labels (16 bytes each)
            f.write(b'HR              ')  # Heart rate channel
            f.write(b'Movement        ')  # Movement channel
            f.write(b'Annotation      ')  # Annotation channel
            
            # Transducer types (80 bytes each)
            for _ in range(num_channels):
                f.write(b' '.ljust(80))
            
            # Physical dimensions (8 bytes each)
            f.write(b'bpm     ')  # Heart rate
            f.write(b'g       ')  # Movement (acceleration)
            f.write(b'        ')  # Annotation
            
            # Physical minimums (8 bytes each)
            f.write(b'30      ')  # HR min
            f.write(b'0       ')  # Movement min
            f.write(b'0       ')  # Annotation min
            
            # Physical maximums (8 bytes each)
            f.write(b'200     ')  # HR max
            f.write(b'10      ')  # Movement max
            f.write(b'10      ')  # Annotation max
            
            # Digital minimums (8 bytes each)
            f.write(b'-32768  ')
            f.write(b'-32768  ')
            f.write(b'-32768  ')
            
            # Digital maximums (8 bytes each)
            f.write(b'32767   ')
            f.write(b'32767   ')
            f.write(b'32767   ')
            
            # Prefiltering (80 bytes each)
            for _ in range(num_channels):
                f.write(b' '.ljust(80))
            
            # Number of samples per data record (8 bytes each)
            f.write(b'100     ')  # HR: 100 samples per second
            f.write(b'100     ')  # Movement: 100 samples per second
            f.write(b'1       ')  # Annotation: 1 sample per second
            
            # Reserved (32 bytes each)
            for _ in range(num_channels):
                f.write(b' '.ljust(32))
            
            # Write data records
            num_data_records = 10
            for _ in range(num_data_records):
                # HR channel: 100 samples (constant 70 bpm)
                hr_digital = int((70 - 30) / (200 - 30) * (32767 - (-32768)) + (-32768))
                for _ in range(100):
                    f.write(struct.pack('<h', hr_digital))
                
                # Movement channel: 100 samples (constant 1.0 g)
                mv_digital = int((1.0 - 0) / (10 - 0) * (32767 - (-32768)) + (-32768))
                for _ in range(100):
                    f.write(struct.pack('<h', mv_digital))
                
                # Annotation channel: 1 sample (stage 0 = awake)
                f.write(struct.pack('<h', 0))
    
    def test_load_sleep_edf_with_real_parser(self, loader):
        """Test loading Sleep-EDF with real EDF parser"""
        with tempfile.TemporaryDirectory() as tmpdir:
            edf_file = Path(tmpdir) / "test_sleep_edf.edf"
            self.create_minimal_edf_file(str(edf_file))
            
            dataset = loader.load_sleep_edf(str(edf_file))
            
            assert isinstance(dataset, Dataset)
            assert len(dataset.heart_rate.values) == 1000  # 10 records * 100 samples
            assert len(dataset.movement.values) == 1000
            assert len(dataset.sleep_stages.stages) == 1000  # aligned to HR/movement length
            assert len(dataset.subject_ids) > 0
    
    def test_load_mit_bih_with_real_parser(self, loader):
        """Test loading MIT-BIH with real EDF parser"""
        with tempfile.TemporaryDirectory() as tmpdir:
            edf_file = Path(tmpdir) / "test_mitbih.edf"
            self.create_minimal_edf_file(str(edf_file))
            
            dataset = loader.load_mit_bih(str(edf_file))
            
            assert isinstance(dataset, Dataset)
            assert len(dataset.heart_rate.values) == 1000
            assert len(dataset.movement.values) == 1000
            assert len(dataset.subject_ids) > 0
    
    def test_load_from_directory(self, loader):
        """Test loading dataset from directory containing EDF files"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create multiple EDF files
            edf_file1 = Path(tmpdir) / "subject1.edf"
            edf_file2 = Path(tmpdir) / "subject2.edf"
            
            self.create_minimal_edf_file(str(edf_file1))
            self.create_minimal_edf_file(str(edf_file2))
            
            # Should load the first file found
            dataset = loader.load_sleep_edf(tmpdir)
            
            assert isinstance(dataset, Dataset)
            assert len(dataset.heart_rate.values) > 0
    
    def test_metadata_extraction(self, loader):
        """Test that metadata is correctly extracted from EDF file"""
        with tempfile.TemporaryDirectory() as tmpdir:
            edf_file = Path(tmpdir) / "test_metadata.edf"
            self.create_minimal_edf_file(str(edf_file))
            
            dataset = loader.load_sleep_edf(str(edf_file))
            
            # Verify subject ID was extracted
            assert len(dataset.subject_ids) > 0
            assert dataset.subject_ids[0] is not None
            
            # Verify sampling rates
            assert dataset.heart_rate.sampling_rate > 0
            assert dataset.movement.sampling_rate > 0
    
    def test_error_handling_corrupted_file(self, loader):
        """Test error handling for corrupted EDF file"""
        with tempfile.TemporaryDirectory() as tmpdir:
            edf_file = Path(tmpdir) / "corrupted.edf"
            
            # Create a file with invalid content
            with open(edf_file, 'wb') as f:
                f.write(b'INVALID_EDF_CONTENT')
            
            with pytest.raises(DatasetLoadError):
                loader.load_sleep_edf(str(edf_file))
    
    def test_split_and_k_fold_with_real_data(self, loader):
        """Test train/test split and k-fold with real loaded data"""
        with tempfile.TemporaryDirectory() as tmpdir:
            edf_file = Path(tmpdir) / "test_split.edf"
            self.create_minimal_edf_file(str(edf_file))
            
            dataset = loader.load_sleep_edf(str(edf_file))
            
            # Test train/test split
            train_set, test_set = loader.split_train_test(dataset, test_ratio=0.2)
            
            assert len(train_set.dataset.heart_rate.values) > 0
            assert len(test_set.dataset.heart_rate.values) > 0
            
            # Test k-fold split
            folds = loader.k_fold_split(dataset, k=3)
            
            assert len(folds) == 3
            for train_set, test_set in folds:
                assert len(train_set.dataset.heart_rate.values) > 0
                assert len(test_set.dataset.heart_rate.values) > 0
