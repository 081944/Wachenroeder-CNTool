"""Repair stale catalog displays from original words and both Japanese glyph maps.

Translation templates are checked against the reconstructed display, never edited.
Only display metadata is refreshed; source codes, selectors and translations remain
the independent inputs to validation and writeback.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from pac_codec import decode_entry, split_pac


def render_source(file, record, template_source, glyphs):
    words = [int(word, 16) for word in record["source_codes"].split()]
    parts = re.findall(r"<(?:[HICPG]:)?[0-9A-F]{4}>|[\s\S]", template_source)
    if len(parts) != len(words) and record.get('kind') == 'fixed-screen-field':
        left = record.get('source_leading_cells', 0)
        right = record.get('source_trailing_cells', 0)
        end = len(words) - right
        if left < 0 or right < 0 or left > end or any(word != 0x20 for word in words[:left] + words[end:]):
            raise ValueError(f"{file}:{record['id']}: fixed-screen padding metadata differs from source words")
        words = words[left:end]
    if len(parts) != len(words):
        raise ValueError(f"{file}:{record['id']}: source display/code counts differ")
    result = []
    pending = False
    has_header = record.get('kind') == 'pointer-block' or (
        file == 'TACTEVT.PAC' and record.get('kind') == 'pointer-text-block'
    )
    mm_subtitle = bool(re.fullmatch(r'MM\d{2}\.PS', file))
    for index, word in enumerate(words):
        # Roles come from the resource format and word position. A parameter
        # can have a glyph-range value, but must never become visible text.
        if pending:
            role = 'P'
            pending = False
        elif index == 0 and has_header:
            role = 'H'
        elif word <= 7:
            role = 'C'
            pending = word in (6, 7) and not mm_subtitle
        elif index == 0 and (word < 0x20 or word >= 0x8000):
            role = 'H'
        else:
            role = 'G'
        explicit = re.fullmatch(r'<([HICPG]):([0-9A-F]{4})>', parts[index])
        if explicit:
            label = 'H' if explicit[1] == 'I' else explicit[1]
            if label != role or int(explicit[2], 16) != word:
                raise ValueError(f"{file}:{record['id']}: source tag {parts[index]} differs from original {role}:{word:04X}")
            result.append(parts[index])
        else:
            result.append(glyphs.get(word, f'<{word:04X}>') if role == 'G' else f'<{word:04X}>')
    if pending:
        raise ValueError(f"{file}:{record['id']}: source marker lacks an operand")
    return "".join(result)


def refresh(root, backup_dir):
    glyphs = {}
    for relative in ("analysis/decoded-samples/font-map.csv", "analysis/japan-extra-glyphs.csv"):
        with (root / relative).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row["Character"]:
                    glyphs[int(row["TextCode"], 16)] = row["Character"]
    pending = {}
    for template in (root / "work/translation/events").glob("*.txt"):
        path = root / "work/text/catalog/japan" / f"{template.stem}.json"
        catalog = json.loads(path.read_text(encoding="utf-8"))
        lines = template.read_text(encoding="utf-8-sig").splitlines()
        rows = [line for line in lines if line and not line.startswith("#")]
        if len(rows) != len(catalog["records"]):
            raise ValueError(f"{template.name}: row count changed; explicit selector migration required")
        raw_layers = {}
        changed = 0
        for line, record in zip(rows, catalog["records"]):
            selector, source, target = line.split(" | ", 2)
            entry = "raw" if record["entry"] < 0 else f"{record['entry']:02d}"
            expected_selector = f"[{record['id']}] @0x{record['decoded_offset']:06X} len={record['code_units']} entry={entry}"
            if selector != expected_selector:
                raise ValueError(f"{template.name}:{record['id']}: selector changed; refusing display-only refresh")
            if source == record["source_text"].replace("\\", "\\\\").replace("|", "\\|"):
                continue
            if record["kind"] == "counted-narrow-u16":
                raise ValueError(f"{template.name}:{record['id']}: narrow source needs its own decoder")
            display = render_source(catalog["file"], record, source, glyphs)
            if source != display.replace("\\", "\\\\").replace("|", "\\|"):
                raise ValueError(f"{template.name}:{record['id']}: source differs from original words/glyph maps")
            if not raw_layers:
                packed = (root / "unpacked/japan" / catalog["file"]).read_bytes()
                if hashlib.sha256(packed).hexdigest().upper() != catalog["source_sha256"]:
                    raise ValueError(f"{template.name}: original resource hash changed")
                raw_layers[None] = packed
            index = record["entry"]
            codes = bytes.fromhex(record["source_codes"])
            start = record["decoded_offset"]
            if index not in raw_layers:
                if index < 0:
                    # entry=-1 means a standalone resource, not necessarily
                    # uncompressed data (MES_TLE.PS is a compressed stream).
                    candidates = [raw_layers[None]]
                    try:
                        candidates.insert(0, decode_entry(raw_layers[None], stack_limit=512))
                    except ValueError:
                        pass
                    raw_layers[index] = next((data for data in candidates
                        if data[start:start + len(codes)] == codes and
                        (not record.get('decoded_sha256') or
                         hashlib.sha256(data).hexdigest().upper() == record['decoded_sha256'].upper())), None)
                    if raw_layers[index] is None:
                        raise ValueError(f"{template.name}:{record['id']}: source words/hash differ from standalone resource")
                else:
                    raw_layers[index] = decode_entry(split_pac(raw_layers[None])[index], stack_limit=512)
            if record.get('decoded_sha256') and hashlib.sha256(raw_layers[index]).hexdigest().upper() != record['decoded_sha256'].upper():
                raise ValueError(f"{template.name}:{record['id']}: decoded resource hash changed")
            if raw_layers[index][start:start + len(codes)] != codes:
                raise ValueError(f"{template.name}:{record['id']}: source words differ from original resource")
            record["source_text"] = display
            changed += 1
        if changed:
            pending[path] = (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", changed)
    if not pending:
        print("Event source displays current")
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="event-displays-", dir=backup_dir))
    for path in pending:
        shutil.copy2(path, backup / path.name)
    try:
        for path, (text, count) in pending.items():
            path.write_text(text, encoding="utf-8")
            print(f"Refreshed {path.name}: {count} displays verified against original words")
    except Exception:
        for path in pending:
            shutil.copy2(backup / path.name, path)
        raise
    print(f"Original catalogs backed up to {backup}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    refresh(root, args.backup_dir or root / "temp/catalog-migration-backups")
