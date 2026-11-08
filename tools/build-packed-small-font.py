#!/usr/bin/env python3
"""Build the proposed 8x8-packed narrow font without changing the live build."""

from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
import runpy
from pathlib import Path

from PIL import ImageFont

from narrow_text_writeback import (
    SHARED_STEMS,
    load_battle,
    load_menu,
    load_shared,
    original_code_map,
    writeback_code_map,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "work/fonts/original/japan/ASCII.PAC.entry01.bin"
FONT = ROOT / "work/fonts/fusion-pixel-8px-proportional-zh_hans.ttf"
LIMIT = 0x1100
FULL_HEIGHT = tuple(range(0x000, 0x015))
UPPER_HALF = tuple(range(0x015, 0x080))
REDRAW = (*range(0x080, 0x0B3), 0x0FF, *range(0x107, 0x10A))
ESCAPE_PREFIX = 0x7F
SINGLE_BYTE_LIMIT = 200
DASH_INDEX = 0x0B0
FIXED_BLANK_TILE = 0x40


def load_font_builder() -> dict:
    return runpy.run_path(str(ROOT / "tools/build-chinese-fonts.py"))


def translated_new_characters() -> list[str]:
    spec = importlib.util.spec_from_file_location(
        "small_font_audit", ROOT / "tools/audit-small-font-tile-packing.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load narrow-font translation audit")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    preserved = writeback_code_map()
    return [char for char in module.translated_narrow_characters() if char not in preserved]


def packed_logical_indices(count: int) -> list[int]:
    candidates = (
        *range(0x0B3, 0x0FF),
        *range(0x100, 0x107),
        *range(0x10B, 0x10B + count),
    )
    return list(candidates[:count])


def byte_text_rows() -> list[tuple[str, int]]:
    rows = []
    for stem in SHARED_STEMS:
        report, targets = load_shared(ROOT, stem)
        rows.extend((target.rstrip(" "), int(record["byte_length"]))
                    for record, target in zip(report["records"], targets) if target)
    return rows


def prioritize_single_byte(characters: list[str]) -> tuple[list[str], int]:
    required = set(characters)
    rows = byte_text_rows()
    frequency = Counter(char for target, _ in rows for char in target)
    _, menu_targets = load_menu(ROOT)
    single = {char for target in menu_targets for char in target if char in required}
    _, battle_targets = load_battle(ROOT)
    battle_chars = list(dict.fromkeys(
        char for target in battle_targets for char in target if char in required
    ))
    single.update(battle_chars)
    for target, capacity in rows:
        if len(target) == capacity:
            single.update(char for char in target if char in required)

    while True:
        violations = []
        for target, capacity in rows:
            overflow = len(target) + sum(char in required and char not in single for char in target) - capacity
            if overflow > 0:
                violations.append((target, overflow))
        if not violations:
            break
        scores = Counter()
        for target, overflow in violations:
            counts = Counter(char for char in target if char in required and char not in single)
            for char, count in counts.items():
                scores[char] += min(count, overflow)
        if not scores:
            raise ValueError("Byte-text row cannot fit the escape encoding")
        single.add(max(scores, key=lambda char: (scores[char], frequency[char], -characters.index(char))))
        if len(single) > SINGLE_BYTE_LIMIT:
            raise ValueError("Byte-text rows need more than 200 single-byte glyphs")

    for char in sorted(characters, key=lambda char: (-frequency[char], characters.index(char))):
        if len(single) == SINGLE_BYTE_LIMIT:
            break
        single.add(char)
    ordered = battle_chars + [char for char in characters
                              if char in single and char not in battle_chars]
    ordered.extend(char for char in characters if char not in single)
    if len(ordered) != len(characters) or len(single) != SINGLE_BYTE_LIMIT:
        raise AssertionError(
            f"Narrow glyph ordering is incomplete: ordered={len(ordered)}, "
            f"required={len(characters)}, single={len(single)}"
        )
    return ordered, len(rows)


def downsample_original(glyph: bytes) -> bytes:
    if len(glyph) != 16:
        raise ValueError("Expected one original 8x16 glyph")
    return bytes(glyph[row * 2] | glyph[row * 2 + 1] for row in range(8))


def build(output: Path, *, resident_preparation: bool = False) -> dict:
    """Prepare encoding and 8px samples, or build the uploadable 8x8 font.

    The 8x12 builder uses the same logical codes but allocates its own atlas.
    Its temporary 8px samples are not subject to the old upload byte limit.
    """
    if not FONT.is_file():
        raise SystemExit(
            f"Missing small font: {FONT} (see docs/自备文件清单.md)"
        )
    source = SOURCE.read_bytes()
    if len(source) != 336 * 16:
        raise ValueError("Original narrow font size changed")
    characters, byte_row_count = prioritize_single_byte(translated_new_characters())
    if len(characters) != len(set(characters)):
        raise ValueError("Translation glyph list contains duplicates")
    font_builder = load_font_builder()
    font = ImageFont.truetype(str(FONT), 8)
    inverse_codes = {code + 0x80: char for char, code in original_code_map().items()}
    renderable = [char for char in characters if char != " "]
    renderable.extend(
        inverse_codes[index] for index in REDRAW
        if index in inverse_codes and inverse_codes[index] != " "
    )
    supported = font_builder["font_codepoints"](FONT)
    missing = [char for char in renderable if ord(char) not in supported]
    if missing:
        raise ValueError("8x8 font lacks glyphs: " + "".join(dict.fromkeys(missing)))
    baseline = font_builder["calculate_baseline"](font, renderable, 8)

    packed = bytearray()
    preserved_tiles: dict[str, int] = {}
    for index in FULL_HEIGHT:
        preserved_tiles[f"0x{index:03X}"] = len(packed) // 8
        packed.extend(source[index * 16 : (index + 1) * 16])
    for index in (*UPPER_HALF, *REDRAW):
        preserved_tiles[f"0x{index:03X}"] = len(packed) // 8
        original = source[index * 16 : (index + 1) * 16]
        if index in UPPER_HALF:
            if any(original[8:]):
                raise ValueError(f"0x{index:03X} is not an upper-only 8x8 glyph")
            packed.extend(original[:8])
        else:
            character = inverse_codes.get(index)
            packed.extend(
                bytes(8) if character == " " else
                downsample_original(original) if index == DASH_INDEX else
                font_builder["render_cell"](font, character, 8, 8, baseline)
                if character is not None else downsample_original(original)
            )

    # Battle-status backgrounds use physical tile 0x40 as a blank without
    # going through a character writer. Keep that tile empty after packing.
    space_key = "0x020"
    plus_key = "0x02B"
    space_tile = preserved_tiles[space_key]
    plus_tile = preserved_tiles[plus_key]
    if plus_tile != FIXED_BLANK_TILE or any(packed[space_tile * 8:space_tile * 8 + 8]):
        raise ValueError("Fixed battle-status blank tile layout changed")
    packed[space_tile * 8:space_tile * 8 + 8], packed[plus_tile * 8:plus_tile * 8 + 8] = (
        packed[plus_tile * 8:plus_tile * 8 + 8], packed[space_tile * 8:space_tile * 8 + 8]
    )
    preserved_tiles[space_key], preserved_tiles[plus_key] = plus_tile, space_tile

    logical_indices = packed_logical_indices(len(characters))
    glyphs: dict[str, dict[str, int | str]] = {}
    byte_codes: dict[str, list[int]] = {}
    for character, logical_index in zip(characters, logical_indices):
        tile = len(packed) // 8
        glyphs[character] = {
            "logical_index": f"0x{logical_index:03X}",
            "tile": tile,
        }
        runtime_code = logical_index - 0x80
        if runtime_code > 0x1FE:
            raise ValueError("Narrow font exceeds the one-byte extension-code range")
        byte_codes[character] = (
            [runtime_code] if runtime_code <= 0xFF
            else [ESCAPE_PREFIX, runtime_code - 0xFF]
        )
        packed.extend(font_builder["render_cell"](font, character, 8, 8, baseline))

    if sum(len(code) == 1 for code in byte_codes.values()) != SINGLE_BYTE_LIMIT:
        raise AssertionError("Single-byte code count differs from the planned 200 slots")
    for target, capacity in byte_text_rows():
        encoded_length = sum(len(byte_codes.get(char, [0])) for char in target)
        if encoded_length > capacity:
            raise ValueError(f"Narrow byte target still exceeds {capacity} bytes: {target}")

    used_bytes = len(packed)
    if not resident_preparation and used_bytes > LIMIT:
        raise ValueError(f"Packed small font needs {len(packed)} bytes; limit is {LIMIT}")
    if len(packed) % 8:
        raise AssertionError("8x8 font is not tile-aligned")
    # The runtime always uploads the complete original 0x1100-byte range.
    # Unassigned tail tiles are intentionally blank and must still be present.
    if not resident_preparation:
        packed.extend(bytes(LIMIT - len(packed)))
    logical_tiles = {int(code, 16): tile for code, tile in preserved_tiles.items()}
    logical_tiles.update(
        (int(item["logical_index"], 16), int(item["tile"]))
        for item in glyphs.values()
    )
    # Text code 0x8A is fixed-width padding, so keep it blank while its old
    # physical slot is reused for a new Chinese glyph.
    blank_tile = logical_tiles[0x080]
    if any(packed[blank_tile * 8:blank_tile * 8 + 8]):
        raise ValueError("Preserved 0x080 tile is not blank")
    logical_tiles[0x10A] = blank_tile
    deltas = bytearray(0x400)
    for logical_index, tile in logical_tiles.items():
        delta = tile - logical_index
        if not -128 <= delta <= 127:
            raise ValueError(f"Tile delta for 0x{logical_index:03X} is not signed 8-bit")
        deltas[logical_index] = delta & 0xFF
    output.mkdir(parents=True, exist_ok=True)
    (output / "ASCII.PAC.entry01.packed8.bin").write_bytes(packed)
    (output / "tile-deltas.bin").write_bytes(deltas)
    font_builder["write_sheet"](bytes(packed), output / "ASCII.PAC.entry01.packed8.png", 8, 8, 2, 32)
    report = {
        "format": "wachenroeder-packed-small-font-v1",
        "font_mode": "8x12" if resident_preparation else "8x8",
        "runtime_upload_ready": not resident_preparation,
        "source_bytes": len(packed),
        "used_bytes": used_bytes,
        "source_limit": None if resident_preparation else LIMIT,
        "vdp2_tiles": len(packed) // 8,
        "vdp2_tile_limit": None if resident_preparation else LIMIT // 8,
        "baseline": baseline,
        "unchanged_8x16": len(FULL_HEIGHT),
        "upper_only_8x8": len(UPPER_HALF),
        "redrawn_8x8": len(REDRAW),
        "new_8x8": len(glyphs),
        "single_byte_new_glyphs": SINGLE_BYTE_LIMIT,
        "extended_new_glyphs": len(glyphs) - SINGLE_BYTE_LIMIT,
        "byte_text_rows_checked": byte_row_count,
        "escape_prefix": f"0x{ESCAPE_PREFIX:02X}",
        "tile_delta_bytes": len(deltas),
        "highest_assigned_logical_index": f"0x{max(logical_tiles):03X}",
        "tile_delta_min": min(tile - index for index, tile in logical_tiles.items()),
        "tile_delta_max": max(tile - index for index, tile in logical_tiles.items()),
        "preserved_tiles": preserved_tiles,
        "glyphs": glyphs,
        "byte_codes": byte_codes,
        "blank_text_code": "0x8A",
        "blank_tile": blank_tile,
        "blank_text_code_requires_alias": True,
    }
    (output / "mapping.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "temp/packed-small-font")
    args = parser.parse_args()
    report = build(args.output.resolve())
    print(json.dumps({key: value for key, value in report.items()
                      if key not in {"preserved_tiles", "glyphs", "byte_codes"}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
