#!/usr/bin/env python3
"""Export image-like resources that are outside the fixed 320x224 pipeline.

The Korean reference patch edits several resources in place without publishing
PNG dumps. This exporter makes those resources inspectable while keeping the
source location in a manifest. Edited PNGs are consumed by the packer only
when their matching imageori record has a verified writeback mapping.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

from PIL import Image

from image_codec import probe_tilemap, render_8bpp_tile_strip
from pac_codec import decode_entry, split_pac


def be16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def bgr555_palette(raw: bytes) -> list[tuple[int, int, int, int]]:
    if len(raw) < 512:
        return []
    colors = []
    for offset in range(0, 512, 2):
        value = be16(raw, offset)
        colors.append(
            (
                (value & 0x1F) * 255 // 31,
                ((value >> 5) & 0x1F) * 255 // 31,
                ((value >> 10) & 0x1F) * 255 // 31,
                255,
            )
        )
    return colors


def grayscale_palette(size: int = 256) -> list[tuple[int, int, int, int]]:
    return [(i, i, i, 255) for i in range(size)]


def render_4bpp(raw: bytes, width: int, height: int, palette: list[tuple[int, int, int, int]]) -> Image.Image:
    expected = (width * height + 1) // 2
    if width <= 0 or height <= 0 or len(raw) < expected:
        raise ValueError("invalid 4bpp sprite dimensions")
    palette = palette or grayscale_palette(16)
    image = Image.new("RGBA", (width, height))
    pixels = image.load()
    for index in range(width * height):
        packed = raw[index // 2]
        color_index = packed >> 4 if index % 2 == 0 else packed & 0x0F
        color = palette[color_index % len(palette)]
        # These menu 4bpp blocks use either 0 or F as the background key.
        pixels[index % width, index // width] = (
            *color[:3],
            0 if color_index in (0, 15) else 255,
        )
    return image


def render_8bpp(raw: bytes, width: int, height: int, palette: list[tuple[int, int, int, int]]) -> Image.Image:
    expected = width * height
    if width <= 0 or height <= 0 or len(raw) < expected:
        raise ValueError("invalid 8bpp sprite dimensions")
    palette = palette or grayscale_palette(256)
    image = Image.new("RGBA", (width, height))
    pixels = image.load()
    for index, color_index in enumerate(raw[:expected]):
        color = palette[color_index % len(palette)]
        pixels[index % width, index // width] = (*color[:3], 0 if color_index == 0 else 255)
    return image


def load_press_start_button_source(
    source_root: Path,
) -> tuple[bytes, bytes, bytes, int, int]:
    """Load PRESS START BUTTON from its compressed START.PS source.

    START.PS is a direct dictionary-compressed resource.  Its decoded payload
    stores the 256-color palette at 0x0008 and the 144x8 indexed texture at
    0x0208.  The runtime VDP1 snapshot is useful for verification only and is
    deliberately not an extraction input.
    """
    source_path = source_root / "START.PS"
    compressed = source_path.read_bytes()
    decoded = decode_entry(compressed)
    palette_offset = 0x0008
    texture_offset = 0x0208
    texture_size = 144 * 8
    palette = decoded[palette_offset : palette_offset + 512]
    texture = decoded[texture_offset : texture_offset + texture_size]
    if len(palette) != 512 or len(texture) != texture_size:
        raise ValueError("START.PS does not contain the complete PRESS START BUTTON source")
    if set(texture) - {0, 1, 224, 225, 226, 227, 228, 229}:
        raise ValueError("unexpected palette indices in START.PS PRESS START BUTTON texture")
    return decoded, texture, palette, texture_offset, len(compressed)


def decode_nested_pair(top_entries: list[bytes], top_entry: int) -> tuple[bytes, bytes, bytes] | None:
    if top_entry >= len(top_entries):
        return None
    try:
        nested = split_pac(top_entries[top_entry])
        if len(nested) < 2:
            return None
        pixels = decode_entry(nested[0])
        definitions = decode_entry(nested[1])
        palette = decode_entry(nested[2]) if len(nested) > 2 else b""
    except (ValueError, IndexError):
        return None
    if not definitions or len(definitions) % 8:
        return None
    return pixels, definitions, palette


def definition_records(pixels: bytes, definitions: bytes) -> list[dict[str, int]] | None:
    sources = [be16(definitions, index + 4) for index in range(0, len(definitions), 8)]
    if any(next_source <= source for source, next_source in zip(sources, sources[1:])):
        return None
    source_base = min(sources)
    records: list[dict[str, int]] = []
    for offset in range(0, len(definitions), 8):
        record_index = offset // 8
        width = be16(definitions, offset)
        height = be16(definitions, offset + 2)
        source = be16(definitions, offset + 4)
        stored_size = be16(definitions, offset + 6)
        pixel_offset = (source - source_base) * 8
        if not (0 < width <= 320 and 0 < height <= 224):
            return None
        next_offset = (
            (sources[record_index + 1] - source_base) * 8
            if record_index + 1 < len(sources)
            else len(pixels)
        )
        available = next_offset - pixel_offset
        packed_bytes = (width * height + 1) // 2
        linear_bytes = width * height
        if available >= linear_bytes:
            bits_per_pixel = 8
            byte_count = linear_bytes
        elif available >= packed_bytes:
            bits_per_pixel = 4
            byte_count = packed_bytes
        else:
            return None
        if pixel_offset < 0 or pixel_offset + byte_count > len(pixels):
            return None
        records.append(
            {
                "width": width,
                "height": height,
                "source": source,
                "stored_size": stored_size,
                "pixel_offset": pixel_offset,
                "bits_per_pixel": bits_per_pixel,
                "byte_count": byte_count,
            }
        )
    return records


def bup_definition_records(pixels: bytes, definitions: bytes) -> list[dict[str, int]]:
    """BUP entry01 stores five contiguous 8bpp images despite a VRAM address gap."""
    if not definitions or len(definitions) % 8:
        raise ValueError("BUP_DATA.PAC entry00 has an invalid definition table")
    records = []
    pixel_offset = 0
    previous_source_end = 0
    for offset in range(0, len(definitions), 8):
        width = be16(definitions, offset)
        height = be16(definitions, offset + 2)
        source = be16(definitions, offset + 4)
        stored_size = be16(definitions, offset + 6)
        byte_count = width * height
        if not (0 < width <= 320 and 0 < height <= 224):
            raise ValueError("BUP_DATA.PAC entry00 has invalid sprite dimensions")
        if source * 8 < previous_source_end or pixel_offset + byte_count > len(pixels):
            raise ValueError("BUP_DATA.PAC entry01 sprite layout exceeds its source")
        records.append({
            "width": width,
            "height": height,
            "source": source,
            "stored_size": stored_size,
            "pixel_offset": pixel_offset,
            "bits_per_pixel": 8,
            "byte_count": byte_count,
        })
        pixel_offset += byte_count
        previous_source_end = source * 8 + byte_count
    if pixel_offset != len(pixels):
        raise ValueError("BUP_DATA.PAC entry01 has unassigned sprite pixels")
    return records


def write_raw(raw_root: Path, name: str, data: bytes) -> str:
    path = raw_root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path.relative_to(raw_root.parents[3])).replace("\\", "/")


def workspace_relative(source_root: Path, path: Path) -> str:
    return str(path.relative_to(source_root.parents[1])).replace("\\", "/")


def add_record(records: list[dict[str, object]], **values: object) -> None:
    records.append(values)


def export_definition_table(
    source_root: Path,
    image_root: Path,
    raw_root: Path,
    records: list[dict[str, object]],
    path: Path,
    top_entry: int,
    category: str,
) -> int:
    try:
        top = split_pac(path.read_bytes())
    except ValueError:
        top = [path.read_bytes()]
    decoded = decode_nested_pair(top, top_entry)
    if decoded is None:
        return 0
    pixels, definitions, palette_raw = decoded
    sprite_records = definition_records(pixels, definitions)
    if sprite_records is None:
        return 0
    palette = bgr555_palette(palette_raw)
    relative = path.relative_to(source_root).as_posix()
    out_dir = image_root / category / path.name
    raw_pixel = write_raw(raw_root, f"{category}/{path.stem}.top{top_entry:02d}.pixels.bin", pixels)
    raw_defs = write_raw(raw_root, f"{category}/{path.stem}.top{top_entry:02d}.definitions.bin", definitions)
    raw_palette = ""
    if palette_raw:
        raw_palette = write_raw(
            raw_root, f"{category}/{path.stem}.top{top_entry:02d}.palette.bin", palette_raw
        )
    count = 0
    for sprite, item in enumerate(sprite_records):
        start = item["pixel_offset"]
        size = item["byte_count"]
        png_path = out_dir / f"{path.name}.top{top_entry:02d}.sprite{sprite:03d}.png"
        png_path.parent.mkdir(parents=True, exist_ok=True)
        render = render_8bpp if item["bits_per_pixel"] == 8 else render_4bpp
        render(pixels[start : start + size], item["width"], item["height"], palette).save(png_path)
        add_record(
            records,
            kind=category,
            source=relative,
            top_entry=top_entry,
            pixel_entry=0,
            inner_entry=1,
            sprite=sprite,
            width=item["width"],
            height=item["height"],
            bits_per_pixel=item["bits_per_pixel"],
            byte_count=item["byte_count"],
            pixel_offset=start,
            source_address=f"0x{item['source']:04X}",
            stored_size=item["stored_size"],
            decoded_pixel_bytes=len(pixels),
            decoded_definition_bytes=len(definitions),
            png=workspace_relative(source_root, png_path),
            raw_pixels=raw_pixel,
            raw_definitions=raw_defs,
            palette=raw_palette,
            source_sha256=sha256(pixels),
        )
        count += 1
    return count


def export_title_glyph_strips(
    source_root: Path,
    image_root: Path,
    raw_root: Path,
    records: list[dict[str, object]],
) -> int:
    """Export the source-backed ``PRESS START BUTTON`` prompt."""
    decoded, texture, palette_raw, texture_offset, compressed_size = load_press_start_button_source(source_root)
    palette = bgr555_palette(palette_raw)

    out_dir = image_root / "title-prompts" / "START.PS"
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_pixels = write_raw(raw_root, "title-glyph-strips/press-start-button.texture.bin", texture)
    raw_palette = write_raw(raw_root, "title-glyph-strips/press-start-button.palette.bin", palette_raw)
    project_root = source_root.parents[1]
    source_path = project_root / "work" / "extracted" / "japan" / "graphics-decoded" / "START.PS.raw.bin"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(decoded)
    raw_source = str(source_path.relative_to(project_root)).replace("\\", "/")
    png_path = out_dir / "START.PS.press-start-button.png"
    render_8bpp(texture, 144, 8, palette).save(png_path)
    add_record(
        records,
        kind="title-press-start-button",
        source="START.PS",
        top_entry="",
        inner_entry="",
        sprite="",
        width=144,
        height=8,
        bits_per_pixel=8,
        byte_count=len(texture),
        pixel_offset=0,
        source_address=f"0x{texture_offset:04X}",
        stored_size=len(texture),
        decoded_pixel_bytes=len(texture),
        decoded_definition_bytes=0,
        png=workspace_relative(source_root, png_path),
        raw_pixels=raw_pixels,
        raw_definitions="",
        palette=raw_palette,
        source_sha256=sha256(texture),
        source_raw=raw_source,
        source_raw_offset=texture_offset,
        source_raw_size=len(texture),
        source_compressed_size=compressed_size,
    )
    return 1


def export_bup_crops(source_root: Path, image_root: Path, raw_root: Path, records: list[dict[str, object]]) -> int:
    path = source_root / "BUP_DATA.PAC"
    top = split_pac(path.read_bytes())
    definitions = decode_entry(top[0])
    raw = decode_entry(top[1])
    palette_source = decode_entry(top[3])
    palette_offset = int.from_bytes(palette_source[8:12], "big")
    palette_raw = palette_source[palette_offset : palette_offset + 512]
    if len(palette_raw) != 512:
        raise ValueError("BUP_DATA.PAC entry03 does not contain a complete palette")
    sprite_records = bup_definition_records(raw, definitions)
    palette = bgr555_palette(palette_raw)
    raw_path = write_raw(raw_root, "bup-ui/BUP_DATA.PAC.entry01.bin", raw)
    definitions_path = write_raw(raw_root, "bup-ui/BUP_DATA.PAC.entry00.definitions.bin", definitions)
    palette_path = write_raw(raw_root, "bup-ui/BUP_DATA.PAC.entry03.palette.bin", palette_raw)
    out_dir = image_root / "bup-sprites" / path.name
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for sprite, item in enumerate(sprite_records):
        offset = item["pixel_offset"]
        width = item["width"]
        height = item["height"]
        bits_per_pixel = item["bits_per_pixel"]
        byte_count = item["byte_count"]
        name = f"sprite{sprite:03d}"
        png_path = out_dir / f"BUP_DATA.PAC.entry01.{name}.png"
        render = render_8bpp if bits_per_pixel == 8 else render_4bpp
        render(raw[offset : offset + byte_count], width, height, palette).save(png_path)
        add_record(
            records,
            kind="bup-sprites",
            source="BUP_DATA.PAC",
            top_entry=1,
            inner_entry=0,
            sprite=sprite,
            bits_per_pixel=bits_per_pixel,
            byte_count=byte_count,
            width=width,
            height=height,
            pixel_offset=offset,
            source_address=f"0x{item['source']:04X}",
            stored_size=item["stored_size"],
            decoded_pixel_bytes=len(raw),
            decoded_definition_bytes=len(definitions),
            png=workspace_relative(source_root, png_path),
            raw_pixels=raw_path,
            raw_definitions=definitions_path,
            palette=palette_path,
            palette_source="BUP_DATA.PAC entry03",
            source_sha256=sha256(raw),
        )
        count += 1
    return count


def export_bup_hidden_title(source_root: Path, image_root: Path, raw_root: Path,
                            records: list[dict[str, object]]) -> int:
    top = split_pac((source_root / "BUP_DATA.PAC").read_bytes())
    raw = decode_entry(top[3])
    info = probe_tilemap(raw)
    first_tile = 96
    tile_count = 22
    if (info.pixel_offset, info.map_offset, info.palette_offset,
            info.bits_per_pixel, info.tile_count) != (0x820, 0x20, 0x7E20, 8, 118):
        raise ValueError("BUP_DATA.PAC entry03 background layout changed")
    png_path = image_root / "bup-hidden-title-tiles/BUP_DATA.PAC/BUP_DATA.PAC.entry03.hidden-title-tiles.png"
    png_path.parent.mkdir(parents=True, exist_ok=True)
    strip = render_8bpp_tile_strip(raw, first_tile, tile_count)
    strip.save(png_path)
    active_words = (0xC0, 0xC2, 0xC4, 0xDA, 0xDC, 0xDE, 0xE0, 0xE4)
    preview = Image.new("RGB", (len(active_words) * 16, 16))
    for index, word in enumerate(active_words):
        tile = word // 2 - first_tile
        preview.paste(strip.crop((tile * 16, 0, (tile + 1) * 16, 16)), (index * 16, 0))
    preview_path = image_root / "previews/references/BUP_DATA.PAC/BUP_DATA.PAC.entry03.header-load-internal.preview.png"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview.save(preview_path)
    raw_path = write_raw(raw_root, "bup-ui/BUP_DATA.PAC.entry03.bin", raw)
    add_record(
        records,
        kind="bup-hidden-title-tiles",
        source="BUP_DATA.PAC",
        top_entry=3,
        bits_per_pixel=8,
        width=tile_count * 16,
        height=16,
        first_tile=first_tile,
        tile_count=tile_count,
        pixel_offset=info.pixel_offset + first_tile * 256,
        byte_count=tile_count * 256,
        map_offset=info.map_offset,
        palette_offset=info.palette_offset,
        png=workspace_relative(source_root, png_path),
        runtime_preview=workspace_relative(source_root, preview_path),
        runtime_map_words=" ".join(f"{word:04X}" for word in active_words),
        raw_pixels=raw_path,
        source_sha256=sha256(raw),
    )
    return 1


def _bup_entry02_dimensions(segment: bytes) -> tuple[int, int]:
    """Return the verified dimensions for one BUP entry02 texture segment."""
    dimensions = {
        64 * 64: (64, 64),
        40 * 28: (40, 28),
    }
    try:
        return dimensions[len(segment)]
    except KeyError as error:
        raise ValueError(
            f"BUP_DATA.PAC entry02 has an unsupported texture segment size: {len(segment)}"
        ) from error


def export_bup_entry02_sprites(
    source_root: Path,
    image_root: Path,
    raw_root: Path,
    records: list[dict[str, object]],
) -> int:
    """Export the source-backed save/load UI textures in BUP entry02.

    Entry02 is a compressed PAC containing two compressed groups. Each group
    decodes to another PAC whose offset table points directly at one 64x64
    texture followed by 40x28 textures. The nested segments are raw indexed
    pixels, not another emulator/VRAM snapshot.
    """
    path = source_root / "BUP_DATA.PAC"
    top = split_pac(path.read_bytes())
    if len(top) <= 3:
        raise ValueError("BUP_DATA.PAC is missing entry02 or its palette entry")
    outer_raw = decode_entry(top[2])
    groups = split_pac(outer_raw)

    palette_source = decode_entry(top[3])
    palette_offset = int.from_bytes(palette_source[8:12], "big")
    palette_raw = palette_source[palette_offset : palette_offset + 512]
    if len(palette_raw) != 512:
        raise ValueError("BUP_DATA.PAC entry03 does not contain a complete palette")
    palette = bgr555_palette(palette_raw)
    palette_path = write_raw(raw_root, "bup-save-load-ui/BUP_DATA.PAC.entry03.palette.bin", palette_raw)

    out_dir = image_root / "bup-save-load-ui" / path.name
    out_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for group_index, compressed_group in enumerate(groups):
        group_raw = decode_entry(compressed_group)
        segments = split_pac(group_raw)
        group_raw_path = write_raw(
            raw_root,
            f"bup-save-load-ui/BUP_DATA.PAC.entry02.group{group_index:02d}.bin",
            group_raw,
        )
        segment_offsets = [
            int.from_bytes(group_raw[index : index + 4], "big")
            for index in range(0, len(segments) * 4, 4)
        ]
        if segment_offsets[0] != len(segments) * 4:
            raise ValueError(f"BUP_DATA.PAC entry02 group{group_index:02d} has an invalid offset table")

        for segment_index, segment in enumerate(segments):
            width, height = _bup_entry02_dimensions(segment)
            raw_segment_path = write_raw(
                raw_root,
                f"bup-save-load-ui/BUP_DATA.PAC.entry02.group{group_index:02d}.segment{segment_index:02d}.bin",
                segment,
            )
            png_path = out_dir / (
                f"BUP_DATA.PAC.entry02.group{group_index:02d}.segment{segment_index:02d}.png"
            )
            render_8bpp(segment, width, height, palette).save(png_path)
            add_record(
                records,
                kind="bup-entry02-sprites",
                source="BUP_DATA.PAC",
                top_entry=2,
                group=group_index,
                inner_entry=segment_index,
                sprite=segment_index,
                width=width,
                height=height,
                bits_per_pixel=8,
                byte_count=len(segment),
                pixel_offset=0,
                source_address=f"0x{segment_offsets[segment_index]:08X}",
                stored_size=len(segment),
                decoded_pixel_bytes=len(segment),
                decoded_definition_bytes=0,
                png=workspace_relative(source_root, png_path),
                raw_pixels=raw_segment_path,
                raw_group=group_raw_path,
                palette=palette_path,
                palette_source="BUP_DATA.PAC entry03",
                source_sha256=sha256(segment),
                group_sha256=sha256(group_raw),
            )
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    source_root = root / "unpacked" / "japan"
    image_root = root / "work" / "imageori"
    raw_root = root / "work" / "extracted" / "japan" / "image-candidates"
    image_root.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []

    menu_count = 0
    for path in sorted(source_root.glob("MM*.PS")):
        menu_count += export_definition_table(source_root, image_root, raw_root, records, path, 1, "menu-sprites")
    for path in sorted(source_root.glob("MEM*.PAC")):
        menu_count += export_definition_table(source_root, image_root, raw_root, records, path, 1, "menu-sprites")

    title_count = export_title_glyph_strips(source_root, image_root, raw_root, records)
    bup_count = export_bup_crops(source_root, image_root, raw_root, records)
    bup_hidden_count = export_bup_hidden_title(source_root, image_root, raw_root, records)
    bup_entry02_count = export_bup_entry02_sprites(source_root, image_root, raw_root, records)

    # Detect the same nested sprite-table shape in other resources.  This is
    # intentionally reported as a candidate and does not change the packer.
    detected = 0
    known = {(r["source"], r["top_entry"]) for r in records if r["kind"] == "menu-sprites"}
    for path in sorted(source_root.iterdir()):
        if path.suffix.upper() not in {".PAC", ".PS"} or path.name.startswith(("MM", "MEM")):
            continue
        try:
            top = split_pac(path.read_bytes())
        except ValueError:
            top = [path.read_bytes()]
        for top_entry in range(len(top)):
            key = (path.name, top_entry)
            if key in known:
                continue
            decoded = decode_nested_pair(top, top_entry)
            if decoded is None:
                continue
            pixels, definitions, palette_raw = decoded
            items = definition_records(pixels, definitions)
            if items is None or len(items) < 4:
                continue
            detected += export_definition_table(source_root, image_root, raw_root, records, path, top_entry, "detected-sprites")
            known.add(key)

    manifest_dir = image_root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest_dir / "image-candidate-manifest.csv"
    fields = sorted({key for record in records for key in record})
    with manifest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    print(
        f"menu sprites: {menu_count}; title VDP2 components: {title_count}; "
        f"BUP crops: {bup_count}; BUP hidden title: {bup_hidden_count}; "
        f"BUP entry02 textures: {bup_entry02_count}; "
        f"other sprite tables: {detected}; "
        f"candidate records: {len(records)}; manifest: {manifest}"
    )


if __name__ == "__main__":
    main()
