# 需求文档

## 简介

CNN-BiLSTM个性化睡眠算法模块是面向智能家居场景的睡眠阶段分类系统，负责处理心率和体动双传感器数据，通过深度学习算法分析睡眠状态，并通过MQTT协议实现环境智能调控和灾害预警。该模块使用Sleep-EDF、MIT-BIH等公开数据集提取心率和体动数据进行训练和验证，集成了双传感器时钟同步、信号预处理、双通道特征提取、睡眠阶段分类、智能家居控制等功能，为智能家居睡眠优化提供数据支持。

## 术语表

- **Dataset_Loader**: 数据集加载器，支持Sleep-EDF、MIT-BIH等公开数据集
- **EDF_Parser**: EDF（European Data Format）文件解析器
- **Heart_Rate_Data**: 心率数据，采样率100Hz，测量范围30-200bpm
- **Movement_Data**: 体动数据，采样率100Hz，测量加速度幅度
- **Time_Synchronizer**: 时钟同步器，负责对齐心率和体动传感器的时间戳
- **Data_Normalizer**: 数据标准化处理器，采用Z-score算法
- **Wavelet_Denoiser**: 小波去噪处理器，使用db5小波基
- **Movement_Filter**: 体动数据滤波器，可选的低通或带通滤波
- **CNN_Extractor**: 卷积神经网络特征提取器，支持双通道输入
- **BiLSTM_Analyzer**: 双向长短期记忆网络分析器
- **Sleep_Classifier**: 睡眠阶段分类器
- **MQTT_Subscriber**: MQTT协议消息订阅器
- **MQTT_Publisher**: MQTT协议消息发布器
- **Environment_Controller**: 环境调控指令生成器
- **Disaster_Monitor**: 灾害预警监控器
- **HRV**: 心率变异性（Heart Rate Variability）
- **Sleep_Stage**: 睡眠阶段（清醒、浅睡、深睡、REM）
- **Training_Set**: 训练数据集
- **Test_Set**: 测试数据集
- **K_Fold_Validator**: K折交叉验证器
- **EDF_File**: EDF格式的睡眠数据文件
- **Time_Frequency_Matrix**: 时频矩阵，维度为1024×128×2（时间×频率×双通道）

## 需求

### 需求1: 公开数据集加载与双传感器数据提取

**用户故事:** 作为机器学习工程师，我希望加载Sleep-EDF和MIT-BIH等公开数据集并提取心率和体动数据，以便使用标准化的双传感器睡眠数据进行模型训练和验证。

#### 验收标准

1. THE Dataset_Loader SHALL 支持加载Sleep-EDF数据集
2. THE Dataset_Loader SHALL 支持加载MIT-BIH Polysomnographic数据集
3. WHEN 指定数据集路径时，THE Dataset_Loader SHALL 验证路径的有效性
4. WHEN 加载数据集时，THE Dataset_Loader SHALL 解析数据集的元数据（受试者ID、记录日期、采样率等）
5. WHEN 加载数据集时，THE Dataset_Loader SHALL 提取心率信号通道
6. WHEN 加载数据集时，THE Dataset_Loader SHALL 提取体动信号通道（加速度计或体动传感器数据）
7. IF 数据集路径无效，THEN THE Dataset_Loader SHALL 返回错误信息并记录日志
8. IF 数据集缺少心率或体动通道，THEN THE Dataset_Loader SHALL 返回错误信息
9. FOR ALL 支持的数据集，加载后的数据 SHALL 包含心率信号、体动信号和睡眠阶段标注（属性测试）

### 需求2: EDF文件格式解析

**用户故事:** 作为数据工程师，我希望解析EDF格式的睡眠数据文件，以便提取心率信号、体动信号和睡眠阶段标注。

#### 验收标准

1. WHEN 接收到EDF文件路径时，THE EDF_Parser SHALL 读取文件头信息
2. WHEN 读取文件头后，THE EDF_Parser SHALL 提取心率通道的采样率和物理单位
3. WHEN 读取文件头后，THE EDF_Parser SHALL 提取体动通道的采样率和物理单位
4. WHEN 提取通道信息后，THE EDF_Parser SHALL 读取心率信号数据
5. WHEN 提取通道信息后，THE EDF_Parser SHALL 读取体动信号数据
6. WHEN 读取信号数据后，THE EDF_Parser SHALL 提取睡眠阶段标注（annotation）
7. THE EDF_Parser SHALL 将物理值转换为数字信号
8. IF EDF文件格式损坏，THEN THE EDF_Parser SHALL 返回错误信息
9. FOR ALL 有效的EDF文件，解析后的心率数据长度 SHALL 等于（记录时长×采样率）（属性测试）
10. FOR ALL 有效的EDF文件，解析后的体动数据长度 SHALL 等于（记录时长×采样率）（属性测试）

### 需求3: 双传感器时钟同步

**用户故事:** 作为系统工程师，我希望同步心率和体动传感器的时间戳，以便确保双通道数据在时间轴上精确对齐。

#### 验收标准

1. WHEN 接收到心率和体动数据流时，THE Time_Synchronizer SHALL 提取各自的时间戳
2. WHEN 提取时间戳后，THE Time_Synchronizer SHALL 计算两个传感器的时间偏移量
3. WHEN 时间偏移量超过10ms时，THE Time_Synchronizer SHALL 对较快的传感器数据进行时间校正
4. THE Time_Synchronizer SHALL 使用线性插值对齐不同采样时刻的数据点
5. THE Time_Synchronizer SHALL 确保对齐后的心率和体动数据具有相同的时间戳序列
6. IF 时间偏移量超过1秒，THEN THE Time_Synchronizer SHALL 记录警告日志并标记数据为不可靠
7. FOR ALL 对齐后的数据对，心率和体动数据的时间戳差 SHALL 小于5ms（属性测试）
8. FOR ALL 对齐操作，对齐后的数据长度 SHALL 等于两个传感器数据长度的最小值（属性测试）

### 需求4: 训练测试数据划分

**用户故事:** 作为机器学习工程师，我希望将数据集划分为训练集和测试集，以便评估模型的泛化性能。

#### 验收标准

1. THE Dataset_Loader SHALL 支持按受试者ID划分训练集和测试集
2. THE Dataset_Loader SHALL 支持配置训练集和测试集的比例（默认80:20）
3. WHEN 划分数据集时，THE Dataset_Loader SHALL 确保同一受试者的数据不会同时出现在训练集和测试集中
4. THE Dataset_Loader SHALL 支持K折交叉验证数据划分
5. WHEN 使用K折交叉验证时，THE K_Fold_Validator SHALL 将数据集划分为K个互斥子集
6. FOR ALL K折划分，每个子集 SHALL 包含大致相等数量的样本（属性测试）

### 需求5: 双通道数据标准化

**用户故事:** 作为算法工程师，我希望将心率和体动数据标准化到统一区间，以便提升模型训练效率和收敛速度。

#### 验收标准

1. WHEN 接收到心率数据时，THE Data_Normalizer SHALL 使用Z-score算法将其映射到[-1,1]区间
2. WHEN 接收到体动数据时，THE Data_Normalizer SHALL 使用Z-score算法将其映射到[-1,1]区间
3. THE Data_Normalizer SHALL 基于训练集分别计算心率和体动的均值和标准差参数
4. THE Data_Normalizer SHALL 使用训练集参数对测试集进行标准化
5. FOR ALL 标准化后的心率数据值，该值 SHALL 在合理范围内（属性测试）
6. FOR ALL 标准化后的体动数据值，该值 SHALL 在合理范围内（属性测试）
7. FOR ALL 训练集心率数据序列，标准化后的均值 SHALL 接近0且标准差接近1（属性测试）
8. FOR ALL 训练集体动数据序列，标准化后的均值 SHALL 接近0且标准差接近1（属性测试）

### 需求6: 心率信号小波去噪

**用户故事:** 作为信号处理工程师，我希望去除心率信号中的工频干扰和噪声，以便提高后续分析的准确性。

#### 验收标准

1. WHEN 接收到含噪声的心率信号时，THE Wavelet_Denoiser SHALL 使用db5小波进行多层分解
2. WHEN 完成小波分解后，THE Wavelet_Denoiser SHALL 对高频系数进行阈值处理
3. WHEN 完成阈值处理后，THE Wavelet_Denoiser SHALL 重构信号并输出去噪结果
4. THE Wavelet_Denoiser SHALL 抑制50Hz工频干扰分量
5. FOR ALL 去噪后的信号，50Hz频段的能量 SHALL 低于去噪前该频段能量的10%（属性测试）

### 需求7: 体动信号滤波处理

**用户故事:** 作为信号处理工程师，我希望对体动信号进行滤波处理，以便去除高频噪声并保留睡眠相关的体动特征。

#### 验收标准

1. WHERE 体动信号滤波功能启用，THE Movement_Filter SHALL 应用低通滤波器
2. WHERE 体动信号滤波功能启用，THE Movement_Filter SHALL 设置截止频率为10Hz
3. WHEN 接收到体动信号时，THE Movement_Filter SHALL 去除高于截止频率的噪声分量
4. THE Movement_Filter SHALL 保留0.1-5Hz频段的体动特征（睡眠相关频段）
5. WHERE 体动信号滤波功能禁用，THE Movement_Filter SHALL 直接输出原始体动数据
6. FOR ALL 滤波后的信号，高频噪声能量 SHALL 低于滤波前的20%（属性测试）

### 需求8: 双通道CNN特征提取

**用户故事:** 作为机器学习工程师，我希望从心率和体动双通道时频矩阵中提取睡眠相关特征，以便识别不同睡眠阶段的特征模式。

#### 验收标准

1. WHEN 接收到双通道时频矩阵时，THE CNN_Extractor SHALL 验证输入维度为1024×128×2（时间×频率×双通道）
2. WHEN 输入维度有效时，THE CNN_Extractor SHALL 通过第一层3×3卷积核提取局部特征
3. WHEN 完成第一层卷积后，THE CNN_Extractor SHALL 通过2×2最大池化降低特征维度
4. WHEN 完成第一层池化后，THE CNN_Extractor SHALL 通过第二层3×3卷积核提取高层特征
5. WHEN 完成第二层卷积后，THE CNN_Extractor SHALL 通过2×2最大池化输出特征图
6. THE CNN_Extractor SHALL 从心率通道突出0.1-0.4Hz频段的HRV特征
7. THE CNN_Extractor SHALL 从体动通道突出0.1-5Hz频段的体动特征
8. IF 输入维度不是1024×128×2，THEN THE CNN_Extractor SHALL 返回错误信息
9. FOR ALL 输入矩阵，输出特征图的维度 SHALL 符合预期的降维比例（属性测试）

### 需求9: BiLSTM时序分析

**用户故事:** 作为机器学习工程师，我希望捕捉心率和体动信号的长短期时间依赖关系，以便准确预测睡眠状态转换。

#### 验收标准

1. WHEN 接收到CNN特征向量时，THE BiLSTM_Analyzer SHALL 初始化细胞状态以保存过去30分钟的特征趋势
2. WHEN 处理时间序列时，THE BiLSTM_Analyzer SHALL 通过输入门选择性引入当前时刻的HRV和体动特征
3. WHEN 处理时间序列时，THE BiLSTM_Analyzer SHALL 通过遗忘门丢弃不相关的历史信息
4. WHEN 处理时间序列时，THE BiLSTM_Analyzer SHALL 通过输出门生成与睡眠深度相关的特征向量
5. THE BiLSTM_Analyzer SHALL 同时处理前向和后向时间序列
6. FOR ALL 时间序列输入，BiLSTM输出向量 SHALL 包含前向和后向上下文信息（属性测试）

### 需求10: 睡眠阶段分类

**用户故事:** 作为算法工程师，我希望基于BiLSTM特征向量对睡眠状态进行分类，以便识别当前睡眠阶段。

#### 验收标准

1. WHEN 接收到BiLSTM特征向量时，THE Sleep_Classifier SHALL 通过全连接层进行线性变换
2. WHEN 完成特征变换后，THE Sleep_Classifier SHALL 通过softmax层输出睡眠阶段概率分布
3. WHEN 完成分类计算后，THE Sleep_Classifier SHALL 输出睡眠阶段预测结果（清醒、浅睡、深睡、REM）
4. WHEN 完成分类计算后，THE Sleep_Classifier SHALL 输出分类置信度
5. FOR ALL 有效输入特征，分类置信度 SHALL 在[0,1]区间内（属性测试）
6. FOR ALL 有效输入特征，所有类别的概率之和 SHALL 等于1（属性测试）

### 需求11: MQTT双传感器数据订阅

**用户故事:** 作为系统集成工程师，我希望订阅心率和体动传感器的MQTT消息，以便实时获取双传感器数据进行睡眠分析。

#### 验收标准

1. THE MQTT_Subscriber SHALL 订阅主题"sensors/heart_rate"以接收心率数据
2. THE MQTT_Subscriber SHALL 订阅主题"sensors/movement"以接收体动数据
3. WHEN 接收到心率消息时，THE MQTT_Subscriber SHALL 解析JSON格式的消息体
4. WHEN 接收到体动消息时，THE MQTT_Subscriber SHALL 解析JSON格式的消息体
5. WHEN 接收到传感器消息时，THE MQTT_Subscriber SHALL 验证消息时间戳的有效性
6. WHEN 接收到心率消息时，THE MQTT_Subscriber SHALL 验证心率值在[30,200]bpm范围内
7. WHEN 接收到体动消息时，THE MQTT_Subscriber SHALL 验证体动幅度值在合理范围内
8. IF 消息时间戳超过5秒，THEN THE MQTT_Subscriber SHALL 丢弃该消息并记录警告日志
9. IF 心率值超出有效范围，THEN THE MQTT_Subscriber SHALL 标记该数据为异常
10. IF 体动值超出有效范围，THEN THE MQTT_Subscriber SHALL 标记该数据为异常

### 需求12: MQTT睡眠状态发布

**用户故事:** 作为系统集成工程师，我希望通过MQTT协议发布睡眠状态，以便智能设备根据睡眠阶段调整环境参数。

#### 验收标准

1. WHEN 检测到睡眠阶段变化时，THE MQTT_Publisher SHALL 发布睡眠状态消息到主题"sleep/stage"
2. THE MQTT_Publisher SHALL 使用QoS级别1确保消息至少送达一次
3. WHEN 发布消息时，THE MQTT_Publisher SHALL 在消息体中包含时间戳、设备ID和睡眠阶段
4. WHEN 发布消息时，THE MQTT_Publisher SHALL 在消息体中包含分类置信度
5. THE MQTT_Publisher SHALL 在500ms内完成消息发布
6. FOR ALL 发布的消息，消息格式 SHALL 符合预定义的JSON schema（属性测试）

### 需求13: MQTT环境调控指令发布

**用户故事:** 作为智能家居集成工程师，我希望根据睡眠阶段发布环境调控指令，以便智能设备自动调整光照、温度和湿度优化睡眠环境。

#### 验收标准

1. WHEN 检测到睡眠阶段为深睡时，THE Environment_Controller SHALL 发布光照调控指令到主题"control/lighting"
2. WHEN 检测到睡眠阶段为深睡时，THE Environment_Controller SHALL 发布温度调控指令到主题"control/temperature"
3. WHEN 检测到睡眠阶段为深睡时，THE Environment_Controller SHALL 发布湿度调控指令到主题"control/humidity"
4. WHEN 检测到睡眠阶段为清醒时，THE Environment_Controller SHALL 发布唤醒模式指令
5. THE Environment_Controller SHALL 在调控指令中包含目标值和优先级
6. THE Environment_Controller SHALL 使用QoS级别1确保指令至少送达一次
7. FOR ALL 发布的调控指令，消息格式 SHALL 符合预定义的JSON schema（属性测试）

### 需求14: MQTT灾害预警监控

**用户故事:** 作为智能家居安全工程师，我希望监控烟雾和燃气传感器并在检测到灾害时发布预警，以便在睡眠期间保障用户安全。

#### 验收标准

1. THE Disaster_Monitor SHALL 订阅主题"sensors/smoke"以接收烟雾传感器数据
2. THE Disaster_Monitor SHALL 订阅主题"sensors/gas"以接收燃气传感器数据
3. WHEN 烟雾浓度超过安全阈值时，THE Disaster_Monitor SHALL 发布烟雾预警到主题"alert/smoke"
4. WHEN 燃气浓度超过安全阈值时，THE Disaster_Monitor SHALL 发布燃气预警到主题"alert/gas"
5. WHEN 发布预警时，THE Disaster_Monitor SHALL 使用QoS级别2确保消息恰好送达一次
6. WHEN 发布预警时，THE Disaster_Monitor SHALL 在消息体中包含传感器位置、浓度值和时间戳
7. THE Disaster_Monitor SHALL 在检测到灾害后100ms内发布预警消息
8. FOR ALL 预警消息，消息格式 SHALL 符合预定义的JSON schema（属性测试）

### 需求15: 数据处理管道配置

**用户故事:** 作为系统管理员，我希望配置数据处理管道的参数，以便根据不同数据集的特征优化算法性能。

#### 验收标准

1. THE Data_Normalizer SHALL 支持通过配置文件设置心率和体动的Z-score参数（均值、标准差）
2. THE Wavelet_Denoiser SHALL 支持通过配置文件选择小波基类型（db4、db5、sym8等）
3. THE Movement_Filter SHALL 支持通过配置文件启用或禁用滤波功能
4. THE Movement_Filter SHALL 支持通过配置文件设置截止频率
5. THE CNN_Extractor SHALL 支持通过配置文件设置卷积核数量和大小
6. THE BiLSTM_Analyzer SHALL 支持通过配置文件设置隐藏层单元数量
7. THE Sleep_Classifier SHALL 支持通过配置文件设置输出类别数量
8. WHEN 配置文件更新时，THE 系统 SHALL 在下一个处理周期应用新配置
9. IF 配置参数无效，THEN THE 系统 SHALL 使用默认配置并记录错误日志

### 需求16: 模型训练与验证

**用户故事:** 作为机器学习工程师，我希望训练和验证睡眠分类模型，以便评估模型在公开数据集上的性能。

#### 验收标准

1. THE 系统 SHALL 支持使用训练集数据训练CNN-BiLSTM模型
2. THE 系统 SHALL 在训练过程中计算训练损失和准确率
3. THE 系统 SHALL 在每个epoch结束后使用验证集评估模型性能
4. THE 系统 SHALL 记录训练过程中的损失曲线和准确率曲线
5. WHEN 验证集准确率连续5个epoch未提升时，THE 系统 SHALL 触发早停机制
6. THE 系统 SHALL 保存验证集上性能最佳的模型权重

### 需求17: 模型持久化与加载

**用户故事:** 作为机器学习工程师，我希望保存和加载训练好的模型，以便在系统重启后快速恢复服务。

#### 验收标准

1. THE CNN_Extractor SHALL 支持将模型权重保存为HDF5格式文件
2. THE BiLSTM_Analyzer SHALL 支持将模型权重保存为HDF5格式文件
3. THE Sleep_Classifier SHALL 支持将模型权重保存为HDF5格式文件
4. WHEN 系统启动时，THE 系统 SHALL 从指定路径加载模型文件
5. IF 模型文件不存在，THEN THE 系统 SHALL 返回错误信息并拒绝启动
6. IF 模型文件损坏，THEN THE 系统 SHALL 返回错误信息并拒绝启动
7. FOR ALL 保存和加载的模型，加载后的模型输出 SHALL 与保存前的模型输出一致（往返属性测试）

### 需求18: 双传感器异常数据处理

**用户故事:** 作为系统开发者，我希望检测和处理异常的心率和体动数据，以便避免错误数据影响睡眠分析结果。

#### 验收标准

1. WHEN 心率数据超出[30,200]bpm范围时，THE 系统 SHALL 标记该数据为异常
2. WHEN 心率数据变化率超过50bpm/秒时，THE 系统 SHALL 标记该数据为异常
3. WHEN 体动数据超出合理幅度范围时，THE 系统 SHALL 标记该数据为异常
4. WHEN 检测到连续5秒的心率异常数据时，THE 系统 SHALL 使用最近的有效数据进行插值填充
5. WHEN 检测到连续5秒的体动异常数据时，THE 系统 SHALL 使用最近的有效数据进行插值填充
6. WHEN 检测到心率传感器断连时，THE 系统 SHALL 发布传感器故障消息到主题"system/sensor_fault"
7. WHEN 检测到体动传感器断连时，THE 系统 SHALL 发布传感器故障消息到主题"system/sensor_fault"
8. THE 系统 SHALL 记录所有异常数据事件到日志系统
9. FOR ALL 插值填充的数据，填充值 SHALL 在相邻有效数据的范围内（属性测试）

### 需求19: 性能评估指标

**用户故事:** 作为机器学习工程师，我希望计算模型的性能评估指标，以便量化模型在睡眠阶段分类任务上的表现。

#### 验收标准

1. THE 系统 SHALL 计算测试集上的总体准确率（Accuracy）
2. THE 系统 SHALL 计算每个睡眠阶段的精确率（Precision）
3. THE 系统 SHALL 计算每个睡眠阶段的召回率（Recall）
4. THE 系统 SHALL 计算每个睡眠阶段的F1分数
5. THE 系统 SHALL 生成混淆矩阵（Confusion Matrix）
6. THE 系统 SHALL 将评估指标保存为JSON格式文件
7. FOR ALL 评估指标，指标值 SHALL 在[0,1]区间内（属性测试）
