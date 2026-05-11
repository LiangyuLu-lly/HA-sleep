# Implementation Plan: CNN-BiLSTM Sleep Algorithm

## Overview

This implementation plan covers the development of a CNN-BiLSTM based sleep stage classification system for smart home environments. The system processes dual-sensor data (heart rate and movement) from public datasets (Sleep-EDF, MIT-BIH), performs real-time sleep stage classification, and integrates with MQTT for smart home control and disaster alerting.

The implementation follows a modular architecture with clear separation between data loading, preprocessing, feature extraction, classification, and MQTT communication layers.

## Tasks

- [x] 1. Project setup and core data structures
  - Create project directory structure (src/, tests/, config/, data/, models/)
  - Set up Python virtual environment and install dependencies (TensorFlow/Keras, PyWavelets, SciPy, Paho MQTT, h5py, numpy, hypothesis for testing)
  - Define core data classes: HeartRateData, MovementData, SleepStage, SleepStages, EDFHeader, TimeFrequencyMatrix, Dataset, TrainingSet, TestSet
  - Create configuration file loader (config.json) with validation
  - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8, 15.9_

- [x] 2. EDF file parsing and dataset loading
  - [x] 2.1 Implement EDF_Parser class
    - Write parse_header() method to extract EDF file metadata (sampling rate, physical units, channel labels)
    - Write extract_heart_rate_channel() method to extract heart rate signal
    - Write extract_movement_channel() method to extract movement/accelerometer signal
    - Write extract_sleep_annotations() method to extract sleep stage labels
    - Add error handling for corrupted EDF files
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8_
  
  - [x] 2.2 Write property test for EDF parsing data length consistency
    - **Property 2: EDF parsing data length consistency**
    - **Validates: Requirements 2.9, 2.10**
  
  - [x] 2.3 Implement Dataset_Loader class
    - Write load_sleep_edf() method to load Sleep-EDF dataset
    - Write load_mit_bih() method to load MIT-BIH Polysomnographic dataset
    - Add dataset path validation and metadata extraction
    - Add error handling for missing channels or invalid paths
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8_
  
  - [x] 2.4 Write property test for dataset loading completeness
    - **Property 1: Dataset loading completeness**
    - **Validates: Requirements 1.9**

- [x] 3. Time synchronization and data splitting
  - [x] 3.1 Implement Time_Synchronizer class
    - Write calculate_time_offset() method to compute sensor time offset
    - Write align_data() method using linear interpolation to align heart rate and movement timestamps
    - Add warning logging for time offsets >1 second
    - Ensure aligned data length equals minimum of both sensor data lengths
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_
  
  - [x] 3.2 Write property tests for time synchronization
    - **Property 3: Time synchronization timestamp precision**
    - **Property 4: Time synchronization data length invariant**
    - **Validates: Requirements 3.7, 3.8**
  
  - [x] 3.3 Implement data splitting methods in Dataset_Loader
    - Write split_train_test() method to split by subject ID (default 80:20 ratio)
    - Write k_fold_split() method for K-fold cross-validation
    - Ensure same subject data doesn't appear in both train and test sets
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  
  - [x] 3.4 Write property test for K-fold split balance
    - **Property 5: K-fold split balance**
    - **Validates: Requirements 4.6**

- [x] 4. Data preprocessing pipeline
  - [x] 4.1 Implement Data_Normalizer class
    - Write fit() method to compute mean and std from training data for both channels
    - Write transform() method to apply Z-score normalization using training parameters
    - Write fit_transform() method for convenience
    - Ensure normalized training data has mean≈0 and std≈1
    - _Requirements: 5.1, 5.2, 5.3, 5.4_
  
  - [x] 4.2 Write property tests for Z-score normalization
    - **Property 6: Z-score normalization range constraint**
    - **Property 7: Z-score normalization statistical properties**
    - **Validates: Requirements 5.5, 5.6, 5.7, 5.8**
  
  - [x] 4.3 Implement Wavelet_Denoiser class
    - Initialize with db5 wavelet and 5-level decomposition
    - Write denoise() method for multi-level wavelet decomposition, thresholding, and reconstruction
    - Ensure 50Hz power line interference suppression (<10% of original energy)
    - _Requirements: 6.1, 6.2, 6.3, 6.4_
  
  - [x] 4.4 Write property test for wavelet denoising effectiveness
    - **Property 8: Wavelet denoising power line suppression**
    - **Validates: Requirements 6.5**
  
  - [x] 4.5 Implement Movement_Filter class
    - Initialize with configurable enable flag and cutoff frequency (default 10Hz)
    - Write filter() method to apply bandpass filter (0.1-5Hz) for movement signal
    - Support disabling filter to pass through raw data
    - Ensure high-frequency noise reduction (<20% of original energy)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_
  
  - [x] 4.6 Write property test for movement filter effectiveness
    - **Property 9: Movement filter high-frequency suppression**
    - **Validates: Requirements 7.6**

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. CNN feature extraction
  - [x] 6.1 Implement CNN_Extractor class
    - Initialize CNN with input shape (1024, 128, 2) for dual-channel time-frequency matrix
    - Build architecture: Conv2D(32 filters, 3x3) → MaxPooling2D(2x2) → Conv2D(64 filters, 3x3) → MaxPooling2D(2x2)
    - Write extract_features() method to process time-frequency matrix and output feature maps
    - Add input dimension validation (must be 1024×128×2)
    - Emphasize 0.1-0.4Hz HRV features from heart rate channel and 0.1-5Hz movement features
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8_
  
  - [x] 6.2 Write property test for CNN dimensionality reduction
    - **Property 10: CNN feature extraction dimensionality consistency**
    - **Validates: Requirements 8.9**

- [x] 7. BiLSTM temporal analysis
  - [x] 7.1 Implement BiLSTM_Analyzer class
    - Initialize with 128 hidden units and 1800-second (30-minute) memory window
    - Build bidirectional LSTM architecture to capture forward and backward temporal dependencies
    - Write analyze() method to process CNN features and output bidirectional context vectors
    - Ensure output dimension is 2×hidden_units (forward + backward)
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_
  
  - [x] 7.2 Write property test for BiLSTM output dimensionality
    - **Property 11: BiLSTM output dimension bidirectionality**
    - **Validates: Requirements 9.6**

- [x] 8. Sleep stage classification
  - [x] 8.1 Implement Sleep_Classifier class
    - Initialize with 4 output classes (Awake, Light, Deep, REM)
    - Build fully connected layer + softmax for classification
    - Write classify() method to output sleep stage and confidence score
    - Write get_probability_distribution() method to output all class probabilities
    - Ensure confidence ∈ [0,1] and probability sum = 1
    - _Requirements: 10.1, 10.2, 10.3, 10.4_
  
  - [x] 8.2 Write property tests for classifier output constraints
    - **Property 12: Classification confidence range constraint**
    - **Property 13: Softmax probability normalization**
    - **Validates: Requirements 10.5, 10.6**

- [x] 9. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. MQTT communication layer
  - [x] 10.1 Implement MQTT_Subscriber class
    - Write subscribe_heart_rate() method for "sensors/heart_rate" topic
    - Write subscribe_movement() method for "sensors/movement" topic
    - Write subscribe_smoke() method for "sensors/smoke" topic
    - Write subscribe_gas() method for "sensors/gas" topic
    - Write on_message() callback to parse JSON payloads and validate data
    - Add validation for heart rate range [30,200]bpm and timestamp freshness (<5 seconds)
    - Mark out-of-range data as anomalous
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9, 11.10_
  
  - [x] 10.2 Implement MQTT_Publisher class
    - Write publish_sleep_stage() method for "sleep/stage" topic (QoS 1)
    - Write publish_environment_control() method for lighting/temperature/humidity control topics (QoS 1)
    - Write publish_disaster_alert() method for "alert/smoke" and "alert/gas" topics (QoS 2)
    - Ensure sleep stage message latency <500ms and disaster alert latency <100ms
    - Validate all messages conform to predefined JSON schemas
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 14.3, 14.4, 14.5, 14.6, 14.7_
  
  - [x] 10.3 Write property test for MQTT message format compliance
    - **Property 14: MQTT message format compliance**
    - **Validates: Requirements 12.6, 13.7, 14.8**
  
  - [x] 10.4 Implement Environment_Controller class
    - Write generate_lighting_control() method based on sleep stage
    - Write generate_temperature_control() method based on sleep stage
    - Write generate_humidity_control() method based on sleep stage
    - Implement control strategy: Deep sleep → lights off, 18-20°C, 50-60% humidity; Awake → gradual light increase
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6_
  
  - [x] 10.5 Implement Disaster_Monitor class
    - Initialize with smoke and gas safety thresholds
    - Write check_smoke_level() method to detect threshold exceedance
    - Write check_gas_level() method to detect threshold exceedance
    - Integrate with MQTT_Publisher to send alerts with QoS 2
    - _Requirements: 14.1, 14.2_

- [x] 11. Anomaly detection and error handling
  - [x] 11.1 Implement Anomaly_Handler class
    - Write detect_heart_rate_anomaly() method to check range [30,200]bpm and rate of change (<50bpm/s)
    - Write detect_movement_anomaly() method to check amplitude range
    - Write interpolate_anomalous_data() method using linear interpolation for <5 second gaps
    - Add sensor fault detection and publish to "system/sensor_fault" topic
    - Log all anomaly events with timestamps
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 18.7, 18.8_
  
  - [x] 11.2 Write property test for interpolation range constraint
    - **Property 16: Interpolation fill range constraint**
    - **Validates: Requirements 18.9**

- [x] 12. Model training and validation
  - [x] 12.1 Implement training pipeline
    - Create end-to-end training script that loads dataset, preprocesses data, and trains CNN-BiLSTM model
    - Implement training loop with loss and accuracy calculation per epoch
    - Add validation set evaluation after each epoch
    - Implement early stopping mechanism (patience=5 epochs)
    - Log training and validation curves
    - Save best model weights based on validation accuracy
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6_
  
  - [x] 12.2 Write unit tests for training pipeline
    - Test training loop with small synthetic dataset
    - Test early stopping trigger
    - Test model checkpoint saving

- [x] 13. Model persistence and loading
  - [x] 13.1 Implement model save/load functionality
    - Write save_model() method to persist CNN, BiLSTM, and classifier weights to HDF5 format
    - Write load_model() method to restore model from HDF5 file
    - Add validation for model file existence and integrity on system startup
    - Return clear error messages for missing or corrupted model files
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5, 17.6_
  
  - [x] 13.2 Write property test for model persistence round-trip consistency
    - **Property 15: Model persistence round-trip consistency**
    - **Validates: Requirements 17.7**

- [x] 14. Performance evaluation metrics
  - [x] 14.1 Implement PerformanceMetrics class
    - Write calculate_accuracy() method for overall accuracy
    - Write calculate_precision_per_class() method for per-class precision
    - Write calculate_recall_per_class() method for per-class recall
    - Write calculate_f1_per_class() method for per-class F1 score
    - Write generate_confusion_matrix() method for 4×4 confusion matrix
    - Write save_metrics() method to export metrics as JSON
    - Ensure all metrics ∈ [0,1]
    - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.5, 19.6_
  
  - [x] 14.2 Write property test for evaluation metrics range constraint
    - **Property 17: Evaluation metrics range constraint**
    - **Validates: Requirements 19.7**

- [x] 15. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 16. Integration and end-to-end wiring
  - [x] 16.1 Create main inference pipeline
    - Wire MQTT_Subscriber → Time_Synchronizer → Anomaly_Handler → Data_Normalizer → Wavelet_Denoiser/Movement_Filter → CNN_Extractor → BiLSTM_Analyzer → Sleep_Classifier → MQTT_Publisher
    - Integrate Environment_Controller to generate control commands based on sleep stage
    - Integrate Disaster_Monitor for smoke and gas alerting
    - Add MQTT connection retry mechanism with exponential backoff
    - Add configuration file loading with default fallback
    - _Requirements: All integration requirements_
  
  - [x] 16.2 Write integration tests
    - Test complete training flow: dataset loading → preprocessing → training → evaluation → model saving
    - Test complete inference flow: MQTT subscription → preprocessing → classification → MQTT publishing
    - Test disaster alerting flow: smoke/gas detection → alert publishing
    - Test error recovery: MQTT reconnection, sensor fault handling, anomaly interpolation

- [x] 17. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The implementation uses Python with TensorFlow/Keras for deep learning, PyWavelets for signal processing, and Paho MQTT for communication
- All MQTT communication follows QoS levels specified in requirements (QoS 1 for sleep state and control, QoS 2 for disaster alerts)
- Checkpoints ensure incremental validation and provide opportunities for user feedback
