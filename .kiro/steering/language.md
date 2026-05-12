---
inclusion: always
---

# 语言规范（Language）

## 核心要求
- **思考过程（reasoning / thinking）使用中文**进行表述。
- **对用户的聊天回复使用中文**。
- **生成或更新的说明文档使用中文**，包括但不限于：
  - `README.md`、`INSTALL.md`、`CHANGELOG.md`
  - `docs/**/*.md`、`sleep_classifier/DOCS.md`
  - Spec 文档：`.kiro/specs/**/requirements.md`、`design.md`、`tasks.md`、`bugfix.md`
  - Steering 文档：`.kiro/steering/*.md`

## 必须保持英文的场景（技术约束，非语言偏好）
为保证代码可运行、工具链兼容，以下内容保持英文：

- **代码标识符**：变量名、函数名、类名、模块名、文件名、路径。
- **CLI 命令、环境变量、配置键**：例如 `pytest --cov=src`、`HA_TOKEN`、`log_level`。
- **第三方 API 字段与库名**：例如 `aiohttp`、`numpy`、`state_changed`、`input_number.*`。
- **Git 元信息**：commit message、分支名、PR 标题（PR 正文可中英混排）。
- **Markdown 语法结构**：代码块、表格、链接语法本身不翻译，仅正文使用中文。

## 代码注释与 docstring
- 跟随目标模块的现有约定；若模块已使用英文 reStructuredText docstring（见 `tech.md`），新增内容保持英文风格。
- 若现有模块注释为中文，则继续使用中文。
- 不要在同一个文件内中英混写注释风格。

## 专有名词与引用
- 技术专有名词（Home Assistant、asyncio、k-NN、REM、Add-on 等）直接使用英文原词，不强行音译。
- 引用英文原文时可保留原句，随后用中文解释其含义。

## 反例
- ❌ 把 `SleepStage.DEEP` 翻译成 `睡眠阶段.深睡`。
- ❌ Spec 文档的正文用英文写。
- ❌ 聊天回复全用英文。
