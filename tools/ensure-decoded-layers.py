"""Decode translated source layers into the temporary build workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from pac_codec import decode_entry, split_pac


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    temp_root = args.temp_root.resolve()
    payload = json.loads((temp_root / "imported.json").read_text(encoding="utf-8-sig"))
    layers: dict[tuple[str, int], dict[str, object]] = {}
    for record in payload["records"]:
        supported_file = bool(
            re.fullmatch(
                r"(?:EVTTK\d{2}|VTKALL|BUP_DATA|OPMES|SHOPDATA|STDAT|TITLE)\.PAC",
                str(record["file"]),
            )
            or bool(re.fullmatch(r"TACTEVT\.PAC|MEM\d{2}\.PAC|MM\d{2}\.PS", str(record["file"])))
            or record["file"] == "MES_TLE.PS"
        )
        if (
            record["storage_policy"] not in {"pointer-repack", "fixed-name-slot", "fixed-grid-in-place"}
            or not supported_file
        ):
            continue
        key = (record["file"], int(record["entry"]))
        layers.setdefault(key, {"file": key[0], "entry": key[1], "records": []})["records"].append(record)

    output = temp_root / "source-layers" / "japan"
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for (name, entry), layer in sorted(layers.items()):
        source = root / "unpacked" / "japan" / name
        source_data = source.read_bytes()
        if source.suffix.upper() == ".PAC" or re.fullmatch(r"MM\d{2}\.PS", name):
            entries = split_pac(source_data)
            if entry < 0 or entry >= len(entries):
                raise ValueError(f"{name}: missing PAC entry {entry}")
            raw = decode_entry(entries[entry])
            label = f"entry{entry:02d}"
        else:
            if entry != -1:
                raise ValueError(f"{name}: standalone resource must use entry -1")
            raw = decode_entry(source_data)
            label = "raw"
        expected = {record["decoded_sha256"] for record in layer["records"]}
        if expected != {sha256(raw)}:
            raise ValueError(f"{name}:{entry}: decoded hash differs from catalog")
        destination = output / f"{name}.{label}.bin"
        destination.write_bytes(raw)
        manifest.append({"file": name, "entry": entry, "path": str(destination), "bytes": len(raw), "sha256": sha256(raw)})
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Decoded {len(manifest)} translated source layers into {output}")


if __name__ == "__main__":
    main()
