# 光盘提取与解码

此目录随仓库提供三个脚本及两张人工整理表：

| 文件 | 用途 |
| --- | --- |
| `extract-iso.ps1` | 从日版 BIN 的 MODE1/2352 数据轨提取 ISO9660 文件到 `unpacked/japan` |
| `decode-samples.ps1` | PAC/PS 拆分与解压，生成 `decoded-samples` |
| `extract-text-samples.ps1` | 从解码结果与人工整理表生成原文字码映射 |
| `event-control-map.csv` | 事件控制码定义 |
| `japan-extra-glyphs.csv` | 补充日文字形映射 |

`tools/export-resources.ps1` 会调用解码与字码映射脚本，无需在它之前手动重复执行。原盘、解码数据、探针和验证日志不随仓库提供。

首次使用见[项目说明](../README.md)与[自备文件清单](../docs/自备文件清单.md)。
