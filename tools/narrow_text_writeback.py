#!/usr/bin/env python3
"""Validate and apply fixed-width 8x16 byte text translations."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

from pac_codec import decode_entry, split_pac


ROOT = Path(__file__).resolve().parents[1]
SHARED_STEMS = ("MM-entry08-narrow-byte", "MEM-entry08-narrow-byte")
MENU_STEM = "0-narrow-menu"
BATTLE_STEM = "0-battle-ui"
EQUIPMENT_STEM = "MM-entry07-equipment"
ROW = re.compile(r"^\[(\d{4})\] @0x([0-9A-F]{6}) len=(\d+) \| (.*) \| (.*)$")
REUSABLE_KANA_CODES = tuple(range(0x33, 0x89))
APPENDED_BYTE_CODES = tuple(range(0x8B, 0x100))
ALLOCATABLE_BYTE_CODES = REUSABLE_KANA_CODES + APPENDED_BYTE_CODES
STATUS_NAME_TABLE = Path("work/translation/专用名词表.txt")

# The tactical HUD builds seven two-word values in stack memory (0x06011324
# onward). Rain at 0xD4AC is shared by small rain, plain rain and heavy rain;
# 0xD4AE holds only the heavy-rain prefix, followed immediately by indoor.
# Keep the source words here to verify every literal before replacing it.
WEATHER_TABLE_ENTRIES = (
    (0x00D4A2, (0x0764, 0x0115), "晴天"),  # 晴れ
    (0x00D4A6, (0x09C4, 0x0113), "阴天"),  # 曇り
    (0x00D4AA, (0x06A4, 0x01DB), "小雨"),  # 小雨
    (0x00D4AE, (0x050B,), "暴"),  # 豪; shares 雨 at 0xD4AC
    (0x00D4B0, (0x023E, 0x09C8), "屋内"),  # 屋内
    (0x00D4B4, (0x053B, 0x0C6A), "沙暴"),  # 砂嵐
)
WEATHER_TABLE_TARGETS = tuple(item[2] for item in WEATHER_TABLE_ENTRIES)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def template_path(root: Path, stem: str) -> Path:
    return root / "work/translation/narrow" / f"{stem}.txt"


def escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|")


def write_template(root: Path, stem: str, source_hash: str, records: list[dict],
                   menu: bool = False,
                   header_label: str = "fixed-width 8x16 text") -> None:
    path = template_path(root, stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    old_targets: dict[str, tuple[str, str]] = {}
    if path.exists():
        for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines()[1:], 2):
            match = ROW.fullmatch(line)
            if match is None:
                raise ValueError(f"{path}:{line_number}: malformed translation row")
            old_targets[match.group(1)] = (match.group(0)[:match.start(5)], match.group(5))
    lines = [f"# {stem} | source_sha256={source_hash} | {header_label}"]
    for index, record in enumerate(records):
        offset = int(record["offset"], 16) if menu else int(record["decoded_offset"])
        length = int(record["capacity_code_units"] if menu else record["byte_length"])
        source = escape(str(record["source_text"]))
        prefix = f"[{index:04d}] @0x{offset:06X} len={length} | {source} | "
        previous = old_targets.get(f"{index:04d}")
        if previous is not None and previous[0] != prefix:
            raise ValueError(f"{path}: source row {index:04d} changed with an existing template")
        lines.append(prefix + (previous[1] if previous else ""))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_template(root: Path, stem: str, source_hash: str, records: list[dict],
                  menu: bool = False,
                  header_label: str = "fixed-width 8x16 text") -> list[str]:
    path = template_path(root, stem)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    header = f"# {stem} | source_sha256={source_hash} | {header_label}"
    if not lines or lines[0] != header or len(lines) != len(records) + 1:
        raise ValueError(f"{path}: header or row count differs from source")
    targets = []
    for index, (line, record) in enumerate(zip(lines[1:], records)):
        match = ROW.fullmatch(line)
        offset = int(record["offset"], 16) if menu else int(record["decoded_offset"])
        length = int(record["capacity_code_units"] if menu else record["byte_length"])
        if (match is None or match.group(1) != f"{index:04d}"
                or int(match.group(2), 16) != offset
                or int(match.group(3)) != length
                or match.group(4) != escape(str(record["source_text"]))):
            raise ValueError(f"{path}:{index + 2}: source selector changed")
        target = match.group(5)
        if "\\" in target or "|" in target or "<" in target or ">" in target:
            raise ValueError(f"{path}:{index + 2}: reserved character in target")
        capacity = int(record.get("write_capacity", length))
        if len(target) > capacity:
            raise ValueError(f"{path}:{index + 2}: target exceeds {capacity} byte cells")
        targets.append(target)
    return targets


def original_code_map() -> dict[str, int]:
    result = {" ": 0x8A, "-": 0x30}
    for code in range(1, 0x8A):
        if 1 <= code <= 10:
            char = str(code - 1)
        elif 0x0B <= code <= 0x24:
            char = chr(ord("A") + code - 0x0B)
        elif code == 0x2F:
            char = "・"
        elif code == 0x30:
            char = "ー"
        elif code == 0x89:
            char = "&"
        elif 0x33 <= code <= 0x88:
            trail = 0x40 + code - 0x33
            if trail >= 0x7F:
                trail += 1
            char = bytes((0x83, trail)).decode("shift_jis")
        else:
            char = None
        if char is not None:
            result[char] = code
    return result


def load_status_name_translations(root: Path) -> dict[str, str]:
    """Load exact translations for the full names stored in MM status rows."""
    path = root / STATUS_NAME_TABLE
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#") or "|" not in line:
            continue
        source, target = line.split("|", 1)
        source = source.strip()
        target = target.strip()
        if not source or not target:
            continue
        previous = result.get(source)
        if previous is not None and previous != target:
            raise ValueError(f"{path}:{line_number}: conflicting translation for {source!r}")
        result[source] = target
    return result


def decode_status_name(data: bytes) -> str:
    """Decode one fixed-width MM status name using the original byte alphabet."""
    reverse = {code: char for char, code in original_code_map().items()}
    chars: list[str] = []
    for code in data:
        if code in (0x00, 0x8A):
            break
        if code not in reverse:
            raise ValueError(f"MM status name contains unknown byte 0x{code:02X}")
        chars.append(reverse[code])
    return "".join(chars)


def translated_status_names(root: Path) -> list[str]:
    """Return translations used by the 46 fixed MM status-name records."""
    source_path = root / "unpacked/japan/MM01.PS"
    if not source_path.exists():
        return []
    raw = decode_entry(split_pac(source_path.read_bytes())[8], stack_limit=512)
    translations = load_status_name_translations(root)
    names = []
    status_base = 46 * 11
    for index in range(46):
        source_name = decode_status_name(raw[status_base + index * 55:status_base + index * 55 + 16])
        if source_name and source_name in translations:
            names.append(translations[source_name])
    return names


def writeback_code_map() -> dict[str, int]:
    return {
        char: code for char, code in original_code_map().items()
        if code not in REUSABLE_KANA_CODES
    }


def code_map(font_dir: Path) -> dict[str, int]:
    result = writeback_code_map()
    packed_path = font_dir / "mapping.json"
    if packed_path.exists():
        packed = json.loads(packed_path.read_text(encoding="utf-8"))
        if packed.get("format") != "wachenroeder-packed-small-font-v1":
            raise ValueError(f"{packed_path}: unsupported packed small-font format")
        for char, glyph in packed["glyphs"].items():
            result[char] = int(glyph["logical_index"], 16) - 0x80
        return result
    with (font_dir / "small-font-map.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("Mapping") == "allocated" and row.get("Character"):
                code = int(row["GlyphIndex"], 16) - 0x80
                if code in ALLOCATABLE_BYTE_CODES and row["Character"] not in result:
                    result[row["Character"]] = code
    return result


def main_code_map(font_dir: Path) -> dict[str, int]:
    """Load the 16-bit text-code map used by the large font."""
    path = font_dir / "font-map-chinese.csv"
    if not path.exists():
        raise ValueError(f"Missing large-font map: {path}")
    result: dict[str, int] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            character = row.get("Character", "")
            text_code = row.get("TextCode", "")
            if character and text_code:
                result[character] = int(text_code, 16)
    return result


def single_row_code_map(font_dir: Path) -> dict[str, int]:
    """Map characters to glyph indices accepted by the 8x8 tile writer."""
    result = {chr(code): code for code in range(0x20, 0x7F)}
    for char, code in code_map(font_dir).items():
        glyph_index = code + 0x80
        if char not in result and glyph_index <= 0xFF:
            result[char] = glyph_index
    return result


def packed_byte_codes(font_dir: Path) -> dict[str, bytes]:
    path = font_dir / "mapping.json"
    if not path.exists():
        return {}
    packed = json.loads(path.read_text(encoding="utf-8"))
    if packed.get("escape_prefix") != "0x7F":
        raise ValueError(f"{path}: unexpected byte-text escape prefix")
    return {char: bytes(code) for char, code in packed["byte_codes"].items()}


def encode(target: str, mapping: dict[str, int], label: str,
           byte_codes: dict[str, bytes] | None = None) -> bytes:
    try:
        result = bytearray()
        for char in target:
            if byte_codes is not None and char in byte_codes:
                result.extend(byte_codes[char])
            else:
                code = mapping[char]
                if not 0 <= code <= 0xFF:
                    raise ValueError(f"{label}: {char!r} needs an extended byte code")
                result.append(code)
        return bytes(result)
    except KeyError as error:
        raise ValueError(f"{label}: no small-font glyph for {error.args[0]!r}") from error


def load_shared(root: Path, stem: str) -> tuple[dict, list[str]]:
    report = json.loads((root / "work/text/japan" / f"{stem}.json").read_text(encoding="utf-8"))
    if report["schema"] != "wachenroeder-entry08-narrow-byte-v1":
        raise ValueError(f"{stem}: source report schema changed")
    count = 46 if stem.startswith("MM-") else 45
    if report.get("character_count") != count:
        raise ValueError(f"{stem}: regenerate the catalog with fixed field boundaries")
    fields = shared_text_fields(count)
    previous_end = 0
    for record in report["records"]:
        start = int(record["decoded_offset"])
        end = start + int(record["byte_length"])
        owner = next((field for field in fields if field[0] <= start < field[0] + field[1]), None)
        if owner is None or end > owner[0] + owner[1] or start < previous_end:
            raise ValueError(f"{stem}:{record['id']}: text crosses or overlaps a fixed field")
        if (record.get("field_start"), record.get("field_capacity"),
                record.get("field_kind"), record.get("character_index")) != owner:
            raise ValueError(f"{stem}:{record['id']}: field metadata differs from the table layout")
        if record.get("write_capacity") != owner[0] + owner[1] - start:
            raise ValueError(f"{stem}:{record['id']}: write capacity crosses a fixed field")
        previous_end = end
    targets = read_template(root, stem, report["decoded_sha256"], report["records"])
    return report, targets


def shared_text_fields(character_count: int) -> list[tuple[int, int, str, int]]:
    """Physical owners of the shared short-name and detailed status text."""
    if character_count not in (45, 46):
        raise ValueError("Unsupported shared character table")
    fields = [(i * 11, 11, "short-name", i) for i in range(character_count)]
    base = character_count * 11
    for i in range(character_count):
        fields.extend((base + i * 55 + offset, size, kind, i)
                      for offset, size, kind in ((0, 16, "full-name"),
                          (16, 13, "weapon"), (29, 13, "skill-1"), (42, 13, "skill-2")))
    return fields


def load_menu(root: Path) -> tuple[dict, list[str]]:
    report = json.loads((root / "work/text/japan" / f"{MENU_STEM}.json").read_text(encoding="utf-8"))
    if report["schema"] != "wachenroeder-executable-narrow-menu-v1":
        raise ValueError("Executable menu report schema changed")
    targets = read_template(root, MENU_STEM, report["source_sha256"], report["records"], menu=True)
    return report, targets


def load_battle(root: Path) -> tuple[dict, list[str]]:
    report = json.loads((root / "work/text/japan" / f"{BATTLE_STEM}.json").read_text(encoding="utf-8"))
    if report["schema"] != "wachenroeder-executable-battle-ui-v1":
        raise ValueError("Executable battle UI report schema changed")
    targets = read_template(root, BATTLE_STEM, report["source_sha256"],
                            report["records"], menu=True,
                            header_label="fixed-width 8x8 battle text")
    return report, targets


def translated_characters(root: Path) -> list[str]:
    chars = []
    for stem in SHARED_STEMS:
        _, targets = load_shared(root, stem)
        chars.extend("".join(targets))
    _, targets = load_menu(root)
    chars.extend("".join(targets))
    _, targets = load_battle(root)
    chars.extend("".join(targets))
    # Full MM status names live in fixed records rather than translation
    # template rows. Include them so the font builder allocates every glyph
    # needed by the detailed-name field as well as the short-name field.
    chars.extend("".join(translated_status_names(root)))
    _, targets = load_mm_equipment(root)
    chars.extend("".join(targets))
    return list(dict.fromkeys(chars))


def load_mm_equipment(root: Path) -> tuple[dict, list[str]]:
    path = root / "work/text/japan" / f"{EQUIPMENT_STEM}.json"
    if not path.exists():
        raise ValueError("MM equipment catalog missing; run tools/export-mm-equipment-text.py or the full translation build")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report["schema"] != "wachenroeder-mm-equipment-u16-v1" or len(report["records"]) != 38:
        raise ValueError("MM equipment table schema changed")
    for index, record in enumerate(report["records"]):
        if int(record["offset"], 16) != 0x1A5A + index * 26 or record["capacity_code_units"] != 13:
            raise ValueError("MM equipment field boundary changed")
    return report, read_template(root, EQUIPMENT_STEM, report["decoded_sha256"], report["records"], menu=True)


def patch_mm_equipment(root: Path, replacements: dict[str, dict[int, bytes]], font_dir: Path) -> None:
    report, targets = load_mm_equipment(root)
    mapping = code_map(font_dir)
    for name in report["source_files"]:
        source = decode_entry(split_pac((root / "unpacked/japan" / name).read_bytes())[7], stack_limit=512)
        patched = bytearray(replacements.get(name, {}).get(7, source))
        for record, target in zip(report["records"], targets):
            start = int(record["offset"], 16)
            expected = b"".join(int(word, 16).to_bytes(2, "big") for word in record["source_codes"])
            if len(expected) != 26 or source[start:start + 26] != expected or patched[start:start + 26] != expected:
                raise ValueError(f"{name}: MM equipment source words differ at {start:04X}")
            if not target:
                raise ValueError(f"{name}: untranslated MM equipment field {record['id']}")
            words = [mapping[char] for char in target]
            if len(words) > 13:
                raise ValueError(f"{name}: MM equipment field exceeds 13 words")
            words.extend([0x8A] * (13 - len(words)))
            patched[start:start + 26] = b"".join(word.to_bytes(2, "big") for word in words)
        replacements.setdefault(name, {})[7] = bytes(patched)


def assert_byte_capacity(root: Path, font_dir: Path | None = None) -> int:
    if font_dir is not None and (font_dir / "mapping.json").exists():
        mapping = code_map(font_dir)
        single_row = single_row_code_map(font_dir)
        byte_codes = packed_byte_codes(font_dir)
        for stem in SHARED_STEMS:
            report, targets = load_shared(root, stem)
            for record, target in zip(report["records"], targets):
                if target and len(encode(target.rstrip(" "), mapping,
                                         f"{stem}:{record['id']}", byte_codes)) > int(record.get("write_capacity", record["byte_length"])):
                    raise ValueError(f"{stem}:{record['id']}: packed byte text exceeds its field")
        report, targets = load_battle(root)
        for record, target in zip(report["records"], targets):
            if target:
                encode(target, single_row, f"{BATTLE_STEM}:{record['id']}")
        for target in dict.fromkeys(translated_status_names(root)):
            encoded = encode(target, mapping, f"MM status name {target}", byte_codes)
            if len(encoded) > 16:
                raise ValueError(
                    f"MM status name {target}: encoded bytes exceed 16-byte field"
                )
        return sum(len(code) == 1 for code in byte_codes.values())
    required = [char for char in translated_characters(root) if char not in writeback_code_map()]
    capacity = len(ALLOCATABLE_BYTE_CODES)
    if len(required) > capacity:
        raise ValueError(
            f"8x16 single-byte templates need {len(required)} new glyphs, "
            f"but reusable kana codes 0x33..0x88 and appended codes 0x8B..0xFF "
            f"provide only {capacity}; runtime byte-text encoding must be extended"
        )
    return len(required)


def patch_shared(root: Path, replacements: dict[str, dict[int, bytes]],
                 font_dir: Path) -> list[dict]:
    mapping = code_map(font_dir)
    byte_codes = packed_byte_codes(font_dir)
    manifest = []
    for stem in SHARED_STEMS:
        report, targets = load_shared(root, stem)
        selected = [(record, target) for record, target in zip(report["records"], targets) if target]
        if not selected:
            continue
        source_layers = []
        for source_info in report["source_files"]:
            file_name = source_info["file"]
            packed = (root / "unpacked/japan" / file_name).read_bytes()
            if sha256(packed) != source_info["source_sha256"]:
                raise ValueError(f"{file_name}: source hash differs from 8x16 report")
            raw = decode_entry(split_pac(packed)[8], stack_limit=512)
            if sha256(raw) != report["decoded_sha256"]:
                raise ValueError(f"{file_name}: entry 8 differs from 8x16 report")
            source_layers.append((file_name, raw))
        for file_name, source_raw in source_layers:
            current = replacements.get(file_name, {}).get(8, source_raw)
            if current != source_raw:
                raise ValueError(f"{file_name}: entry 8 already has an incompatible replacement")
            patched = bytearray(current)
            for record, target in selected:
                offset = int(record["decoded_offset"])
                length = int(record["byte_length"])
                capacity = int(record["write_capacity"])
                expected = bytes.fromhex(record["source_codes"])
                if len(expected) != length or patched[offset:offset + length] != expected:
                    raise ValueError(f"{file_name}:{record['id']}: source bytes differ")
                if any(source_raw[offset + length:offset + capacity]):
                    raise ValueError(f"{file_name}:{record['id']}: nonzero data after source text")
                encoded = encode(target.rstrip(" "), mapping,
                                 f"{file_name}:{record['id']}", byte_codes)
                if len(encoded) > capacity:
                    raise ValueError(f"{file_name}:{record['id']}: encoded bytes exceed {capacity}")
                patched[offset:offset + capacity] = encoded.ljust(capacity, b"\x8a")
            if stem.startswith("MM-"):
                restore_mm_status_fields(patched, source_raw, report["records"],
                                         targets, mapping, byte_codes,
                                         load_status_name_translations(root))
            replacements.setdefault(file_name, {})[8] = bytes(patched)
            manifest.append({"file": file_name, "entry": 8, "fields": len(selected),
                             "source_sha256": sha256(source_raw), "rebuilt_sha256": sha256(patched)})
    patch_mm_equipment(root, replacements, font_dir)
    for row in manifest:
        if row["file"].startswith("MM"):
            row["equipment_entry"] = 7
            row["equipment_fields"] = 38
            row["equipment_rebuilt_sha256"] = sha256(replacements[row["file"]][7])
    return manifest


def restore_mm_status_fields(patched: bytearray, source: bytes, records: list[dict],
                             targets: list[str], mapping: dict[str, int],
                             byte_codes: dict[str, bytes],
                             full_name_translations: dict[str, str] | None = None) -> None:
    """Apply full-name terminology and preserve unused status placeholders."""
    short_stride = 11
    status_base = 46 * short_stride
    status_stride = 55
    if status_base + 46 * status_stride != 0x0BDC:
        raise AssertionError("MM status table geometry changed")
    names: list[str] = []
    for index in range(46):
        start = index * short_stride
        matches = [row for row, record in enumerate(records)
                   if start <= int(record["decoded_offset"]) < start + short_stride]
        if len(matches) > 1:
            raise ValueError(f"MM short-name slot {index} has multiple source runs")
        if not matches:
            names.append("")
            continue
        target = targets[matches[0]].strip(" ")
        # Rebuild the whole physical slot: retaining the Japanese leading
        # zeros and padding the shorter translation on the right shifts names.
        encoded = encode(target, mapping, f"MM short name {index}", byte_codes)
        if len(encoded) > short_stride:
            raise ValueError(f"MM short name {index} exceeds its {short_stride}-byte field")
        patched[start:start + short_stride] = encoded.rjust(short_stride, b"\x8a")
        names.append(target)

    full_name_translations = full_name_translations or {}
    for index, short_name in enumerate(names):
        start = status_base + index * status_stride
        source_name = decode_status_name(source[start:start + 16])
        name = full_name_translations.get(source_name, short_name)
        if not name:
            continue
        encoded = encode(name, mapping, f"MM status name {index}", byte_codes)
        if len(encoded) > 16:
            raise ValueError(f"MM status name {index} exceeds its 16-byte field")
        patched[start:start + 16] = encoded.ljust(16, b"\x8a")

    for index in range(46):
        start = status_base + index * status_stride
        for field_offset in (16, 29, 42):
            field_start = start + field_offset
            original = source[field_start:field_start + 13]
            if original in (b"\x30" * 13, b"\x00" * 13):
                patched[field_start:field_start + 13] = original


def patch_menu(root: Path, executable: bytearray, font_dir: Path) -> list[dict]:
    report, targets = load_menu(root)
    if sha256(bytes(executable)) != report["source_sha256"]:
        raise ValueError("Executable source differs from menu report")
    mapping = code_map(font_dir)
    changes = []
    for record, target in zip(report["records"], targets):
        if not target:
            continue
        if " " in target:
            raise ValueError(f"Executable menu {record['id']}: embedded space is unsupported")
        offset = int(record["offset"], 16)
        capacity = int(record["capacity_code_units"])
        old = b"".join(int(code, 16).to_bytes(2, "big") for code in record["source_codes"])
        if executable[offset:offset + len(old)] != old:
            raise ValueError(f"Executable menu {record['id']}: source words differ")
        codes = encode(target, mapping, record["id"])
        words = [0x0100 | code for code in codes]
        words.extend([0x0100] * (capacity - len(words)))
        rebuilt = b"".join(word.to_bytes(2, "big") for word in words)
        executable[offset:offset + len(old)] = rebuilt
        changes.append({"offset": offset, "length": len(old), "translation": target})
    return changes


def patch_battle(root: Path, executable: bytearray, font_dir: Path,
                 source_data: bytes | None = None) -> list[dict]:
    report, targets = load_battle(root)
    source = source_data if source_data is not None else bytes(executable)
    if sha256(source) != report["source_sha256"]:
        raise ValueError("Executable source differs from battle UI report")
    mapping = single_row_code_map(font_dir)
    changes = []
    for record, target in zip(report["records"], targets):
        if not target:
            continue
        offset = int(record["offset"], 16)
        capacity = int(record["capacity_code_units"])
        codes = encode(target, mapping, record["id"])
        if len(codes) > capacity:
            raise ValueError(f"Battle label {record['id']}: exceeds {capacity} byte cells")
        if record["kind"] == "ascii-table-slot":
            expected = bytes.fromhex(record["source_codes"])
            if source[offset:offset + capacity] != expected or executable[offset:offset + capacity] != expected:
                raise ValueError(f"Battle label {record['id']}: source table bytes differ")
            executable[offset:offset + capacity] = codes.rjust(capacity, b" ")
            changes.append({"offset": offset, "length": capacity, "translation": target})
        elif record["kind"] == "sh2-immediate-label":
            if len(codes) != capacity:
                raise ValueError(f"Battle label {record['id']}: needs exactly {capacity} characters")
            for position, (address, instruction) in enumerate(zip(
                    record["instruction_offsets"], record["source_instructions"])):
                instruction_offset = int(address, 16)
                expected = bytes.fromhex(instruction)
                if (source[instruction_offset:instruction_offset + 2] != expected
                        or executable[instruction_offset:instruction_offset + 2] != expected):
                    raise ValueError(f"Battle label {record['id']}: source opcode differs")
                executable[instruction_offset + 1] = codes[position]
                changes.append({"offset": instruction_offset + 1, "length": 1,
                                "translation": target})
        else:
            raise ValueError(f"Battle label {record['id']}: unknown kind")
    return changes


def patch_weather_table(root: Path, executable: bytearray, font_dir: Path,
                        source_data: bytes | None = None) -> list[dict]:
    """Replace the executable's dynamic weather values with Chinese codes."""
    source = source_data if source_data is not None else bytes(executable)
    mapping = main_code_map(font_dir)
    changes: list[dict] = []
    for index, (offset, source_codes, target) in enumerate(WEATHER_TABLE_ENTRIES):
        expected = b"".join(code.to_bytes(2, "big") for code in source_codes)
        length = len(expected)
        if source[offset:offset + length] != expected:
            raise ValueError(f"Executable weather entry {index} source words differ")
        if executable[offset:offset + length] != expected:
            raise ValueError(f"Executable weather entry {index} was modified before weather patch")
        try:
            codes = [mapping[character] for character in target]
        except KeyError as error:
            raise ValueError(
                f"Weather table entry {index} has no large-font glyph for {error.args[0]!r}"
            ) from error
        if len(codes) != len(source_codes):
            raise ValueError(f"Weather table entry {index} must preserve its source word count")
        rebuilt = b"".join(code.to_bytes(2, "big") for code in codes)
        executable[offset:offset + length] = rebuilt
        changes.append({
            "offset": offset,
            "length": length,
            "translation": target,
            "source_codes": [f"0x{code:04X}" for code in source_codes],
            "target_codes": [f"0x{code:04X}" for code in codes],
        })
    return changes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--font-dir", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    font_dir = args.font_dir or root / "work/fonts/chinese"
    for stem in SHARED_STEMS:
        load_shared(root, stem)
    load_menu(root)
    load_battle(root)
    mapping = code_map(font_dir)
    byte_codes = packed_byte_codes(font_dir)
    report, targets = load_mm_equipment(root)
    for record, target in zip(report["records"], targets):
        if not target:
            raise ValueError(f"{EQUIPMENT_STEM}:{record['id']}: untranslated equipment")
        for character in target:
            if character not in mapping or not 0 <= mapping[character] <= 0xFFFF:
                raise ValueError(f"{EQUIPMENT_STEM}:{record['id']}: no narrow glyph for {character!r}")
    for stem in SHARED_STEMS:
        report, targets = load_shared(root, stem)
        for record, target in zip(report["records"], targets):
            if target:
                encoded = encode(target.rstrip(" "), mapping,
                                 f"{stem}:{record['id']}", byte_codes)
                if len(encoded) > int(record.get("write_capacity", record["byte_length"])):
                    raise ValueError(f"{stem}:{record['id']}: packed byte text exceeds its field")
    report, targets = load_menu(root)
    for record, target in zip(report["records"], targets):
        if target:
            encode(target, mapping, record["id"])
    report, targets = load_battle(root)
    for record, target in zip(report["records"], targets):
        if target:
            encoded = encode(target, single_row_code_map(font_dir), record["id"])
            if record["kind"] == "sh2-immediate-label" and len(encoded) != int(record["capacity_code_units"]):
                raise ValueError(f"Battle label {record['id']}: needs exactly two characters")
    print("Validated fixed-width 8x16 templates and font codes")


if __name__ == "__main__":
    main()
