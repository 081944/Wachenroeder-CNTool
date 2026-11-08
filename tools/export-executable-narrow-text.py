#!/usr/bin/env python3
"""Export the verified 12-cell, 8x16-font menu table from executable 0."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from narrow_text_writeback import write_template


LOAD_ADDRESS = 0x06004000
SLOT_UNITS = 12
LABELS = ("EXIT", "BLACKMARKET", "STATUS", "DATA")


def encode_slot(text: str) -> bytes:
    if len(text) > SLOT_UNITS or any(not "A" <= char <= "Z" for char in text):
        raise ValueError(f"invalid narrow menu label: {text!r}")
    codes = [0x010B + ord(char) - ord("A") for char in text]
    codes.extend([0x0100] * (SLOT_UNITS - len(codes)))
    return b"".join(code.to_bytes(2, "big") for code in codes)


def decode_slot(data: bytes, offset: int) -> tuple[str, list[int]]:
    end = offset + SLOT_UNITS * 2
    if end > len(data):
        raise ValueError("narrow menu slot extends beyond executable")
    words = [int.from_bytes(data[pos:pos + 2], "big")
             for pos in range(offset, end, 2)]
    text = []
    padding = False
    for word in words:
        if word == 0x0100:
            padding = True
        elif 0x010B <= word <= 0x0124 and not padding:
            text.append(chr(ord("A") + word - 0x010B))
        else:
            raise ValueError(f"unexpected narrow menu code 0x{word:04X} at 0x{offset:06X}")
    return "".join(text), words


def extract(data: bytes) -> tuple[int, list[dict]]:
    signature = encode_slot(LABELS[0])
    first = data.find(signature)
    if first < 0 or data.find(signature, first + 1) >= 0:
        raise ValueError("expected exactly one EXIT slot in executable 0")
    records = []
    for index, expected in enumerate(LABELS):
        offset = first + index * SLOT_UNITS * 2
        label, words = decode_slot(data, offset)
        if label != expected:
            raise ValueError(f"narrow menu slot {index} changed: {label!r} != {expected!r}")
        records.append({
            "id": f"0:0x{offset:06X}",
            "file": "0",
            "offset": f"0x{offset:06X}",
            "runtime_address": f"0x{LOAD_ADDRESS + offset:08X}",
            "capacity_code_units": SLOT_UNITS,
            "encoding": "narrow-font-u16be-01xx",
            "source_codes": [f"{word:04X}" for word in words],
            "source_text": label,
            "translation": "",
        })
    return first, records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output_dir or root / "work/text/japan"
    source = root / "unpacked/japan/0"
    data = source.read_bytes()
    table_offset, records = extract(data)
    report = {
        "schema": "wachenroeder-executable-narrow-menu-v1",
        "source_file": "unpacked/japan/0",
        "source_sha256": hashlib.sha256(data).hexdigest().upper(),
        "table_offset": f"0x{table_offset:06X}",
        "slot_size_bytes": SLOT_UNITS * 2,
        "record_count": len(records),
        "records": records,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "0-narrow-menu.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"[{index:04d}] @{record['offset']} len={SLOT_UNITS} | {record['source_text']}"
             for index, record in enumerate(records, 1)]
    (output / "0-narrow-menu.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_template(root, "0-narrow-menu", report["source_sha256"], records, menu=True)
    print(f"exported {len(records)} records from 0 at 0x{table_offset:06X} to {output}")


if __name__ == "__main__":
    main()
