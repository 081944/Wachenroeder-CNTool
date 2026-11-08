#!/usr/bin/env python3
"""Report whether the files the build needs but Git does not store are present.

Read-only: nothing is created, moved or rewritten. Exit status is 1 when a
required file or tool is missing, so the check can gate a build.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CUE = ROOT / "roms/Wachenroeder (Japan).cue"
BIN = ROOT / "roms/Wachenroeder (Japan).bin"
UNPACKED = [
    ("日版提取目录", ROOT / "unpacked/japan", True),
]
DECODED = ROOT / "analysis/decoded-samples/font-map.csv"
FONTS_MAIN = [
    ("主字库", ROOT / "work/fonts/ChillBitmap_16px.ttf"),
    ("窄字库", ROOT / "work/fonts/fusion-pixel-8px-proportional-zh_hans.ttf"),
]
FONTS_8X12 = [
    ("8×12 小字库", ROOT / "work/fonts/MZPXorig.ttf"),
]
ORIGINAL_FONTS = [
    ("原版大字形", ROOT / "work/fonts/original/japan/ASCII.PAC.entry00.bin"),
    ("原版窄字形", ROOT / "work/fonts/original/japan/ASCII.PAC.entry01.bin"),
    ("暂定字表", ROOT / "work/fonts/original/japan/font-map-provisional.csv"),
]
TOOLS = [
    ("PowerShell 7", "pwsh"),
    ("C++ 编译器", "clang-cl"),
]


def describe(path: Path) -> str:
    if path.is_dir():
        return "目录"
    return f"{path.stat().st_size:,} 字节" if path.is_file() else "缺失"


def pad(text: str, width: int) -> str:
    """Pad by terminal columns so CJK labels line up with ASCII paths."""
    used = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - used)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict", action="store_true",
        help="also require the C++ compiler on PATH",
    )
    parser.add_argument("--font-mode", choices=("8x8", "8x12"), default="8x12")
    args = parser.parse_args()

    missing: list[str] = []

    def check(path: Path, label: str, required: bool = True) -> None:
        ok = path.exists()
        if not ok and required:
            missing.append(str(path))
        mark = "OK  " if ok else ("缺失" if required else "可选")
        rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
        print(f"[{mark}] {pad(label, 16)} {pad(str(rel), 56)} {describe(path)}")

    print("原盘")
    check(CUE, "日版 CUE")
    check(BIN, "日版 BIN")

    print("\n提取与解码")
    for label, path, required in UNPACKED:
        check(path, label, required=required)
    check(DECODED, "解码字表")

    print("\n字体")
    for label, path in FONTS_MAIN:
        check(path, label)
    for label, path in FONTS_8X12:
        check(path, label, required=args.font_mode == "8x12")

    print("\n导出字库")
    for label, path in ORIGINAL_FONTS:
        check(path, label)

    print("\n工具链")
    for label, command in TOOLS:
        found = shutil.which(command)
        required = args.strict or command == "pwsh"
        if not found and required:
            missing.append(f"{label} ({command})")
        mark = "OK  " if found else ("缺失" if required else "可选")
        print(f"[{mark}] {pad(label, 16)} {pad(command, 56)} {found or '-'}")
    for module, label in (("PIL", "Pillow"), ("fontTools", "fonttools")):
        found = importlib.util.find_spec(module) is not None
        if not found:
            missing.append(f"{label} (pip)")
        print(f"[{'OK  ' if found else '缺失'}] {pad(label, 16)} {pad('python package', 56)} {'已安装' if found else '未安装'}")

    if missing:
        print(f"\n缺少 {len(missing)} 项必需内容，见 docs/自备文件清单.md")
        for item in missing:
            print(f"  {item}")
        return 1
    print("\n必需内容齐全")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
