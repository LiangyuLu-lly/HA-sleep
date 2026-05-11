"""Integration tests for EDF_Parser demonstrating complete workflow"""
import pytest
import numpy as np
import tempfile
import struct
from pathlib import Path

from src.edf_parser import EDFParser, EDFParseError
from src.data_structures import SleepStage


class TestEDFParserIntegration:
    """Integration tests for complete EDF parsing workflow"""
    
    def create_complete_edf_file(self) -> str:
        """Create a complete mock EDF file with all required channels"""
        temp_file = tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.edf')
        
        # Parameters
        duration_seconds = 120.0  # 2 minutes
        sampling_rate = 100
        duration_data_record = 1.0
        num_data_records = int(duration_seconds / duration_data_record)
        num_samples_per_record = int(sampling_rate * duration_data_record)
        num_channels = 2
        header_bytes = 256 + num_channels * 256
        
        # Write fixed header
        temp_file.write(b'0       ')
        temp_file.write(b'TestPatient' + b' ' * 69)
        temp_file.write(b'TestRecording' + b' ' * 67)
        temp_file.write(b'15.03.23')
        temp_file.write(b'14.30.00')
        temp_file.write(f'{header_bytes:<8}'.encode('ascii'))
        temp_file.write(b' ' * 44)
        temp_file.write(f'{num_data_records:<8}'.encode('ascii'))
        temp_file.write(f'{duration_data_record:<8}'.encode('ascii'))
        temp_file.write(f'{num_channels:<4}'.encode('ascii'))
        
        # Channel labels
        for label in ['ECG', 'Accel']:
            temp_file.write(f'{label:<16}'.encode('ascii'))
        
        # Transducer types
        for _ in range(num_channels):
            temp_file.write(b' ' * 80)
        
        # Physical dimensions
        for dim in ['bpm', 'g']:
            temp_file.write(f'{dim:<8}'.encode('ascii'))
        
        # Physical min/max
        phys_params = [(30.0, 200.0), (-5.0, 5.0)]
        for pmin, pmax in phys_params:
            temp_file.write(f'{pmin:<8}'.encode('ascii'))
        for pmin, pmax in phys_params:
            temp_file.write(f'{pmax:<8}'.encode('ascii'))
        
        # Digital min/max
        for _ in range(num_channels):
            temp_file.write(f'{-32768:<8}'.encode('ascii'))
        for _ in range(num_channels):
            temp_file.write(f'{32767:<8}'.encode('ascii'))
        
        # Prefiltering
        for _ in range(num_channels):
            temp_file.write(b' ' * 80)
        
        # Samples per record
        for _ in range(num_channels):
            temp_file.write(f'{num_samples_per_record:<8}'.encode('ascii'))
        
        # Reserved
        for _ in range(num_channels):
            temp_file.write(b' ' * 32)
        
        # Write signal data
        for record_idx in range(num_data_records):
            for ch_idx in range(num_channels):
                phys_min, phys_max = phys_params[ch_idx]
                dig_min, dig_max = -32768, 32767
                
                for sample_idx in range(num_samples_per_record):
                    t = (record_idx * num_samples_per_record + sample_idx) / sampling_rate
                    
                    if ch_idx == 0:  # Heart rate
                        # Simulate varying heart rate: 60-80 bpm
                        physical_value = 70.0 + 10.0 * np.sin(2 * np.pi * t / 30.0)
                    else:  # Movement
                        # Simulate periodic movement
                        physical_value = 1.0 * np.sin(2 * np.pi * t / 10.0)
                    
                    # Convert to digital
                    gain = (phys_max - phys_min) / (dig_max - dig_min)
                    offset = phys_min - gain * dig_min
                    digital_value = int((physical_value - offset) / gain)
                    digital_value = max(dig_min, min(dig_max, digital_value))
                    
                    temp_file.write(struct.pack('<h', digital_value))
        
        temp_file.close()
        return temp_file.name
    
    def test_complete_parsing_workflow(self):
        """Test complete workflow: parse header, extract all channels"""
        edf_file = self.create_complete_edf_file()
        
        try:
            parser = EDFParser()
            
            # Step 1: Parse header
            header = parser.parse_header(edf_file)
            assert header is not None
            assert header.duration_seconds == 120.0
            assert header.num_channels == 2
            
            # Step 2: Extract heart rate
            hr_data = parser.extract_heart_rate_channel(edf_file)
            assert len(hr_data.values) == 12000  # 120 seconds * 100 Hz
            assert hr_data.sampling_rate == 100
            assert np.all((hr_data.values >= 30) & (hr_data.values <= 200))
            
            # Step 3: Extract movement
            mv_data = parser.extract_movement_channel(edf_file)
            assert len(mv_data.values) == 12000
            assert mv_data.sampling_rate == 100
            
            # Step 4: Verify data consistency
            assert len(hr_data.timestamps) == len(mv_data.timestamps)
            
            # Step 5: Verify timestamps are sequential
            assert np.all(np.diff(hr_data.timestamps) > 0)
            assert np.all(np.diff(mv_data.timestamps) > 0)
            
            print(f"✓ Successfully parsed EDF file with {header.num_channels} channels")
            print(f"✓ Heart rate: {len(hr_data.values)} samples, range [{hr_data.values.min():.1f}, {hr_data.values.max():.1f}] bpm")
            print(f"✓ Movement: {len(mv_data.values)} samples, range [{mv_data.values.min():.2f}, {mv_data.values.max():.2f}] g")
            
        finally:
            Path(edf_file).unlink()
    
    def test_parser_reuses_loaded_data(self):
        """Test that parser efficiently reuses loaded signal data"""
        edf_file = self.create_complete_edf_file()
        
        try:
            parser = EDFParser()
            
            # First call loads data
            hr_data1 = parser.extract_heart_rate_channel(edf_file)
            
            # Verify internal data is cached
            assert len(parser._signals_data) > 0
            
            # Second call should reuse cached data
            mv_data = parser.extract_movement_channel(edf_file)
            
            # Third call should also reuse cached data
            hr_data2 = parser.extract_heart_rate_channel(edf_file)
            
            # Verify consistency
            assert np.array_equal(hr_data1.values, hr_data2.values)
            assert np.array_equal(hr_data1.timestamps, hr_data2.timestamps)
            
        finally:
            Path(edf_file).unlink()
    
    def test_error_handling_corrupted_file(self):
        """Test error handling for corrupted EDF file"""
        # Create a file with invalid content
        temp_file = tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.edf')
        temp_file.write(b'Invalid EDF content')
        temp_file.close()
        
        try:
            parser = EDFParser()
            
            with pytest.raises(EDFParseError):
                parser.parse_header(temp_file.name)
                
        finally:
            Path(temp_file.name).unlink()
    
    def test_channel_finding_case_insensitive(self):
        """Test that channel finding is case-insensitive"""
        edf_file = self.create_complete_edf_file()
        
        try:
            parser = EDFParser()
            parser.parse_header(edf_file)
            
            # Should find 'ECG' channel with 'ecg' keyword
            hr_label = parser._find_channel(['ecg', 'heart'])
            assert hr_label == 'ECG'
            
            # Should find 'Accel' channel with 'accel' keyword
            mv_label = parser._find_channel(['accel', 'movement'])
            assert mv_label == 'Accel'
            
        finally:
            Path(edf_file).unlink()


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
