# Wachenroeder 土星版汉化工具

本仓库提供从日版 BIN/CUE 镜像解包、导出资源、应用现有中文文本与图片、生成字库、回写资源到构建汉化镜像的完整工具流程。

仓库包含 `work/translation/` 中的汉化文本与术语表、`work/imageedit/` 中的汉化图片，以及稳定字码分配所需的 CSV。游戏镜像、字体、提取资源、构建产物、模拟器调试工具和开发测试不随仓库提供。

## 环境与自备文件

- Windows，PowerShell 7（`pwsh`）、Python 3.10 或更高。
- MSVC（`cl` 或 `clang-cl`）与 Windows SDK 10；首次构建会编译扇区 ECC 辅助程序。
- 自备日版 `roms/Wachenroeder (Japan).bin` 与同名 `.cue`。
- 自备 `work/fonts/ChillBitmap_16px.ttf` 与 `work/fonts/fusion-pixel-8px-proportional-zh_hans.ttf`。
- 默认 8×12 字库模式还需 `work/fonts/MZPXorig.ttf`。

文件格式与字体要求见[自备文件清单](docs/自备文件清单.md)。运行以下命令安装 Python 依赖：

```powershell
$ErrorActionPreference = 'Stop'
py -3 -m pip install -r requirements.txt
```

## 首次解包与资源准备

在项目根目录的 PowerShell 7 中依次运行；每一步成功后再继续：

```powershell
$ErrorActionPreference = 'Stop'
pwsh -NoProfile -File ./analysis/extract-iso.ps1 -TrackPath 'roms/Wachenroeder (Japan).bin' -OutputDirectory ./unpacked/japan
pwsh -NoProfile -File ./tools/export-resources.ps1
pwsh -NoProfile -File ./tools/export-tilemap-images.ps1
pwsh -NoProfile -File ./tools/export-translation-catalog.ps1
```

资源导出会从原盘生成解码数据、原文字表、原始图片及回写元数据。文本目录导出包含主程序菜单、战斗 HUD、MM/MEM 共享字段；已有非空汉化译文不会被空模板覆盖，来源不匹配会报错。

准备完成后检查输入：

```powershell
$ErrorActionPreference = 'Stop'
python ./tools/check-assets.py
python ./tools/validate-image-edits.py
```

## 构建汉化游戏

```powershell
$ErrorActionPreference = 'Stop'
pwsh -NoProfile -File ./tools/build-full-translation.ps1
```

也可双击根目录 `build-full-translation.bat`。脚本依次刷新已确认的文本元数据、生成中文字库、校验译文、导入文本、重排事件、回写 PAC/PS 和主程序，再构建 BIN/CUE 并回读验证。

- 默认使用 8×12 模式，输出 `work/build-8x12/final/`；`build-full-translation-8x12.bat` 也使用同一路径。
- 历史 `-FontMode 8x8` 选项仍保留，但当前完整译文包含 348 个新增小字，超过旧模式的 340 字容量，不能用于直接构建当前译文。
- 临时数据写入 `temp/`；可复用字库写入 `work/fonts/chinese/`。

完整步骤与输出说明见[构建流程](docs/translation-build-workflow.md)，支持范围和限制见[资源覆盖](docs/text-coverage.md)。

## 修改汉化素材

正式文本在 `work/translation/events/` 和 `work/translation/narrow/`；术语表与背景资料也保留在 `work/translation/`。修改时保留编号、来源选择器和控制标签，仅编辑译文。

汉化图片在 `work/imageedit/`，相对路径须与导出的 `work/imageori/` 对应。回写使用原版调色板和资源容量限制，超限会报错。两个已跟踪的字库 CSV 用于保持字码分配稳定，请保留。

汉化镜像生成后仍需自行在兼容的土星模拟器或硬件中检查显示与运行效果；资源回读校验不等于完整游戏通关测试。
