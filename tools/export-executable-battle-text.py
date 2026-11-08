#!/usr/bin/env python3
"""Export verified one-row battle HUD labels from executable 0."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from narrow_text_writeback import write_template


LOAD_ADDRESS = 0x06004000
TABLE_OFFSET = 0x00ECB4
TYPE_LABELS = ("SWORD-TYPE", "  GUN-TYPE", "  BOW-TYPE", "   SP-TYPE")
DIRECT_LABELS = (
    ("HP", (0x00EF5A, 0x00EF64)),
    ("LV", (0x00EFFE, 0x00F008)),
)


def read_mov_immediates(data: bytes, text: str, offsets: tuple[int, ...]) -> list[str]:
    if len(offsets) != len(text):
        raise ValueError(f"{text}: instruction count differs from the label")
    instructions = []
    for char, offset in zip(text, offsets):
        instruction = data[offset:offset + 2]
        if len(instruction) != 2 or instruction[0] & 0xF0 != 0xE0 or instruction[1] != ord(char):
            raise ValueError(f"{text}: unexpected SH-2 immediate at 0x{offset:06X}")
        instructions.append(instruction.hex().upper())
    return instructions


def extract(data: bytes) -> list[dict]:
    table = b"".join(label.encode("ascii") for label in TYPE_LABELS)
    if len(table) != 40 or data[TABLE_OFFSET:TABLE_OFFSET + 40] != table:
        raise ValueError("battle type table differs from the verified 4x10 layout")
    if data.find(table, TABLE_OFFSET + 1) >= 0:
        raise ValueError("battle type table is not unique")

    records = []
    for index, label in enumerate(TYPE_LABELS):
        offset = TABLE_OFFSET + index * 10
        records.append({
            "id": f"0:0x{offset:06X}",
            "file": "0",
            "offset": f"0x{offset:06X}",
            "runtime_address": f"0x{LOAD_ADDRESS + offset:08X}",
            "kind": "ascii-table-slot",
            "encoding": "small-font-direct-byte",
            "capacity_code_units": 10,
            "source_codes": data[offset:offset + 10].hex().upper(),
            "source_text": label,
        })
    for label, offsets in DIRECT_LABELS:
        instructions = read_mov_immediates(data, label, offsets)
        records.append({
            "id": f"0:0x{offsets[0]:06X}",
            "file": "0",
            "offset": f"0x{offsets[0]:06X}",
            "runtime_address": f"0x{LOAD_ADDRESS + offsets[0]:08X}",
            "kind": "sh2-immediate-label",
            "encoding": "small-font-direct-byte",
            "capacity_code_units": len(label),
            "instruction_offsets": [f"0x{offset:06X}" for offset in offsets],
            "source_instructions": instructions,
            "source_codes": label.encode("ascii").hex().upper(),
            "source_text": label,
        })

    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    source = (root / "unpacked/japan/0").read_bytes()
    records = extract(source)
    source_hash = hashlib.sha256(source).hexdigest().upper()
    report = {
        "schema": "wachenroeder-executable-battle-ui-v1",
        "source_file": "unpacked/japan/0",
        "source_sha256": source_hash,
        "record_count": len(records),
        "records": records,
    }
    output = root / "work/text/japan"
    output.mkdir(parents=True, exist_ok=True)
    (output / "0-battle-ui.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [f"[{index:04d}] @{record['offset']} len={record['capacity_code_units']} "
             f"| {record['source_text']}"
             for index, record in enumerate(records, 1)]
    (output / "0-battle-ui.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_template(root, "0-battle-ui", source_hash, records, menu=True,
                   header_label="fixed-width 8x8 battle text")
    print(f"exported {len(records)} writable battle labels")


if __name__ == "__main__":
    main()
