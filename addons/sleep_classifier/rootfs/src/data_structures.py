"""Core data structures for CNN-BiLSTM Sleep Algorithm"""
from dataclasses import dataclass
from enum import Enum
from typing import List, Dict, Tuple
from datetime import datetime
import numpy as np


class SleepStage(Enum):
    """Sleep stage enumeration"""
    AWAKE = 0      # 清醒
    LIGHT = 1      # 浅睡
    DEEP = 2       # 深睡
    REM = 3        # 快速眼动睡眠


@dataclass
class HeartRateData:
    """Heart rate data structure"""
    timestamps: np.ndarray  # Unix timestamps in seconds
    values: np.ndarray      # Heart rate values in bpm
    sampling_rate: int      # Sampling rate (default 100Hz)
    
    def __post_init__(self):
        assert len(self.timestamps) == len(self.values), \
            "Timestamps and values must have same length"
        assert self.sampling_rate > 0, \
            "Sampling rate must be positive"
        assert np.all((self.values >= 30) & (self.values <= 200)), \
            "Heart rate values must be in [30, 200] bpm range"


@dataclass
class MovementData:
    """Movement/accelerometer data structure"""
    timestamps: np.ndarray  # Unix timestamps in seconds
    values: np.ndarray      # Movement amplitude (accelerometer data)
    sampling_rate: int      # Sampling rate (default 100Hz)
    
    def __post_init__(self):
        assert len(self.timestamps) == len(self.values), \
            "Timestamps and values must have same length"
        assert self.sampling_rate > 0, \
            "Sampling rate must be positive"


@dataclass
class SleepStages:
    """Sleep stage annotation sequence"""
    timestamps: np.ndarray  # Timestamp array
    stages: np.ndarray      # Sleep stage array (SleepStage enum values)
    
    def __post_init__(self):
        assert len(self.timestamps) == len(self.stages), \
            "Timestamps and stages must have same length"
        assert np.all(np.isin(self.stages, [0, 1, 2, 3])), \
            "Sleep stages must be valid (0-3)"


@dataclass
class EDFHeader:
    """EDF file header information"""
    subject_id: str                    # Subject ID
    recording_date: datetime           # Recording date
    duration_seconds: float            # Recording duration in seconds
    num_channels: int                  # Number of channels
    channel_labels: List[str]          # Channel label list
    sampling_rates: Dict[str, int]     # Sampling rate per channel
    physical_units: Dict[str, str]     # Physical unit per channel


@dataclass
class TimeFrequencyMatrix:
    """Time-frequency matrix for CNN input"""
    matrix: np.ndarray  # Shape (1024, 128, 2) - time × frequency × dual-channel
    
    def __post_init__(self):
        assert self.matrix.shape == (1024, 128, 2), \
            f"Matrix shape must be (1024, 128, 2), got {self.matrix.shape}"


@dataclass
class Dataset:
    """Dataset containing dual-sensor data and annotations"""
    heart_rate: HeartRateData
    movement: MovementData
    sleep_stages: SleepStages
    subject_ids: List[str]  # Subject ID list
    
    def __post_init__(self):
        assert len(self.heart_rate.timestamps) == len(self.movement.timestamps), \
            "Heart rate and movement data must have same length"
        assert len(self.heart_rate.timestamps) == len(self.sleep_stages.timestamps), \
            "Heart rate and sleep stages must have same length"


@dataclass
class TrainingSet:
    """Training dataset with normalization parameters"""
    dataset: Dataset
    normalization_params: Dict[str, Tuple[float, float]]  # {'heart_rate': (mean, std), 'movement': (mean, std)}


@dataclass
class TestSet:
    """Test dataset"""
    dataset: Dataset


@dataclass
class MQTTMessage:
    """MQTT message structure"""
    topic: str
    payload: Dict  # JSON format message body
    timestamp: float
    qos: int  # QoS level (0, 1, 2)


@dataclass
class ModelWeights:
    """Model weights for persistence"""
    cnn_weights: np.ndarray
    bilstm_weights: np.ndarray
    classifier_weights: np.ndarray
    file_path: str  # HDF5 file path


@dataclass
class PerformanceMetrics:
    """Performance evaluation metrics"""
    accuracy: float                          # Overall accuracy
    precision_per_class: Dict[SleepStage, float]  # Precision per class
    recall_per_class: Dict[SleepStage, float]     # Recall per class
    f1_per_class: Dict[SleepStage, float]         # F1 score per class
    confusion_matrix: np.ndarray             # Confusion matrix (4×4)
    
    def __post_init__(self):
        assert 0.0 <= self.accuracy <= 1.0, \
            "Accuracy must be in [0, 1] range"
        for metric in [self.precision_per_class, self.recall_per_class, self.f1_per_class]:
            for value in metric.values():
                assert 0.0 <= value <= 1.0, \
                    "Metric values must be in [0, 1] range"
