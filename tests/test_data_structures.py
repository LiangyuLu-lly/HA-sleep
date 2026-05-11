"""Unit tests for core data structures"""
import pytest
import numpy as np
from datetime import datetime
from src.data_structures import (
    HeartRateData, MovementData, SleepStage, SleepStages,
    EDFHeader, TimeFrequencyMatrix, Dataset, TrainingSet, TestSet,
    MQTTMessage, ModelWeights, PerformanceMetrics
)


def test_heart_rate_data_valid():
    """Test valid heart rate data creation"""
    timestamps = np.array([0.0, 0.01, 0.02, 0.03])
    values = np.array([70.0, 72.0, 71.0, 73.0])
    hr_data = HeartRateData(timestamps, values, 100)
    
    assert len(hr_data.timestamps) == 4
    assert len(hr_data.values) == 4
    assert hr_data.sampling_rate == 100


def test_heart_rate_data_invalid_range():
    """Test heart rate data with out-of-range values"""
    timestamps = np.array([0.0, 0.01, 0.02])
    values = np.array([250.0, 72.0, 71.0])  # 250 is out of range
    
    with pytest.raises(AssertionError):
        HeartRateData(timestamps, values, 100)


def test_movement_data_valid():
    """Test valid movement data creation"""
    timestamps = np.array([0.0, 0.01, 0.02, 0.03])
    values = np.array([0.1, 0.2, 0.15, 0.3])
    mv_data = MovementData(timestamps, values, 100)
    
    assert len(mv_data.timestamps) == 4
    assert len(mv_data.values) == 4
    assert mv_data.sampling_rate == 100


def test_sleep_stages_valid():
    """Test valid sleep stages creation"""
    timestamps = np.array([0.0, 30.0, 60.0, 90.0])
    stages = np.array([0, 1, 2, 3])  # AWAKE, LIGHT, DEEP, REM
    sleep_stages = SleepStages(timestamps, stages)
    
    assert len(sleep_stages.timestamps) == 4
    assert len(sleep_stages.stages) == 4


def test_sleep_stages_invalid():
    """Test sleep stages with invalid stage values"""
    timestamps = np.array([0.0, 30.0, 60.0])
    stages = np.array([0, 1, 5])  # 5 is invalid
    
    with pytest.raises(AssertionError):
        SleepStages(timestamps, stages)


def test_edf_header():
    """Test EDF header creation"""
    header = EDFHeader(
        subject_id="S001",
        recording_date=datetime(2023, 1, 1, 0, 0, 0),
        duration_seconds=28800.0,
        num_channels=2,
        channel_labels=["ECG", "Accelerometer"],
        sampling_rates={"ECG": 100, "Accelerometer": 100},
        physical_units={"ECG": "mV", "Accelerometer": "g"}
    )
    
    assert header.subject_id == "S001"
    assert header.num_channels == 2
    assert header.duration_seconds == 28800.0


def test_time_frequency_matrix_valid():
    """Test valid time-frequency matrix creation"""
    matrix = np.random.randn(1024, 128, 2)
    tf_matrix = TimeFrequencyMatrix(matrix)
    
    assert tf_matrix.matrix.shape == (1024, 128, 2)


def test_time_frequency_matrix_invalid_shape():
    """Test time-frequency matrix with invalid shape"""
    matrix = np.random.randn(512, 64, 2)  # Wrong shape
    
    with pytest.raises(AssertionError):
        TimeFrequencyMatrix(matrix)


def test_dataset_valid():
    """Test valid dataset creation"""
    timestamps = np.array([0.0, 0.01, 0.02, 0.03])
    hr_data = HeartRateData(timestamps, np.array([70.0, 72.0, 71.0, 73.0]), 100)
    mv_data = MovementData(timestamps, np.array([0.1, 0.2, 0.15, 0.3]), 100)
    sleep_stages = SleepStages(timestamps, np.array([0, 0, 1, 1]))
    
    dataset = Dataset(hr_data, mv_data, sleep_stages, ["S001"])
    
    assert len(dataset.heart_rate.timestamps) == 4
    assert len(dataset.movement.timestamps) == 4
    assert len(dataset.sleep_stages.timestamps) == 4


def test_performance_metrics_valid():
    """Test valid performance metrics creation"""
    metrics = PerformanceMetrics(
        accuracy=0.85,
        precision_per_class={
            SleepStage.AWAKE: 0.90,
            SleepStage.LIGHT: 0.85,
            SleepStage.DEEP: 0.80,
            SleepStage.REM: 0.75
        },
        recall_per_class={
            SleepStage.AWAKE: 0.88,
            SleepStage.LIGHT: 0.82,
            SleepStage.DEEP: 0.85,
            SleepStage.REM: 0.78
        },
        f1_per_class={
            SleepStage.AWAKE: 0.89,
            SleepStage.LIGHT: 0.83,
            SleepStage.DEEP: 0.82,
            SleepStage.REM: 0.76
        },
        confusion_matrix=np.array([[10, 1, 0, 0],
                                   [1, 8, 1, 0],
                                   [0, 1, 9, 0],
                                   [0, 0, 1, 7]])
    )
    
    assert metrics.accuracy == 0.85
    assert 0.0 <= metrics.accuracy <= 1.0


def test_performance_metrics_invalid_accuracy():
    """Test performance metrics with invalid accuracy"""
    with pytest.raises(AssertionError):
        PerformanceMetrics(
            accuracy=1.5,  # Invalid: > 1.0
            precision_per_class={SleepStage.AWAKE: 0.90},
            recall_per_class={SleepStage.AWAKE: 0.88},
            f1_per_class={SleepStage.AWAKE: 0.89},
            confusion_matrix=np.array([[10]])
        )


def test_mqtt_message():
    """Test MQTT message creation"""
    message = MQTTMessage(
        topic="sensors/heart_rate",
        payload={"device_id": "hr_001", "heart_rate": 72.0},
        timestamp=1678901234.567,
        qos=1
    )
    
    assert message.topic == "sensors/heart_rate"
    assert message.qos == 1
    assert message.payload["heart_rate"] == 72.0
