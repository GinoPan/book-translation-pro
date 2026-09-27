# Book Translation Pro

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

面向整本书翻译的可恢复工作流，支持 **PDF、DOCX、EPUB 和 CHM** 输入，并提供结构保真、术语管理、上下文衔接、视觉证据、完整性检查与出版级输出。

它不是一个“把整本书一次性交给模型”的脚本。语言模型负责翻译、编辑和语义判断；确定性工具负责提取、映射、哈希、状态、校验、图片处理和构建。任务中断后可以根据依赖状态继续，而不必从头重做。

## 主要能力

- 保留章节、图表、注释、公式、交叉引用和阅读顺序
- 为 PDF 生成逐页 Page Map、页面渲染和视觉风险记录
- 针对扫描页支持 OCR 恢复，针对矢量图支持确定性裁切重建
- 通过 Glossary、Style Guide 和 Context Capsule 保持术语与上下文一致
- 使用稳定工作单元、内容哈希和依赖状态实现断点续作
- 提供 `fast`、`study` 和 `publication` 三种质量模式
- 输出 Markdown、DOCX、EPUB、PDF，以及可选的双语对照版本
- 在构建前执行视觉、完整性、术语、资源和出版签核检查
- 支持顺序执行、本地并行、宿主工作线程和外部队列

## 工作流

```text
源文件
  ↓
提取与结构分析 ──→ Book Map / PDF Page Map
  ↓
术语与风格确认
  ↓
分块 + Context Capsules
  ↓
翻译 → 编辑 → 视觉 QA → 完整性 QA
  ↓
排版候选 → 人工复核 → 最终构建
  ↓
Markdown / DOCX / EPUB / PDF / 双语版
```

## 安装

需要 Python 3.11 或更高版本。

直接从 GitHub 安装：

```bash
python -m pip install "git+https://github.com/GinoPan/book-translation-pro.git"
```

或者克隆后以可编辑模式安装：

```bash
git clone https://github.com/GinoPan/book-translation-pro.git
cd book-translation-pro
python -m pip install -e .
```

安装完成后检查环境：

```bash
btp release-check
btp runtime-conformance
btp doctor --mode study
```

PDF 视觉处理依赖 PyMuPDF 和 Pillow，均会随 Python 包安装。扫描页 OCR 需要 Tesseract；CHM 输入需要 7-Zip 或 Windows `hh.exe`；PDF 导出需要 Microsoft Word 或 LibreOffice；Publication 模式的 EPUB 还需要 EPUBCheck。`btp doctor` 会报告当前环境具备的能力。

## 作为 Codex Skill 使用

将仓库克隆到 Codex 的 skills 目录：

```bash
git clone https://github.com/GinoPan/book-translation-pro.git ~/.codex/skills/book-translation-pro
```

然后在 Codex 中调用：

```text
使用 $book-translation-pro，把 D:\Books\source.pdf 翻译成简体中文。
采用 study 模式，输出 DOCX、EPUB 和 PDF，项目保存在 D:\Books\source-zh。
```

核心行为定义在 [`SKILL.md`](SKILL.md) 中；平台元数据只负责发现该 Skill，不会复制或改写工作流规则。

## 命令行快速开始

下面的示例创建一个英译中 Study 模式项目：

```bash
btp init D:/Books/source.pdf \
  --project D:/Books/source-zh \
  --source-language en \
  --target zh-CN \
  --mode study \
  --output docx \
  --output epub \
  --output pdf \
  --style-template general

btp prepare D:/Books/source-zh
btp plan D:/Books/source-zh
btp status D:/Books/source-zh
```

`prepare` 会提取内容、建立结构与状态；语言代理随后根据 `plan` 生成的工作队列编写 Context Capsules、翻译和编辑结果，并用 `record` 写入经过校验的状态。完成语义工作后运行：

```bash
btp qa D:/Books/source-zh
btp typeset D:/Books/source-zh
btp build D:/Books/source-zh
btp status D:/Books/source-zh
```

`typeset` 只生成供检查的排版候选，不会绕过 QA 或把项目标记为完成。Publication 模式还需要完成出版复核、例外记录和最终签核。

查看所有命令：

```bash
btp --help
btp <command> --help
```

## 三种模式

| 模式 | 适用场景 | 检查强度 |
|---|---|---|
| `fast` | 快速个人阅读 | 保证完整性，减少语义与视觉复核轮次 |
| `study` | 深度阅读、内部参考 | 完整结构、逐单元编辑、风险页面视觉检查 |
| `publication` | 面向分发的候选版本 | 全量视觉与出版检查、格式验证、例外审批和最终签核 |

Fast 模式也不会允许省略内容。三种模式都要求源文件指纹、解析后的配置、Book Map、Glossary、Style Guide、完整覆盖和限制说明。

## 常用命令

| 命令 | 作用 |
|---|---|
| `btp doctor` | 检查本地依赖和模式能力 |
| `btp init` | 创建翻译项目并冻结初始配置 |
| `btp prepare` | 提取、映射、分块并初始化状态 |
| `btp visualize` | 创建或刷新 PDF 视觉证据 |
| `btp plan` | 计算待翻译、待记录、待编辑和阻塞项 |
| `btp show-unit` | 输出一个可移植的工作单元包 |
| `btp record` | 校验并记录翻译、编辑、术语或复核结果 |
| `btp qa` | 运行视觉、完整性和资源检查 |
| `btp typeset` | 创建非最终排版候选 |
| `btp build` | 在所有门禁通过后生成最终文件 |
| `btp status` | 查看项目状态、输出和限制 |

## 文档

- [完整工作流](references/workflow.md)
- [翻译协议](references/translation-protocol.md)
- [模式与质量策略](references/modes.md)
- [数据契约](references/data-contracts.md)
- [质量门禁](references/quality-gates.md)
- [出版输出](references/publication-output.md)
- [运行时兼容性](references/runtime-compatibility.md)

## 开发与测试

```bash
git clone https://github.com/GinoPan/book-translation-pro.git
cd book-translation-pro
python -m pip install -e .
python -m pytest -q
```

项目的确定性引擎位于 `scripts/book_translation_pro/`，JSON Schema 位于 `references/schemas/`，自动化测试位于 `tests/`。

## 版权与授权

代码以 [MIT License](LICENSE) 发布。第三方组件说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

使用本工具时，请确保你对源书拥有合法的读取、翻译和分发权限。源书内容默认只在用户指定的本地工作流中处理。
