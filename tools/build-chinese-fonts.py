#!/usr/bin/env python3
"""Build replacement Saturn bitmap fonts from the project's TTF inputs.

The large font keeps the original prefix through text code 0x0172 and assigns
translated characters from the Japanese-kanji range beginning at 0x0173.
The small font reuses the original katakana cells and appends translated
small-font characters within the verified upload allocation. Its source
glyphs are rendered at 8x12 and centered in the game's 8x16 cell.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont
from narrow_text_writeback import (
    ALLOCATABLE_BYTE_CODES,
    assert_byte_capacity,
    translated_characters as narrow_template_characters,
    WEATHER_TABLE_TARGETS,
    writeback_code_map,
)
from fontTools.ttLib import TTFont


ROOT = Path(__file__).resolve().parents[1]
MAIN_FONT = ROOT / "work" / "fonts" / "ChillBitmap_16px.ttf"
SMALL_FONT = ROOT / "work" / "fonts" / "MZPXorig.ttf"
ORIGINAL_MAIN = ROOT / "work" / "fonts" / "original" / "japan" / "ASCII.PAC.entry00.bin"
ORIGINAL_SMALL = ROOT / "work" / "fonts" / "original" / "japan" / "ASCII.PAC.entry01.bin"
ORIGINAL_MAP = ROOT / "work" / "fonts" / "original" / "japan" / "font-map-provisional.csv"
OUTPUT = ROOT / "work" / "fonts" / "chinese"

TARGET_TAG = re.compile(r"<(?:H|C|P|G):[0-9A-F]{4}>|<[0-9A-F]{4}>")
MAIN_CODE_START = 0x0173
PREFIX_CODE_END = MAIN_CODE_START - 1
MAIN_CELL_SIZE = 16


def is_han_character(character: str) -> bool:
    """Return whether a translated character must use a newly rendered glyph."""
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def read_original_map(path: Path) -> tuple[dict[str, int], dict[int, str]]:
    by_character: dict[str, int] = {}
    by_code: dict[int, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            character = row.get("Character", "")
            if not character:
                continue
            code = int(row["TextCode"], 16)
            by_code[code] = character
            if code <= PREFIX_CODE_END and character not in by_character:
                by_character[character] = code
    return by_character, by_code


def translated_characters(paths: Iterable[Path], allowed_ids: set[str] | None = None) -> list[str]:
    seen: OrderedDict[str, None] = OrderedDict()
    for directory_or_file in paths:
        paths_to_read = [directory_or_file] if directory_or_file.is_file() else sorted(directory_or_file.glob("*.txt"))
        for path in paths_to_read:
            for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
                if not raw_line or raw_line.startswith("#"):
                    continue
                parts = raw_line.split(" | ", 2)
                if len(parts) < 3 or not parts[2].strip():
                    continue
                if allowed_ids is not None:
                    selector = re.match(r"^\[(\d{4})\]", parts[0])
                    if selector is None or selector.group(1) not in allowed_ids:
                        continue
                target = parts[2]
                for token in TARGET_TAG.split(target):
                    if token and not token.startswith("<"):
                        for character in token:
                            seen.setdefault(character, None)
    return list(seen)


def load_existing_allocations(path: Path, minimum_code: int) -> dict[str, int]:
    if not path.exists():
        return {}
    result: dict[str, int] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            character = row.get("Character", "")
            mapping = row.get("Mapping", "")
            if not character or mapping != "allocated":
                continue
            code = int(row["TextCode"], 16)
            if code >= minimum_code:
                result[character] = code
    return result


def font_codepoints(path: Path) -> set[int]:
    font = TTFont(str(path))
    result: set[int] = set()
    for table in font["cmap"].tables:
        result.update(table.cmap.keys())
    return result


def calculate_baseline(
    font: ImageFont.FreeTypeFont,
    characters: Iterable[str],
    height: int,
) -> int:
    """Choose one baseline that keeps every selected glyph inside the cell."""
    bboxes = [font.getbbox(character, anchor="ls") for character in characters]
    bboxes = [bbox for bbox in bboxes if bbox is not None]
    if not bboxes:
        raise ValueError("Cannot calculate a baseline without drawable glyphs")
    minimum = max(-bbox[1] for bbox in bboxes)
    maximum = min(height - bbox[3] for bbox in bboxes)
    if minimum > maximum:
        raise ValueError(
            f"Font glyphs do not fit in a {height}-pixel cell: "
            f"baseline range {minimum}..{maximum}"
        )
    return maximum


def render_cell(
    font: ImageFont.FreeTypeFont,
    character: str,
    width: int,
    height: int,
    baseline: int,
) -> bytes:
    bbox = font.getbbox(character, anchor="ls")
    if bbox is None or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError(f"Font has no drawable glyph for {character!r}")
    glyph_width = bbox[2] - bbox[0]
    glyph_height = bbox[3] - bbox[1]
    if glyph_width > width or glyph_height > height:
        raise ValueError(
            f"Glyph {character!r} is {glyph_width}x{glyph_height}, "
            f"larger than {width}x{height}"
        )
    if bbox[1] + baseline < 0 or bbox[3] + baseline > height:
        raise ValueError(
            f"Glyph {character!r} does not fit at baseline {baseline}: "
            f"y={bbox[1] + baseline}..{bbox[3] + baseline}"
        )
    image = Image.new("1", (width, height), 0)
    draw = ImageDraw.Draw(image)
    # Rendering to L and thresholding loses low-coverage pixels from
    # one-pixel strokes before the Saturn 1bpp data is written.
    draw.fontmode = "1"
    x = (width - glyph_width) // 2 - bbox[0]
    draw.text((x, baseline), character, font=font, fill=1, anchor="ls")
    result = bytearray((width + 7) // 8 * height)
    stride = (width + 7) // 8
    for row in range(height):
        for column in range(width):
            if image.getpixel((column, row)):
                result[row * stride + column // 8] |= 0x80 >> (column % 8)
    return bytes(result)


def render_small_cell(font: ImageFont.FreeTypeFont, character: str, baseline: int) -> bytes:
    # MZPXorig is an 8x12 design. Render that cell first, then center it in
    # the Saturn's 8x16 storage cell with two blank rows above and below.
    inner = render_cell(font, character, 8, 12, baseline)
    result = bytearray(16)
    result[2:14] = inner
    return bytes(result)


def write_sheet(data: bytes, path: Path, width: int, height: int, scale: int, columns: int) -> None:
    stride = (width + 7) // 8
    count = len(data) // (stride * height)
    pitch_x = width * scale + 1
    pitch_y = height * scale + 1
    rows = (count + columns - 1) // columns
    image = Image.new("RGB", (columns * pitch_x - 1, rows * pitch_y - 1), "black")
    for index in range(count):
        left = (index % columns) * pitch_x
        top = (index // columns) * pitch_y
        for row in range(height):
            for column in range(width):
                if data[index * stride * height + row * stride + column // 8] & (0x80 >> (column % 8)):
                    for dy in range(scale):
                        for dx in range(scale):
                            image.putpixel((left + column * scale + dx, top + row * scale + dy), (255, 255, 255))
    image.save(path)


def write_main_font(output: Path, characters: list[str], prefix_chars: dict[str, int]) -> dict[str, object]:
    original = ORIGINAL_MAIN.read_bytes()
    if len(original) % 32:
        raise ValueError("Original main font is not divisible into 32-byte glyphs")
    glyph_count = len(original) // 32
    max_code = 0x20 + glyph_count - 1
    existing_path = output / "font-map-chinese.csv"
    allocations = load_existing_allocations(existing_path, MAIN_CODE_START)
    next_code = max([MAIN_CODE_START - 1, *allocations.values()]) + 1
    for character in characters:
        # Keep the original digits, punctuation, Latin, hiragana and
        # katakana mappings. A Han character must use a freshly rasterized
        # glyph even when the Japanese provisional map contains it too.
        if character in prefix_chars and not is_han_character(character):
            continue
        if character not in allocations:
            if next_code > max_code:
                raise ValueError(f"Main font capacity exhausted at character {character!r}")
            allocations[character] = next_code
            next_code += 1

    supported = font_codepoints(MAIN_FONT)
    missing = sorted(character for character in allocations if ord(character) not in supported)
    if missing:
        raise ValueError(f"{MAIN_FONT.name} lacks glyphs: " + "".join(missing))
    font = ImageFont.truetype(str(MAIN_FONT), MAIN_CELL_SIZE)
    main_baseline = calculate_baseline(font, allocations, MAIN_CELL_SIZE)
    rebuilt = bytearray(original)
    for character, code in allocations.items():
        index = code - 0x20
        rebuilt[index * 32 : (index + 1) * 32] = render_cell(
            font, character, MAIN_CELL_SIZE, MAIN_CELL_SIZE, main_baseline
        )
    (output / "ASCII.PAC.entry00.bin").write_bytes(rebuilt)
    write_sheet(bytes(rebuilt), output / "ASCII.PAC.entry00.png", 16, 16, 2, 32)

    rows = []
    for index in range(glyph_count):
        code = 0x20 + index
        character = ""
        mapping = "unmapped"
        evidence = ""
        for candidate, candidate_code in prefix_chars.items():
            if candidate_code == code:
                character = candidate
                mapping = "preserved"
                evidence = "original prefix"
                break
        for candidate, candidate_code in allocations.items():
            if candidate_code == code:
                character = candidate
                mapping = "allocated"
                evidence = f"rendered from {MAIN_FONT.name} in monochrome mode"
                break
        rows.append({
            "GlyphIndex": f"0x{index:03X}",
            "TextCode": f"0x{code:04X}",
            "Character": character,
            "Mapping": mapping,
            "Evidence": evidence,
        })
    with (output / "font-map-chinese.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["GlyphIndex", "TextCode", "Character", "Mapping", "Evidence"], quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    return {
        "glyph_count": glyph_count,
        "storage_cell": "16x16",
        "source_render_cell": "16x16",
        "rasterization": "FreeType monochrome 1bpp",
        "baselines": {
            MAIN_FONT.name: main_baseline,
        },
        "preserved_prefix_code_end": f"0x{PREFIX_CODE_END:04X}",
        "allocation_start": f"0x{MAIN_CODE_START:04X}",
        "allocated_characters": len(allocations),
        "fallback_characters": 0,
        "allocated_last_code": f"0x{max(allocations.values(), default=MAIN_CODE_START - 1):04X}",
        "source_sha256": sha256(original),
        "output_sha256": sha256(bytes(rebuilt)),
    }


def write_small_font(output: Path, characters: list[str], byte_characters: list[str]) -> dict[str, object]:
    original = ORIGINAL_SMALL.read_bytes()
    if len(original) % 16:
        raise ValueError("Original small font is not divisible into 16-byte glyphs")
    glyph_count = len(original) // 16
    last_nonempty = max((index for index in range(glyph_count) if any(original[index * 16 : (index + 1) * 16])), default=-1)
    existing_path = output / "small-font-map.csv"
    existing: dict[str, int] = {}
    if existing_path.exists():
        with existing_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("Character") and row.get("Mapping") == "allocated":
                    existing[row["Character"]] = int(row["GlyphIndex"], 16)
    # Glyph index last_nonempty + 1 is the blank padding cell used by the
    # narrow u16 fields (text code 0x008A).  Keep it empty and allocate new
    # Chinese glyphs after it, matching the game's counted-string convention.
    space_index = last_nonempty + 1
    original_codes = writeback_code_map()
    ordered = list(dict.fromkeys(
        character for character in byte_characters + characters
        if character not in original_codes
    ))
    capacity = len(ALLOCATABLE_BYTE_CODES)
    byte_new = [character for character in byte_characters if character not in original_codes]
    if len(byte_new) > capacity:
        raise ValueError(
            f"Small font single-byte text needs {len(byte_new)} new glyphs, "
            f"but the reusable kana and appended byte codes provide only {capacity}"
        )
    if len(ordered) > capacity:
        raise ValueError(
            f"Small font needs {len(ordered)} new glyphs, "
            f"but the 6144-byte VRAM allocation exposes only {capacity} "
            "verified reusable byte codes"
        )
    available_indices = {code + 0x80 for code in ALLOCATABLE_BYTE_CODES}
    allocations = {
        character: index for character, index in existing.items()
        if character in ordered and index in available_indices
    }
    used_indices = set(allocations.values())
    for character in ordered:
        if character in allocations:
            continue
        index = next(index for index in sorted(available_indices) if index not in used_indices)
        allocations[character] = index
        used_indices.add(index)
    next_index = max((space_index + 1, *(index + 1 for index in allocations.values())))
    if next_index > glyph_count:
        original += bytes((next_index - glyph_count) * 16)
        glyph_count = next_index
    supported = font_codepoints(SMALL_FONT)
    missing = sorted(character for character in allocations if ord(character) not in supported)
    if missing:
        raise ValueError("Small font lacks glyphs: " + "".join(missing))
    font = ImageFont.truetype(str(SMALL_FONT), 12)
    baseline = calculate_baseline(font, allocations, 12)
    rebuilt = bytearray(original)
    for character, index in allocations.items():
        rebuilt[index * 16 : (index + 1) * 16] = render_small_cell(font, character, baseline)
    (output / "ASCII.PAC.entry01.bin").write_bytes(rebuilt)
    write_sheet(bytes(rebuilt), output / "ASCII.PAC.entry01.png", 8, 16, 2, 16)
    rows = []
    for index in range(glyph_count):
        character = next((item for item, item_index in allocations.items() if item_index == index), "")
        rows.append({
            "GlyphIndex": f"0x{index:03X}",
            "Character": character,
            "Mapping": "allocated" if character else ("original" if index <= last_nonempty else "unmapped"),
            "Evidence": "rendered from MZPXorig.ttf at 8x12 in monochrome mode, centered in 8x16" if character else "",
        })
    with (output / "small-font-map.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["GlyphIndex", "Character", "Mapping", "Evidence"], quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    return {
        "glyph_count": glyph_count,
        "storage_cell": "8x16",
        "source_render_cell": "8x12",
        "vertical_padding_rows": [2, 2],
        "rasterization": "FreeType monochrome 1bpp",
        "baseline": baseline,
        "last_original_nonempty_glyph": f"0x{last_nonempty:03X}",
        "allocation_start": f"0x{space_index + 1:03X}",
        "space_glyph_index": f"0x{space_index:03X}",
        "allocated_characters": len(allocations),
        "reused_kana_slots": sum(index in {code + 0x80 for code in ALLOCATABLE_BYTE_CODES[:86]} for index in allocations.values()),
        "source_sha256": sha256(original),
        "output_sha256": sha256(bytes(rebuilt)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--font-mode", choices=("8x8", "8x12"), default="8x8")
    args = parser.parse_args()
    if args.root.resolve() != ROOT.resolve():
        raise SystemExit("This script currently supports the checked-out project root only")
    # Only the resident 8x12 path requires MZPXorig. Production 8x8 uses
    # the separate fusion-pixel face loaded by the packed-font builder.
    required_fonts = (SMALL_FONT,) if args.font_mode == '8x12' else ()
    for path in (MAIN_FONT, ORIGINAL_MAIN, ORIGINAL_SMALL, ORIGINAL_MAP, *required_fonts):
        if not path.is_file():
            raise SystemExit(
                f"Missing required input: {path} (see docs/自备文件清单.md)"
            )
    # Fonts are reusable project artifacts, not per-build intermediates.
    # Keep them beside the supplied source fonts under work/fonts.
    output = ROOT / "work" / "fonts" / "chinese"
    output.mkdir(parents=True, exist_ok=True)
    prefix_chars, _ = read_original_map(ORIGINAL_MAP)
    events_dir = ROOT / "work" / "translation" / "events"
    all_characters = translated_characters([events_dir])
    # Dynamic tactical weather values live in the executable's 16-bit table,
    # so they are not present in the PAC translation catalog. Reserve their
    # large-font glyphs alongside ordinary translated event text.
    all_characters = list(dict.fromkeys(all_characters + list("".join(WEATHER_TABLE_TARGETS))))
    # Only confirmed counted fields in these resources use the 8x16 font.
    small_characters = []
    psel_path = events_dir / "PSEL.PAC.txt"
    if psel_path.exists():
        small_characters.extend(translated_characters([psel_path]))
    for file_name in ("STDAT.PAC", "SHOPDATA.PAC"):
        template_path = events_dir / f"{file_name}.txt"
        if not template_path.exists():
            continue
        catalog_path = ROOT / "work" / "text" / "catalog" / "japan" / f"{file_name}.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        narrow_ids = {
            str(record["id"]).zfill(4)
            for record in catalog["records"]
            if record.get("kind") == "counted-narrow-u16"
        }
        small_characters.extend(translated_characters([template_path], narrow_ids))
    small_characters = list(dict.fromkeys(small_characters))
    byte_characters = narrow_template_characters(ROOT)
    main_result = write_main_font(output, all_characters, prefix_chars)
    packed_path = ROOT / "tools/build-packed-small-font.py"
    spec = importlib.util.spec_from_file_location("packed_small_font", packed_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {packed_path}")
    packed_builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packed_builder)
    small_result = packed_builder.build(output, resident_preparation=args.font_mode == "8x12")
    (output / "ASCII.PAC.entry01.bin").write_bytes(
        (output / "ASCII.PAC.entry01.packed8.bin").read_bytes()
    )
    manifest = {
        "format": "wachenroeder-chinese-fonts-v1",
        "main_font": MAIN_FONT.name,
        "main_font_fallback": None,
        "font_mode": args.font_mode,
        "small_font": (f"{SMALL_FONT.name} (resident 8x12; 8px samples for battle labels)"
                       if args.font_mode == "8x12" else f"{packed_builder.FONT.name} (packed 8x8)"),
        "main": main_result,
        "small": small_result,
        "translated_visible_characters": len(all_characters),
        "small_candidate_characters": len(small_characters),
        "note": "Narrow-byte escape codes and physical tiles are in mapping.json.",
    }
    (output / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
