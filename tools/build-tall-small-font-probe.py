#!/usr/bin/env python3
"""Isolated 8x16 menu/name experiment; never changes the production build.

Keep every existing 8x8 tile and byte code. Append contiguous 8x12 glyph pairs
for menu labels and MM short/full names. Only double-row writers use the pairs;
single-row HUD/counted writers keep the original packed mapping.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import runpy
import shutil

from PIL import ImageFont

from narrow_text_writeback import load_menu, load_shared, translated_status_names


ROOT = Path(__file__).resolve().parents[1]
# This gap precedes the existing hook payload. Check every byte before using it.
TALL_TABLE = 0x00219D80
VRAM_SOURCE_LIMIT = 0x1800


class Assembler:
    """Small literal/branch resolver for the SH-2 hooks used by this probe."""

    def __init__(self, address: int):
        self.address = address
        self.words: list[int | tuple] = []
        self.labels: dict[str, int] = {}
        self.literals: dict[str, tuple[str, int]] = {}

    def emit(self, *words):
        self.words.extend(words)

    def label(self, name):
        self.labels[name] = self.address + len(self.words) * 2

    def load(self, width, register, name, value):
        self.literals[name] = (width, value)
        self.emit((width, register, name))

    def finish(self):
        result = bytearray(len(self.words) * 2)
        locations = {}
        for width in ("word", "long"):
            alignment = 2 if width == "word" else 4
            while (self.address + len(result)) % alignment:
                result.append(0)
            for name, (kind, value) in self.literals.items():
                if kind == width:
                    locations[name] = self.address + len(result)
                    result.extend(value.to_bytes(alignment, "big"))
        while len(result) % 4:
            result.append(0)
        for index, item in enumerate(self.words):
            pc = self.address + index * 2
            if isinstance(item, int):
                word = item
            elif item[0] in ("bt", "bf", "bra"):
                delta = (self.labels[item[1]] - (pc + 4)) // 2
                bits = 12 if item[0] == "bra" else 8
                if not -(1 << (bits - 1)) <= delta < (1 << (bits - 1)):
                    raise ValueError("Branch out of range")
                word = {"bt": 0x8900, "bf": 0x8B00, "bra": 0xA000}[item[0]] | (delta & ((1 << bits) - 1))
            else:
                width, register, name = item
                base = pc + 4 if width == "word" else (pc + 4) & ~3
                stride = 2 if width == "word" else 4
                delta = locations[name] - base
                if delta < 0 or delta % stride or delta // stride > 255:
                    raise ValueError(f"Literal out of range: {name}")
                word = (0x9000 if width == "word" else 0xD000) | register << 8 | delta // stride
            result[index * 2:index * 2 + 2] = word.to_bytes(2, "big")
        return bytes(result)


def lookup(a, g):
    """r4 logical -> r2 top/r3 bottom, scratch r0/r1; keep r4-r15/PR."""
    a.emit(0x6043, 0x4000)  # mov r4,r0; shll r0 (word index)
    a.load("long", 1, "tall", TALL_TABLE)
    a.emit(0x021D, 0x622D, 0x2228, ("bt", "small"))
    a.emit(0x6323, 0x7301, ("bra", "mapped"), 0x0009)
    a.label("small")
    a.emit(0x6043)
    a.load("long", 1, "deltas", g["RUNTIME_TABLE"])
    a.emit(0x021C, 0x324C)
    a.load("word", 3, "blank", g["BLANK_TILE"])
    a.label("mapped")


def dual_writer(address, g):
    a = Assembler(address)
    a.emit(0x644D, 0x655D, 0x666D, 0x2F46)
    a.emit(0x4608, 0x4608, 0x366C, 0x4608, 0x355C, 0x365C)
    a.load("word", 1, "mask", 0x01FF)
    a.emit(0x2419, 0x747F, 0x7401)
    lookup(a, g)
    a.load("word", 1, "palette", 0x1000)
    a.emit(0x321C, 0x331C, 0x6063)
    a.load("long", 1, "top", 0x002E8000)
    a.emit(0x0125)
    a.load("long", 1, "bottom", 0x002E8080)
    a.emit(0x0135, 0xE701, 0x4718, 0x64F6, 0x000B, 0x0009)
    return a.finish()


def menu_writer(address, g, selected):
    a = Assembler(address)
    if selected:
        a.emit(0x671D, 0x2F36)  # relocated extu.w r1,r7; save map offset
    a.emit(0x2F46, 0x647C, 0x747F, 0x7401)
    lookup(a, g)
    a.emit(0x64F6)
    a.load("word", 1, "palette", 0x1000)
    a.emit(0x321C, 0x331C)
    if selected:
        a.emit(0x60F6, 0x0525, 0x6233)
        resume = g["MENU_RESUME"]
    else:
        a.emit(0x50E7, 0x0B25)
        resume = g["MENU_UNSELECTED_RESUME"]
    a.load("long", 1, "resume", resume)
    a.emit(0x412B, 0x0009)
    return a.finish()


def runtime():
    base = runpy.run_path(str(ROOT / "tools/patch-packed-font-ascii-probe.py"))
    g = base["patch_executable"].__globals__
    cursor = g["RUNTIME_DOUBLE_CODE"]
    for address_name, blob_name, factory in (
        ("RUNTIME_DOUBLE_CODE", "DOUBLE_WRITER", lambda at: dual_writer(at, g)),
        ("RUNTIME_STATUS_DOUBLE_CODE", "STATUS_DOUBLE_WRITER", lambda at: dual_writer(at, g)),
        ("RUNTIME_MENU_CODE", "MENU_WRITER", lambda at: menu_writer(at, g, True)),
        ("RUNTIME_NAME_CODE", "NAME_LOOP", lambda at: g["name_loop"](at, g["RUNTIME_STATUS_DOUBLE_CODE"])),
        ("RUNTIME_BYTE_CODE", "BYTE_LOOP", lambda at: g["fixed_byte_loop"](at, g["RUNTIME_STATUS_DOUBLE_CODE"])),
        ("RUNTIME_MENU_UNSELECTED_CODE", "MENU_UNSELECTED_WRITER", lambda at: menu_writer(at, g, False)),
    ):
        cursor = (cursor + 3) & ~3
        g[address_name] = cursor
        g[blob_name] = factory(cursor)
        cursor += len(g[blob_name])
    return g


def build(output: Path, build_root: Path | None = None,
          baseline_build_root: Path = ROOT / "work/build", temp_root: Path = ROOT / "temp"):
    output.mkdir(parents=True, exist_ok=True)
    p = runpy.run_path(str(ROOT / "tools/build-packed-small-font.py"))
    baseline_dir = output / "baseline-8x8"
    mapping = p["build"](baseline_dir)
    production = ROOT / "work/fonts/chinese"
    formal_mapping = json.loads((production / "mapping.json").read_text(encoding="utf-8"))
    if any(mapping[key] != formal_mapping[key] for key in ("glyphs", "byte_codes", "preserved_tiles")):
        raise ValueError("Production font/encoded resources are stale; rebuild the normal build first")
    packed = bytearray((baseline_dir / "ASCII.PAC.entry01.packed8.bin").read_bytes())
    if packed != (production / "ASCII.PAC.entry01.bin").read_bytes():
        raise ValueError("Production small-font bytes differ from current generated font")
    _, menu = load_menu(ROOT)
    report, names = load_shared(ROOT, "MM-entry08-narrow-byte")
    names = [target for record, target in zip(report["records"], names)
             if int(record["decoded_offset"]) < 46 * 11]
    requested = set("".join(menu + names + translated_status_names(ROOT)))
    tall_chars = [char for char in mapping["glyphs"] if char in requested]
    fb = p["load_font_builder"]()
    font = ImageFont.truetype(str(fb["SMALL_FONT"]), 12)
    supported = fb["font_codepoints"](fb["SMALL_FONT"])
    missing = [char for char in tall_chars if ord(char) not in supported]
    if missing:
        raise ValueError(f"Tall font lacks glyphs: {sorted(missing)}")
    baseline = fb["calculate_baseline"](font, tall_chars, 12)
    max_logical = int(mapping["highest_assigned_logical_index"], 16)
    table = bytearray((max_logical + 1) * 2)
    glyphs = {}
    pairs = {}
    for char in tall_chars:
        bitmap = fb["render_small_cell"](font, char, baseline)
        if bitmap not in pairs:
            pairs[bitmap] = len(packed) // 8
            packed.extend(bitmap)
        top = pairs[bitmap]
        logical = int(mapping["glyphs"][char]["logical_index"], 16)
        table[logical * 2:logical * 2 + 2] = top.to_bytes(2, "big")
        glyphs[char] = {"logical": logical, "top": top, "bottom": top + 1}
    if len(packed) > VRAM_SOURCE_LIMIT:
        raise ValueError(f"Probe needs {len(packed)} source bytes; only {VRAM_SOURCE_LIMIT} allowed")
    g = runtime()
    main = (production / "ASCII.PAC.entry00.bin").read_bytes()
    table_offset = TALL_TABLE - 0x00200000
    if TALL_TABLE + len(table) > g["RUNTIME_TABLE"]:
        raise ValueError("Tall table overlaps the existing runtime mapping")
    if len(main) < table_offset + len(table) or any(main[table_offset:]):
        raise ValueError("Main-font tail is no longer free; refusing to overwrite glyphs")
    patched_main = bytearray(g["patch_main_font"](
        main, (baseline_dir / "tile-deltas.bin").read_bytes()))
    patched_main[table_offset:table_offset + len(table)] = table
    builder = runpy.run_path(str(ROOT / "tools/build-translation-pacs.py"))
    builder["validate_translation_inputs"](ROOT, temp_root)
    program = bytearray(g["SOURCE"].read_bytes())
    source_program = bytes(program)
    builder["patch_menu"](ROOT, program, production)
    builder["patch_battle"](ROOT, program, production, source_program)
    builder["patch_weather_table"](ROOT, program, production, source_program)
    program = bytearray(g["patch_executable"](bytes(program)))
    program[0x30F28:0x30F2A] = (len(packed) - 1).to_bytes(2, "big")
    (output / "0").write_bytes(program)
    (output / "ASCII.PAC.entry00.bin").write_bytes(patched_main)
    (output / "ASCII.PAC.entry01.bin").write_bytes(packed)
    (output / "tall-tops.bin").write_bytes(table)
    fb["write_sheet"](bytes(packed), output / "tiles.png", 8, 8, 3, 32)
    fb["write_sheet"](b"".join(bytes(packed[x["top"] * 8:x["top"] * 8 + 16])
                              for x in glyphs.values()), output / "tall-glyphs.png", 8, 16, 4, 24)
    manifest = {"format": "wachenroeder-tall-small-font-probe-v1",
                "scope": "menu and MM short/full-name characters on double-row paths",
                "baseline_8x8_tiles": 544, "source_bytes": len(packed),
                "tiles": len(packed) // 8, "source_limit": VRAM_SOURCE_LIMIT,
                "new_tall_glyphs": len(glyphs), "font": fb["SMALL_FONT"].name,
                "cell": "8x16", "ink_cell": "8x12", "ink_y": 2,
                "table_address": TALL_TABLE, "table_bytes": len(table),
                "glyphs": glyphs,
                "hooks": {key: g[key] for key in g if key.startswith("RUNTIME_") and isinstance(g[key], int)}}
    if build_root is not None:
        source_dir = baseline_build_root / "repacked/pac"
        destination = build_root / "repacked/pac"
        destination.mkdir(parents=True, exist_ok=True)
        if destination.resolve() == source_dir.resolve():
            raise ValueError("Probe must use an isolated build root")
        # Preserve all previously built text/image replacements byte for byte.
        for source in source_dir.iterdir():
            if source.is_file():
                shutil.copy2(source, destination / source.name)
        (destination / "0").write_bytes(program)
        manifest["ascii_pac"] = builder["build_one"](
            ROOT / "unpacked/japan/ASCII.PAC", destination / "ASCII.PAC",
            {0: bytes(patched_main), 1: bytes(packed)})
        manifest["build_root"] = str(build_root)
        disc_manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        disc_manifest["files"] = [r for r in disc_manifest["files"] if r["file"] not in ("0", "ASCII.PAC")]
        disc_manifest["files"].extend([manifest["ascii_pac"], {
            "file": "0", "type": "runtime-tall-small-font-probe",
            "rebuilt_sha256": builder["sha256"](bytes(program)), "rebuilt_bytes": len(program)}])
        disc_manifest["font_experiment"] = {k: v for k, v in manifest.items() if k != "glyphs"}
        (destination / "manifest.json").write_text(json.dumps(disc_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "temp/tall-small-font-probe")
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--baseline-build-root", type=Path, default=ROOT / "work/build")
    parser.add_argument("--temp-root", type=Path, default=ROOT / "temp")
    args = parser.parse_args()
    result = build(args.output.resolve(), args.build_root.resolve() if args.build_root else None,
                   args.baseline_build_root.resolve(), args.temp_root.resolve())
    print(json.dumps({k: v for k, v in result.items() if k != "glyphs"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
