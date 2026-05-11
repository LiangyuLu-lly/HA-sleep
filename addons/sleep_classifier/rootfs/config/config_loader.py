"""Configuration file loader with validation"""
import json
import os
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Configuration validation error"""
    pass


def get_default_config() -> Dict[str, Any]:
    """Get default configuration"""
    return {
        "data_processing": {
            "normalization": {
                "method": "z-score"
            },
            "wavelet_denoising": {
                "wavelet_type": "db5",
                "decomposition_level": 5
            },
            "movement_filter": {
                "enabled": True,
                "cutoff_frequency": 10.0
            }
        },
        "model": {
            "cnn": {
                "num_conv_layers": 2,
                "kernel_size": [3, 3],
                "num_filters": [32, 64],
                "pool_size": [2, 2]
            },
            "bilstm": {
                "hidden_units": 128,
                "memory_window_seconds": 1800
            },
            "classifier": {
                "num_classes": 4
            }
        },
        "mqtt": {
            "broker_address": "localhost",
            "broker_port": 1883,
            "username": "",
            "password": "",
            "topics": {
                "heart_rate": "sensors/heart_rate",
                "movement": "sensors/movement",
                "sleep_stage": "sleep/stage",
                "lighting_control": "control/lighting",
                "temperature_control": "control/temperature",
                "humidity_control": "control/humidity",
                "smoke_sensor": "sensors/smoke",
                "gas_sensor": "sensors/gas",
                "smoke_alert": "alert/smoke",
                "gas_alert": "alert/gas",
                "sensor_fault": "system/sensor_fault"
            }
        },
        "home_assistant": {
            "enabled": True,
            "discovery_prefix": "homeassistant",
            "device_id": "sleep_classifier_bedroom",
            "device_name": "Bedroom Sleep Classifier",
            "device_manufacturer": "CNN-BiLSTM Sleep Project",
            "device_model": "CNN-BiLSTM-v1",
            "device_sw_version": "1.0.0",
            "state_topic": "sleep_classifier/state",
            "availability_topic": "sleep_classifier/availability",
            "publish_interval_seconds": 30,
            "expire_after_seconds": 120,
            "api": {
                "base_url": "http://homeassistant.local:8123",
                "access_token": "",
                "verify_ssl": True,
                "area_filter": "bedroom",
                "heart_rate_keywords": ["heart_rate", "hr", "heartrate", "pulse"],
                "movement_keywords": ["movement", "motion", "activity", "accel"],
                "temperature_keywords": ["temperature", "temp"],
                "humidity_keywords": ["humidity"],
                "illuminance_keywords": ["illuminance", "lux"],
                "controllable_domains": [
                    "light", "climate", "fan", "humidifier",
                    "switch", "media_player",
                ],
            },
            "preference_learner": {
                "enabled": True,
                "history_path": "data/user_preferences.json",
                "min_sessions_for_personalisation": 3,
                "quality_quantile": 0.7,
                "max_sessions_kept": 60,
                "exploration_rate": 0.1,
            },
            "smart_control": {
                "enabled": True,
                "min_seconds_between_actions": 120,
                "deadband_temperature_c": 0.5,
                "deadband_humidity_pct": 5,
                "deadband_brightness_pct": 10,
                "dry_run": False,
            },
        },
        "disaster_monitoring": {
            "smoke_threshold": 100.0,
            "gas_threshold": 50.0
        },
        "training": {
            "batch_size": 32,
            "epochs": 100,
            "learning_rate": 0.001,
            "early_stopping_patience": 5,
            "k_fold": 5
        }
    }


def validate_config(config: Dict[str, Any]) -> None:
    """
    Validate configuration parameters
    
    Args:
        config: Configuration dictionary
        
    Raises:
        ConfigurationError: If configuration is invalid
    """
    # Validate CNN parameters
    if config['model']['cnn']['num_filters'][0] <= 0:
        raise ConfigurationError("CNN filter count must be positive")
    
    if config['model']['cnn']['num_conv_layers'] <= 0:
        raise ConfigurationError("CNN layer count must be positive")
    
    # Validate BiLSTM parameters
    if config['model']['bilstm']['hidden_units'] <= 0:
        raise ConfigurationError("BiLSTM hidden units must be positive")
    
    if config['model']['bilstm']['memory_window_seconds'] <= 0:
        raise ConfigurationError("BiLSTM memory window must be positive")
    
    # Validate classifier parameters
    if config['model']['classifier']['num_classes'] != 4:
        raise ConfigurationError("Classifier must have 4 classes")
    
    # Validate wavelet denoising parameters
    if config['data_processing']['wavelet_denoising']['decomposition_level'] <= 0:
        raise ConfigurationError("Wavelet decomposition level must be positive")
    
    # Validate movement filter parameters
    if config['data_processing']['movement_filter']['cutoff_frequency'] <= 0:
        raise ConfigurationError("Filter cutoff frequency must be positive")
    
    # Validate MQTT parameters
    if config['mqtt']['broker_port'] <= 0 or config['mqtt']['broker_port'] > 65535:
        raise ConfigurationError("MQTT broker port must be in range [1, 65535]")
    
    # Validate disaster monitoring thresholds
    if config['disaster_monitoring']['smoke_threshold'] <= 0:
        raise ConfigurationError("Smoke threshold must be positive")
    
    if config['disaster_monitoring']['gas_threshold'] <= 0:
        raise ConfigurationError("Gas threshold must be positive")
    
    # Validate training parameters
    if config['training']['batch_size'] <= 0:
        raise ConfigurationError("Batch size must be positive")
    
    if config['training']['epochs'] <= 0:
        raise ConfigurationError("Epochs must be positive")
    
    if config['training']['learning_rate'] <= 0:
        raise ConfigurationError("Learning rate must be positive")
    
    if config['training']['early_stopping_patience'] <= 0:
        raise ConfigurationError("Early stopping patience must be positive")
    
    if config['training']['k_fold'] < 2:
        raise ConfigurationError("K-fold must be at least 2")


def load_config(config_path: str = "config/config.json") -> Dict[str, Any]:
    """
    Load configuration file with validation
    
    Args:
        config_path: Path to configuration file
        
    Returns:
        Configuration dictionary
    """
    try:
        if not os.path.exists(config_path):
            logger.warning(f"Configuration file not found: {config_path}, using default config")
            return get_default_config()
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        validate_config(config)
        logger.info(f"Configuration loaded successfully from {config_path}")
        return config
        
    except json.JSONDecodeError as e:
        logger.error(f"Configuration file JSON parse error, using default config: {e}")
        return get_default_config()
    
    except ConfigurationError as e:
        logger.error(f"Configuration validation failed, using default config: {e}")
        return get_default_config()
    
    except Exception as e:
        logger.error(f"Configuration file load failed, using default config: {e}")
        return get_default_config()
