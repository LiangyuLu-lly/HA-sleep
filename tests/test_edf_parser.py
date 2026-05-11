"""Unit tests for EDF_Parser class"""
import pytest
import numpy as np
import tempfile
import struct
from datetime import datetime
from pathlib import Path

from src.edf_parser import EDFParser, EDFParseError
from src.data_structures import HeartRateData, MovementData, SleepStages


class TestEDFParser:
    """Test suite for EDF_Parser"""
    
    def create_mock_edf_file(
        self,
        duration_seconds: float = 60.0,
        sampling_rate: int = 100,
        num_channels: int = 2
    ) -> str:
        """
        Create a mock EDF file for testing
        
        Args:
            duration_seconds: Recording duration in seconds
            sampling_rate: Sampling rate in Hz
            num_channels: Number of channels
            
        Returns:
            Path to temporary EDF file
        """
        # Create temporary file
        temp_file = tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.edf')
        
        # Calculate parameters
        duration_data_record = 1.0  # 1 second per data record
        num_data_records = int(duration_seconds / duration_data_record)
        num_samples_per_record = int(sampling_rate * duration_data_record)
        header_bytes = 256 + num_channels * 256
        
        # Write fixed header (256 bytes)
        temp_file.write(b'0       ')  # version (8 bytes)
        temp_file.write(b'Patient001' + b' ' * 70)  # patient ID (80 bytes)
        temp_file.write(b'Recording001' + b' ' * 68)  # recording ID (80 bytes)
        temp_file.write(b'01.01.23')  # date (8 bytes)
        temp_file.write(b'12.00.00')  # time (8 bytes)
        temp_file.write(f'{header_bytes:<8}'.encode('ascii'))  # header bytes (8 bytes)
        temp_file.write(b' ' * 44)  # reserved (44 bytes)
        temp_file.write(f'{num_data_records:<8}'.encode('ascii'))  # num data records (8 bytes)
        temp_file.write(f'{duration_data_record:<8}'.encode('ascii'))  # duration (8 bytes)
        temp_file.write(f'{num_channels:<4}'.encode('ascii'))  # num channels (4 bytes)
        
        # Channel labels
        channel_labels = ['HR', 'Movement'][:num_channels]
        for label in channel_labels:
            temp_file.write(f'{label:<16}'.encode('ascii'))
        
        # Transducer types
        for _ in range(num_channels):
            temp_file.write(b' ' * 80)
        
        # Physical dimensions
        physical_dims = ['bpm', 'g'][:num_channels]
        for dim in physical_dims:
            temp_file.write(f'{dim:<8}'.encode('ascii'))
        
        # Physical minimums
        phys_mins = [30.0, -10.0][:num_channels]
        for pmin in phys_mins:
            temp_file.write(f'{pmin:<8}'.encode('ascii'))
        
        # Physical maximums
        phys_maxs = [200.0, 10.0][:num_channels]
        for pmax in phys_maxs:
            temp_file.write(f'{pmax:<8}'.encode('ascii'))
        
        # Digital minimums
        dig_mins = [-32768, -32768][:num_channels]
        for dmin in dig_mins:
            temp_file.write(f'{dmin:<8}'.encode('ascii'))
        
        # Digital maximums
        dig_maxs = [32767, 32767][:num_channels]
        for dmax in dig_maxs:
            temp_file.write(f'{dmax:<8}'.encode('ascii'))
        
        # Prefiltering
        for _ in range(num_channels):
            temp_file.write(b' ' * 80)
        
        # Number of samples per record
        for _ in range(num_channels):
            temp_file.write(f'{num_samples_per_record:<8}'.encode('ascii'))
        
        # Reserved
        for _ in range(num_channels):
            temp_file.write(b' ' * 32)
        
        # Write signal data
        for record_idx in range(num_data_records):
            for ch_idx in range(num_channels):
                phys_min = phys_mins[ch_idx]
                phys_max = phys_maxs[ch_idx]
                dig_min = dig_mins[ch_idx]
                dig_max = dig_maxs[ch_idx]
                
                # Generate sample data
                for sample_idx in range(num_samples_per_record):
                    if ch_idx == 0:  # Heart rate channel
                        # Generate heart rate around 70 bpm with some variation
                        physical_value = 70.0 + 10.0 * np.sin(
                            2 * np.pi * (record_idx * num_samples_per_record + sample_idx) / (sampling_rate * 10)
                        )
                    else:  # Movement channel
                        # Generate movement data
                        physical_value = 0.5 * np.sin(
                            2 * np.pi * (record_idx * num_samples_per_record + sample_idx) / (sampling_rate * 5)
                        )
                    
                    # Convert physical to digital
                    gain = (phys_max - phys_min) / (dig_max - dig_min)
                    offset = phys_min - gain * dig_min
                    digital_value = int((physical_value - offset) / gain)
                    digital_value = max(dig_min, min(dig_max, digital_value))
                    
                    # Write as 16-bit signed integer
                    temp_file.write(struct.pack('<h', digital_value))
        
        temp_file.close()
        return temp_file.name
    
    def test_parse_header_valid_file(self):
        """Test parsing header from valid EDF file"""
        edf_file = self.create_mock_edf_file(duration_seconds=60.0, sampling_rate=100)
        
        try:
            parser = EDFParser()
            header = parser.parse_header(edf_file)
            
            assert header.subject_id == 'Patient001'
            assert header.duration_seconds == 60.0
            assert header.num_channels == 2
            assert 'HR' in header.channel_labels
            assert 'Movement' in header.channel_labels
            assert header.sampling_rates['HR'] == 100
            assert header.physical_units['HR'] == 'bpm'
        finally:
            Path(edf_file).unlink()
    
    def test_parse_header_file_not_found(self):
        """Test error handling for non-existent file"""
        parser = EDFParser()
        
        with pytest.raises(EDFParseError, match="EDF file not found"):
            parser.parse_header("nonexistent_file.edf")
    
    def test_extract_heart_rate_channel(self):
        """Test extracting heart rate channel"""
        edf_file = self.create_mock_edf_file(duration_seconds=10.0, sampling_rate=100)
        
        try:
            parser = EDFParser()
            hr_data = parser.extract_heart_rate_channel(edf_file)
            
            assert isinstance(hr_data, HeartRateData)
            assert len(hr_data.timestamps) == 1000  # 10 seconds * 100 Hz
            assert len(hr_data.values) == 1000
            assert hr_data.sampling_rate == 100
            assert np.all((hr_data.values >= 30) & (hr_data.values <= 200))
        finally:
            Path(edf_file).unlink()
    
    def test_extract_movement_channel(self):
        """Test extracting movement channel"""
        edf_file = self.create_mock_edf_file(duration_seconds=10.0, sampling_rate=100)
        
        try:
            parser = EDFParser()
            mv_data = parser.extract_movement_channel(edf_file)
            
            assert isinstance(mv_data, MovementData)
            assert len(mv_data.timestamps) == 1000  # 10 seconds * 100 Hz
            assert len(mv_data.values) == 1000
            assert mv_data.sampling_rate == 100
        finally:
            Path(edf_file).unlink()
    
    def test_extract_heart_rate_channel_not_found(self):
        """Test error when heart rate channel not found"""
        # Create EDF file with different channel names
        edf_file = self.create_mock_edf_file()
        
        try:
            parser = EDFParser()
            parser.parse_header(edf_file)
            
            # Manually modify channel labels to not include heart rate
            parser._header.channel_labels = ['Channel1', 'Channel2']
            
            with pytest.raises(EDFParseError, match="Heart rate channel not found"):
                parser.extract_heart_rate_channel(edf_file)
        finally:
            Path(edf_file).unlink()
    
    def test_extract_movement_channel_not_found(self):
        """Test error when movement channel not found"""
        edf_file = self.create_mock_edf_file()
        
        try:
            parser = EDFParser()
            parser.parse_header(edf_file)
            
            # Manually modify channel labels to not include movement
            parser._header.channel_labels = ['Channel1', 'Channel2']
            
            with pytest.raises(EDFParseError, match="Movement channel not found"):
                parser.extract_movement_channel(edf_file)
        finally:
            Path(edf_file).unlink()
    
    def test_data_length_consistency(self):
        """Test that extracted data length matches expected length"""
        duration = 30.0
        sampling_rate = 100
        edf_file = self.create_mock_edf_file(
            duration_seconds=duration,
            sampling_rate=sampling_rate
        )
        
        try:
            parser = EDFParser()
            hr_data = parser.extract_heart_rate_channel(edf_file)
            mv_data = parser.extract_movement_channel(edf_file)
            
            expected_length = int(duration * sampling_rate)
            
            assert len(hr_data.values) == expected_length
            assert len(mv_data.values) == expected_length
            assert len(hr_data.timestamps) == expected_length
            assert len(mv_data.timestamps) == expected_length
        finally:
            Path(edf_file).unlink()
    
    def test_digital_to_physical_conversion(self):
        """Test digital to physical value conversion"""
        parser = EDFParser()
        
        digital_values = [0, 16384, 32767, -16384, -32768]
        phys_min = 30.0
        phys_max = 200.0
        dig_min = -32768
        dig_max = 32767
        
        physical_values = parser._digital_to_physical(
            digital_values, phys_min, phys_max, dig_min, dig_max
        )
        
        # Check that conversion is within expected range
        assert all(phys_min <= val <= phys_max for val in physical_values)
        
        # Check specific conversions
        # Digital 0 should map to middle of physical range
        mid_physical = (phys_min + phys_max) / 2
        assert abs(physical_values[0] - mid_physical) < 1.0
    
    def test_extract_sleep_annotations_not_found(self):
        """Test error when sleep annotations not found"""
        edf_file = self.create_mock_edf_file()
        
        try:
            parser = EDFParser()
            
            with pytest.raises(EDFParseError, match="Sleep stage annotations not found"):
                parser.extract_sleep_annotations(edf_file)
        finally:
            Path(edf_file).unlink()
    
    def test_multiple_extractions_reuse_data(self):
        """Test that multiple extractions reuse loaded signal data"""
        edf_file = self.create_mock_edf_file(duration_seconds=5.0, sampling_rate=100)
        
        try:
            parser = EDFParser()
            
            # First extraction should load data
            hr_data1 = parser.extract_heart_rate_channel(edf_file)
            
            # Second extraction should reuse loaded data
            mv_data = parser.extract_movement_channel(edf_file)
            
            # Third extraction should also reuse data
            hr_data2 = parser.extract_heart_rate_channel(edf_file)
            
            # Verify data is consistent
            assert np.array_equal(hr_data1.values, hr_data2.values)
            assert len(hr_data1.values) == len(mv_data.values)
        finally:
            Path(edf_file).unlink()


class TestEDFParserProperties:
    """Property-based tests for EDF_Parser"""
    
    def create_mock_edf_file_with_params(
        self,
        duration_seconds: float,
        sampling_rate: int,
        num_channels: int = 2
    ) -> str:
        """
        Create a mock EDF file with specific parameters for property testing
        
        Args:
            duration_seconds: Recording duration in seconds
            sampling_rate: Sampling rate in Hz
            num_channels: Number of channels
            
        Returns:
            Path to temporary EDF file
        """
        # Create temporary file
        temp_file = tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.edf')
        
        # Calculate parameters
        duration_data_record = 1.0  # 1 second per data record
        num_data_records = int(duration_seconds / duration_data_record)
        num_samples_per_record = int(sampling_rate * duration_data_record)
        header_bytes = 256 + num_channels * 256
        
        # Write fixed header (256 bytes)
        temp_file.write(b'0       ')  # version (8 bytes)
        temp_file.write(b'Patient001' + b' ' * 70)  # patient ID (80 bytes)
        temp_file.write(b'Recording001' + b' ' * 68)  # recording ID (80 bytes)
        temp_file.write(b'01.01.23')  # date (8 bytes)
        temp_file.write(b'12.00.00')  # time (8 bytes)
        temp_file.write(f'{header_bytes:<8}'.encode('ascii'))  # header bytes (8 bytes)
        temp_file.write(b' ' * 44)  # reserved (44 bytes)
        temp_file.write(f'{num_data_records:<8}'.encode('ascii'))  # num data records (8 bytes)
        temp_file.write(f'{duration_data_record:<8}'.encode('ascii'))  # duration (8 bytes)
        temp_file.write(f'{num_channels:<4}'.encode('ascii'))  # num channels (4 bytes)
        
        # Channel labels
        channel_labels = ['HR', 'Movement'][:num_channels]
        for label in channel_labels:
            temp_file.write(f'{label:<16}'.encode('ascii'))
        
        # Transducer types
        for _ in range(num_channels):
            temp_file.write(b' ' * 80)
        
        # Physical dimensions
        physical_dims = ['bpm', 'g'][:num_channels]
        for dim in physical_dims:
            temp_file.write(f'{dim:<8}'.encode('ascii'))
        
        # Physical minimums
        phys_mins = [30.0, -10.0][:num_channels]
        for pmin in phys_mins:
            temp_file.write(f'{pmin:<8}'.encode('ascii'))
        
        # Physical maximums
        phys_maxs = [200.0, 10.0][:num_channels]
        for pmax in phys_maxs:
            temp_file.write(f'{pmax:<8}'.encode('ascii'))
        
        # Digital minimums
        dig_mins = [-32768, -32768][:num_channels]
        for dmin in dig_mins:
            temp_file.write(f'{dmin:<8}'.encode('ascii'))
        
        # Digital maximums
        dig_maxs = [32767, 32767][:num_channels]
        for dmax in dig_maxs:
            temp_file.write(f'{dmax:<8}'.encode('ascii'))
        
        # Prefiltering
        for _ in range(num_channels):
            temp_file.write(b' ' * 80)
        
        # Number of samples per record
        for _ in range(num_channels):
            temp_file.write(f'{num_samples_per_record:<8}'.encode('ascii'))
        
        # Reserved
        for _ in range(num_channels):
            temp_file.write(b' ' * 32)
        
        # Write signal data
        for record_idx in range(num_data_records):
            for ch_idx in range(num_channels):
                phys_min = phys_mins[ch_idx]
                phys_max = phys_maxs[ch_idx]
                dig_min = dig_mins[ch_idx]
                dig_max = dig_maxs[ch_idx]
                
                # Generate sample data
                for sample_idx in range(num_samples_per_record):
                    if ch_idx == 0:  # Heart rate channel
                        # Generate heart rate around 70 bpm with some variation
                        physical_value = 70.0 + 10.0 * np.sin(
                            2 * np.pi * (record_idx * num_samples_per_record + sample_idx) / (sampling_rate * 10)
                        )
                    else:  # Movement channel
                        # Generate movement data
                        physical_value = 0.5 * np.sin(
                            2 * np.pi * (record_idx * num_samples_per_record + sample_idx) / (sampling_rate * 5)
                        )
                    
                    # Convert physical to digital
                    gain = (phys_max - phys_min) / (dig_max - dig_min)
                    offset = phys_min - gain * dig_min
                    digital_value = int((physical_value - offset) / gain)
                    digital_value = max(dig_min, min(dig_max, digital_value))
                    
                    # Write as 16-bit signed integer
                    temp_file.write(struct.pack('<h', digital_value))
        
        temp_file.close()
        return temp_file.name
    
    # Feature: cnn-bilstm-sleep-algorithm, Property 2: EDF parsing data length consistency
    def test_edf_parsing_data_length_consistency_property(self):
        """
        **Validates: Requirements 2.9, 2.10**
        
        Property: For any valid EDF file, parsed heart rate and movement data length 
        should equal (recording duration × sampling rate)
        
        This property test generates multiple EDF files with varying durations and 
        sampling rates to verify the data length consistency invariant holds across 
        all valid inputs.
        """
        from hypothesis import given, strategies as st, settings
        
        @given(
            duration=st.floats(min_value=10.0, max_value=300.0),  # 10 seconds to 5 minutes
            sampling_rate=st.integers(min_value=50, max_value=200)  # 50Hz to 200Hz
        )
        @settings(max_examples=100, deadline=None)
        def property_test(duration, sampling_rate):
            """Property: data_length = duration × sampling_rate"""
            # Create mock EDF file with specified parameters
            edf_file = self.create_mock_edf_file_with_params(
                duration_seconds=duration,
                sampling_rate=sampling_rate
            )
            
            try:
                # Parse the EDF file
                parser = EDFParser()
                header = parser.parse_header(edf_file)
                hr_data = parser.extract_heart_rate_channel(edf_file)
                mv_data = parser.extract_movement_channel(edf_file)
                
                # Calculate expected length based on the ACTUAL duration stored in the EDF file
                # (not the input parameter, since EDF files store integer number of data records)
                actual_duration = header.duration_seconds
                expected_length = int(actual_duration * sampling_rate)
                
                # Verify property: data length = duration × sampling rate
                assert len(hr_data.values) == expected_length, \
                    f"Heart rate data length mismatch: expected {expected_length}, got {len(hr_data.values)} (actual duration: {actual_duration}s)"
                assert len(mv_data.values) == expected_length, \
                    f"Movement data length mismatch: expected {expected_length}, got {len(mv_data.values)} (actual duration: {actual_duration}s)"
                assert len(hr_data.timestamps) == expected_length, \
                    f"Heart rate timestamps length mismatch: expected {expected_length}, got {len(hr_data.timestamps)} (actual duration: {actual_duration}s)"
                assert len(mv_data.timestamps) == expected_length, \
                    f"Movement timestamps length mismatch: expected {expected_length}, got {len(mv_data.timestamps)} (actual duration: {actual_duration}s)"
                
                # Verify sampling rates are correctly extracted
                assert hr_data.sampling_rate == sampling_rate, \
                    f"Heart rate sampling rate mismatch: expected {sampling_rate}, got {hr_data.sampling_rate}"
                assert mv_data.sampling_rate == sampling_rate, \
                    f"Movement sampling rate mismatch: expected {sampling_rate}, got {mv_data.sampling_rate}"
                
            finally:
                # Clean up temporary file
                Path(edf_file).unlink()
        
        # Run the property test
        property_test()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
