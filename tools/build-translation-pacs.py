#!/usr/bin/env python3
"""Build and verify translated PAC files from decoded project outputs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import runpy
from pathlib import Path

from PIL import Image

from image_codec import encode_8bpp_tile_strip_png, encode_tilemap_png, encode_wb_png
from pac_codec import (
    build_pac,
    decode_entry,
    encode_entry_sparse,
    encode_entry_best,
    replace_pac_entries,
    split_pac,
)
from narrow_text_writeback import (
    assert_byte_capacity,
    code_map,
    patch_battle,
    patch_menu,
    patch_shared,
    patch_weather_table,
)


COMPRESSION_BLOCK_SIZE_BY_FILE = {
    # These are the resource-specific values used by the Korean reference
    # build. The default for newly written layers remains 3500.
    "B3OPN.PAC": 5000,
    "PSEL.PAC": 2000,
    "SENG01.PAC": 5000,
    "SENG04.PAC": 5000,
    "STDAT.PAC": 2500,
    "WB_02.PAC": 5000,
    "WB_05.PAC": 4000,
    "WB_07.PAC": 4000,
    # The edited opening tilemap is one sector larger when packed at the
    # default size.  This resource has no spare ISO allocation; 4000 keeps
    # it in place while remaining inside the Korean compressor's safe range.
    "OP_02.PS": 4000,
    "INTR_04.PS": 4500,
    "INTR_08.PS": 4500,
}


def compression_block_size(file_name: str, entry: int | None = None) -> int:
    """Return the Korean-reference block size for one replacement layer."""
    if file_name.upper() == "ASCII.PAC":
        # The large boot font needs the smaller runtime-safe blocks. The
        # narrow auxiliary bank uses the normal text-stream setting.
        return 5000 if entry == 0 else 3500
    return COMPRESSION_BLOCK_SIZE_BY_FILE.get(file_name.upper(), 3500)


COUNTED_NARROW_BLOCK_SIZES = (
    512,
    1024,
    1536,
    2048,
    2560,
    3000,
    3500,
    4096,
    5000,
    8192,
    16384,
)

RUNTIME_EXECUTABLE = "0"
RUNTIME_SMALL_FONT_LIMIT_OFFSET = 0x30F28
RUNTIME_SMALL_FONT_LIMIT_ORIGINAL = 0x10FF
PACKED_RUNTIME = runpy.run_path(str(Path(__file__).with_name("patch-packed-font-ascii-probe.py")))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def validate_translation_inputs(root: Path, temp_root: Path) -> None:
    import_path = temp_root / "imported.json"
    imported = json.loads(import_path.read_text(encoding="utf-8-sig"))
    saved = imported.get("translation_input_sha256")
    if not isinstance(saved, dict):
        raise ValueError("Translation import lacks input hashes; rerun import-translation.ps1")
    translation_root = root / "work/translation"
    current = {
        path.relative_to(translation_root).as_posix(): sha256(path.read_bytes())
        for directory in ("events", "narrow")
        for path in (translation_root / directory).glob("*.txt") if path.is_file()
    }
    if current != saved:
        changed = sorted(set(current) ^ set(saved) |
                         {path for path in current.keys() & saved.keys()
                          if current[path] != saved[path]})
        raise ValueError(
            "Translation import is stale; rerun import-translation.ps1: "
            + ", ".join(changed[:8])
        )
    stamp_path = temp_root / "repacked/japan/imported.sha256"
    if not stamp_path.is_file() or stamp_path.read_text(encoding="utf-8-sig").strip() != sha256(import_path.read_bytes()):
        raise ValueError("Event layers are stale; rerun repack-event-text.ps1")


def resolve_image_input(root: Path, original_path: Path) -> Path | None:
    """Return a manually edited image, or None when it was not edited."""
    imageori_root = root / "work" / "imageori"
    try:
        relative_path = original_path.relative_to(imageori_root)
    except ValueError:
        return None
    edited_path = root / "work" / "imageedit" / relative_path
    if edited_path.is_file():
        return edited_path
    return None


def image_edit_paths(root: Path) -> set[Path]:
    """Return every edited PNG that must be accounted for by a manifest."""
    edited_root = root / "work" / "imageedit"
    if not edited_root.exists():
        return set()
    return {path.resolve() for path in edited_root.rglob("*.png") if path.is_file()}


def load_image_patches(root: Path) -> list[dict[str, object]]:
    """Load only manually edited Japan images under work/imageedit."""
    patches: list[dict[str, object]] = []
    tilemap_manifest = root / "work" / "imageori" / "manifests" / "tilemap-manifest.csv"
    if tilemap_manifest.exists():
        with tilemap_manifest.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                original_png_path = root / Path(row["Png"])
                png_path = resolve_image_input(root, original_png_path)
                if png_path is None:
                    continue
                raw_path = (
                    root
                    / "work"
                    / "extracted"
                    / "japan"
                    / "graphics-decoded"
                    / png_path.with_suffix(".bin").name
                )
                if not png_path.exists() or not raw_path.exists():
                    raise FileNotFoundError(
                        f"Missing tilemap image input or source: {png_path} / {raw_path}"
                    )
                patches.append(
                    {
                        "kind": "tilemap",
                        "file": row["File"],
                        "entry": row["Entry"],
                        "png": png_path,
                        "original_png": original_png_path,
                        "edited": True,
                        "source_raw": raw_path.read_bytes(),
                    }
                )

    wb_manifest = root / "work" / "imageori" / "manifests" / "image-manifest.csv"
    if wb_manifest.exists():
        with wb_manifest.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row["Version"] != "japan":
                    continue
                original_png_path = root / Path(row["Png"])
                png_path = resolve_image_input(root, original_png_path)
                if png_path is None:
                    continue
                raw_path = (
                    root
                    / "work"
                    / "extracted"
                    / "japan"
                    / "decoded"
                    / f"{row['File']}.entry{int(row['Entry']):02d}.bin"
                )
                if not png_path.exists() or not raw_path.exists():
                    raise FileNotFoundError(
                        f"Missing WB image input or source: {png_path} / {raw_path}"
                    )
                patches.append(
                    {
                        "kind": "wb",
                        "file": row["File"],
                        "entry": int(row["Entry"]),
                        "png": png_path,
                        "original_png": original_png_path,
                        "edited": True,
                        "source_raw": raw_path.read_bytes(),
                    }
                )
    return patches


def load_candidate_image_patches(root: Path) -> list[dict[str, object]]:
    """Load edited candidate images with an explicit source-backed mapping."""
    manifest_path = root / "work" / "imageori" / "manifests" / "image-candidate-manifest.csv"
    if not manifest_path.exists():
        return []
    patches: list[dict[str, object]] = []
    with manifest_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            original_png = root / Path(row["png"])
            png_path = resolve_image_input(root, original_png)
            if png_path is None:
                continue
            if not original_png.is_file():
                raise FileNotFoundError(f"Candidate source image is missing: {original_png}")
            if row.get("kind") in {
                "menu-sprites",
                "bup-sprites",
                "bup-8bpp-crop",
                "bup-entry02-sprites",
                "bup-hidden-title-tiles",
            }:
                raw_pixels_path = root / Path(row["raw_pixels"])
                if not raw_pixels_path.is_file():
                    raise FileNotFoundError(
                        f"Candidate source pixels are missing: {raw_pixels_path}"
                    )
                source_raw = raw_pixels_path.read_bytes()
                expected_sha = row.get("source_sha256", "")
                if expected_sha and sha256(source_raw) != expected_sha:
                    raise ValueError(
                        f"{png_path}: candidate pixel source hash does not match the manifest"
                    )
                if row.get("kind") == "menu-sprites":
                    patches.append(
                        {
                            "kind": "menu-sprite",
                            "file": row["source"],
                            "top_entry": int(row["top_entry"]),
                            "pixel_entry": int(row.get("pixel_entry", "0")),
                            "definition_entry": int(row.get("inner_entry", "1")),
                            "sprite": int(row["sprite"]),
                            "width": int(row["width"]),
                            "height": int(row["height"]),
                            "bits_per_pixel": int(row["bits_per_pixel"]),
                            "byte_count": int(row["byte_count"]),
                            "pixel_offset": int(row["pixel_offset"]),
                            "png": png_path,
                            "original_png": original_png,
                            "source_raw": source_raw,
                        }
                    )
                elif row.get("kind") in {"bup-sprites", "bup-8bpp-crop"}:
                    palette_path = root / Path(row["palette"])
                    palette_raw = palette_path.read_bytes()
                    patches.append(
                        {
                            "kind": "bup-sprite",
                            "file": row["source"],
                            "entry": int(row["top_entry"]),
                            "sprite": row["sprite"],
                            "width": int(row["width"]),
                            "height": int(row["height"]),
                            "bits_per_pixel": int(row["bits_per_pixel"]),
                            "byte_count": int(row["byte_count"]),
                            "pixel_offset": int(row["pixel_offset"]),
                            "palette": _candidate_palette(palette_raw),
                            "png": png_path,
                            "original_png": original_png,
                            "source_raw": source_raw,
                        }
                    )
                elif row.get("kind") == "bup-entry02-sprites":
                    group_raw_path = root / Path(row["raw_group"])
                    palette_path = root / Path(row["palette"])
                    if not group_raw_path.is_file() or not palette_path.is_file():
                        raise FileNotFoundError(
                            f"Candidate BUP entry02 group or palette is missing: "
                            f"{group_raw_path} / {palette_path}"
                        )
                    group_raw = group_raw_path.read_bytes()
                    if row.get("group_sha256") and sha256(group_raw) != row["group_sha256"]:
                        raise ValueError(
                            f"{png_path}: BUP entry02 group source hash does not match the manifest"
                        )
                    patches.append(
                        {
                            "kind": "bup-entry02-sprite",
                            "file": row["source"],
                            "entry": int(row["top_entry"]),
                            "group": int(row["group"]),
                            "inner_entry": int(row["inner_entry"]),
                            "sprite": int(row["sprite"]),
                            "width": int(row["width"]),
                            "height": int(row["height"]),
                            "bits_per_pixel": int(row["bits_per_pixel"]),
                            "byte_count": int(row["byte_count"]),
                            "palette": _candidate_palette(palette_path.read_bytes()),
                            "png": png_path,
                            "original_png": original_png,
                            "source_raw": source_raw,
                            "group_raw": group_raw,
                        }
                    )
                elif row.get("kind") == "bup-hidden-title-tiles":
                    patches.append(
                        {
                            "kind": "bup-hidden-title-tiles",
                            "file": row["source"],
                            "entry": int(row["top_entry"]),
                            "first_tile": int(row["first_tile"]),
                            "tile_count": int(row["tile_count"]),
                            "png": png_path,
                            "original_png": original_png,
                            "source_raw": source_raw,
                        }
                    )
                else:
                    raise ValueError(f"Unsupported image candidate kind: {row.get('kind')}")
                continue
            if row.get("kind") != "title-press-start-button":
                raise ValueError(
                    f"Edited candidate has no verified writeback mapping: {png_path}"
                )
            source_path = root / "unpacked" / "japan" / "START.PS"
            source_data = decode_entry(source_path.read_bytes())
            offset = int(row.get("source_raw_offset", "0"), 0)
            size = int(row.get("source_raw_size", "0"), 0)
            source_raw = source_data[offset : offset + size]
            palette = source_data[0x0008 : 0x0008 + 512]
            if len(source_raw) != size or size != 144 * 8 or len(palette) != 512:
                raise ValueError(f"{png_path}: START.PS source span is invalid")
            patches.append(
                {
                    "kind": "start-ps-texture",
                    "file": "START.PS",
                    "entry": "raw",
                    "png": png_path,
                    "original_png": original_png,
                    "source_raw": source_raw,
                    "source_offset": offset,
                    "source_palette": palette,
                }
            )
    return patches


def _start_ps_palette(raw: bytes) -> list[tuple[int, int, int]]:
    if len(raw) != 512:
        raise ValueError("START.PS palette must contain 256 BGR555 colors")
    return [
        (
            (value & 0x1F) * 255 // 31,
            ((value >> 5) & 0x1F) * 255 // 31,
            ((value >> 10) & 0x1F) * 255 // 31,
        )
        for value in (int.from_bytes(raw[index : index + 2], "big") for index in range(0, 512, 2))
    ]


def _candidate_palette(raw: bytes) -> list[tuple[int, int, int]]:
    """Decode a 256-color big-endian BGR555 candidate palette."""
    if len(raw) < 512:
        raise ValueError("candidate palette must contain 256 BGR555 colors")
    return [
        (
            (value & 0x1F) * 255 // 31,
            ((value >> 5) & 0x1F) * 255 // 31,
            ((value >> 10) & 0x1F) * 255 // 31,
        )
        for value in (
            int.from_bytes(raw[index : index + 2], "big")
            for index in range(0, 512, 2)
        )
    ]


def _nearest_candidate_color(
    rgb: tuple[int, int, int],
    palette: list[tuple[int, int, int]],
    candidates: range,
) -> int:
    return min(
        candidates,
        key=lambda index: sum(
            (rgb[channel] - palette[index][channel]) ** 2 for channel in range(3)
        ),
    )


def _same_candidate_color(
    pixel: tuple[int, int, int, int],
    color: tuple[int, int, int],
    tolerance: int = 2,
) -> bool:
    return pixel[3] != 0 and all(
        abs(pixel[channel] - color[channel]) <= tolerance for channel in range(3)
    )


def _duplicate_palette_index_by_row(
    base_pixels: bytes,
    pixel_offset: int,
    width: int,
    height: int,
    candidates: list[int],
    palette: list[tuple[int, int, int]],
) -> dict[tuple[int, int], int]:
    """Recover duplicate-index semantics used by runtime-selected BUP rows.

    BUP entry01 keeps separate palette indices for selected and unselected
    rows even when the corresponding CLUT entries have identical RGB values.
    New glyph pixels must use the index belonging to the source row, rather
    than the first numerically matching candidate.
    """
    by_color: dict[tuple[int, int, int], list[int]] = {}
    for index in candidates:
        by_color.setdefault(palette[index], []).append(index)
    duplicate_groups = [indices for indices in by_color.values() if len(indices) > 1]
    if not duplicate_groups:
        return {}

    rows = [
        base_pixels[pixel_offset + row * width : pixel_offset + (row + 1) * width]
        for row in range(height)
    ]
    result: dict[tuple[int, int], int] = {}
    for indices in duplicate_groups:
        group = set(indices)
        # The dominant non-group source index identifies the visual row
        # context (for example, red background 0x81 vs blue background 0x82).
        contexts: list[int | None] = []
        for row in rows:
            counts: dict[int, int] = {}
            for value in row:
                if value in group:
                    continue
                counts[value] = counts.get(value, 0) + 1
            contexts.append(max(counts, key=counts.get) if counts else None)

        context_to_index: dict[int, int] = {}
        for row, values in enumerate(rows):
            context = contexts[row]
            if context is None:
                continue
            counts = {index: values.count(index) for index in indices}
            used = [index for index, count in counts.items() if count]
            if used:
                context_to_index[context] = max(used, key=lambda index: counts[index])

        for row, context in enumerate(contexts):
            if context in context_to_index:
                result[(row, palette[indices[0]])] = context_to_index[context]
    return result


def _encode_bup_crop_png(
    base_pixels: bytes,
    png_path: Path,
    width: int,
    height: int,
    pixel_offset: int,
    byte_count: int,
    palette: list[tuple[int, int, int]],
) -> tuple[bytes, dict[str, int]]:
    """Replace an 8bpp BUP label crop without exceeding its shared pool."""
    expected_size = width * height
    if byte_count != expected_size:
        raise ValueError(f"{png_path.name}: BUP crop byte count does not match its dimensions")
    if pixel_offset < 0 or pixel_offset + byte_count > len(base_pixels):
        raise ValueError(f"{png_path.name}: BUP crop exceeds the shared pixel pool")
    if len(palette) < 256:
        raise ValueError(f"{png_path.name}: BUP palette is too short")
    with Image.open(png_path) as image:
        if image.size != (width, height):
            raise ValueError(
                f"{png_path.name}: expected {width}x{height}, "
                f"got {image.width}x{image.height}"
            )
        pixels = list(image.convert("RGBA").getdata())

    output = bytearray(base_pixels)
    changed = 0
    quantized = 0
    partial_alpha = 0
    for index, pixel in enumerate(pixels):
        old_index = base_pixels[pixel_offset + index]
        if pixel[3] == 0:
            new_index = 0
        else:
            if pixel[3] != 255:
                partial_alpha += 1
            new_index = (
                old_index
                if old_index != 0
                and _same_candidate_color(pixel, palette[old_index])
                else _nearest_candidate_color(pixel[:3], palette, range(1, 256))
            )
            if palette[new_index] != pixel[:3]:
                quantized += 1
        if new_index != old_index:
            changed += 1
        output[pixel_offset + index] = new_index
    return bytes(output), {
        "changed_pixels": changed,
        "quantized_pixels": quantized,
        "partial_alpha_pixels": partial_alpha,
        "crop_offset": pixel_offset,
        "crop_capacity": byte_count,
        "pool_capacity": len(base_pixels),
    }


def _encode_menu_sprite_png(
    base_pixels: bytes,
    png_path: Path,
    width: int,
    height: int,
    bits_per_pixel: int,
    pixel_offset: int,
    byte_count: int,
    palette: list[tuple[int, int, int]],
    restrict_to_source_indices: bool = False,
) -> tuple[bytes, dict[str, int]]:
    """Replace one menu sprite inside its shared linear 4/8bpp pixel pool.

    Some BUP palettes contain duplicate RGB values with different runtime
    meanings. When requested, keep newly encoded pixels inside the source
    sprite's existing index set so an unrelated CLUT entry cannot bypass the
    runtime selection mask.
    """
    with Image.open(png_path) as image:
        if image.size != (width, height):
            raise ValueError(
                f"{png_path.name}: expected {width}x{height}, "
                f"got {image.width}x{image.height}"
            )
        pixels = list(image.convert("RGBA").getdata())
    expected_size = width * height if bits_per_pixel == 8 else (width * height + 1) // 2
    if byte_count != expected_size:
        raise ValueError(f"{png_path.name}: manifest byte count does not match its format")
    if pixel_offset < 0 or pixel_offset + byte_count > len(base_pixels):
        raise ValueError(f"{png_path.name}: sprite data exceeds the menu pixel pool")
    if bits_per_pixel not in (4, 8):
        raise ValueError(f"{png_path.name}: unsupported menu sprite depth {bits_per_pixel}")
    if len(palette) < (16 if bits_per_pixel == 4 else 256):
        raise ValueError(f"{png_path.name}: menu palette is too short")

    output = bytearray(base_pixels)
    changed = 0
    quantized = 0
    partial_alpha = 0
    if bits_per_pixel == 8:
        if restrict_to_source_indices:
            candidates = sorted(
                {
                    value
                    for value in base_pixels[pixel_offset : pixel_offset + byte_count]
                    if value != 0
                }
            )
            if not candidates:
                raise ValueError(f"{png_path.name}: source sprite has no opaque palette indices")
        else:
            candidates = range(1, 256)
        row_duplicate_indices = (
            _duplicate_palette_index_by_row(
                base_pixels,
                pixel_offset,
                width,
                height,
                list(candidates),
                palette,
            )
            if restrict_to_source_indices
            else {}
        )
        for index, pixel in enumerate(pixels):
            old_index = base_pixels[pixel_offset + index]
            if pixel[3] == 0:
                new_index = 0
            else:
                if pixel[3] != 255:
                    partial_alpha += 1
                new_index = (
                    old_index
                    if old_index != 0
                    and _same_candidate_color(pixel, palette[old_index])
                    else _nearest_candidate_color(pixel[:3], palette, candidates)
                )
                color = pixel[:3]
                row = index // width
                if old_index == 0 or not _same_candidate_color(pixel, palette[old_index]):
                    new_index = row_duplicate_indices.get((row, color), new_index)
                if palette[new_index] != pixel[:3]:
                    quantized += 1
            if new_index != old_index:
                changed += 1
            output[pixel_offset + index] = new_index
    else:
        if restrict_to_source_indices:
            candidates = sorted(
                {
                    value
                    for packed in base_pixels[pixel_offset : pixel_offset + byte_count]
                    for value in (packed >> 4, packed & 0x0F)
                    if value not in (0, 15)
                }
            )
            if not candidates:
                raise ValueError(f"{png_path.name}: source sprite has no opaque palette indices")
        else:
            candidates = range(1, 15)
        for index, pixel in enumerate(pixels):
            byte_index = pixel_offset + index // 2
            packed = base_pixels[byte_index]
            old_index = packed >> 4 if index % 2 == 0 else packed & 0x0F
            if pixel[3] == 0:
                new_index = old_index if old_index in (0, 15) else 0
            else:
                if pixel[3] != 255:
                    partial_alpha += 1
                new_index = (
                    old_index
                    if old_index not in (0, 15)
                    and _same_candidate_color(pixel, palette[old_index])
                    else _nearest_candidate_color(pixel[:3], palette, candidates)
                )
                if palette[new_index] != pixel[:3]:
                    quantized += 1
            if new_index != old_index:
                changed += 1
            if index % 2 == 0:
                # Read the untouched nibble from the current output byte. The
                # other pixel in this byte may already have been rewritten.
                output[byte_index] = (new_index << 4) | (output[byte_index] & 0x0F)
            else:
                output[byte_index] = (output[byte_index] & 0xF0) | new_index
    return bytes(output), {
        "changed_pixels": changed,
        "quantized_pixels": quantized,
        "partial_alpha_pixels": partial_alpha,
    }


def _encode_start_ps_png(
    source_raw: bytes,
    png_path: Path,
    palette_raw: bytes,
) -> tuple[bytes, dict[str, int]]:
    """Encode the 144x8 indexed START.PS texture using its source palette."""
    if len(source_raw) != 144 * 8:
        raise ValueError(f"{png_path.name}: START.PS texture must be 144x8")
    palette = _start_ps_palette(palette_raw)
    with Image.open(png_path) as image:
        if image.size != (144, 8):
            raise ValueError(f"{png_path.name}: expected 144x8, got {image.width}x{image.height}")
        pixels = list(image.convert("RGBA").getdata())
    output = bytearray(source_raw)
    changed = 0
    quantized = 0
    partial_alpha = 0
    for index, pixel in enumerate(pixels):
        old_index = source_raw[index]
        if pixel[3] == 0:
            new_index = 0
        else:
            if pixel[3] != 255:
                partial_alpha += 1
            new_index = min(
                range(1, 256),
                key=lambda palette_index: sum(
                    (pixel[channel] - palette[palette_index][channel]) ** 2
                    for channel in range(3)
                ),
            )
            if palette[new_index] != pixel[:3]:
                quantized += 1
        if new_index != old_index:
            changed += 1
        output[index] = new_index
    return bytes(output), {
        "changed_pixels": changed,
        "quantized_pixels": quantized,
        "partial_alpha_pixels": partial_alpha,
    }


def validate_image_edit_consumption(root: Path, reports: list[dict[str, object]]) -> None:
    """Require every edited PNG with an imageori counterpart to be reported."""
    edited_paths = image_edit_paths(root)
    imageori_root = (root / "work" / "imageori").resolve()
    missing_originals = []
    for edited in sorted(edited_paths):
        relative = edited.relative_to((root / "work" / "imageedit").resolve())
        original = imageori_root / relative
        if not original.is_file():
            missing_originals.append(str(edited))
    consumed = {Path(str(report["png"])).resolve() for report in reports}
    unconsumed = sorted(edited_paths - consumed)
    if missing_originals or unconsumed:
        details = []
        if missing_originals:
            details.append("edited PNGs without imageori counterparts: " + ", ".join(missing_originals))
        if unconsumed:
            details.append("edited PNGs not consumed by an image manifest: " + ", ".join(map(str, unconsumed)))
        raise ValueError("; ".join(details))


def apply_image_patches(
    root: Path,
    source_dir: Path,
    pac_replacements: dict[str, dict[int, bytes]],
    standalone_replacements: dict[str, bytes],
    raw_pac_replacements: dict[str, dict[int, bytes]],
    patches: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Apply edited image PNGs on top of any text-layer replacements."""
    reports: list[dict[str, object]] = []
    if patches is None:
        patches = load_image_patches(root) + load_candidate_image_patches(root)
    menu_states: dict[tuple[str, int], dict[str, object]] = {}
    bup_entry02_states: dict[tuple[str, int, int], dict[str, object]] = {}
    for patch in patches:
        file_name = str(patch["file"])
        if patch["kind"] == "menu-sprite":
            top_entry = int(patch["top_entry"])
            state_key = (file_name, top_entry)
            source = source_dir / file_name
            if not source.exists():
                raise FileNotFoundError(f"Image source file is missing: {source}")
            state = menu_states.get(state_key)
            if state is None:
                source_entries = split_pac(source.read_bytes())
                current_top = raw_pac_replacements.get(file_name, {}).get(top_entry)
                if current_top is None:
                    if top_entry >= len(source_entries):
                        raise ValueError(f"{file_name}: menu sprite entry {top_entry} is missing")
                    # The menu layer is a raw nested PAC. Its inner pixel,
                    # definition, and palette entries are compressed, but the
                    # outer entry itself must not go through decode_entry.
                    current_top = source_entries[top_entry]
                nested = split_pac(current_top)
                pixel_entry = int(patch["pixel_entry"])
                definition_entry = int(patch["definition_entry"])
                if max(pixel_entry, definition_entry) >= len(nested):
                    raise ValueError(f"{file_name}: menu sprite inner entry is missing")
                source_pixels = decode_entry(nested[pixel_entry])
                source_definitions = decode_entry(nested[definition_entry])
                source_raw = bytes(patch["source_raw"])
                if source_pixels != source_raw:
                    raise ValueError(
                        f"{file_name}: menu sprite pixel source differs from the manifest"
                    )
                palette_entry = 2
                if palette_entry >= len(nested):
                    raise ValueError(f"{file_name}: menu sprite palette entry is missing")
                palette = _candidate_palette(decode_entry(nested[palette_entry]))
                state = {
                    "nested": list(nested),
                    "pixels": source_pixels,
                    "original_pixels": source_pixels,
                    "definitions": source_definitions,
                    "palette": palette,
                    "pixel_entry": pixel_entry,
                }
                menu_states[state_key] = state

            definitions = bytes(state["definitions"])
            sprite = int(patch["sprite"])
            definition_offset = sprite * 8
            if definition_offset + 8 > len(definitions):
                raise ValueError(f"{file_name}: menu sprite definition {sprite} is missing")
            width = int.from_bytes(definitions[definition_offset : definition_offset + 2], "big")
            height = int.from_bytes(definitions[definition_offset + 2 : definition_offset + 4], "big")
            source_address = int.from_bytes(
                definitions[definition_offset + 4 : definition_offset + 6], "big"
            )
            source_base = min(
                int.from_bytes(definitions[index + 4 : index + 6], "big")
                for index in range(0, len(definitions), 8)
            )
            pixel_offset = (source_address - source_base) * 8
            if width != int(patch["width"]) or height != int(patch["height"]):
                raise ValueError(f"{file_name}: menu sprite definition dimensions changed")
            if pixel_offset != int(patch["pixel_offset"]):
                raise ValueError(f"{file_name}: menu sprite pixel offset changed")
            rebuilt_pixels, stats = _encode_menu_sprite_png(
                bytes(state["pixels"]),
                Path(str(patch["png"])),
                width,
                height,
                int(patch["bits_per_pixel"]),
                pixel_offset,
                int(patch["byte_count"]),
                list(state["palette"]),
            )
            changed = rebuilt_pixels != bytes(state["pixels"])
            state["pixels"] = rebuilt_pixels
            reports.append(
                {
                    "kind": patch["kind"],
                    "file": file_name,
                    "top_entry": top_entry,
                    "inner_entry": int(state["pixel_entry"]),
                    "sprite": sprite,
                    "png": str(patch["png"]),
                    "original_png": str(patch["original_png"]),
                    "edited_input": True,
                    "replacement_applied": True,
                    "changed_from_base": changed,
                    "source_round_trip": rebuilt_pixels == bytes(patch["source_raw"]),
                    "sprite_offset": pixel_offset,
                    **stats,
                }
            )
            continue
        if patch["kind"] == "bup-entry02-sprite":
            entry_number = int(patch["entry"])
            group_number = int(patch["group"])
            segment_number = int(patch["inner_entry"])
            state_key = (file_name, entry_number, group_number)
            source = source_dir / file_name
            if not source.exists():
                raise FileNotFoundError(f"Image source file is missing: {source}")
            state = bup_entry02_states.get(state_key)
            if state is None:
                source_entries = split_pac(source.read_bytes())
                current_top = raw_pac_replacements.get(file_name, {}).get(entry_number)
                if current_top is None:
                    if entry_number >= len(source_entries):
                        raise ValueError(f"{file_name}: BUP entry02 is missing")
                    current_top = decode_entry(source_entries[entry_number])
                groups = split_pac(current_top)
                if group_number >= len(groups):
                    raise ValueError(f"{file_name}: BUP entry02 group {group_number} is missing")
                group_raw = decode_entry(groups[group_number])
                if group_raw != bytes(patch["group_raw"]):
                    raise ValueError(
                        f"{file_name}: BUP entry02 group source differs from the manifest"
                    )
                segments = split_pac(group_raw)
                state = {
                    "groups": list(groups),
                    "segments": list(segments),
                    "original_segments": list(segments),
                    "group_raw": group_raw,
                    "palette": list(patch["palette"]),
                }
                bup_entry02_states[state_key] = state
            segments = list(state["segments"])
            if segment_number >= len(segments):
                raise ValueError(
                    f"{file_name}: BUP entry02 group {group_number} segment {segment_number} is missing"
                )
            source_segment = bytes(segments[segment_number])
            if source_segment != bytes(patch["source_raw"]):
                raise ValueError(
                    f"{file_name}: BUP entry02 segment source differs from the manifest"
                )
            rebuilt_segment, stats = _encode_menu_sprite_png(
                source_segment,
                Path(str(patch["png"])),
                int(patch["width"]),
                int(patch["height"]),
                int(patch["bits_per_pixel"]),
                0,
                int(patch["byte_count"]),
                list(state["palette"]),
                restrict_to_source_indices=True,
            )
            segments[segment_number] = rebuilt_segment
            state["segments"] = segments
            reports.append(
                {
                    "kind": patch["kind"],
                    "file": file_name,
                    "entry": entry_number,
                    "group": group_number,
                    "inner_entry": segment_number,
                    "sprite": int(patch["sprite"]),
                    "png": str(patch["png"]),
                    "original_png": str(patch["original_png"]),
                    "edited_input": True,
                    "replacement_applied": True,
                    "changed_from_base": rebuilt_segment != source_segment,
                    "source_round_trip": rebuilt_segment == bytes(patch["source_raw"]),
                    **stats,
                }
            )
            continue
        entry = patch["entry"]
        png_path = patch["png"]
        source_raw = patch["source_raw"]
        source = source_dir / file_name
        if not source.exists():
            raise FileNotFoundError(f"Image source file is missing: {source}")
        source_data = source.read_bytes()
        if patch["kind"] == "start-ps-texture":
            base_raw = standalone_replacements.get(file_name)
            if base_raw is None:
                base_raw = decode_entry(source_data)
            source_offset = int(patch["source_offset"])
            palette = bytes(patch["source_palette"])
            rebuilt_texture, stats = _encode_start_ps_png(source_raw, png_path, palette)
            if source_offset < 0 or source_offset + len(rebuilt_texture) > len(base_raw):
                raise ValueError(f"{file_name}: PRESS START BUTTON span exceeds START.PS")
            rebuilt = bytearray(base_raw)
            rebuilt[source_offset : source_offset + len(rebuilt_texture)] = rebuilt_texture
            rebuilt_raw = bytes(rebuilt)
            changed = rebuilt_raw != base_raw
            if changed:
                standalone_replacements[file_name] = rebuilt_raw
            reports.append(
                {
                    "kind": patch["kind"],
                    "file": file_name,
                    "entry": entry,
                    "png": str(png_path),
                    "original_png": str(patch["original_png"]),
                    "edited_input": True,
                    "replacement_applied": True,
                    "changed_from_base": changed,
                    "source_round_trip": rebuilt_texture == source_raw,
                    "source_offset": source_offset,
                    **stats,
                }
            )
            continue
        if source.suffix.upper() == ".PAC":
            if isinstance(entry, str) and entry.isdigit():
                entry_number = int(entry)
            elif isinstance(entry, int):
                entry_number = entry
            else:
                raise ValueError(f"{file_name}: PAC image entry is not numeric: {entry}")
            source_entries = split_pac(source_data)
            if entry_number >= len(source_entries):
                raise ValueError(f"{file_name}: image entry {entry_number} is missing")
            replacement_map = pac_replacements.get(file_name)
            base_raw = replacement_map.get(entry_number) if replacement_map is not None else None
            if base_raw is None:
                base_raw = decode_entry(source_entries[entry_number])
            if patch["kind"] == "wb":
                rebuilt_raw, stats = encode_wb_png(source_raw, png_path, base_raw)
            elif patch["kind"] == "bup-hidden-title-tiles":
                rebuilt_raw, stats = encode_8bpp_tile_strip_png(
                    source_raw,
                    png_path,
                    int(patch["first_tile"]),
                    int(patch["tile_count"]),
                    base_raw,
                )
            elif patch["kind"] == "bup-sprite":
                rebuilt_raw, stats = _encode_menu_sprite_png(
                    base_raw,
                    png_path,
                    int(patch["width"]),
                    int(patch["height"]),
                    int(patch["bits_per_pixel"]),
                    int(patch["pixel_offset"]),
                    int(patch["byte_count"]),
                    list(patch["palette"]),
                    restrict_to_source_indices=True,
                )
            else:
                exact_indexed = False
                stable_rand = False
                if entry_number == 1:
                    with Image.open(png_path) as image:
                        exact_indexed = (image.mode == "P" and
                                         (file_name.upper() in {f"RAND{i:02d}.PAC" for i in range(31)}
                                          or image.info.get("wachenroeder-tilemap-layout") == "repacked"))
                        stable_rand = (exact_indexed and file_name.upper() == "RAND00.PAC"
                                       and image.info.get("wachenroeder-tilemap-layout") != "repacked")
                rebuilt_raw, stats = encode_tilemap_png(
                    source_raw,
                    png_path,
                    base_raw,
                    preserve_tile_indices=stable_rand or file_name.upper() in {"TITLEBG.PAC", "BUP_DATA.PAC"},
                    preserve_png_indices=exact_indexed,
                )
            changed = rebuilt_raw != base_raw
            if changed:
                pac_replacements.setdefault(file_name, {})[entry_number] = rebuilt_raw
            reports.append(
                {
                    "kind": patch["kind"],
                    "file": file_name,
                    "entry": entry_number,
                    "png": str(png_path),
                    "original_png": str(patch["original_png"]),
                    "edited_input": bool(patch.get("edited", True)),
                    "source_round_trip": rebuilt_raw == source_raw,
                    "replacement_applied": True,
                    "changed_from_base": changed,
                    **stats,
                }
            )
        elif source.suffix.upper() == ".PS":
            if entry != "raw":
                raise ValueError(f"{file_name}: direct PS image entry is {entry!r}")
            base_raw = standalone_replacements.get(file_name)
            if base_raw is None:
                base_raw = decode_entry(source_data)
            intr_tilemap = file_name.upper().startswith("INTR_")
            rebuilt_raw, stats = encode_tilemap_png(
                source_raw,
                png_path,
                base_raw,
                preserve_tile_indices=intr_tilemap,
                propagate_visible_tiles=intr_tilemap,
                reference_png_path=Path(str(patch["original_png"])) if intr_tilemap else None,
            )
            changed = rebuilt_raw != base_raw
            if changed:
                standalone_replacements[file_name] = rebuilt_raw
            reports.append(
                {
                    "kind": patch["kind"],
                    "file": file_name,
                    "entry": entry,
                    "png": str(png_path),
                    "original_png": str(patch["original_png"]),
                    "edited_input": bool(patch["edited"]),
                    "source_round_trip": rebuilt_raw == source_raw,
                    "replacement_applied": True,
                    "changed_from_base": changed,
                    **stats,
                }
            )
        else:
            raise ValueError(f"Unsupported image source file: {source}")

    for (file_name, top_entry), state in menu_states.items():
        pixels = bytes(state["pixels"])
        if pixels == bytes(state["original_pixels"]):
            continue
        nested = list(state["nested"])
        pixel_entry = int(state["pixel_entry"])
        # These menu pools are decoded by the title-screen loader, which uses
        # the original 5000-byte output-block contract.  Smaller streams are
        # valid for our generic decoder, but 8000-byte blocks overrun that
        # loader's working buffer and make the menu sprites disappear.
        nested[pixel_entry] = encode_entry_best(pixels)
        rebuilt_top = build_pac(nested)
        raw_pac_replacements.setdefault(file_name, {})[top_entry] = rebuilt_top
    for (file_name, top_entry, group_number), state in bup_entry02_states.items():
        segments = list(state["segments"])
        original_segments = list(state["original_segments"])
        if segments == original_segments:
            continue
        groups = list(state["groups"])
        rebuilt_group_raw = build_pac(segments)
        groups[group_number] = encode_entry_best(rebuilt_group_raw)
        rebuilt_outer_raw = build_pac(groups)
        raw_pac_replacements.setdefault(file_name, {})[top_entry] = encode_entry_best(
            rebuilt_outer_raw
        )
    return reports


def _title_palette(source_data: bytes) -> list[tuple[int, int, int]]:
    """Read the 256-color palette used by TITLE.PAC's indexed sprites."""
    top = split_pac(source_data)
    background = decode_entry(split_pac(top[0])[0])
    palette_offset = int.from_bytes(background[8:12], "big")
    return [
        (
            (color & 0x1F) * 255 // 31,
            ((color >> 5) & 0x1F) * 255 // 31,
            ((color >> 10) & 0x1F) * 255 // 31,
        )
        for color in (
            int.from_bytes(background[palette_offset + index * 2 : palette_offset + index * 2 + 2], "big")
            for index in range(256)
        )
    ]


def _nearest_title_color(
    rgb: tuple[int, int, int], palette: list[tuple[int, int, int]]
) -> int:
    return min(
        range(1, len(palette)),
        key=lambda index: sum((rgb[channel] - palette[index][channel]) ** 2 for channel in range(3)),
    )


def load_title_image_patches(root: Path) -> list[dict[str, object]]:
    """Load only title PNGs explicitly copied into work/imageedit."""
    manifest_path = root / "work" / "imageori" / "manifests" / "title-image-manifest.csv"
    if not manifest_path.exists():
        return []
    patches: list[dict[str, object]] = []
    with manifest_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if not row.get("Png") or not row.get("Raw"):
                continue
            original_png = root / Path(row["Png"])
            png_path = resolve_image_input(root, original_png)
            if png_path is None:
                continue
            raw_path = root / Path(row["Raw"])
            if not png_path.exists() or not raw_path.exists():
                raise FileNotFoundError(f"Missing TITLE image input or source: {png_path} / {raw_path}")
            patches.append(
                {
                    "row": row,
                    "png": png_path,
                    "original_png": original_png,
                    "source_raw": raw_path.read_bytes(),
                }
            )
    return patches


def apply_title_image_patches(
    root: Path,
    source_dir: Path,
    raw_pac_replacements: dict[str, dict[int, bytes]],
    patches: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Patch TITLE.PAC nested images only at their manifest-selected location."""
    if patches is None:
        patches = load_title_image_patches(root)
    if not patches:
        return []
    source_path = source_dir / "TITLE.PAC"
    source_data = source_path.read_bytes()
    top = split_pac(source_data)
    nested_replacements: dict[int, dict[int, bytes]] = {}
    palette = _title_palette(source_data)
    reports: list[dict[str, object]] = []

    for patch in patches:
        row = patch["row"]
        top_entry = int(row["TopEntry"])
        inner_entry = int(row["InnerEntry"])
        nested = split_pac(top[top_entry])
        current = nested_replacements.setdefault(top_entry, {}).get(inner_entry)
        if current is None:
            current = decode_entry(nested[inner_entry])
        source_raw = patch["source_raw"]
        png_path = patch["png"]
        kind = row["Kind"]
        if kind == "nested-tilemap":
            rebuilt_raw, stats = encode_tilemap_png(
                source_raw,
                png_path,
                current,
                preserve_tile_indices=True,
            )
        elif kind == "nested-8bpp-sprite":
            if top_entry != 1 or inner_entry != 1:
                raise ValueError("TITLE sprite is outside TITLE.PAC entry01.inner01")
            sprite = int(row["Sprite"])
            definitions = decode_entry(nested[0])
            source_base = min(
                int.from_bytes(definitions[index * 8 + 4 : index * 8 + 6], "big")
                for index in range(len(definitions) // 8)
            )
            width = int(row["Width"])
            height = int(row["Height"])
            source_address = int.from_bytes(
                definitions[sprite * 8 + 4 : sprite * 8 + 6], "big"
            )
            offset = (source_address - source_base) * 8
            with Image.open(png_path) as image:
                if image.size != (width, height):
                    raise ValueError(f"{png_path.name}: expected {width}x{height}, got {image.size}")
                pixels = list(image.convert("RGBA").getdata())
            if offset < 0 or offset + width * height > len(current):
                raise ValueError(f"{png_path.name}: sprite data exceeds TITLE pixel pool")
            rebuilt = bytearray(current)
            changed = 0
            quantized = 0
            partial_alpha = 0
            for index, pixel in enumerate(pixels):
                old_index = current[offset + index]
                if pixel[3] == 0:
                    new_index = 0
                else:
                    if pixel[3] != 255:
                        partial_alpha += 1
                    new_index = _nearest_title_color(pixel[:3], palette)
                    if palette[new_index] != pixel[:3]:
                        quantized += 1
                if new_index != old_index:
                    changed += 1
                rebuilt[offset + index] = new_index
            rebuilt_raw = bytes(rebuilt)
            stats = {
                "changed_pixels": changed,
                "quantized_pixels": quantized,
                "partial_alpha_pixels": partial_alpha,
                "sprite_offset": offset,
            }
        else:
            raise ValueError(f"Unsupported TITLE image kind: {kind}")

        changed = rebuilt_raw != current
        nested_replacements[top_entry][inner_entry] = rebuilt_raw
        reports.append(
            {
                "kind": kind,
                "file": "TITLE.PAC",
                "top_entry": top_entry,
                "inner_entry": inner_entry,
                "sprite": row.get("Sprite", ""),
                "png": str(png_path),
                "original_png": str(patch["original_png"]),
                "edited_input": True,
                "replacement_applied": True,
                "changed_from_base": changed,
                **stats,
            }
        )

    for top_entry, replacements in nested_replacements.items():
        if not replacements:
            continue
        nested = split_pac(top[top_entry])
        rebuilt_nested = list(nested)
        for inner_entry, raw in replacements.items():
            # Keep the title background inside its original compressed span;
            # the title loader is sensitive to growth of this nested layer.
            block_size = 5000
            encoded = encode_entry_sparse(raw, block_size)
            if (top_entry, inner_entry) == (0, 0) and len(encoded) > len(nested[inner_entry]):
                raise ValueError(
                    "TITLE.PAC entry00.inner00 exceeds its original compressed span: "
                    f"{len(encoded)} > {len(nested[inner_entry])} bytes"
                )
            rebuilt_nested[inner_entry] = encoded
        raw_pac_replacements.setdefault("TITLE.PAC", {})[top_entry] = build_pac(rebuilt_nested)
    return reports


def build_one(
    source: Path,
    output: Path,
    replacements: dict[int, bytes],
    raw_replacements: dict[int, bytes] | None = None,
    sparse_entries: set[int] | None = None,
) -> dict[str, object]:
    source_data = source.read_bytes()
    # Every newly written decoded layer follows the Korean reference
    # compressor. Existing entries not listed in replacements remain byte
    # identical, including graphic layers whose structure is unknown.
    sparse_entries = set(replacements) | set(sparse_entries or set())
    sparse_block_sizes = {
        index: compression_block_size(source.name, index)
        for index in sparse_entries
    }
    # The accepted result overlay exceeds SENG01's 18 ISO sectors by 11 bytes
    # with uniform 5000-byte blocks. A 4992-byte first block saves exactly
    # those bytes; subsequent decoded blocks still stay within the 5000-byte
    # runtime-safe limit. No pixels, palette, entry offsets in decoded data,
    # or untouched compressed entries are changed.
    first_block_sizes = {1: 4992} if source.name.upper() == "SENG01.PAC" else {}
    rebuilt, stats = replace_pac_entries(
        source_data,
        replacements,
        sparse_entries=sparse_entries,
        sparse_block_size=3500,
        sparse_block_sizes=sparse_block_sizes,
        sparse_first_block_sizes=first_block_sizes,
    )
    rebuilt_entries = split_pac(rebuilt)
    raw_replacements = raw_replacements or {}
    for index, raw_entry in raw_replacements.items():
        if index >= len(rebuilt_entries):
            raise ValueError(f"{source.name}: raw entry {index} is missing")
        rebuilt_entries[index] = raw_entry
        stats[index]["output_compressed_bytes"] = len(raw_entry)
        stats[index]["raw_nested_container"] = 1
    title_padding = 0
    if source.name == "TITLE.PAC":
        # The title loader reads entry01's nested offset table with SH-2
        # longword loads. Its base must stay four-byte aligned in RAM.
        title_padding = -(len(rebuilt_entries) * 4 + len(rebuilt_entries[0])) % 4
        if title_padding:
            rebuilt_entries[0] += bytes(title_padding)
            stats[0]["output_compressed_bytes"] = len(rebuilt_entries[0])
            stats[0]["alignment_padding_bytes"] = title_padding
    rebuilt = build_pac(rebuilt_entries)
    for index, entry in enumerate(rebuilt_entries):
        expected = replacements.get(index)
        if expected is not None and decode_entry(entry) != expected:
            raise ValueError(f"{source.name}: rebuilt entry {index} failed verification")
        expected_raw = raw_replacements.get(index)
        if expected_raw is not None:
            if source.name == "TITLE.PAC" and index == 0:
                expected_raw += bytes(title_padding)
            if entry != expected_raw:
                raise ValueError(f"{source.name}: raw entry {index} failed verification")
    output.write_bytes(rebuilt)
    return {
        "file": source.name,
        "source_sha256": sha256(source_data),
        "rebuilt_sha256": sha256(rebuilt),
        "source_bytes": len(source_data),
        "rebuilt_bytes": len(rebuilt),
        "entries": stats,
    }


def build_standalone(source: Path, output: Path, raw: bytes) -> dict[str, object]:
    source_data = source.read_bytes()
    # A standalone PS may combine an edited image with a text-layer
    # replacement, so its replacement is intentionally allowed to differ
    # from the original decoded bytes. The compressed output is still
    # verified by decoding it back to the exact replacement.
    rebuilt = encode_entry_sparse(
        raw,
        block_size=compression_block_size(source.name),
    )
    if decode_entry(rebuilt) != raw:
        raise ValueError(f"{source.name}: rebuilt resource failed verification")
    source_sectors = (len(source_data) + 2047) // 2048
    rebuilt_sectors = (len(rebuilt) + 2047) // 2048
    if rebuilt_sectors > source_sectors:
        raise ValueError(
            f"{source.name}: rebuilt resource exceeds its ISO allocation: "
            f"{rebuilt_sectors} sectors > {source_sectors}"
        )
    output.write_bytes(rebuilt)
    return {
        "file": source.name,
        "source_sha256": sha256(source_data),
        "rebuilt_sha256": sha256(rebuilt),
        "source_bytes": len(source_data),
        "rebuilt_bytes": len(rebuilt),
        "entries": [{"entry": -1, "source_decoded_bytes": len(raw), "output_decoded_bytes": len(raw)}],
    }


def build_counted_narrow_fields(
    source_dir: Path,
    output_dir: Path,
    temp_root: Path,
    font_dir: Path,
    existing_replacements: dict[str, dict[int, bytes]],
) -> list[dict[str, object]]:
    """Write confirmed counted 8x16 fields in place."""

    payload = json.loads((temp_root / "imported.json").read_text(encoding="utf-8-sig"))
    records: list[dict[str, object]] = []
    for imported in payload["records"]:
        if imported["file"] not in {"PSEL.PAC", "STDAT.PAC", "SHOPDATA.PAC"} or not imported["target_text"]:
            continue
        # The catalog carries structural fields omitted from imported.json.
        catalog_path = source_dir.parent.parent / "work" / "text" / "catalog" / "japan" / f"{imported['file']}.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        source_record = next(item for item in catalog["records"] if item["id"] == imported["id"])
        if source_record["kind"] != "counted-narrow-u16":
            continue
        record = dict(imported)
        record.update(source_record)
        records.append(record)
    if not records:
        return []

    narrow_map = code_map(font_dir)

    by_file: dict[str, list[dict[str, object]]] = {}
    for record in records:
        by_file.setdefault(str(record["file"]), []).append(record)

    manifest: list[dict[str, object]] = []
    for file_name, file_records in sorted(by_file.items()):
        source = source_dir / file_name
        source_data = source.read_bytes()
        if sha256(source_data) != file_records[0]["source_sha256"]:
            raise ValueError(f"{file_name}: source file hash differs from catalog")
        entries = split_pac(source_data)
        # Event/text insertion may already have produced a replacement for
        # this same PAC. Start from those decoded layers so the counted-field
        # pass cannot overwrite earlier translation work with the raw Japan
        # entry.
        replacements: dict[int, bytes] = dict(existing_replacements.get(file_name, {}))
        field_manifest: list[dict[str, object]] = []
        for entry_number, entry_records in sorted(
            _group_records_by_entry(file_records).items()
        ):
            source_raw = decode_entry(entries[entry_number])
            source_hash = sha256(source_raw)
            for record in entry_records:
                if source_hash != record["decoded_sha256"]:
                    raise ValueError(f"{file_name}:{record['id']} decoded layer hash differs")
            raw = bytearray(replacements.get(entry_number, source_raw))
            for record in entry_records:
                offset = int(record["decoded_offset"])
                count = int(record["code_units"])
                field_offset = int(record["counted_field_offset"])
                stored_count = int.from_bytes(raw[field_offset : field_offset + 2], "big")
                if stored_count != count:
                    raise ValueError(f"{file_name}:{record['id']} narrow field count differs")
                old = [
                    int.from_bytes(raw[offset + index : offset + index + 2], "big")
                    for index in range(0, count * 2, 2)
                ]
                expected = [int(value, 16) for value in str(record["source_codes"]).split()]
                if old != expected:
                    raise ValueError(f"{file_name}:{record['id']} source narrow codes changed")
                target = str(record["target_text"])
                if len(target) > count:
                    raise ValueError(f"{file_name}:{record['id']} exceeds narrow capacity {count}")
                try:
                    encoded = [narrow_map[character] for character in target]
                except KeyError as error:
                    raise ValueError(
                        f"{file_name}:{record['id']} has no narrow glyph for {error.args[0]!r}"
                    ) from error
                encoded.extend([0x8A] * (count - len(encoded)))
                for index, code in enumerate(encoded):
                    raw[offset + index * 2 : offset + index * 2 + 2] = code.to_bytes(2, "big")
                field_manifest.append(
                    {
                        "file": file_name,
                        "entry": entry_number,
                        "id": record["id"],
                        "offset": offset,
                        "code_units": count,
                        "translation": target,
                        "type": "counted_narrow_u16",
                    }
                )
            replacements[entry_number] = bytes(raw)
        rebuilt = build_one(
            source,
            output_dir / file_name,
            replacements,
        )
        manifest.append(
            {
                "file": file_name,
                **rebuilt,
                "type": "counted_narrow_u16_container",
                "fields": field_manifest,
            }
        )
    return manifest


def _group_records_by_entry(records: list[dict[str, object]]) -> dict[int, list[dict[str, object]]]:
    grouped: dict[int, list[dict[str, object]]] = {}
    for record in records:
        grouped.setdefault(int(record["entry"]), []).append(record)
    return grouped


def build_runtime_font_patch(
    root: Path,
    output_dir: Path,
    font_dir: Path,
) -> dict[str, object]:
    """Patch the packed-font writers while preserving the boot upload range."""

    source = root / "unpacked" / "japan" / RUNTIME_EXECUTABLE
    source_data = source.read_bytes()
    data = bytearray(source_data)
    menu_changes = patch_menu(root, data, font_dir)
    battle_changes = patch_battle(root, data, font_dir, source_data)
    weather_changes = patch_weather_table(root, data, font_dir, source_data)
    data = bytearray(PACKED_RUNTIME["patch_executable"](bytes(data)))
    offset = RUNTIME_SMALL_FONT_LIMIT_OFFSET
    original_limit = int.from_bytes(data[offset : offset + 2], "big")
    if original_limit != RUNTIME_SMALL_FONT_LIMIT_ORIGINAL:
        raise ValueError(
            f"{RUNTIME_EXECUTABLE}: unexpected small-font upload limit "
            f"0x{original_limit:04X} at 0x{offset:05X}"
        )
    small_font = (font_dir / "ASCII.PAC.entry01.bin").read_bytes()
    if len(small_font) != 0x1100:
        raise ValueError("Packed small font must exactly fill the original 4352-byte upload")
    last_byte_offset = len(small_font) - 1
    if last_byte_offset > 0xFFFF:
        raise ValueError(
            f"Small font is too large for the runtime byte limit: 0x{last_byte_offset:X}"
        )
    data[offset : offset + 2] = last_byte_offset.to_bytes(2, "big")
    output = output_dir / RUNTIME_EXECUTABLE
    output.write_bytes(data)
    return {
        "file": RUNTIME_EXECUTABLE,
        "type": "runtime-packed-small-font",
        "source_sha256": sha256(source_data),
        "rebuilt_sha256": sha256(bytes(data)),
        "source_bytes": len(source_data),
        "rebuilt_bytes": len(data),
        "patches": menu_changes + battle_changes + weather_changes + [
            {
                "offset": offset,
                "length": 2,
                "old": f"0x{original_limit:04X}",
                "new": f"0x{last_byte_offset:04X}",
                "small_font_byte_count": len(small_font),
                "small_font_tile_count": len(small_font) // 8,
            }
        ] + [
            {"offset": PACKED_RUNTIME["offset"](entry), "length": 6,
             "type": "packed-font-hook"}
            for entry in (PACKED_RUNTIME["ENTRY"], PACKED_RUNTIME["WORD_ENTRY"],
                          PACKED_RUNTIME["DOUBLE_ENTRY"], PACKED_RUNTIME["MENU_ENTRY"],
                          PACKED_RUNTIME["NAME_ENTRY"], PACKED_RUNTIME["BYTE_ENTRY"],
                          PACKED_RUNTIME["MENU_UNSELECTED_ENTRY"])
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--font-mode", choices=("8x8", "8x12"), default="8x8")
    args = parser.parse_args()
    root = args.root.resolve()
    source_dir = root / "unpacked" / "japan"
    font_dir = root / "work" / "fonts" / "chinese"
    font_mapping = json.loads((font_dir / "mapping.json").read_text(encoding="utf-8"))
    if font_mapping.get("font_mode", "8x8") != args.font_mode:
        raise ValueError("Font mapping mode differs; rerun build-chinese-fonts.py with --font-mode " + args.font_mode)
    assert_byte_capacity(root, font_dir)
    small_font_path = root / "work/fonts/chinese/ASCII.PAC.entry01.bin"
    if args.font_mode == "8x8" and small_font_path.exists() and small_font_path.stat().st_size > 6144:
        raise ValueError("Small font output exceeds its 6144-byte VRAM allocation; rebuild it first")
    build_root = args.build_root.resolve() if args.build_root else root / "work" / "build"
    temp_root = args.temp_root.resolve() if args.temp_root else root / "temp"
    validate_translation_inputs(root, temp_root)
    event_raw_dir = temp_root / "repacked" / "japan"
    output_dir = build_root / "repacked" / "pac"
    output_dir.mkdir(parents=True, exist_ok=True)
    for existing in output_dir.iterdir():
        if existing.name in {"manifest.json", RUNTIME_EXECUTABLE} or existing.suffix.upper() in {".PAC", ".PS"}:
            existing.unlink()

    ascii_path = source_dir / "ASCII.PAC"
    # 8x12 resources use the shared encoding; its font and executable are
    # emitted together by the resident builder after this resource-only pass.
    jobs: list[tuple[Path, dict[int, bytes]]] = [] if args.font_mode == "8x12" else [
        (
            ascii_path,
            {
                0: PACKED_RUNTIME["patch_main_font"](
                    (font_dir / "ASCII.PAC.entry00.bin").read_bytes(),
                    (font_dir / "tile-deltas.bin").read_bytes(),
                ),
                1: (font_dir / "ASCII.PAC.entry01.bin").read_bytes(),
            },
        )
    ]
    pac_replacements: dict[str, dict[int, bytes]] = {}
    raw_pac_replacements: dict[str, dict[int, bytes]] = {}
    standalone_replacements: dict[str, bytes] = {}
    for raw_path in sorted(event_raw_dir.glob("*.bin")):
        if raw_path.name == "manifest.csv":
            continue
        if ".entry" in raw_path.name:
            stem, suffix = raw_path.name.rsplit(".entry", 1)
            entry = int(suffix[:-4])
            raw = raw_path.read_bytes()
            if stem == "MES_TLE.PS" and entry == -1:
                standalone_replacements[stem] = raw
            else:
                pac_replacements.setdefault(stem, {})[entry] = raw
        elif raw_path.name.endswith(".raw.bin"):
            standalone_replacements[raw_path.name[:-8]] = raw_path.read_bytes()

    print("Checking edited images and preparing resource replacements...", flush=True)
    image_reports = apply_image_patches(
        root,
        source_dir,
        pac_replacements,
        standalone_replacements,
        raw_pac_replacements,
    )
    image_reports.extend(
        apply_title_image_patches(root, source_dir, raw_pac_replacements)
    )
    validate_image_edit_consumption(root, image_reports)
    print(f"Verified {len(image_reports)} image records; preparing shared text tables...", flush=True)
    narrow_byte_manifest = patch_shared(root, pac_replacements, font_dir)
    image_job_files = sorted(set(pac_replacements) | set(raw_pac_replacements))
    for file_name in image_job_files:
        replacements = pac_replacements.get(file_name, {})
        source = source_dir / file_name
        # MM*.PS uses the same PAC offset-table container as *.PAC. Keep its
        # extension, but route entry replacements through the verified PAC
        # codec as well.
        if source.exists() and (
            source.suffix.upper() == ".PAC"
            or source.name.startswith("MM") and source.suffix.upper() == ".PS"
        ):
            jobs.append((source, replacements))
    image_sparse_entries: dict[str, set[int]] = {}
    for report in image_reports:
        if not report.get("changed_from_base", False):
            continue
        kind = str(report["kind"])
        file_name = str(report["file"])
        entry = report.get("entry")
        if kind in {"tilemap", "wb", "bup-sprite", "bup-8bpp-crop", "bup-hidden-title-tiles"} and isinstance(entry, int):
            image_sparse_entries.setdefault(file_name, set()).add(entry)

    standalone_jobs = [
        (source_dir / file_name, raw)
        for file_name, raw in sorted(standalone_replacements.items())
        if (source_dir / file_name).exists()
    ]

    manifest = []
    for job_index, (source, replacements) in enumerate(jobs, 1):
        print(f"[{job_index}/{len(jobs)}] Building and verifying {source.name}", flush=True)
        manifest.append(
            build_one(
                source,
                output_dir / source.name,
                replacements,
                raw_pac_replacements.get(source.name),
                image_sparse_entries.get(source.name),
            )
        )
    narrow_byte_by_file = {row["file"]: row for row in narrow_byte_manifest}
    for row in manifest:
        if row["file"] in narrow_byte_by_file:
            row["narrow_byte_text"] = narrow_byte_by_file[row["file"]]
    for source, raw in standalone_jobs:
        manifest.append(build_standalone(source, output_dir / source.name, raw))
    counted_manifest = build_counted_narrow_fields(
        source_dir,
        output_dir,
        temp_root,
        font_dir,
        pac_replacements,
    )
    counted_files = {str(record["file"]) for record in counted_manifest}
    # The counted pass produces the final merged container for these files;
    # discard the earlier per-layer report so the manifest has one record per
    # output file and cannot suggest that an intermediate build is final.
    manifest = [record for record in manifest if str(record["file"]) not in counted_files]
    manifest.extend(counted_manifest)
    if args.font_mode == "8x8":
        manifest.append(build_runtime_font_patch(root, output_dir, font_dir))
    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "format": "wachenroeder-pac-rebuild-v1",
                "codec": "wachenroeder-dictionary-v1",
                "font_mode": args.font_mode,
                "runtime_font_pending": args.font_mode == "8x12",
                "files": manifest,
                "images": image_reports,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Built and verified {len(manifest)} PAC files in {output_dir}")


if __name__ == "__main__":
    main()
