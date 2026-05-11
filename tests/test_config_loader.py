"""Unit tests for configuration loader"""
import pytest
import json
import tempfile
import os
from training_config.config_loader import (
    load_config, validate_config, get_default_config, ConfigurationError
)


def test_load_default_config():
    """Test loading default configuration"""
    config = get_default_config()
    
    assert config['model']['cnn']['num_filters'] == [32, 64]
    assert config['model']['bilstm']['hidden_units'] == 128
    assert config['model']['classifier']['num_classes'] == 4
    assert config['mqtt']['broker_port'] == 1883


def test_validate_valid_config():
    """Test validation of valid configuration"""
    config = get_default_config()
    validate_config(config)  # Should not raise


def test_validate_invalid_cnn_filters():
    """Test validation with invalid CNN filter count"""
    config = get_default_config()
    config['model']['cnn']['num_filters'][0] = -1
    
    with pytest.raises(ConfigurationError):
        validate_config(config)


def test_validate_invalid_bilstm_units():
    """Test validation with invalid BiLSTM hidden units"""
    config = get_default_config()
    config['model']['bilstm']['hidden_units'] = 0
    
    with pytest.raises(ConfigurationError):
        validate_config(config)


def test_validate_invalid_num_classes():
    """Test validation with invalid number of classes"""
    config = get_default_config()
    config['model']['classifier']['num_classes'] = 5
    
    with pytest.raises(ConfigurationError):
        validate_config(config)


def test_validate_invalid_mqtt_port():
    """Test validation with invalid MQTT port"""
    config = get_default_config()
    config['mqtt']['broker_port'] = 70000
    
    with pytest.raises(ConfigurationError):
        validate_config(config)


def test_load_config_from_file():
    """Test loading configuration from file"""
    config_data = get_default_config()
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(config_data, f)
        temp_path = f.name
    
    try:
        loaded_config = load_config(temp_path)
        assert loaded_config['model']['cnn']['num_filters'] == [32, 64]
        assert loaded_config['model']['bilstm']['hidden_units'] == 128
    finally:
        os.unlink(temp_path)


def test_load_config_nonexistent_file():
    """Test loading configuration from nonexistent file"""
    config = load_config("nonexistent_config.json")
    
    # Should return default config
    assert config['model']['cnn']['num_filters'] == [32, 64]


def test_load_config_invalid_json():
    """Test loading configuration from invalid JSON file"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write("{ invalid json }")
        temp_path = f.name
    
    try:
        config = load_config(temp_path)
        # Should return default config
        assert config['model']['cnn']['num_filters'] == [32, 64]
    finally:
        os.unlink(temp_path)


def test_load_config_invalid_parameters():
    """Test loading configuration with invalid parameters"""
    config_data = get_default_config()
    config_data['model']['cnn']['num_filters'][0] = -1  # Invalid
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(config_data, f)
        temp_path = f.name
    
    try:
        config = load_config(temp_path)
        # Should return default config due to validation failure
        assert config['model']['cnn']['num_filters'] == [32, 64]
    finally:
        os.unlink(temp_path)
