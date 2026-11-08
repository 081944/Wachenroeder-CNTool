# 汉化流程工具

本目录只跟踪解包、资源导出、汉化回写、字库生成和镜像构建所需的脚本及共享模块。首次使用按[项目说明](../README.md)或[构建流程](../docs/translation-build-workflow.md)执行。

| 阶段 | 入口 |
| --- | --- |
| 提取光盘文件 | `../analysis/extract-iso.ps1` |
| 资源与原始字库导出 | `export-resources.ps1` |
| 原始图片导出 | `export-tilemap-images.ps1` |
| 文本及回写元数据导出 | `export-translation-catalog.ps1` |
| 输入与图片检查 | `check-assets.py`、`validate-image-edits.py` |
| 字库、译文、资源与镜像完整构建 | `build-full-translation.ps1` |
| 最终镜像回读校验 | `verify-translation-build.py`（完整构建自动调用） |

`export-executable-battle-text.py`、`export-executable-narrow-text.py`、MM/MEM 导出工具均需保留。它们生成从零构建所需的来源与字段元数据。

字库依赖包括 `build-chinese-fonts.py`、`build-packed-small-font.py`、`audit-small-font-tile-packing.py`、`patch-packed-font-ascii-probe.py`，以及 默认 8×12 模式使用的 `build-resident-small-font-probe.py`、`build-tall-small-font-probe.py`。这些文件虽有历史命名，仍参与正式构建。

`pac_codec.py`、`image_codec.py`、`event_cells.py`、`narrow_text_writeback.py` 以及 PowerShell 共享模块提供格式处理和边界校验。`chd-disc-tool.cpp` 在首次构建时编译为 ECC 辅助程序，不能省略。

开发用 `debug/`、`patches/`、`tests/`、旧审校映射、模拟器探针和图片制作工具不纳入公开仓库。正式构建使用仓库现有译文和 PNG，无需重放历史补丁或启动模拟器。
