# 解包与汉化构建流程

## 1. 准备环境与输入

按[自备文件清单](自备文件清单.md)安装 Python、PowerShell 7、C++ 编译器和 Windows SDK，放置日版 BIN/CUE 与所需字体。仓库自带正式译文、汉化图片及稳定字码分配表。

```powershell
$ErrorActionPreference = 'Stop'
py -3 -m pip install -r requirements.txt
```

## 2. 首次提取与导出

在项目根目录逐步运行，每步成功后继续：

```powershell
$ErrorActionPreference = 'Stop'
pwsh -NoProfile -File ./analysis/extract-iso.ps1 -TrackPath 'roms/Wachenroeder (Japan).bin' -OutputDirectory ./unpacked/japan
pwsh -NoProfile -File ./tools/export-resources.ps1
pwsh -NoProfile -File ./tools/export-tilemap-images.ps1
pwsh -NoProfile -File ./tools/export-translation-catalog.ps1
python ./tools/check-assets.py
python ./tools/validate-image-edits.py
```

第一步提取原始 PAC/PS 和主程序 `0`。资源导出生成解码层、原始字库与字表；图片导出生成 `work/imageori` 及清单；文本导出生成 `work/text/catalog/japan`、主程序菜单与战斗 HUD 的 JSON、MM/MEM 单字节字段报告及译文模板。已有译文会被保留，来源选择器不一致时会报错。

原始镜像与 `unpacked/japan` 只作为输入，不会被汉化构建覆盖。无需英文补丁、模拟器、BIOS、开发测试、旧审校补丁或其他未入库的分析数据。

## 3. 构建

```powershell
$ErrorActionPreference = 'Stop'
pwsh -NoProfile -File ./tools/build-full-translation.ps1
```

默认使用 8×12 模式，需提供 `MZPXorig.ttf`；也可显式指定模式：

```powershell
$ErrorActionPreference = 'Stop'
pwsh -NoProfile -File ./tools/build-full-translation.ps1 -FontMode 8x12
```

完整构建自动依次刷新原文显示与共享字段边界、生成字库、校验与导入译文、重排事件、回写资源及主程序、生成 BIN/CUE，最后从生成镜像回读验证。字体缺字、控制字变化、字段越界或图片容量不足都会报错，请先解决错误再继续。

| 内容 | 默认路径 |
| --- | --- |
| 正式汉化文本、术语及背景资料 | `work/translation/` |
| 人工编辑图片 | `work/imageedit/` |
| 原始导出与来源元数据 | `work/extracted/`、`work/text/`、`work/imageori/`、`work/fonts/original/` |
| 可复用中文字库 | `work/fonts/chinese/` |
| 导入、重排等临时数据 | `temp/` |
| 默认 8×12 模式最终镜像 | `work/build-8x12/final/` |

## 4. 修改与验证

编辑文本时保留记录编号、偏移、长度、原文和控制标签，只改译文。`events/` 与 `narrow/` 都是有效输入，不能省略后者。修改图片时保持相对路径、尺寸与原调色板兼容。

两个已入库的中文字库 CSV 是已有字码分配记录，构建会读取并更新它们，请保留。字体、PNG 预览、二进制字库与构建清单由本地生成，不入库。

更换译文、图片或字体后重新运行完整构建。回读校验核对资源一致性；实际窗口显示、场景加载和游戏推进仍需自行检查。支持范围见[资源覆盖](text-coverage.md)。

当前译文需要 348 个新增小字，旧 8×8 模式仅容纳 340 个。保留的 `-FontMode 8x8` 仅供精简译文后的历史模式研究使用，公开流程使用 8×12，不能通过放宽旧字库容量检查绕过限制。
