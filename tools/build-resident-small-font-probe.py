#!/usr/bin/env python3
"""Build a full-height font in two explicitly bounded VDP2 regions.

Keep original non-kana pixels and the original physical prefix. Do not move
the dynamic text buffer at 0x25E66000..0x25E695FF. Overflow tiles use the
candidate gap 0x25E6C000..0x25E6FFFF; runtime ownership must be verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import struct

from PIL import ImageFont

from narrow_text_writeback import load_battle, patch_menu, patch_battle, patch_weather_table
from pac_codec import _bpe_block, _encode_sparse_dictionary, build_pac, decode_entry, encode_entry_best


ROOT = Path(__file__).resolve().parents[1]
TABLE = 0x00219D80
LOW_START, LOW_END = 0x25E60000, 0x25E66000
# Includes the adjacent V3 candidate documented in memory-gap-audit.md.
# Stop before the other graphics-mode base at 0x25E70000.
HIGH_START, HIGH_END = 0x25E6C000, 0x25E70000
LOW_TILES = (LOW_END - LOW_START) // 32
HIGH_TILES = (HIGH_END - HIGH_START) // 32
HIGH_INDEX = (HIGH_START - LOW_START) // 32


def sha(data):
    return hashlib.sha256(data).hexdigest()


def adaptive_stream(raw):
    """Shortest-path BPE partition, with every decoded block <= 5000 bytes."""
    costs = {0: 0}
    previous = {}
    encoded = {}
    for index, start in enumerate(range(0, len(raw), 1000)):
        if start not in costs:
            continue
        for size in (2000, 3000, 4000, 5000):
            end = min(start + size, len(raw))
            key = (start, end)
            if key not in encoded:
                definitions, tokens = _bpe_block(raw[start:end])
                encoded[key] = _encode_sparse_dictionary(definitions) + struct.pack(">H", len(tokens)) + bytes(tokens)
            cost = costs[start] + len(encoded[key])
            if cost < costs.get(end, float("inf")):
                costs[end] = cost
                previous[end] = start
        if index % 20 == 0:
            print(f"Adaptive font compression: {start}/{len(raw)}", flush=True)
    end = len(raw)
    path = []
    while end:
        start = previous[end]
        path.append((start, end))
        end = start
    path.reverse()
    payload = b"".join(encoded[key] for key in path)
    result = len(payload).to_bytes(3, "big") + payload
    if decode_entry(result) != raw:
        raise ValueError("Adaptive stream round-trip failed")
    return result, [end - start for start, end in path]


def pair_lookup(a):
    a.load("word", 1, "original_limit", 0x0B3)
    a.emit(0x3412, ("bf", "original"))
    mapping = json.loads((ROOT / "work/fonts/chinese/mapping.json").read_text(encoding="utf-8"))
    logical_limit = int(mapping["highest_assigned_logical_index"], 16) + 1
    a.load("word", 1, "logical_limit", logical_limit)
    a.emit(0x3412, ("bt", "invalid"))
    a.emit(0x6043, 0x7080, 0x70CD, 0x4008)  # (logical - 0xB3) * 4
    a.load("long", 1, "map", TABLE)
    a.emit(0x021D, 0x622D, 0x7002, 0x031D, 0x633D)
    a.emit(("bra", "mapped"), 0x0009)
    a.label("original")
    a.emit(0x6243, 0x322C, 0x6323, 0x7301, ("bra", "mapped"), 0x0009)
    a.label("invalid")
    a.load("word", 2, "blank_pair", 0x40)
    a.emit(0x6323)
    a.label("mapped")


def writer(address, A, kind, single_table=None):
    a = A(address)
    byte = kind == "byte"
    if byte:
        # Original 0600F2A8 leaves r2/r3 intact. Numeric formatters and UI
        # callers keep live state there across individual character draws.
        a.emit(0x2F26, 0x2F36)
    a.emit(0x644C if byte else 0x644D, 0x655C if byte else 0x655D,
           0x666C if byte else 0x666D, 0x2F46)
    a.emit(0x4608, 0x4608, 0x366C, 0x4608, 0x355C, 0x365C)
    if kind == "double":
        a.load("word", 1, "mask", 0x01FF)
        a.emit(0x2419, 0x747F, 0x7401)
    elif kind == "word":
        a.load("word", 1, "mask", 0x03FF)
        a.emit(0x2419)
    if byte:
        # The battle UI remains 8x8. Use its explicit small-glyph bank only
        # in the single-row entry; double-row fields still use the high pair.
        a.load("word", 1, "single_first", 0xB3)
        a.emit(0x3412, ("bf", "regular_pair"), 0x6043, 0x7080, 0x70CD, 0x4000)
        a.load("long", 1, "single_table", single_table)
        a.emit(0x021D, 0x622D)
        a.load("word", 1, "single_missing", 0xFFFF)
        a.emit(0x611D, 0x3210, ("bt", "regular_pair"))
        a.load("word", 3, "single_blank", 0x40)
        a.emit(("bra", "byte_mapped"), 0x0009)
        a.label("regular_pair")
    pair_lookup(a)
    if byte:
        a.label("byte_mapped")
    if not byte:
        a.load("word", 1, "palette", 0x1000)
        a.emit(0x321C, 0x331C)
    a.emit(0x6063)
    a.load("long", 1, "top", 0x002E8000)
    a.emit(0x0125)
    if kind != "double":
        a.load("word", 1, "ascii_limit", 0xB3)
        a.emit(0x3412, ("bf", "done"))
    if byte:
        # Original byte ASCII is genuinely single-row. Only added Chinese
        # glyphs have nonblank lower halves here; do not clear the next HUD row.
        a.load("word", 1, "blank", 0x40)
        a.emit(0x3310, ("bt", "done"))
    a.load("long", 1, "bottom", 0x002E8080)
    a.emit(0x0135)
    if kind != "double":
        a.label("done")
    if kind == "double":
        a.emit(0xE701, 0x4718)
    a.emit(0x64F6)
    if byte:
        a.emit(0x63F6, 0x62F6)
    a.emit(0x000B, 0x0009)
    return a.finish()


def menu_writer(address, A, g, selected):
    a = A(address)
    if selected:
        a.emit(0x671D, 0x2F36)
    a.emit(0x2F46, 0x647C, 0x747F, 0x7401)
    pair_lookup(a)
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


def uploader(address, A, count):
    a = A(address)
    a.emit(0x2F86, 0x2F96, 0x4F22)
    a.load("long", 8, "source", 0x00240000 + LOW_TILES * 8)
    a.load("word", 9, "limit", count * 8 - 1)
    a.load("long", 4, "dest", HIGH_START)
    a.load("long", 5, "dest2", HIGH_START + 2)
    a.load("long", 6, "lut", 0x060655F4)
    a.emit(0xE700, 0xE300)
    a.label("loop")
    # Original 1bpp -> 4bpp expansion loop, identical pixel/palette semantics.
    a.emit(*[int(x, 16) for x in (
        "6073 018C 621C 6123 4109 4109 311C 6013 016D 7701 "
        "6033 0415 E00F 2209 322C 6023 016D 3797 6033 0515"
    ).split()])
    # cmp/gt r9,r7 above; branch back while <= limit.
    a.emit(0x7304, ("bf", "loop"))
    a.emit(0xE400, 0xE500)
    a.load("long", 1, "original", 0x06056C58)
    a.emit(0x410B, 0x0009, 0x4F26, 0x69F6, 0x68F6, 0x000B, 0x0009)
    return a.finish()


def font_data(output):
    p = runpy.run_path(str(ROOT / "tools/build-packed-small-font.py"))
    baseline = p["build"](output / "baseline-8x8", resident_preparation=True)
    formal = json.loads((ROOT / "work/fonts/chinese/mapping.json").read_text(encoding="utf-8"))
    if any(baseline[k] != formal[k] for k in ("glyphs", "byte_codes", "preserved_tiles")):
        raise ValueError("Encoded-resource mapping is stale")
    original = p["SOURCE"].read_bytes()
    fb = p["load_font_builder"]()
    chars = list(baseline["glyphs"])
    supported = fb["font_codepoints"](fb["SMALL_FONT"])
    missing = [char for char in chars if ord(char) not in supported]
    if missing:
        raise ValueError("8x12 font lacks glyphs: " + "".join(missing))
    font = ImageFont.truetype(str(fb["SMALL_FONT"]), 12)
    ink_baseline = fb["calculate_baseline"](font, chars, 12)
    low = [None] * LOW_TILES
    # Keep direct references to original ASCII, bars, symbols and arrows valid.
    protected = [*range(0x0B3), 0x107, 0x108, 0x109]
    for slot in protected:
        low[slot * 2] = original[slot * 16:slot * 16 + 8]
        low[slot * 2 + 1] = original[slot * 16 + 8:slot * 16 + 16]
    available = iter(i for i, bitmap in enumerate(low) if bitmap is None)
    tile_by_bitmap = {}
    for i, bitmap in enumerate(low):
        if bitmap is not None:
            tile_by_bitmap.setdefault(bitmap, i)
    # Physical tile 0x40 is the original blank and remains blank.
    assert low[0x40] == bytes(8)
    tile_by_bitmap[bytes(8)] = 0x40
    high = []

    def alloc(bitmap):
        if bitmap in tile_by_bitmap:
            return tile_by_bitmap[bitmap]
        index = next(available, None)
        if index is None:
            index = HIGH_INDEX + len(high)
            high.append(bitmap)
        else:
            low[index] = bitmap
        tile_by_bitmap[bitmap] = index
        return index

    table = [(0x40, 0x40)] * (int(baseline["highest_assigned_logical_index"], 16) + 1)
    for slot in protected:
        table[slot] = (slot * 2, slot * 2 + 1)
    glyphs = {}
    for char, item in baseline["glyphs"].items():
        bitmap = fb["render_small_cell"](font, char, ink_baseline)
        top, bottom = alloc(bitmap[:8]), alloc(bitmap[8:])
        logical = int(item["logical_index"], 16)
        table[logical] = (top, bottom)
        glyphs[char] = dict(logical=logical, top=top, bottom=bottom, bitmap=bitmap.hex())
    _, battle_targets = load_battle(ROOT)
    battle_chars = set("".join(battle_targets))
    small_source = (output / "baseline-8x8/ASCII.PAC.entry01.packed8.bin").read_bytes()
    for char, item in baseline["glyphs"].items():
        if char in battle_chars:
            logical = int(item["logical_index"], 16)
            if not 0xB3 <= logical <= 0xFF:
                raise ValueError("Battle single-row glyph is outside its byte lookup bank")
            source_tile = item["tile"]
            glyphs[char]["small_tile"] = alloc(small_source[source_tile * 8:source_tile * 8 + 8])
    table[0x10A] = (0x40, 0x40)
    if len(high) > HIGH_TILES:
        raise ValueError(f"Overflow atlas needs {len(high)} tiles; region holds {HIGH_TILES}")
    data = b"".join(bitmap or bytes(8) for bitmap in low) + b"".join(high)
    pairs = b"".join(t.to_bytes(2, "big") + b.to_bytes(2, "big") for t, b in table)
    fb["write_sheet"](data, output / "atlas.png", 8, 8, 3, 32)
    fb["write_sheet"](b"".join(bytes.fromhex(v["bitmap"]) for v in glyphs.values()),
                      output / "chinese-high.png", 8, 16, 4, 24)
    return data, pairs, glyphs, len(high)


def build(output, build_root, baseline_root, temp_root):
    output.mkdir(parents=True, exist_ok=True)
    data, pairs, glyphs, high_count = font_data(output)
    mapping = json.loads((ROOT / "work/fonts/chinese/mapping.json").read_text(encoding="utf-8"))
    if len(pairs) != (int(mapping["highest_assigned_logical_index"], 16) + 1) * 4:
        raise ValueError("Pair table differs from the runtime logical-index bound")
    # Original prefix keeps physical index 2*code, so it needs no table.
    pairs = pairs[0xB3 * 4:]
    single_table = bytearray(b"\xff\xff" * (0x100 - 0xB3))
    for glyph in glyphs.values():
        if "small_tile" in glyph:
            offset = (glyph["logical"] - 0xB3) * 2
            single_table[offset:offset + 2] = glyph["small_tile"].to_bytes(2, "big")
    single_address = TABLE + len(pairs)
    proto = runpy.run_path(str(ROOT / "tools/build-tall-small-font-probe.py"))
    A = proto["Assembler"]
    g = runpy.run_path(str(ROOT / "tools/patch-packed-font-ascii-probe.py"))["patch_executable"].__globals__
    g["RUNTIME_TABLE"] = TABLE
    cursor = (single_address + len(single_table) + 3) & ~3
    blobs = []
    for addr_name, blob_name, factory in (
        ("RUNTIME_CODE", "ASCII_WRITER", lambda at: writer(at, A, "byte", single_address)),
        ("RUNTIME_WORD_CODE", "WORD_WRITER", lambda at: writer(at, A, "word")),
        ("RUNTIME_DOUBLE_CODE", "DOUBLE_WRITER", lambda at: writer(at, A, "double")),
        ("RUNTIME_STATUS_DOUBLE_CODE", "STATUS_DOUBLE_WRITER", lambda at: writer(at, A, "double")),
        ("RUNTIME_MENU_CODE", "MENU_WRITER", lambda at: menu_writer(at, A, g, True)),
        ("RUNTIME_NAME_CODE", "NAME_LOOP", lambda at: g["name_loop"](at, g["RUNTIME_STATUS_DOUBLE_CODE"])),
        ("RUNTIME_BYTE_CODE", "BYTE_LOOP", lambda at: g["fixed_byte_loop"](at, g["RUNTIME_STATUS_DOUBLE_CODE"])),
        ("RUNTIME_MENU_UNSELECTED_CODE", "MENU_UNSELECTED_WRITER", lambda at: menu_writer(at, A, g, False)),
        ("RUNTIME_UPLOAD_CODE", "UPLOAD_WRITER", lambda at: uploader(at, A, high_count)),
    ):
        g[addr_name] = cursor
        blob = factory(cursor)
        g[blob_name] = blob
        blobs.append((cursor, blob))
        cursor += len(blob)
    main = bytearray((ROOT / "work/fonts/chinese/ASCII.PAC.entry00.bin").read_bytes())
    if cursor > 0x00200000 + len(main) or any(main[TABLE - 0x00200000:]):
        raise ValueError("Resident font metadata/hooks do not fit the free main-font tail")
    main[TABLE - 0x00200000:TABLE - 0x00200000 + len(pairs)] = pairs
    main[single_address - 0x00200000:single_address - 0x00200000 + len(single_table)] = single_table
    for address, blob in blobs:
        main[address - 0x00200000:address - 0x00200000 + len(blob)] = blob
    b = runpy.run_path(str(ROOT / "tools/build-translation-pacs.py"))
    b["validate_translation_inputs"](ROOT, temp_root)
    original_program = g["SOURCE"].read_bytes()
    program = bytearray(original_program)
    fonts = ROOT / "work/fonts/chinese"
    patch_menu(ROOT, program, fonts)
    patch_battle(ROOT, program, fonts, original_program)
    patch_weather_table(ROOT, program, fonts, original_program)
    program = bytearray(g["patch_executable"](bytes(program)))
    program[0x30F28:0x30F2A] = (LOW_TILES * 8 - 1).to_bytes(2, "big")
    if program[0x30F54:0x30F58] != bytes.fromhex("06056c58"):
        raise ValueError("Boot upload continuation differs from verified original")
    program[0x30F54:0x30F58] = g["RUNTIME_UPLOAD_CODE"].to_bytes(4, "big")
    for name, content in (("0", program), ("ASCII.PAC.entry00.bin", main),
                          ("ASCII.PAC.entry01.bin", data), ("tile-pairs.bin", pairs),
                          ("battle-small-tiles.bin", single_table)):
        (output / name).write_bytes(content)
    destination = build_root / "repacked/pac"
    source = baseline_root / "repacked/pac"
    if destination.resolve() == source.resolve():
        raise ValueError("Use a separate build root")
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_file():
            shutil.copy2(path, destination / path.name)
    (destination / "0").write_bytes(program)
    ascii_report = b["build_one"](ROOT / "unpacked/japan/ASCII.PAC", destination / "ASCII.PAC", {0: bytes(main), 1: data})
    if (destination / "ASCII.PAC").stat().st_size > 44 * 2048:
        compressed_main, blocks = adaptive_stream(bytes(main))
        compressed_small = encode_entry_best(data)
        compressed = build_pac([compressed_main, compressed_small])
        if len(compressed) > 44 * 2048:
            raise ValueError(f"Adaptive font PAC still exceeds 44 sectors: {len(compressed)}")
        (destination / "ASCII.PAC").write_bytes(compressed)
        ascii_report["rebuilt_sha256"] = sha(compressed)
        ascii_report["rebuilt_bytes"] = len(compressed)
        ascii_report["adaptive_main_block_sizes"] = blocks
        for row, stream in zip(ascii_report["entries"], (compressed_main, compressed_small)):
            row["output_compressed_bytes"] = len(stream)
    manifest = dict(format="wachenroeder-resident-high-font-probe-v1", source_bytes=len(data),
                    low_vram=[LOW_START, LOW_END], overflow_vram=[HIGH_START, HIGH_END],
                    untouched_dynamic_vram=[0x25E66000, 0x25E69600],
                    overflow_tiles=high_count, protected_original_cells=182, glyphs=glyphs,
                    table_address=TABLE, table_first_logical=0xB3, hooks_end=cursor,
                    battle_ui="original 8x8 packed glyphs through single-row entry",
                    battle_single_table_address=single_address,
                    hooks={k: v for k, v in g.items() if k.startswith("RUNTIME_") and isinstance(v, int)},
                    runtime_status="Candidate overflow region; requires scene/DMA/write verification")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    disc = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    disc["files"] = [r for r in disc["files"] if r["file"] not in ("0", "ASCII.PAC")]
    disc["files"].extend([ascii_report, dict(file="0", type="resident-high-font-probe", rebuilt_sha256=sha(program), rebuilt_bytes=len(program))])
    disc["font_experiment"] = {k: v for k, v in manifest.items() if k != "glyphs"}
    disc["font_mode"] = "8x12"
    disc["runtime_font_pending"] = False
    disc["translation_input_sha256"] = json.loads(
        (temp_root / 'imported.json').read_text(encoding='utf-8-sig'))['translation_input_sha256']
    (destination / "manifest.json").write_text(json.dumps(disc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=ROOT / "temp/resident-high-font-probe")
    p.add_argument("--build-root", type=Path, default=ROOT / "work/build-resident-high-font-probe")
    p.add_argument("--baseline-build-root", type=Path, default=ROOT / "work/build-tall-baseline")
    p.add_argument("--temp-root", type=Path, default=ROOT / "temp/tall-baseline")
    a = p.parse_args()
    result = build(a.output.resolve(), a.build_root.resolve(), a.baseline_build_root.resolve(), a.temp_root.resolve())
    print(json.dumps({k: v for k, v in result.items() if k != "glyphs"}, indent=2))


if __name__ == "__main__":
    main()
