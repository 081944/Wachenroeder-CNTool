#!/usr/bin/env python3
"""Estimate compact 1bpp source bytes and 8x8 VDP2 tile use."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

from PIL import ImageFont

from narrow_text_writeback import translated_characters as byte_characters
from narrow_text_writeback import writeback_code_map


ROOT = Path(__file__).resolve().parents[1]
FONT_BUILD = runpy.run_path(str(ROOT / "tools/build-chinese-fonts.py"))
SOURCE = (ROOT / "work/fonts/original/japan/ASCII.PAC.entry01.bin").read_bytes()
BLANK = bytes(8)
TILE_CAPACITY = 6144 // 16 * 2  # 6144 source bytes expand to 24 KiB / 32-byte tiles.


def translated_narrow_characters() -> list[str]:
    events = ROOT / "work/translation/events"
    candidates = []
    psel = events / "PSEL.PAC.txt"
    if psel.exists():
        candidates.extend(FONT_BUILD["translated_characters"]([psel]))
    for name in ("STDAT.PAC", "SHOPDATA.PAC"):
        template = events / f"{name}.txt"
        if not template.exists():
            continue
        catalog = json.loads(
            (ROOT / "work/text/catalog/japan" / f"{name}.json").read_text(encoding="utf-8")
        )
        narrow_ids = {
            str(record["id"]).zfill(4)
            for record in catalog["records"]
            if record.get("kind") == "counted-narrow-u16"
        }
        candidates.extend(FONT_BUILD["translated_characters"]([template], narrow_ids))
    candidates.extend(byte_characters(ROOT))
    return list(dict.fromkeys(candidates))


def original_glyph(code: int) -> bytes:
    index = code + 0x80
    return SOURCE[index * 16 : (index + 1) * 16]


def summarize(label: str, glyphs: dict[str, bytes], new_chars: set[str]) -> dict[str, int | str]:
    tiles = {BLANK}
    both = top_only = bottom_only = blank = 0
    packed_source_bytes = 0
    for glyph in glyphs.values():
        top, bottom = glyph[:8], glyph[8:]
        tiles.add(top)
        tiles.add(bottom)
        if top != BLANK and bottom != BLANK:
            both += 1
        elif top != BLANK:
            top_only += 1
        elif bottom != BLANK:
            bottom_only += 1
        else:
            blank += 1
    for character in glyphs:
        # This models an upload hook reading variable-height 1bpp records.
        # The existing uploader still expects fixed 16-byte records.
        if character.startswith("ascii-"):
            packed_source_bytes += 8
        elif character in new_chars:
            packed_source_bytes += 12
        else:
            packed_source_bytes += 16
    return {
        "set": label,
        "glyphs": len(glyphs),
        "both_halves": both,
        "top_only": top_only,
        "bottom_only": bottom_only,
        "blank": blank,
        "unique_8x8_tiles": len(tiles),
        "tile_capacity": TILE_CAPACITY,
        "spare_tiles": TILE_CAPACITY - len(tiles),
        "packed_4bpp_bytes": len(tiles) * 32,
        "packed_1bpp_source_bytes": packed_source_bytes,
        "two_byte_descriptors": len(glyphs) * 2,
        "source_plus_descriptors": packed_source_bytes + len(glyphs) * 2,
    }


def main() -> None:
    targets = translated_narrow_characters()
    original_codes = writeback_code_map()
    new_chars = [char for char in targets if char not in original_codes]
    new_char_set = set(new_chars)
    supported = FONT_BUILD["font_codepoints"](FONT_BUILD["SMALL_FONT"])
    missing = [char for char in new_chars if ord(char) not in supported]
    if missing:
        raise ValueError("Small TTF lacks translated characters: " + "".join(missing))
    font = ImageFont.truetype(str(FONT_BUILD["SMALL_FONT"]), 12)
    baseline = FONT_BUILD["calculate_baseline"](font, new_chars, 12)
    render = FONT_BUILD["render_small_cell"]

    translated = {
        char: original_glyph(original_codes[char]) if char in original_codes
        else render(font, char, baseline)
        for char in targets
    }
    plain_originals = {
        char: original_glyph(code) for char, code in original_codes.items()
    }
    ascii_8x8 = {
        f"ascii-{code:02X}": SOURCE[code * 16 : (code + 1) * 16]
        for code in range(0x20, 0x7F)
    }
    upper_ascii_8x8 = {
        f"ascii-{code:02X}": SOURCE[code * 16 : (code + 1) * 16]
        for code in range(0x20, 0x60)
    }
    all_ascii_8x8 = {
        f"ascii-{code:02X}": SOURCE[code * 16 : (code + 1) * 16]
        for code in range(0x80)
    }
    all_originals = {
        f"original-{index:03X}": SOURCE[index * 16 : (index + 1) * 16]
        for index in range(272)
    }
    new_only = {char: translated[char] for char in new_chars}
    han_only = {
        char: glyph for char, glyph in new_only.items()
        if FONT_BUILD["is_han_character"](char)
    }
    fixed_indices = (
        *range(0x000, 0x015),
        *range(0x080, 0x0B3),
        0x0FF,
        *range(0x107, 0x10A),
    )
    fixed_requested = {
        f"fixed-{index:03X}": SOURCE[index * 16 : (index + 1) * 16]
        for index in fixed_indices
    }
    compact_ascii_requested = {
        f"ascii-{index:02X}": SOURCE[index * 16 : (index + 1) * 16]
        for index in range(0x015, 0x080)
    }
    unassigned_kana = {
        f"fixed-{index:03X}": SOURCE[index * 16 : (index + 1) * 16]
        for index in range(0x0F0, 0x0FF)
    }
    requested_base = new_only | fixed_requested | compact_ascii_requested
    requested_han_only = han_only | fixed_requested | compact_ascii_requested
    report = {
        "translated_characters": len(targets),
        "new_characters": len(new_chars),
        "han_characters": sum(FONT_BUILD["is_han_character"](char) for char in targets),
        "render_baseline": baseline,
        "sets": [
            summarize("new-translated-glyphs", new_only, new_char_set),
            summarize("translated-targets-only", translated, new_char_set),
            summarize("targets-plus-upper-ascii-8x8", translated | upper_ascii_8x8, new_char_set),
            summarize("targets-plus-printable-ascii-8x8", translated | ascii_8x8, new_char_set),
            summarize("targets-plus-all-ascii-8x8", translated | all_ascii_8x8, new_char_set),
            summarize("targets-plus-original-plain-codes", translated | plain_originals, new_char_set),
            summarize(
                "targets-plus-original-plain-and-ascii-8x8",
                translated | plain_originals | ascii_8x8,
                new_char_set,
            ),
            summarize("targets-plus-all-original-uploaded", translated | all_originals, new_char_set),
            summarize("requested-ranges-0F0-0FE-preserved", requested_base | unassigned_kana, new_char_set),
            summarize("requested-ranges-0F0-0FE-reclaimed", requested_base, new_char_set),
            summarize("requested-ranges-han-only-0F0-0FE-reclaimed", requested_han_only, new_char_set),
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
