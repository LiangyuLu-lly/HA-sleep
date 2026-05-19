# Population Prior（公开数据集先验）

> 关联 spec：`.kiro/specs/algorithmic-moat-v3.0.0/`（requirements §7、§8、§14；design §3.1）
> 关联代码：`src/population_prior.py`、`scripts/train_population_prior.py`、`sleep_classifier/rootfs/training_config/population_prior.pickle`
> 适用版本：v3.0.0 起

本文档描述 **Population Prior（PP）** 模块所依赖的公开数据集来源、伦理 / DUA 契约、桶定义、字段含义、训练与隐私契约。运行时 `population_prior.py` 启动时会在 INFO 日志中打印一行 NSRR DUA 摘要 + DOI（R14.1），其文案与本文档一致；如有差异请以本文档为准并同步代码。

---

## 1. 数据来源

v3.0.0 出厂的 `population_prior.pickle` 由两份公开 PSG（多导睡眠图）数据集训练而成，全部通过 [National Sleep Research Resource（NSRR）](https://sleepdata.org/) 申请获取：

| 数据集 | 全称 | 受试者数 | NSRR 数据集页 | 学术引用 DOI |
|---|---|---|---|---|
| **MESA Sleep** | Multi-Ethnic Study of Atherosclerosis Sleep Study | ≈ 2056 | <https://sleepdata.org/datasets/mesa> | `10.1093/sleep/zsv164` |
| **SHHS** | Sleep Heart Health Study | ≈ 6441 | <https://sleepdata.org/datasets/shhs> | `10.1093/sleep/20.12.1077` |

合计约 **8497 受试者夜**（`PriorMetadata.n_subject_nights`），其中 SHHS 含两次访视（SHHS-1 / SHHS-2），训练脚本会按 NSRR 推荐的去重规则只保留每个 subject 一次基线访视，避免桶被同一个体过度加权。

> 注：本仓库**不分发**原始 EDF / annotation 文件。`population_prior.pickle` 中**只**保存桶级聚合统计（均值、方差、样本数），不含任何个体可还原信息。原始数据需开发者自己向 NSRR 申请并签署 DUA 后下载，再用 `scripts/train_population_prior.py` 重新训练。

---

## 2. 引用格式

如果你在论文、博客、商业宣传中提到本 add-on 的 prior 部分，请在引用列表中加入以下三条（APA 7th 风格，仅供参考）：

```text
Zhang, G.-Q., Cui, L., Mueller, R., Tao, S., Kim, M., Rueschman, M.,
  Mariani, S., Mobley, D., & Redline, S. (2018).
  The National Sleep Research Resource: towards a sleep data commons.
  Journal of the American Medical Informatics Association, 25(10), 1351–1358.
  https://doi.org/10.1093/jamia/ocy064

Chen, X., Wang, R., Zee, P., Lutsey, P. L., Javaheri, S., Alcántara, C.,
  Jackson, C. L., Williams, M. A., & Redline, S. (2015).
  Racial/ethnic differences in sleep disturbances: The Multi-Ethnic Study of
  Atherosclerosis (MESA). Sleep, 38(6), 877–888.
  https://doi.org/10.1093/sleep/zsv164

Quan, S. F., Howard, B. V., Iber, C., Kiley, J. P., Nieto, F. J., O'Connor,
  G. T., Rapoport, D. M., Redline, S., Robbins, J., Samet, J. M., &
  Wahl, P. W. (1997). The Sleep Heart Health Study: design, rationale,
  and methods. Sleep, 20(12), 1077–1085.
  https://doi.org/10.1093/sleep/20.12.1077
```

启动期 INFO 日志的简短引用形式（与 design §6.2 的 v3 status banner 一致）：

```text
[INFO] Prior provenance: MESA v0.6.0 (DOI:10.1093/sleep/zsv164) + SHHS v8 (DOI:10.1093/sleep/20.12.1077)
```

---

## 3. 伦理审查与 NSRR DUA 摘要

### 3.1 原始数据集的 IRB

- MESA、SHHS 均经各承担机构的 IRB / 伦理委员会审批，受试者签署书面知情同意。
- NSRR 在分发前对原始数据做了去标识化（HIPAA Safe Harbor + 时间偏移），所有日期被替换为相对天数。
- 本 add-on 的 prior 训练流程**只读取已经过 NSRR 去标识化的数据**，不会接触任何原始 PHI。

### 3.2 NSRR DUA 摘要（运行时 INFO 日志同款文案）

NSRR 数据使用协议（Data Use Agreement）的核心条款：

1. **仅用于研究**：所获数据仅可用于 IRB 批准的研究、教学或个人学习目的，**禁止**用于临床诊断、商业产品验证或对个人的健康判断。
2. **禁止再分发**：不得以原始或可还原形式向第三方提供数据；衍生作品（如本 add-on 的桶级聚合 prior）需在文档中说明数据来源并附引用。
3. **禁止再识别**：不得以任何方式尝试反向识别受试者身份；不得将数据与外部数据集做可能导致再识别的连接。
4. **结果发表**：在论文 / 摘要 / 海报中使用数据时必须按 NSRR 模板致谢，并附 §2 列出的引用。
5. **撤回响应**：若 NSRR 通知数据下架或更正，使用者需在合理时间内停用相应版本。

`population_prior.py` 启动时会在 stdout 打印一次以下摘要（R14.1，仅启动时一次，避免日志刷屏）：

```text
[INFO] Population prior is derived from de-identified, aggregated bucket-level
       statistics of MESA and SHHS PSG datasets distributed by NSRR
       (sleepdata.org). Use is restricted to non-clinical research and personal
       sleep optimisation; redistribution of subject-level data is forbidden;
       no attempt to re-identify subjects is permitted. See docs/POPULATION_PRIOR.md.
```

> 添加新数据源或修改 DUA 摘要文案时，**必须同步更新**：
> - 本文档第 3.2 节
> - `src/population_prior.py` 启动期 INFO 日志字面量
> - `scripts/train_population_prior.py` 训练完成时打印的 DUA 摘要

---

## 4. 桶定义（BucketKey）

每个桶由 4 维 key 唯一确定，所有维度均使用英文枚举字面量（与 design §3.1.1 / `data_structures.py` 对齐）：

```python
BucketKey = tuple[AgeBand, Sex, Chronotype, Season]
```

| 维度 | 类型别名 | 取值 | 说明 |
|---|---|---|---|
| age_band | `AgeBand` | `18-25` / `26-35` / `36-50` / `51-65` / `65+` | 5 段年龄区间，按 NSF 推荐睡眠时长分组合并 |
| sex | `Sex` | `M` / `F` / `unspecified` | NSRR 字段 `gender` 的归一化映射；用户未填写时落入 `unspecified` |
| chronotype | `Chronotype` | `morning` / `evening` / `neutral` | 由 NSRR 提供的 MEQ / 入睡中位时刻派生；用户在 onboarding 第 3 步可手动设置 |
| season | `Season` | `spring` / `summer` / `autumn` / `winter` | 按本地（add-on 部署机器）月份归类（北半球 3-5 春、6-8 夏、9-11 秋、12-2 冬） |

总桶数上限 = `5 × 3 × 3 × 4 = 180`。实际训练后部分组合 `n_samples = 0`（如「18-25 / morning / winter」在 MESA 中样本极少），运行时由 `PopulationPriorRepository.lookup` 按以下顺序逐层放宽：

| `fallback_level` | 含义 |
|---|---|
| 0 | 精确匹配，目标桶 `n_samples ≥ 50` |
| 1 | sex 放宽到 `unspecified` |
| 2 | chronotype 放宽到 `neutral` |
| 3 | age_band 放宽到相邻区间（最终回落到全人群均值） |

只要任一层 `n_samples ≥ 50` 即停止放宽（R8.6 的小样本硬阈值，由 PBT P15 守护）。

---

## 5. 字段含义（PriorBucket）

每个 leaf 桶是一个 `frozen slots` dataclass，字段如下（与 design §3.1.1 一致）：

| 字段 | 类型 | 单位 | 物理 / 统计含义 |
|---|---|---|---|
| `temperature_mean_c` | `float` | °C | 该桶内受试者「主观睡眠质量较好」夜晚的卧室温度后验均值；预期 ∈ `[16, 28]`（PBT P6） |
| `temperature_var_c2` | `float` | °C² | 同桶温度后验方差，用于 BAO 初始化时的 prior_weight 计算 |
| `humidity_mean_pct` | `float` | %RH | 卧室相对湿度后验均值；预期 ∈ `[30, 70]`（PBT P6） |
| `humidity_var_pct2` | `float` | (%RH)² | 同桶湿度后验方差 |
| `brightness_mean_pct` | `float` | % | 卧室照度后验均值（0 = 全黑，100 = 直射阳光强度）；预期 ∈ `[0, 50]`（PBT P6） |
| `brightness_var_pct2` | `float` | %² | 同桶亮度后验方差 |
| `n_samples` | `int` | 受试者夜 | 聚合到该桶的受试者夜计数；运行时用于判断是否需要兜底（R8.6） |

> 「主观睡眠质量较好」的判定：MESA / SHHS 提供 PSQI / ESS 等问卷，训练脚本按数据集自带的 PSQI 总分 ≤ 5 过滤；这是为了让 prior 拟合「可作为目标的环境」而非「随便一个受试者夜」。详细过滤规则见 `scripts/train_population_prior.py` 内文档字符串。

### 5.1 顶层结构

```python
@dataclass(frozen=True, slots=True)
class PopulationPrior:
    buckets: Mapping[BucketKey, PriorBucket]
    metadata: PriorMetadata
```

`PriorMetadata` 字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | `int` | v3.0.0 = 1；v3.1.0 联邦扩展时 +1 |
| `sources` | `tuple[str, ...]` | 含 DOI 的来源列表，例如 `("MESA v0.6.0 (DOI:10.1093/sleep/zsv164)", "SHHS v8 (DOI:10.1093/sleep/20.12.1077)")` |
| `trained_at` | `str` | ISO-8601 UTC 时间戳 |
| `git_commit` | `str` | 训练时 git short SHA |
| `n_subject_nights` | `int` | 训练数据总受试者夜数 |
| `sha256` | `str` | 桶 dict 序列化后的 SHA-256，运行时 `load()` 与文件再哈希比对，不一致即拒绝加载 |

---

## 6. 训练流程概览

完整训练流程见 `scripts/train_population_prior.py`（task 10.1 实现）。简化版：

```text
NSRR EDF / CSV ─► 受试者夜过滤（PSQI ≤ 5）
              ─► chronotype / age_band / season 派生
              ─► 4 维 GroupBy
              ─► 每桶 hierarchical Bayesian posterior（用上一层均值作为先验）
              ─► 序列化为 dict[BucketKey, PriorBucket]
              ─► 嵌入 PriorMetadata（含 SHA-256）
              ─► pickle.dump → population_prior.pickle（≤ 8 MB）
```

**层次贝叶斯**的关键点：每个叶子桶的 posterior 不是简单的样本均值 / 方差，而是以**上一层桶**（如 sex 放宽后的桶）的均值 / 方差为正态先验、本桶样本为 likelihood，计算共轭后验。这样小样本桶的均值会向更稳定的父桶收缩，避免极端值；这也是 §4 兜底逻辑能保持平滑的根本原因。

CLI 用法（详见 task 10.1）：

```bash
python scripts/train_population_prior.py \
    --mesa-dir /data/nsrr/mesa \
    --shhs-dir /data/nsrr/shhs \
    --out sleep_classifier/rootfs/training_config/population_prior.pickle \
    --seed 20260518
```

退出码：`0` = OK；`1` = 数据 schema 不符；`2` = 输出 > 8 MB（R7.3）。

构建期由 `scripts/check_artifacts.py --strict` 守护文件存在性、大小、SHA-256 一致性（R7.5）。

---

## 7. 隐私契约

**v3.0.0 算法栈对 prior 的隐私承诺**（与 PRIVACY.md「v3.0.0 算法栈数据流」段落一致）：

1. **不上传任何个体数据**：`population_prior.pickle` 内**只**有桶级聚合标量（均值、方差、`n_samples`），不含 EDF 波形、annotation、subject ID、时间戳等任何个体可还原信息。
2. **运行时只读**：`PopulationPriorRepository` 在运行时**只**调用 `Path.read_bytes` + `pickle.loads` + `hashlib.sha256`，**永远不写**该文件（R14.2）。
3. **用户画像本地化**：用户在 Web UI 第 3 步填写的 `age_band / sex / chronotype` 仅写入 `/data/web_ui_overrides.json` 的 `v3_user_profile` 子字段，**不离开**设备（R8.7、R14.2）。
4. **不外传衍生信息**：用户激活的桶 key 与 `prior_weight` 仅作为 add-on 内部 sensor 暴露给本地 HA 实例；启用 `telemetry_enabled` 时也不会上传画像或桶 key（R14.2 的「不可还原个体」红线）。
5. **可重启即可重置**：用户可在 Web UI 删除画像 / 锁定 `prior_weight = 0`，BAO 会立即停用 prior 影响；删除 `web_ui_overrides.json` 后下次启动会回到 `unspecified / neutral` 兜底桶。

> 一句话：**pickle 只是一个 ≤ 8 MB 的查找表，不是数据库；它进入设备一次（随镜像），就再也不会变，也再也不会出去。**

---

## 8. 维护者 checklist

新增数据源 / 升级 prior schema 时按以下顺序更新（顺序不可调换）：

1. 修改 `scripts/train_population_prior.py` 的过滤 / 桶规则。
2. `PriorMetadata.schema_version` 累加。
3. 同步更新本文档第 1、2、3.2、5、6 节。
4. 同步更新 `src/population_prior.py` 启动期 INFO 日志字面量与 `scripts/train_population_prior.py` 训练完成时打印的 DUA 摘要。
5. 跑 `pytest tests/test_population_prior.py -v`（含 P6 / P15 property 测试）。
6. 跑 `python scripts/check_artifacts.py --strict` 校验镜像产物。
7. 在 `CHANGELOG.md` v3.x.0 章节记录数据源 / schema 变更，附旧 `sha256` → 新 `sha256` 的对照。

---

## 9. 参考文献

- Zhang, G.-Q., et al. (2018). *The National Sleep Research Resource: towards a sleep data commons.* JAMIA, 25(10). <https://doi.org/10.1093/jamia/ocy064>
- Chen, X., et al. (2015). *Racial/ethnic differences in sleep disturbances: MESA.* Sleep, 38(6). <https://doi.org/10.1093/sleep/zsv164>
- Quan, S. F., et al. (1997). *The Sleep Heart Health Study: design, rationale, and methods.* Sleep, 20(12). <https://doi.org/10.1093/sleep/20.12.1077>
- NSRR Data Use Agreement template: <https://sleepdata.org/data/requests/forms>

— 以上文档与 spec `.kiro/specs/algorithmic-moat-v3.0.0/requirements.md` 第 7、8、14 章节对齐；任何不一致以 spec 为准并提 PR 修正本文。
