#!/usr/bin/env python3
"""Extract every image layer stored inside TITLE.PAC.

TITLE.PAC contains two raw nested PAC containers.  The first contains the
title tilemaps; the second contains an 8bpp sprite pixel pool and 50 sprite
definitions.  This exporter keeps those locations explicit so an edited PNG
can be mapped back to the exact nested resource without guessing aliases.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw

from image_codec import probe_tilemap
from pac_codec import decode_entry, split_pac


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def be16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def bgr555_to_rgb(color: int) -> tuple[int, int, int]:
    return (
        (color & 0x1F) * 255 // 31,
        ((color >> 5) & 0x1F) * 255 // 31,
        ((color >> 10) & 0x1F) * 255 // 31,
    )


def title_palette(title_top: list[bytes]) -> list[tuple[int, int, int, int]]:
    """Use the title tilemap palette for the indexed title sprites."""
    nested = split_pac(title_top[0])
    background = decode_entry(nested[0])
    info = probe_tilemap(background)
    return [
        (*bgr555_to_rgb(be16(background, info.palette_offset + index * 2)), 255)
        for index in range(256)
    ]


def render_tilemap(raw: bytes) -> Image.Image:
    info = probe_tilemap(raw)
    palette = [
        bgr555_to_rgb(be16(raw, info.palette_offset + index * 2))
        for index in range(256)
    ]
    image = Image.new("RGBA", (320, 224))
    pixels = image.load()
    for y in range(224):
        for x in range(320):
            word = be16(raw, info.map_offset + ((y // 16) * 32 + x // 16) * 2)
            sample = ((y % 16) // 8 * 2 + (x % 16) // 8) * 64 + (y % 8) * 8 + x % 8
            tile_offset = info.pixel_offset + (word & 0x03FF) * 128
            if info.bits_per_pixel == 8:
                index = raw[tile_offset + sample]
                transparent = info.transparent_index_zero and index == 0
            else:
                packed = raw[tile_offset + sample // 2]
                index = ((word >> 12) & 0x0F) * 16 + (
                    packed >> 4 if sample % 2 == 0 else packed & 0x0F
                )
                transparent = info.transparent_index_zero and index % 16 == 0
            pixels[x, y] = (*palette[index], 0 if transparent else 255)
    return image


def sprite_offset(definition: bytes, sprite: int, source_base: int) -> tuple[int, int, int, int, int]:
    width = be16(definition, sprite * 8)
    height = be16(definition, sprite * 8 + 2)
    source = be16(definition, sprite * 8 + 4)
    stored_size = be16(definition, sprite * 8 + 6)
    offset = (source - source_base) * 8
    return width, height, source, stored_size, offset


def render_sprite(
    pixels: bytes,
    palette: list[tuple[int, int, int, int]],
    width: int,
    height: int,
    offset: int,
) -> Image.Image:
    expected = width * height
    if offset < 0 or offset + expected > len(pixels):
        raise ValueError(f"sprite data exceeds pixel pool at 0x{offset:X}")
    image = Image.new("RGBA", (width, height))
    output = image.load()
    for y in range(height):
        for x in range(width):
            index = pixels[offset + y * width + x]
            output[x, y] = (*palette[index][:3], 0 if index == 0 else 255)
    return image


def save_sheet(images: list[tuple[int, Image.Image]], destination: Path) -> None:
    cell_width = max(image.width for _, image in images)
    cell_height = max(image.height for _, image in images)
    columns = 5
    label_height = 18
    rows = (len(images) + columns - 1) // columns
    sheet = Image.new("RGBA", (columns * cell_width, rows * (cell_height + label_height)), (32, 32, 32, 255))
    draw = ImageDraw.Draw(sheet)
    for index, image in images:
        x = (index % columns) * cell_width
        y = (index // columns) * (cell_height + label_height)
        sheet.alpha_composite(image, (x, y + label_height))
        draw.text((x + 2, y + 1), f"{index:02d} {image.width}x{image.height}", fill=(255, 255, 255, 255))
    sheet.save(destination)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    source = root / "unpacked" / "japan" / "TITLE.PAC"
    image_root = root / "work" / "imageori"
    manifest_root = image_root / "manifests"
    raw_root = root / "work" / "extracted" / "japan" / "title"
    manifest_root.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)

    top = split_pac(source.read_bytes())
    records: list[dict[str, object]] = []

    # Both title image containers are raw nested PACs, not dictionary streams.
    for top_entry in (0, 1):
        nested = split_pac(top[top_entry])
        for inner_entry, compressed in enumerate(nested):
            raw = decode_entry(compressed)
            stem = f"TITLE.PAC.entry{top_entry:02d}.inner{inner_entry:02d}"
            raw_path = raw_root / f"{stem}.bin"
            raw_path.write_bytes(raw)
            try:
                info = probe_tilemap(raw)
            except ValueError:
                info = None
            if info is not None:
                png_path = image_root / "title-tilemaps" / source.name / f"{stem}.png"
                png_path.parent.mkdir(parents=True, exist_ok=True)
                render_tilemap(raw).save(png_path)
                records.append(
                    {
                        "Kind": "nested-tilemap",
                        "TopEntry": top_entry,
                        "InnerEntry": inner_entry,
                        "Sprite": "",
                        "Raw": f"work/extracted/japan/title/{raw_path.name}",
                        "Png": png_path.relative_to(root).as_posix(),
                        "Width": 320,
                        "Height": 224,
                        "Offset": 0,
                        "DecodedBytes": len(raw),
                        "SHA256": sha256(raw),
                    }
                )
                continue

            # The second nested container is a definition table followed by
            # one 8bpp pixel pool.  Export every definition independently.
            if top_entry != 1 or inner_entry != 1:
                continue
            definitions = decode_entry(nested[0])
            if len(definitions) % 8 or not definitions:
                raise ValueError("TITLE.PAC sprite definition table is not 8-byte aligned")
            palette = title_palette(top)
            source_base = min(
                be16(definitions, index * 8 + 4)
                for index in range(len(definitions) // 8)
            )
            exported: list[tuple[int, Image.Image]] = []
            for sprite in range(len(definitions) // 8):
                width, height, source_address, stored_size, offset = sprite_offset(
                    definitions, sprite, source_base
                )
                image = render_sprite(raw, palette, width, height, offset)
                png_path = image_root / "title-sprites" / source.name / f"{stem}.sprite{sprite:02d}.png"
                png_path.parent.mkdir(parents=True, exist_ok=True)
                image.save(png_path)
                exported.append((sprite, image))
                records.append(
                    {
                        "Kind": "nested-8bpp-sprite",
                        "TopEntry": top_entry,
                        "InnerEntry": inner_entry,
                        "Sprite": sprite,
                        "Raw": f"work/extracted/japan/title/{raw_path.name}",
                        "Png": png_path.relative_to(root).as_posix(),
                        "Width": width,
                        "Height": height,
                        "Offset": offset,
                        "SourceAddress": f"0x{source_address:04X}",
                        "StoredSize": stored_size,
                        "DecodedBytes": len(raw),
                        "SHA256": sha256(raw),
                    }
                )
            sheet_path = image_root / "previews" / "title-sheets" / source.name / f"{stem}.sheet.png"
            sheet_path.parent.mkdir(parents=True, exist_ok=True)
            save_sheet(exported, sheet_path)

    manifest = manifest_root / "title-image-manifest.csv"
    fields = sorted({key for record in records for key in record})
    with manifest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    print(f"TITLE.PAC image layers: {len(records)}; manifest: {manifest}")


if __name__ == "__main__":
    main()
