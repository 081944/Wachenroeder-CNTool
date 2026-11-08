#!/usr/bin/env python3
"""Patch translated PAC files into the original Saturn disc and build BIN/CUE."""

from __future__ import annotations

import argparse
import json
import math
import mmap
import re
import shutil
from pathlib import Path


SECTOR_RAW = 2352
SECTOR_USER = 2048
USER_OFFSET = 16
PVD_LBA = 16
PVD_USER_OFFSET = PVD_LBA * SECTOR_RAW + USER_OFFSET
FILE_RE = re.compile(r'^FILE\s+"(?P<name>[^"]+)"\s+BINARY\s*$', re.I)
TRACK_RE = re.compile(r'^\s*TRACK\s+(?P<number>\d+)\s+(?P<type>[^\s]+)\s*$', re.I)
INDEX_RE = re.compile(r'^\s*INDEX\s+(?P<number>\d+)\s+(?P<msf>\d{2}:\d{2}:\d{2})\s*$', re.I)


def u32le(data: bytes | mmap.mmap, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def put_both_u32(data: mmap.mmap, offset: int, value: int) -> None:
    data[offset : offset + 4] = value.to_bytes(4, "little")
    data[offset + 4 : offset + 8] = value.to_bytes(4, "big")


def read_user(data: mmap.mmap, lba: int) -> bytes:
    start = lba * SECTOR_RAW + USER_OFFSET
    return bytes(data[start : start + SECTOR_USER])


def iso_name(record: bytes) -> str:
    length = record[32]
    raw = record[33 : 33 + length]
    return raw.decode("ascii", errors="strict").upper()


def parse_directory(data: mmap.mmap, extent: int, length: int) -> list[dict[str, int | str]]:
    result: list[dict[str, int | str]] = []
    directory_sectors = math.ceil(length / SECTOR_USER)
    for sector_index in range(directory_sectors):
        lba = extent + sector_index
        sector = read_user(data, lba)
        position = 0
        while position < SECTOR_USER:
            record_length = sector[position]
            if record_length == 0:
                break
            if position + record_length > SECTOR_USER or record_length < 34:
                raise ValueError(f"Invalid ISO9660 directory record at LBA {lba}, offset {position}")
            record = sector[position : position + record_length]
            result.append(
                {
                    "name": iso_name(record),
                    "extent": u32le(record, 2),
                    "length": u32le(record, 10),
                    "flags": record[25],
                    "record_lba": lba,
                    "record_offset": position,
                    "record_length": record_length,
                }
            )
            position += record_length
    return result


def bcd(value: int) -> int:
    return (value // 10) * 16 + value % 10


def set_sector_address(sector: mmap.mmap, raw_offset: int, lba: int) -> None:
    absolute_frame = lba + 150
    minute, remainder = divmod(absolute_frame, 60 * 75)
    second, frame = divmod(remainder, 75)
    sector[raw_offset + 12 : raw_offset + 16] = bytes((bcd(minute), bcd(second), bcd(frame), 1))


def parse_msf(value: str) -> int:
    minutes, seconds, frames = (int(part) for part in value.split(":"))
    if seconds >= 60 or frames >= 75:
        raise ValueError(f"Invalid CUE time: {value}")
    return (minutes * 60 + seconds) * 75 + frames


def extract_cue_bin(source_cue: Path, source_bin: Path, extracted: Path) -> dict[str, object]:
    """Split a combined MODE1/2352 CUE/BIN into the metadata used below."""
    lines = source_cue.read_text(encoding="ascii").splitlines()
    files = [match.group("name") for line in lines if (match := FILE_RE.match(line))]
    if len(files) != 1:
        raise ValueError("The source CUE must contain one combined BINARY file")

    tracks: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in lines:
        track_match = TRACK_RE.match(line)
        if track_match:
            current = {
                "number": int(track_match.group("number")),
                "cue_type": track_match.group("type").upper(),
                "start_frame": None,
                "index01_frame": None,
            }
            tracks.append(current)
            continue
        index_match = INDEX_RE.match(line)
        if index_match and current is not None:
            index_number = int(index_match.group("number"))
            frame = parse_msf(index_match.group("msf"))
            if index_number == 0 and current["start_frame"] is None:
                current["start_frame"] = frame
            elif index_number == 1:
                current["index01_frame"] = frame
                if current["start_frame"] is None:
                    current["start_frame"] = frame

    if not tracks or tracks[0]["cue_type"] not in {"MODE1/2352", "MODE1_RAW"}:
        raise ValueError("The first CUE track must be MODE1/2352")
    if any(track["start_frame"] is None for track in tracks):
        raise ValueError("Every CUE track must have INDEX 00 or INDEX 01")
    if source_bin.stat().st_size % SECTOR_RAW:
        raise ValueError(f"BIN size is not divisible by {SECTOR_RAW}: {source_bin}")

    total_frames = source_bin.stat().st_size // SECTOR_RAW
    for index, track in enumerate(tracks):
        start = int(track["start_frame"])
        end = int(tracks[index + 1]["start_frame"]) if index + 1 < len(tracks) else total_frames
        if start < 0 or end <= start or end > total_frames:
            raise ValueError(f"Invalid CUE track range for track {track['number']}: {start}-{end}")
        number = int(track["number"])
        output = extracted / f"track{number:02d}.bin"
        output.parent.mkdir(parents=True, exist_ok=True)
        with source_bin.open("rb") as source, output.open("wb") as destination:
            source.seek(start * SECTOR_RAW)
            remaining = (end - start) * SECTOR_RAW
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError(f"Unexpected EOF while extracting track {number}")
                destination.write(chunk)
                remaining -= len(chunk)
        track["type"] = "AUDIO" if track["cue_type"] == "AUDIO" else "MODE1_RAW"
        track["frames"] = end - start
        track["first_frame"] = start
        index01 = track["index01_frame"]
        track["pregap"] = max(0, int(index01) - start) if index01 is not None else 0
        track["pregap_type"] = "VAUDIO" if track["type"] == "AUDIO" else "MODE1"
        track["pregap_sub"] = "NONE"
        track["postgap"] = 0
        track["file"] = output.name
        track.pop("cue_type")
        track.pop("start_frame")
        track.pop("index01_frame")

    metadata = {
        "format": "wachenroeder-cue-bin-extract-v1",
        "source_cue": str(source_cue),
        "source_bin": str(source_bin),
        "tracks": tracks,
    }
    extracted.mkdir(parents=True, exist_ok=True)
    (extracted / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return metadata


def patch_sources(pac_dir: Path) -> dict[str, Path]:
    """Return PAC/PS outputs that should be written into the ISO.

    Some resources are retained in the build directory for inspection but
    are represented inside another container on disc. Their PAC manifest can
    mark them with ``disc_patch: false`` to avoid spending ISO space twice.
    """
    excluded: set[str] = set()
    manifest_path = pac_dir / "manifest.json"
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        for record in payload.get("files", []):
            if record.get("disc_patch") is False:
                excluded.add(str(record["file"]).upper())
    return {
        path.name.upper(): path
        for path in sorted(pac_dir.iterdir())
        if (path.name == "0" or path.suffix.upper() in {".PAC", ".PS"})
        and path.name.upper() not in excluded
    }


def verify_patched_iso(track_path: Path, pac_dir: Path, expected_frames: int) -> None:
    targets = patch_sources(pac_dir)
    with track_path.open("r+b") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        pvd = PVD_USER_OFFSET
        root_record = bytes(data[pvd + 156 : pvd + 190])
        entries = parse_directory(data, u32le(root_record, 2), u32le(root_record, 10))
        files = {str(entry["name"]).split(";", 1)[0]: entry for entry in entries if not int(entry["flags"]) & 2}
        ranges: list[tuple[int, int, str]] = []
        for entry in entries:
            if int(entry["flags"]) & 2:
                continue
            start = int(entry["extent"])
            sectors = math.ceil(int(entry["length"]) / SECTOR_USER)
            if start < 0 or start + sectors > expected_frames:
                # CDDA placeholder entries intentionally point beyond the data track.
                continue
            ranges.append((start, start + sectors, str(entry["name"])))
        for index, (start, end, name) in enumerate(sorted(ranges)):
            if index and start < sorted(ranges)[index - 1][1]:
                raise ValueError(f"ISO file extents overlap after patch: {name}")
        for name, source in targets.items():
            key = name.split(";", 1)[0]
            entry = files[key]
            length = int(entry["length"])
            extent = int(entry["extent"])
            content = b"".join(read_user(data, extent + index) for index in range(math.ceil(length / SECTOR_USER)))[:length]
            expected = source.read_bytes()
            if content != expected:
                raise ValueError(f"ISO content verification failed for {name}")


def patch_data_track(track_path: Path, pac_dir: Path, output_path: Path, expected_frames: int) -> dict[str, object]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(track_path, output_path)
    if output_path.stat().st_size != expected_frames * SECTOR_RAW:
        raise ValueError("Extracted data track size does not match CHD metadata")

    targets = patch_sources(pac_dir)
    if not targets:
        raise FileNotFoundError(f"No translated PAC files found in {pac_dir}")

    with output_path.open("r+b") as handle, mmap.mmap(handle.fileno(), 0) as data:
        if bytes(data[PVD_USER_OFFSET + 1 : PVD_USER_OFFSET + 6]) != b"CD001":
            raise ValueError("Data track does not contain an ISO9660 primary volume descriptor")
        volume_sectors = u32le(data, PVD_USER_OFFSET + 80)
        # The ISO volume size covers the later audio tracks too.  File extents
        # must remain inside the first MODE1 track, which is the writable area.
        available_sectors = min(volume_sectors, expected_frames)
        root_record = bytes(data[PVD_USER_OFFSET + 156 : PVD_USER_OFFSET + 190])
        root_extent = u32le(root_record, 2)
        root_length = u32le(root_record, 10)
        entries = parse_directory(data, root_extent, root_length)
        files = {str(entry["name"]).split(";", 1)[0]: entry for entry in entries if not int(entry["flags"]) & 2}

        missing = sorted(name for name in targets if name.split(";", 1)[0] not in files)
        if missing:
            raise ValueError("Translated PACs missing from ISO root: " + ", ".join(missing))

        reserved_end = max(31, root_extent + math.ceil(root_length / SECTOR_USER), PVD_LBA + 1)
        occupied: list[tuple[int, int]] = [(0, reserved_end)]
        plans: dict[str, tuple[dict[str, int | str], Path, bytes, int, bool]] = {}
        for name, pac_path in targets.items():
            key = name.split(";", 1)[0]
            entry = files[key]
            content = pac_path.read_bytes()
            sectors = math.ceil(len(content) / SECTOR_USER)
            old_extent = int(entry["extent"])
            old_sectors = math.ceil(int(entry["length"]) / SECTOR_USER)
            if old_extent >= available_sectors:
                raise ValueError(
                    f"{key} is outside the writable MODE1 data track"
                )
            if sectors > old_sectors:
                raise ValueError(
                    f"{key} exceeds its original ISO allocation: "
                    f"{sectors} sectors > {old_sectors}; reduce the compressed stream "
                    "instead of relocating the file"
                )
            # The reference writer always preserves each ISO directory
            # extent.  The game may use cached/fixed resource locations even
            # though a generic ISO reader would accept relocation.
            plans[key] = (entry, pac_path, content, sectors, True)
        for entry in entries:
            key = str(entry["name"]).split(";", 1)[0]
            if key in plans:
                planned_entry, _, _, planned_sectors, in_place = plans[key]
                if in_place:
                    occupied.append((int(planned_entry["extent"]), int(planned_entry["extent"]) + planned_sectors))
                continue
            start = int(entry["extent"])
            if start >= available_sectors:
                # ISO directory entries for later CDDA tracks point beyond
                # the writable MODE1 data track.
                continue
            end = min(
                start + math.ceil(int(entry["length"]) / SECTOR_USER),
                available_sectors,
            )
            occupied.append((start, end))
        occupied.sort()
        free_intervals: list[list[int]] = []
        cursor = reserved_end
        for start, end in occupied:
            if start > cursor:
                free_intervals.append([cursor, min(start, available_sectors)])
            cursor = max(cursor, end)
        if cursor < available_sectors:
            free_intervals.append([cursor, available_sectors])

        manifest: list[dict[str, object]] = []
        data_occupied_ends = [end for start, end in occupied if start < available_sectors and end <= available_sectors]
        max_extent = max(data_occupied_ends, default=reserved_end)
        allocation_order = sorted(plans.items(), key=lambda item: (-item[1][3], item[0]))
        for key, (entry, pac_path, content, sectors, in_place) in allocation_order:
            if in_place:
                next_extent = int(entry["extent"])
            else:
                allocation = next(
                    (interval for interval in free_intervals if interval[1] - interval[0] >= sectors),
                    None,
                )
                if allocation is None:
                    raise ValueError(f"No contiguous free ISO extent can hold {key} ({sectors} sectors)")
                next_extent = allocation[0]
                allocation[0] += sectors
            record_raw = int(entry["record_lba"]) * SECTOR_RAW + USER_OFFSET + int(entry["record_offset"])
            put_both_u32(data, record_raw + 2, next_extent)
            put_both_u32(data, record_raw + 10, len(content))
            for sector_index in range(sectors):
                chunk = content[sector_index * SECTOR_USER : (sector_index + 1) * SECTOR_USER]
                chunk = chunk.ljust(SECTOR_USER, b"\0")
                raw_offset = (next_extent + sector_index) * SECTOR_RAW
                data[raw_offset + USER_OFFSET : raw_offset + USER_OFFSET + SECTOR_USER] = chunk
                set_sector_address(data, raw_offset, next_extent + sector_index)
            manifest.append(
                {
                    "file": key,
                    "source": str(pac_path.relative_to(pac_dir.parent.parent)),
                    "old_extent": entry["extent"],
                    "old_length": entry["length"],
                    "new_extent": next_extent,
                    "new_length": len(content),
                    "sectors": sectors,
                }
            )
            max_extent = max(max_extent, next_extent + sectors)
        data.flush()

    verify_patched_iso(output_path, pac_dir, expected_frames)
    data_entries = [entry for entry in entries if int(entry["extent"]) < expected_frames]
    return {
        "data_track": str(track_path),
        "patched_track": str(output_path),
        "volume_sectors": volume_sectors,
        "original_max_allocated_lba": max(
            int(entry["extent"]) + math.ceil(int(entry["length"]) / SECTOR_USER) - 1 for entry in data_entries
        ),
        "new_max_allocated_lba": max_extent - 1,
        "files": manifest,
    }


def msf(frames: int) -> str:
    minutes, remainder = divmod(frames, 60 * 75)
    seconds, frame = divmod(remainder, 75)
    return f"{minutes:02d}:{seconds:02d}:{frame:02d}"


def build_bin_cue(extracted: Path, metadata: dict[str, object], patched_data: Path, output_dir: Path) -> dict[str, object]:
    tracks = metadata["tracks"]
    assert isinstance(tracks, list)
    bin_path = output_dir / "Wachenroeder-CN.bin"
    cue_path = output_dir / "Wachenroeder-CN.cue"
    output_dir.mkdir(parents=True, exist_ok=True)

    with bin_path.open("wb") as output:
        for track in tracks:
            assert isinstance(track, dict)
            number = int(track["number"])
            source = patched_data if number == int(tracks[0]["number"]) else extracted / str(track["file"])
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)

    cue_lines = [f'FILE "{bin_path.name}" BINARY']
    start_frame = 0
    cue_tracks: list[dict[str, object]] = []
    for track in tracks:
        assert isinstance(track, dict)
        number = int(track["number"])
        track_type = str(track["type"])
        cue_type = "AUDIO" if track_type == "AUDIO" else "MODE1/2352"
        cue_lines.append(f"  TRACK {number:02d} {cue_type}")
        pregap = int(track.get("pregap", 0))
        if pregap and track_type == "AUDIO":
            cue_lines.append(f"    INDEX 00 {msf(start_frame)}")
            cue_lines.append(f"    INDEX 01 {msf(start_frame + pregap)}")
        else:
            cue_lines.append(f"    INDEX 01 {msf(start_frame)}")
        cue_tracks.append({
            "number": number,
            "type": track_type,
            "start_frame": start_frame,
            "pregap": pregap,
            "index01_frame": start_frame + pregap,
            "frames": int(track["frames"]),
        })
        start_frame += int(track["frames"])
    cue_path.write_text("\n".join(cue_lines) + "\n", encoding="ascii", newline="\n")
    expected_bytes = start_frame * SECTOR_RAW
    if bin_path.stat().st_size != expected_bytes:
        raise ValueError(f"Combined BIN size mismatch: {bin_path.stat().st_size} != {expected_bytes}")
    return {"bin": str(bin_path), "cue": str(cue_path), "frames": start_frame, "tracks": cue_tracks}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--extracted", type=Path, required=True)
    parser.add_argument("--patched-track", type=Path, required=True)
    parser.add_argument("--source-cue", type=Path)
    parser.add_argument("--source-bin", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    build_root = args.build_root.resolve() if args.build_root else root / "work" / "build"
    resource_manifest = build_root / "repacked/pac/manifest.json"
    if resource_manifest.is_file():
        resources = json.loads(resource_manifest.read_text(encoding="utf-8"))
        if resources.get("runtime_font_pending"):
            raise ValueError("8x12 resource baseline has no runtime font; run the resident font builder first")
    extracted = args.extracted.resolve()
    patched_track = args.patched_track.resolve()
    source_cue = args.source_cue.resolve() if args.source_cue else None
    source_bin = args.source_bin.resolve() if args.source_bin else None
    if (source_cue is None) != (source_bin is None):
        raise ValueError("--source-cue and --source-bin must be provided together")
    if source_cue is not None:
        if not source_cue.is_file() or not source_bin.is_file():
            raise FileNotFoundError(f"Missing source CUE/BIN: {source_cue}, {source_bin}")
        extract_cue_bin(source_cue, source_bin, extracted)
    metadata = json.loads((extracted / "metadata.json").read_text(encoding="utf-8"))
    tracks = metadata.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("CHD extraction metadata has no tracks")
    data_track = tracks[0]
    if data_track.get("type") not in {"MODE1_RAW", "MODE1/2352"}:
        raise ValueError(f"First track is not MODE1/2352: {data_track.get('type')}")
    pac_dir = build_root / "repacked" / "pac"
    final_dir = build_root / "final"
    patch_manifest = patch_data_track(extracted / str(data_track["file"]), pac_dir, patched_track, int(data_track["frames"]))
    bin_cue = build_bin_cue(extracted, metadata, patched_track, final_dir)
    manifest = {
        "format": "wachenroeder-bin-cue-rebuild-v1",
        "source_cue": str(source_cue) if source_cue else None,
        "source_bin": str(source_bin) if source_bin else None,
        "pac_manifest": str(pac_dir / "manifest.json"),
        "patch": patch_manifest,
        "output": bin_cue,
    }
    (final_dir / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
