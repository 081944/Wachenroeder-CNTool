#!/usr/bin/env python3
"""Lossless image importers for the verified Wachenroeder image formats."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image


SCREEN_WIDTH = 320
SCREEN_HEIGHT = 224
VISIBLE_COLUMNS = SCREEN_WIDTH // 16
MAP_COLUMNS = 32
MAP_ROWS = SCREEN_HEIGHT // 16
MAP_WORDS = 1024


@dataclass(frozen=True)
class TilemapInfo:
    pixel_offset: int
    map_offset: int
    palette_offset: int
    bits_per_pixel: int
    tile_count: int
    transparent_index_zero: bool


def _be16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def _be32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def _palette_rgb(data: bytes, palette_offset: int) -> list[tuple[int, int, int]]:
    return [
        (
            (color & 0x1F) * 255 // 31,
            ((color >> 5) & 0x1F) * 255 // 31,
            ((color >> 10) & 0x1F) * 255 // 31,
        )
        for color in (_be16(data, palette_offset + index * 2) for index in range(256))
    ]


def probe_tilemap(raw: bytes) -> TilemapInfo:
    """Validate and describe the 320x224 tilemap layout used by the exporter."""
    if len(raw) < 0xA20:
        raise ValueError("tilemap is too short")
    pixel_offset = _be32(raw, 0)
    map_offset = _be32(raw, 4)
    palette_offset = _be32(raw, 8)
    if (
        pixel_offset < 0x20
        or map_offset < 0x20
        or palette_offset < 0x20
        or pixel_offset >= len(raw)
        or map_offset > len(raw) - MAP_WORDS * 2
        or palette_offset > len(raw) - 512
        or pixel_offset == map_offset
    ):
        raise ValueError("invalid tilemap offsets")
    pixel_end = len(raw)
    if map_offset > pixel_offset:
        pixel_end = min(pixel_end, map_offset)
    if palette_offset > pixel_offset:
        pixel_end = min(pixel_end, palette_offset)
    pixel_bytes = pixel_end - pixel_offset
    if pixel_bytes < 128:
        raise ValueError("tilemap has no pixel data")

    max_index = 0
    has_odd_index = False
    has_palette_bank = False
    for index in range(MAP_WORDS):
        word = _be16(raw, map_offset + index * 2)
        if word & 0x0C00:
            raise ValueError("tilemap uses unsupported map flags")
        tile_index = word & 0x03FF
        max_index = max(max_index, tile_index)
        has_odd_index |= bool(tile_index & 1)
        has_palette_bank |= bool(word & 0xF000)

    bits_per_pixel = 4 if has_odd_index or has_palette_bank else 8
    tile_bytes = 256 if bits_per_pixel == 8 else 128
    if pixel_bytes % tile_bytes:
        raise ValueError("tilemap pixel data is not tile aligned")
    # The map stores offsets in 128-byte units. An 8bpp tile occupies two
    # units and therefore uses an even map index.
    if max_index * 128 + tile_bytes > pixel_bytes:
        raise ValueError("tilemap references pixels outside its tile data")

    palette_index_zero = _be16(raw, palette_offset)
    zero_tiles = 0
    for y in range(0, SCREEN_HEIGHT, 8):
        for x in range(0, SCREEN_WIDTH, 8):
            if _transparent_pixel(raw, pixel_offset, map_offset, bits_per_pixel, x, y):
                zero_tiles += 1
    zero_percent = zero_tiles * 100.0 / ((SCREEN_WIDTH // 8) * (SCREEN_HEIGHT // 8))
    transparent = zero_percent > 50 and (palette_index_zero & 0x7FFF) != 0
    return TilemapInfo(
        pixel_offset=pixel_offset,
        map_offset=map_offset,
        palette_offset=palette_offset,
        bits_per_pixel=bits_per_pixel,
        tile_count=pixel_bytes // tile_bytes,
        transparent_index_zero=transparent,
    )


def _pixel_index(
    raw: bytes,
    pixel_offset: int,
    map_offset: int,
    bits_per_pixel: int,
    x: int,
    y: int,
) -> int:
    word = _be16(raw, map_offset + ((y // 16) * MAP_COLUMNS + x // 16) * 2)
    px = x % 16
    py = y % 16
    sample = ((py // 8) * 2 + px // 8) * 64 + (py % 8) * 8 + px % 8
    tile_bytes = 256 if bits_per_pixel == 8 else 128
    tile_offset = pixel_offset + (word & 0x03FF) * 128
    if bits_per_pixel == 8:
        return raw[tile_offset + sample]
    packed = raw[tile_offset + sample // 2]
    nibble = packed >> 4 if sample % 2 == 0 else packed & 0x0F
    return ((word >> 12) & 0x0F) * 16 + nibble


def _transparent_pixel(
    raw: bytes,
    pixel_offset: int,
    map_offset: int,
    bits_per_pixel: int,
    x: int,
    y: int,
) -> bool:
    """Color zero is transparent within the active 4bpp palette bank."""
    index = _pixel_index(raw, pixel_offset, map_offset, bits_per_pixel, x, y)
    return index == 0 if bits_per_pixel == 8 else (index & 0x0F) == 0


def _sample_offset(x: int, y: int) -> int:
    return ((y // 8) * 2 + x // 8) * 64 + (y % 8) * 8 + x % 8


def render_8bpp_tile_strip(raw: bytes, first_tile: int, tile_count: int) -> Image.Image:
    info = probe_tilemap(raw)
    if info.bits_per_pixel != 8 or first_tile < 0 or tile_count <= 0 or first_tile + tile_count > info.tile_count:
        raise ValueError("invalid 8bpp tile strip")
    palette = _palette_rgb(raw, info.palette_offset)
    image = Image.new("RGB", (tile_count * 16, 16))
    pixels = image.load()
    for tile in range(tile_count):
        for y in range(16):
            for x in range(16):
                offset = info.pixel_offset + (first_tile + tile) * 256 + _sample_offset(x, y)
                pixels[tile * 16 + x, y] = palette[raw[offset]]
    return image


def encode_8bpp_tile_strip_png(
    source_raw: bytes,
    png_path: Path,
    first_tile: int,
    tile_count: int,
    base_raw: bytes | None = None,
) -> tuple[bytes, dict[str, int]]:
    info = probe_tilemap(source_raw)
    if info.bits_per_pixel != 8 or first_tile < 0 or tile_count <= 0 or first_tile + tile_count > info.tile_count:
        raise ValueError(f"{png_path.name}: invalid 8bpp tile strip")
    if base_raw is None:
        base_raw = source_raw
    base_info = probe_tilemap(base_raw)
    if len(base_raw) != len(source_raw) or (
        info.pixel_offset, info.map_offset, info.palette_offset,
        info.bits_per_pixel, info.tile_count,
    ) != (
        base_info.pixel_offset, base_info.map_offset, base_info.palette_offset,
        base_info.bits_per_pixel, base_info.tile_count,
    ):
        raise ValueError(f"{png_path.name}: tilemap layout changed")
    with Image.open(png_path) as image:
        if image.size != (tile_count * 16, 16):
            raise ValueError(f"{png_path.name}: expected {tile_count * 16}x16")
        pixels = list(image.convert("RGBA").getdata())
    palette = _palette_rgb(source_raw, info.palette_offset)
    output = bytearray(base_raw)
    changed = 0
    quantized = 0
    for tile in range(tile_count):
        for y in range(16):
            for x in range(16):
                pixel = pixels[y * tile_count * 16 + tile * 16 + x]
                if pixel[3] != 255:
                    raise ValueError(f"{png_path.name}: BUP background title must be opaque")
                offset = info.pixel_offset + (first_tile + tile) * 256 + _sample_offset(x, y)
                old_index = source_raw[offset]
                new_index = (
                    old_index
                    if palette[old_index] == pixel[:3]
                    else _nearest_palette(pixel[:3], palette, range(256))
                )
                changed += new_index != base_raw[offset]
                quantized += palette[new_index] != pixel[:3]
                output[offset] = new_index
    return bytes(output), {
        "changed_pixels": changed,
        "quantized_pixels": quantized,
        "first_tile": first_tile,
        "tile_count": tile_count,
        "tile_capacity": info.tile_count,
    }


def _read_png(path: Path, width: int, height: int) -> list[tuple[int, int, int, int]]:
    with Image.open(path) as image:
        if image.size != (width, height):
            raise ValueError(
                f"{path.name}: expected {width}x{height}, got {image.width}x{image.height}"
            )
        return list(image.convert("RGBA").getdata())


def _nearest_palette(
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


def _same_visible_pixel(
    pixel: tuple[int, int, int, int],
    source_rgb: tuple[int, int, int],
    source_transparent: bool,
    tolerance: int = 0,
) -> bool:
    if source_transparent:
        return pixel[3] == 0
    return pixel[3] != 0 and all(
        abs(pixel[channel] - source_rgb[channel]) <= tolerance for channel in range(3)
    )


def encode_wb_png(source_raw: bytes, png_path: Path, base_raw: bytes | None = None) -> tuple[bytes, dict[str, int]]:
    """Encode a 256-color 208x32 WB image using its original palette."""
    if len(source_raw) < 520:
        raise ValueError(f"{png_path.name}: WB image is too short")
    width = _be16(source_raw, 0)
    height = _be16(source_raw, 2)
    pixel_offset = _be32(source_raw, 4)
    if (
        width < 16
        or width > 512
        or height != 32
        or pixel_offset != 520
        or len(source_raw) != pixel_offset + width * height
    ):
        raise ValueError(f"{png_path.name}: unsupported WB image layout")
    if base_raw is None:
        base_raw = source_raw
    if len(base_raw) != len(source_raw):
        raise ValueError(f"{png_path.name}: base WB image size changed")

    pixels = _read_png(png_path, width, height)
    palette = _palette_rgb(source_raw, 8)
    output = bytearray(base_raw)
    changed = 0
    quantized = 0
    partial_alpha = 0
    for index, pixel in enumerate(pixels):
        old_index = source_raw[pixel_offset + index]
        if pixel[3] == 0:
            new_index = 0
        else:
            if pixel[3] != 255:
                partial_alpha += 1
            new_index = (
                old_index
                if _same_visible_pixel(pixel, palette[old_index], False, tolerance=2)
                else _nearest_palette(pixel[:3], palette, range(256))
            )
            if new_index != old_index and palette[new_index] != pixel[:3]:
                quantized += 1
        if new_index != old_index:
            changed += 1
        output[pixel_offset + index] = new_index
    return bytes(output), {
        "changed_pixels": changed,
        "quantized_pixels": quantized,
        "partial_alpha_pixels": partial_alpha,
    }


def encode_tilemap_png(
    source_raw: bytes,
    png_path: Path,
    base_raw: bytes | None = None,
    preserve_tile_indices: bool = False,
    propagate_visible_tiles: bool = False,
    reference_png_path: Path | None = None,
    preserve_png_indices: bool = False,
) -> tuple[bytes, dict[str, int]]:
    """Encode a 320x224 screen PNG into the original fixed-size tile pool.

    The Korean reference patch repacks the complete 32x32 map into the
    existing pixel pool after an edit.  Keeping the decoded buffer size and
    all three offsets unchanged is important for these boot/runtime-loaded
    Saturn resources, so this encoder deliberately rejects edits that do not
    fit instead of extending the resource.
    """
    info = probe_tilemap(source_raw)
    if base_raw is None:
        base_raw = source_raw
    if len(base_raw) != len(source_raw):
        raise ValueError(f"{png_path.name}: base tilemap size changed")
    base_info = probe_tilemap(base_raw)
    if (
        info.pixel_offset != base_info.pixel_offset
        or info.map_offset != base_info.map_offset
        or info.palette_offset != base_info.palette_offset
        or info.bits_per_pixel != base_info.bits_per_pixel
        or info.tile_count != base_info.tile_count
    ):
        raise ValueError(f"{png_path.name}: base tilemap layout changed")

    pixels = _read_png(png_path, SCREEN_WIDTH, SCREEN_HEIGHT)
    reference_pixels = (
        _read_png(reference_png_path, SCREEN_WIDTH, SCREEN_HEIGHT)
        if reference_png_path is not None else None
    )
    palette = _palette_rgb(source_raw, info.palette_offset)
    indexed_pixels = None
    if preserve_png_indices:
        if info.bits_per_pixel != 4 or not info.transparent_index_zero:
            raise ValueError("Exact PNG indices require a transparent 4bpp tilemap")
        with Image.open(png_path) as indexed:
            if indexed.mode != "P" or indexed.size != (SCREEN_WIDTH, SCREEN_HEIGHT):
                raise ValueError(f"{png_path.name}: expected a 320x224 indexed PNG")
            if indexed.getpalette() != [component for rgb in palette[:33] for component in rgb]:
                raise ValueError(f"{png_path.name}: PNG palette does not match the game resource")
            expected_alpha = bytes(0 if index % 16 == 0 else 255 for index in range(33))
            if indexed.info.get("transparency") != expected_alpha:
                raise ValueError(f"{png_path.name}: PNG transparency does not match the game palette banks")
            indexed_pixels = list(indexed.getdata())
    output = bytearray(base_raw)
    tile_bytes = 256 if info.bits_per_pixel == 8 else 128
    tile_step = 2 if info.bits_per_pixel == 8 else 1
    source_tiles = [
        source_raw[
            info.pixel_offset + tile * tile_bytes : info.pixel_offset + (tile + 1) * tile_bytes
        ]
        for tile in range(info.tile_count)
    ]
    changed_pixels = 0
    quantized = 0
    partial_alpha = 0
    changed_tiles = 0
    desired_tiles: list[bytes] = []
    map_words: list[int] = []

    def choose_index(
        pixel: tuple[int, int, int, int],
        source_index: int,
        source_rgb: tuple[int, int, int],
        candidates: range,
    ) -> int:
        nonlocal quantized, partial_alpha
        source_transparent = info.transparent_index_zero and (
            source_index == 0
            if info.bits_per_pixel == 8
            else (source_index & 0x0F) == 0
        )
        if source_transparent and pixel[3] == 0:
            return source_index
        if pixel[3] == 0:
            if info.bits_per_pixel == 8:
                return 0
            transparent_index = source_index & 0xF0
            return transparent_index
        if pixel[3] != 255:
            partial_alpha += 1
        if _same_visible_pixel(pixel, source_rgb, source_transparent):
            return source_index
        new_index = _nearest_palette(pixel[:3], palette, candidates)
        if palette[new_index] != pixel[:3]:
            quantized += 1
        return new_index

    for map_index in range(MAP_WORDS):
        cell_x = map_index % MAP_COLUMNS
        cell_y = map_index // MAP_COLUMNS
        source_word = _be16(source_raw, info.map_offset + map_index * 2)
        base_word = _be16(base_raw, info.map_offset + map_index * 2)
        source_tile_index = source_word & 0x03FF
        source_tile_number = source_tile_index // tile_step
        desired = bytearray(source_tiles[source_tile_number])
        bank = (source_word >> 12) & 0x0F
        if info.bits_per_pixel == 4:
            candidates = range(bank * 16 + 1, bank * 16 + 16)
        elif info.transparent_index_zero:
            candidates = range(1, 256)
        else:
            candidates = range(256)
        if cell_x < VISIBLE_COLUMNS and cell_y < MAP_ROWS:
            for py in range(16):
                for px in range(16):
                    screen_x = cell_x * 16 + px
                    screen_y = cell_y * 16 + py
                    screen_index = screen_y * SCREEN_WIDTH + screen_x
                    source_index = _pixel_index(
                        source_raw,
                        info.pixel_offset,
                        info.map_offset,
                        info.bits_per_pixel,
                        screen_x,
                        screen_y,
                    )
                    new_index = (
                        indexed_pixels[screen_index]
                        if indexed_pixels is not None else
                        source_index
                        if reference_pixels is not None
                        and pixels[screen_index] == reference_pixels[screen_index]
                        else choose_index(
                            pixels[screen_index],
                            source_index,
                            palette[source_index],
                            candidates,
                        )
                    )
                    if indexed_pixels is not None and new_index >> 4 != bank:
                        raise ValueError(f"{png_path.name}: index {new_index} at {screen_x},{screen_y} "
                                         f"does not belong to palette bank {bank}")
                    if new_index != source_index:
                        changed_pixels += 1
                    sample = _sample_offset(px, py)
                    if info.bits_per_pixel == 8:
                        desired[sample] = new_index
                    elif sample % 2 == 0:
                        desired[sample // 2] = (
                            (new_index & 0x0F) << 4 | desired[sample // 2] & 0x0F
                        )
                    else:
                        desired[sample // 2] = (
                            desired[sample // 2] & 0xF0 | new_index & 0x0F
                        )
        desired_data = bytes(desired)
        if desired_data != source_tiles[source_tile_number]:
            changed_tiles += 1
        desired_tiles.append(desired_data)
        map_words.append(base_word)

    if not changed_pixels:
        return base_raw, {
            "changed_pixels": 0,
            "quantized_pixels": quantized,
            "partial_alpha_pixels": partial_alpha,
            "changed_tiles": 0,
            "allocated_tiles": 0,
            "expanded_tiles": 0,
            "repacked_tiles": 0,
            "tile_capacity": info.tile_count,
        }

    if propagate_visible_tiles and not preserve_tile_indices:
        raise ValueError("visible tile propagation requires stable tile indices")

    if preserve_tile_indices:
        # Some screens animate by referring to the original tile numbers from
        # code rather than only through the map.  Keep those numbers stable;
        # a visual round-trip is not sufficient when the runtime updates a
        # tile after the initial upload.
        desired_by_source: dict[int, bytes] = {}
        for map_index, desired_data in enumerate(desired_tiles):
            if propagate_visible_tiles and (
                map_index % MAP_COLUMNS >= VISIBLE_COLUMNS
                or map_index // MAP_COLUMNS >= MAP_ROWS
            ):
                continue
            source_word = _be16(source_raw, info.map_offset + map_index * 2)
            source_tile_number = (source_word & 0x03FF) // tile_step
            previous = desired_by_source.get(source_tile_number)
            if previous is not None and previous != desired_data:
                raise ValueError(
                    f"{png_path.name}: one source tile needs multiple edited contents; "
                    "stable tile indices cannot represent this edit"
                )
            desired_by_source[source_tile_number] = desired_data

        output = bytearray(base_raw)
        packed_pixels = b"".join(
            desired_by_source.get(tile, source_tiles[tile])
            for tile in range(info.tile_count)
        )
        pixel_end = info.map_offset if info.map_offset > info.pixel_offset else info.palette_offset
        output[info.pixel_offset:pixel_end] = packed_pixels.ljust(
            pixel_end - info.pixel_offset, b"\0"
        )
        return bytes(output), {
            "changed_pixels": changed_pixels,
            "quantized_pixels": quantized,
            "partial_alpha_pixels": partial_alpha,
            "changed_tiles": changed_tiles,
            "allocated_tiles": 0,
            "expanded_tiles": 0,
            "repacked_tiles": len(desired_by_source),
            "tile_capacity": info.tile_count,
            "preserved_tile_indices": 1,
            "propagated_visible_tiles": int(propagate_visible_tiles),
        }

    tile_by_data: dict[bytes, int] = {}
    packed_tiles: list[bytes] = []
    for map_index, tile_data in enumerate(desired_tiles):
        tile_index = tile_by_data.get(tile_data)
        if tile_index is None:
            tile_index = len(packed_tiles) * tile_step
            tile_by_data[tile_data] = tile_index
            packed_tiles.append(tile_data)
        map_words[map_index] = (map_words[map_index] & 0xF000) | tile_index

    if len(packed_tiles) > info.tile_count:
        raise ValueError(
            f"{png_path.name}: edited tilemap needs {len(packed_tiles)} tiles, "
            f"but the fixed pool holds {info.tile_count}"
        )

    output = bytearray(base_raw)
    pixel_end = info.map_offset if info.map_offset > info.pixel_offset else info.palette_offset
    packed_pixels = b"".join(packed_tiles).ljust(pixel_end - info.pixel_offset, b"\0")
    output[info.pixel_offset:pixel_end] = packed_pixels
    for map_index, word in enumerate(map_words):
        output[info.map_offset + map_index * 2 : info.map_offset + map_index * 2 + 2] = word.to_bytes(
            2, "big"
        )

    return bytes(output), {
        "changed_pixels": changed_pixels,
        "quantized_pixels": quantized,
        "partial_alpha_pixels": partial_alpha,
        "changed_tiles": changed_tiles,
        "allocated_tiles": max(0, len(packed_tiles) - len(set(source_tiles))),
        "expanded_tiles": 0,
        "repacked_tiles": len(packed_tiles),
        "tile_capacity": info.tile_count,
    }
