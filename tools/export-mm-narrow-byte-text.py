#!/usr/bin/env python3
"""Export shared MM/MEM entry-08 text within verified fixed field boundaries."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from pac_codec import decode_entry, split_pac
from narrow_text_writeback import (write_template, read_template,
                                  load_status_name_translations, shared_text_fields, load_shared)


TEXT_END = 0x0BDC
DATA_START = 0x0BFD


def decode_code(code: int) -> str | None:
    if 1 <= code <= 10:
        return str(code - 1)
    if 0x0B <= code <= 0x24:
        return chr(ord("A") + code - 0x0B)
    if code == 0x2F:
        return "・"
    if code == 0x30:
        return "ー"
    if code == 0x89:
        return "&"
    if 0x33 <= code <= 0x88:
        trail = 0x40 + code - 0x33
        if trail >= 0x7F:
            trail += 1
        return bytes((0x83, trail)).decode("shift_jis")
    return None


def extract(raw: bytes, text_end: int, character_count: int | None = None) -> list[dict]:
    if character_count is not None:
        if text_end != character_count * (11 + 55):
            raise ValueError("Fixed character tables do not cover the text region")
        records = []
        fields = shared_text_fields(character_count)
        for start, size, kind, index in fields:
            if raw[start:start + size] in (b"\x30" * size, b"\0" * size):
                continue  # Preserve unused equipment/skill placeholders byte for byte.
            for record in extract(raw[start:start + size], size):
                record.update(id=f"{len(records):04d}",
                              decoded_offset=start + record["decoded_offset"],
                              kind="fixed-field-text", status="verified-field-boundaries",
                              field_start=start, field_capacity=size,
                              field_kind=kind, character_index=index)
                record["write_capacity"] = start + size - record["decoded_offset"]
                records.append(record)
        return records
    records = []
    position = 0
    while position < text_end:
        end = raw.find(b"\0", position, text_end)
        if end < 0:
            end = text_end
        codes = raw[position:end]
        chars = [decode_code(code) for code in codes]
        letters = [char for char in chars if char is not None and char.isalpha()]
        if len(codes) >= 3 and len(letters) >= 2 and all(char is not None for char in chars):
            records.append({
                "id": f"{len(records):04d}",
                "decoded_offset": position,
                "byte_length": len(codes),
                "encoding": "narrow-font-byte",
                "kind": "zero-delimited-run",
                "status": "review-boundaries",
                "source_codes": codes.hex(" ").upper(),
                "source_text": "".join(chars),
            })
        position = end + 1
    return records


def load_shared_layers(root: Path, names: list[str]) -> tuple[list[dict], bytes]:
    layers = []
    for name in names:
        path = root / "unpacked/japan" / name
        packed = path.read_bytes()
        raw = decode_entry(split_pac(packed)[8], stack_limit=512)
        layers.append((name, hashlib.sha256(packed).hexdigest().upper(), raw))
    reference = layers[0][2]
    if any(raw != reference for _, _, raw in layers[1:]):
        raise ValueError("Entry 8 is not identical across the selected files")
    return ([{"file": name, "source_sha256": digest} for name, digest, _ in layers], reference)


def write_report(root: Path, output: Path, stem: str, files: list[dict], raw: bytes,
                 text_end: int, purpose: str, character_count: int,
                 migrate_boundaries: bool = False,
                 pending: dict[Path, str] | None = None) -> int:
    records = extract(raw, text_end, character_count)
    migrated = None
    if migrate_boundaries:
        try:
            # A translated template may already have the new selectors while
            # the generated JSON is still from an older checkout.
            migrated = read_template(root, stem, hashlib.sha256(raw).hexdigest().upper(), records)
        except ValueError:
            old = json.loads((output / f"{stem}.json").read_text(encoding="utf-8"))
            targets = read_template(root, stem, old["decoded_sha256"], old["records"])
            existing = {(r["decoded_offset"], r["source_text"]): t
                        for r, t in zip(old["records"], targets)}
            terms = load_status_name_translations(root)
            migrated = []
            for record in records:
                key = (record["decoded_offset"], record["source_text"])
                if key in existing:
                    migrated.append(existing[key])
                elif record["source_text"] in terms:
                    migrated.append(terms[record["source_text"]])
                else:
                    raise ValueError(f"{stem}:{record['id']}: boundary migration needs an explicit translation: {key}")
    report = {
        "schema": "wachenroeder-entry08-narrow-byte-v1",
        "purpose": purpose,
        "source_files": files,
        "entry": 8,
        "decoded_sha256": hashlib.sha256(raw).hexdigest().upper(),
        "text_region_end": f"0x{text_end:04X}",
        "record_count": len(records),
        "character_count": character_count,
        "short_name_stride": 11,
        "status_stride": 55,
        "records": records,
    }
    def save(path, text):
        if pending is None:
            path.write_text(text, encoding="utf-8")
        else:
            pending[path] = text

    save(output / f"{stem}.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = [
        f"[{record['id']}] @0x{record['decoded_offset']:06X} len={record['byte_length']} | {record['source_text']}"
        for record in records
    ]
    save(output / f"{stem}.txt", "\n".join(lines) + "\n")
    if migrated is None and pending is None:
        write_template(root, stem, report["decoded_sha256"], records)
    else:
        if migrated is None:
            migrated = [""] * len(records)
        header = f"# {stem} | source_sha256={report['decoded_sha256']} | fixed-width 8x16 text"
        template = [header] + [f"{line} | {target}" for line, target in zip(lines, migrated)]
        save(root / "work/translation/narrow" / f"{stem}.txt", "\n".join(template) + "\n")
    return len(records)


def ensure_reports(root: Path, output: Path, specs: list[tuple], backup_dir: Path) -> None:
    pending = {}
    for stem, files, raw, end, purpose, count in specs:
        path = output / f"{stem}.json"
        template = root / "work/translation/narrow" / f"{stem}.txt"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if old is not None and all((
            old.get("schema") == "wachenroeder-entry08-narrow-byte-v1",
            old.get("character_count") == count,
            old.get("decoded_sha256") == hashlib.sha256(raw).hexdigest().upper(),
            old.get("source_files") == files,
            old.get("records") == extract(raw, end, count),
        )):
            read_template(root, stem, old["decoded_sha256"], old["records"])
            print(f"Catalog current: {stem}")
            continue
        if template.exists() and old is None:
            raise ValueError(f"{stem}: translation exists but its source catalog is missing; cannot migrate")
        print(f"Regenerating catalog and migrating translations: {stem}")
        write_report(root, output, stem, files, raw, end, purpose, count,
                     migrate_boundaries=template.exists(), pending=pending)
    if not pending:
        return
    # Resolve every translation before writing either catalog. Retain originals
    # for recovery if a later filesystem operation fails.
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="shared-catalogs-", dir=backup_dir))
    for path in pending:
        if path.exists():
            saved = backup / path.relative_to(root)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
    try:
        for path, text in pending.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for stem, *_ in specs:
            load_shared(root, stem)
    except Exception:
        for path in pending:
            saved = backup / path.relative_to(root)
            if saved.exists():
                shutil.copy2(saved, path)
            elif path.exists():
                path.unlink()
        raise
    print(f"Catalog migration complete; originals backed up to {backup}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--migrate-boundaries", action="store_true",
                        help="Explicitly migrate old source runs, preserving unchanged translations")
    parser.add_argument("--ensure-current", action="store_true",
                        help="Regenerate stale catalogs and migrate translations before building")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output_dir or root / "work/text/japan"
    mm_files, mm_raw = load_shared_layers(root, [f"MM{number:02d}.PS" for number in range(1, 32)])
    if len(mm_raw) != 0x1494 or any(mm_raw[TEXT_END:DATA_START]):
        raise ValueError("MM entry-08 text/data boundary changed")
    if mm_raw[DATA_START:DATA_START + 14] != bytes([0x11]) * 14:
        raise ValueError("MM entry-08 numeric data signature changed")
    mem_names = sorted(path.name for path in (root / "unpacked/japan").glob("MEM*.PAC"))
    if len(mem_names) != 16:
        raise ValueError(f"Expected 16 MEM files, found {len(mem_names)}")
    mem_files, mem_raw = load_shared_layers(root, mem_names)
    if len(mem_raw) != 0xB9C or mem_raw[-2:] != bytes.fromhex("097B"):
        raise ValueError("MEM entry-08 length or trailer changed")
    output.mkdir(parents=True, exist_ok=True)
    if args.ensure_current:
        if output.resolve() != (root / "work/text/japan").resolve():
            raise ValueError("--ensure-current uses the project's live catalog directory")
        ensure_reports(root, output, [
            ("MM-entry08-narrow-byte", mm_files, mm_raw, TEXT_END,
             "46 fixed short names and 46 fixed status records; no text crosses a field boundary.", 46),
            ("MEM-entry08-narrow-byte", mem_files, mem_raw, len(mem_raw) - 2,
             "45 fixed short names and 45 fixed status records; prototype runtime use remains unverified.", 45),
        ], args.backup_dir or root / "temp/catalog-migration-backups")
        return
    mm_count = write_report(
        root, output, "MM-entry08-narrow-byte", mm_files, mm_raw, TEXT_END,
        "46 fixed short names and 46 fixed status records; no text crosses a field boundary.",
        46, args.migrate_boundaries,
    )
    mem_count = write_report(
        root, output, "MEM-entry08-narrow-byte", mem_files, mem_raw, len(mem_raw) - 2,
        "45 fixed short names and 45 fixed status records; prototype runtime use remains unverified.",
        45, args.migrate_boundaries,
    )
    print(f"exported MM={mm_count} runs across 31 files; MEM={mem_count} runs across 16 files to {output}")


if __name__ == "__main__":
    main()
