"""Export the separate 38 x 13-word battle-status equipment names in MM entry 7."""
import hashlib
import json
from pathlib import Path

from narrow_text_writeback import (ROOT, original_code_map, load_status_name_translations,
                                  write_template, read_template)
from pac_codec import split_pac, decode_entry


def main():
    stem = "MM-entry07-equipment"
    template = ROOT / "work/translation/narrow" / f"{stem}.txt"
    existed = template.exists()
    files = [f"MM{i:02d}.PS" for i in range(1, 32)]
    layers = [decode_entry(split_pac((ROOT / "unpacked/japan" / name).read_bytes())[7],
                           stack_limit=512) for name in files]
    start, end = 0x1A5A, 0x1E36
    if any(raw[start:end] != layers[0][start:end] for raw in layers):
        raise ValueError("The 38 equipment names differ between MM files")
    raw = layers[0]
    source_map = original_code_map()
    reverse = {value: key for key, value in source_map.items()}
    terms = load_status_name_translations(ROOT)
    catalog = json.loads((ROOT / "work/text/catalog/japan/STDAT.PAC.json").read_text(encoding="utf-8"))
    lines = (ROOT / "work/translation/events/STDAT.PAC.txt").read_text(encoding="utf-8-sig").splitlines()[1:]
    targets_by_words = {}
    for row in catalog["records"]:
        if row["kind"] == "counted-narrow-u16":
            words = tuple(int(word, 16) for word in row["source_codes"].split() if int(word, 16))
            target = lines[int(row["id"])].split("|", 2)[2].strip()
            if target:
                targets_by_words[words] = target
    for source, target in terms.items():
        if all(char in source_map for char in source):
            targets_by_words.setdefault(tuple(source_map[char] for char in source), target)
    records, targets = [], []
    for i, offset in enumerate(range(start, end, 26)):
        words = [int.from_bytes(raw[p:p + 2], "big") for p in range(offset, offset + 26, 2)]
        text_words = tuple(word for word in words if word)
        source = "".join(reverse[word] for word in text_words)
        if text_words not in targets_by_words and not existed:
            raise ValueError(f"Missing equipment translation at {offset:04X}: {source}")
        records.append(dict(id=f"{i:04d}", offset=f"0x{offset:06X}",
                            capacity_code_units=13, kind="fixed-narrow-u16",
                            source_codes=[f"{word:04X}" for word in words], source_text=source))
        targets.append(targets_by_words.get(text_words, ""))
    digest = hashlib.sha256(raw).hexdigest().upper()
    report = dict(schema="wachenroeder-mm-equipment-u16-v1", entry=7,
                  decoded_sha256=digest, source_files=files, records=records,
                  table_start=start, table_end=end, record_count=len(records))
    path = ROOT / "work/text/japan" / f"{stem}.json"
    if existed:
        targets = read_template(ROOT, stem, digest, records, menu=True)
    for record, target in zip(records, targets):
        if not target or len(target) > record["capacity_code_units"]:
            raise ValueError(f"{stem}:{record['id']}: missing translation or exceeds 13 cells")
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == serialized and existed:
        print("MM equipment catalog current: 38 names across 31 MM files")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")
    if not existed:
        write_template(ROOT, stem, digest, records, menu=True)
        lines = template.read_text(encoding="utf-8").splitlines()
        template.write_text("\n".join([lines[0]] + [line + target for line, target in zip(lines[1:], targets)]) + "\n", encoding="utf-8")
    print("Exported 38 separate battle-status equipment names shared by 31 MM files")


if __name__ == "__main__":
    main()
