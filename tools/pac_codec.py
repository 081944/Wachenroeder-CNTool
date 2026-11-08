#!/usr/bin/env python3
"""Codec for Wachenroeder's dictionary-compressed PAC entries."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import struct


MAX_OUTPUT = 16 * 1024 * 1024
MAX_STACK = 32
BLOCK_BYTES = 0xFFFF


def read_be32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def split_pac(data: bytes) -> list[bytes]:
    if len(data) < 8:
        raise ValueError("PAC is too short")
    table_bytes = read_be32(data, 0)
    if table_bytes < 4 or table_bytes > len(data) or table_bytes % 4:
        raise ValueError("invalid PAC offset table")
    count = table_bytes // 4
    offsets = [read_be32(data, index * 4) for index in range(count)]
    if offsets[0] != table_bytes:
        raise ValueError("PAC data does not follow its offset table")
    result = []
    for index, start in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < count else len(data)
        if start < table_bytes or end < start or end > len(data):
            raise ValueError("PAC offset outside file")
        result.append(data[start:end])
    return result


def build_pac(entries: list[bytes]) -> bytes:
    if not entries:
        raise ValueError("PAC must contain at least one entry")
    table_bytes = len(entries) * 4
    output = bytearray(table_bytes)
    offset = table_bytes
    for index, entry in enumerate(entries):
        if offset > 0xFFFFFFFF:
            raise ValueError("PAC is too large")
        struct.pack_into(">I", output, index * 4, offset)
        output.extend(entry)
        offset += len(entry)
    return bytes(output)


def decode_entry(data: bytes, *, stack_limit: int = MAX_STACK) -> bytes:
    if len(data) < 3:
        raise ValueError("compressed entry is too short")
    end = 3 + int.from_bytes(data[:3], "big")
    if end > len(data):
        raise ValueError("compressed length exceeds entry")
    position = 3
    output = bytearray()
    while position < end:
        left = list(range(256))
        right = [0] * 256
        control = data[position]
        position += 1
        symbol = control - 127 if control >= 128 else 0
        run_left = 1 if control >= 128 else control + 1
        while symbol < 256:
            if position >= end:
                raise ValueError("truncated dictionary entry")
            value = data[position]
            position += 1
            left[symbol] = value
            if value != symbol:
                if position >= end:
                    raise ValueError("truncated dictionary pair")
                right[symbol] = data[position]
                position += 1
            symbol += 1
            run_left -= 1
            if run_left == 0 and symbol < 256:
                if position >= end:
                    raise ValueError("truncated dictionary control")
                control = data[position]
                position += 1
                if control >= 128:
                    symbol += control - 127
                    run_left = 1
                else:
                    run_left = control + 1
                if symbol > 256:
                    raise ValueError("dictionary skip overflow")
        if position + 2 > end:
            raise ValueError("truncated block length")
        count = int.from_bytes(data[position : position + 2], "big")
        position += 2
        if stack_limit < 2:
            raise ValueError("stack limit must be at least 2")
        stack = [0] * stack_limit
        for _ in range(count):
            if position >= end:
                raise ValueError("truncated block data")
            depth = 1
            stack[0] = data[position]
            position += 1
            while depth:
                value = stack[depth - 1]
                depth -= 1
                if left[value] == value:
                    output.append(value)
                else:
                    if depth + 2 > len(stack):
                        raise ValueError("dictionary expansion exceeds SH-2 stack")
                    stack[depth] = right[value]
                    stack[depth + 1] = left[value]
                    depth += 2
                if len(output) > MAX_OUTPUT:
                    raise ValueError("decoded entry exceeds 16 MiB")
    if position != end:
        raise ValueError("compressed entry length mismatch")
    return bytes(output)


def _replace_pairs(tokens: list[int], rules: dict[int, tuple[int, int]]) -> None:
    present = set(tokens)
    free = [value for value in range(256) if value not in present]
    depths = {value: 1 for value in present}
    # The dictionary format permits 255 rules, but a bounded pass keeps large
    # font resources predictable while still removing the useful repetition.
    while free and len(tokens) > 2 and len(rules) < 64:
        pairs = Counter(zip(tokens, tokens[1:]))
        candidate = None
        candidate_count = 0
        for pair, count in pairs.most_common(24):
            if count < 3:
                continue
            depth = max(depths.get(pair[0], 1), depths.get(pair[1], 1)) + 1
            if depth >= MAX_STACK:
                continue
            occurrences = 0
            index = 0
            while index + 1 < len(tokens):
                if (tokens[index], tokens[index + 1]) == pair:
                    occurrences += 1
                    index += 2
                else:
                    index += 1
            if occurrences > candidate_count:
                candidate = (pair, depth, occurrences)
                candidate_count = occurrences
        if candidate is None:
            return
        (left, right), depth, occurrences = candidate
        if occurrences < 3:
            return
        symbol = free.pop(0)
        rules[symbol] = (left, right)
        depths[symbol] = depth
        rebuilt: list[int] = []
        index = 0
        while index < len(tokens):
            if index + 1 < len(tokens) and tokens[index] == left and tokens[index + 1] == right:
                rebuilt.append(symbol)
                index += 2
            else:
                rebuilt.append(tokens[index])
                index += 1
        tokens[:] = rebuilt


def _bpe_block(raw: bytes, minimum_frequency: int = 2) -> tuple[dict[int, tuple[int, int]], list[int]]:
    """Build the sparse byte-pair dictionary used by the Korean reference."""
    sequence = list(raw)
    available = [value for value in range(255, -1, -1) if value not in set(raw)]
    definitions: dict[int, tuple[int, int]] = {}
    expansion_lengths = {value: 1 for value in set(raw)}
    stack_peaks = {value: 0 for value in set(raw)}

    while available:
        counts = Counter(zip(sequence, sequence[1:]))
        best: tuple[int, int, tuple[int, int]] | None = None
        for pair, frequency in counts.items():
            length = expansion_lengths.get(pair[0], 1) + expansion_lengths.get(pair[1], 1)
            peak = max(
                2,
                1 + stack_peaks.get(pair[0], 0),
                stack_peaks.get(pair[1], 0),
            )
            if frequency < minimum_frequency or length > 500 or peak > MAX_STACK:
                continue
            candidate = (frequency, length, pair)
            if best is None or candidate > best:
                best = candidate
        if best is None:
            break

        _, length, pair = best
        token = available.pop(0)
        definitions[token] = pair
        expansion_lengths[token] = length
        stack_peaks[token] = max(
            2,
            1 + stack_peaks.get(pair[0], 0),
            stack_peaks.get(pair[1], 0),
        )
        rebuilt: list[int] = []
        index = 0
        while index < len(sequence):
            if index + 1 < len(sequence) and (sequence[index], sequence[index + 1]) == pair:
                rebuilt.append(token)
                index += 2
            else:
                rebuilt.append(sequence[index])
                index += 1
        sequence = rebuilt
    return definitions, sequence


def _encode_sparse_dictionary(definitions: dict[int, tuple[int, int]]) -> bytes:
    """Encode sparse dictionary runs with the shortest valid controls."""
    entry_cost = [2 if index in definitions else 1 for index in range(256)]
    changed = [index in definitions for index in range(257)]
    prefix_changed = [0]
    for index in range(256):
        prefix_changed.append(prefix_changed[-1] + int(changed[index]))

    infinity = 1 << 30
    cost = [infinity] * 257
    choice: list[tuple[str, int] | None] = [None] * 257
    cost[256] = 0
    for index in range(255, -1, -1):
        running = 0
        for length in range(1, min(128, 256 - index) + 1):
            running += entry_cost[index + length - 1]
            candidate = 1 + running + cost[index + length]
            if candidate < cost[index]:
                cost[index] = candidate
                choice[index] = ("run", length)
        for distance in range(1, min(128, 256 - index) + 1):
            target = index + distance
            if prefix_changed[target] != prefix_changed[index]:
                break
            candidate = 1
            if target < 256:
                candidate += entry_cost[target] + cost[target + 1]
            if candidate < cost[index]:
                cost[index] = candidate
                choice[index] = ("skip", distance)

    output = bytearray()
    index = 0
    while index < 256:
        selected = choice[index]
        if selected is None:
            raise ValueError("could not encode sparse dictionary")
        kind, amount = selected
        if kind == "run":
            output.append(amount - 1)
            for entry in range(index, index + amount):
                if entry in definitions:
                    output.extend(definitions[entry])
                else:
                    output.append(entry)
            index += amount
        else:
            output.append(127 + amount)
            target = index + amount
            if target < 256:
                if target in definitions:
                    output.extend(definitions[target])
                else:
                    output.append(target)
                index = target + 1
            else:
                index = 256
    return bytes(output)


def encode_entry_sparse(raw: bytes, block_size: int = 3500, *, first_block_size: int | None = None) -> bytes:
    """Encode using the sparse BPE compressor used by the Korean project."""
    if block_size <= 0 or block_size >= 0x8000:
        raise ValueError("invalid sparse compression block size")
    if first_block_size is not None and not 0 < first_block_size <= block_size:
        raise ValueError("first sparse block must be positive and no larger than the block limit")
    payload = bytearray()
    start = 0
    while start < len(raw):
        size = first_block_size if start == 0 and first_block_size is not None else block_size
        definitions, tokens = _bpe_block(raw[start : start + size])
        start += size
        if len(tokens) >= 0x8000:
            raise ValueError("compressed block token count exceeds SH-2 signed range")
        payload.extend(_encode_sparse_dictionary(definitions))
        payload.extend(len(tokens).to_bytes(2, "big"))
        payload.extend(tokens)
    if len(payload) > 0xFFFFFF:
        raise ValueError("compressed stream exceeds 24-bit length field")
    encoded = len(payload).to_bytes(3, "big") + payload
    if decode_entry(encoded) != raw:
        raise ValueError("sparse compressor round-trip mismatch")
    return encoded


def encode_entry_best(
    raw: bytes,
    block_sizes: tuple[int, ...] = (3500, 4000, 5000),
) -> bytes:
    """Choose the smallest stream within the Korean runtime-safe size range.

    The Saturn title/menu loader has a smaller working buffer than the generic
    decoder.  The generic codec can round-trip 6000/8000-byte blocks, but
    those streams are not valid inputs for that loader.
    """
    return min((encode_entry_sparse(raw, size) for size in block_sizes), key=len)


def _encode_block(raw: bytes, compress_large: bool = False) -> bytes:
    tokens = list(raw)
    rules: dict[int, tuple[int, int]] = {}
    # Large font blocks are already close to incompressible and scanning them
    # repeatedly is expensive. Their literal dictionary is fully valid and
    # keeps rebuilds fast and deterministic.
    if compress_large or len(raw) <= 8192:
        _replace_pairs(tokens, rules)
    dictionary = bytearray()
    for start in (0, 128):
        dictionary.append(127)
        for symbol in range(start, start + 128):
            left, right = rules.get(symbol, (symbol, 0))
            dictionary.append(left)
            if left != symbol:
                dictionary.append(right)
    return bytes(dictionary) + struct.pack(">H", len(tokens)) + bytes(tokens)


def encode_entry(raw: bytes, compress_large: bool = False) -> bytes:
    payload = bytearray()
    for start in range(0, len(raw), BLOCK_BYTES):
        payload.extend(_encode_block(raw[start : start + BLOCK_BYTES], compress_large))
    if len(payload) > 0xFFFFFF:
        raise ValueError("compressed entry exceeds 24-bit length field")
    return len(payload).to_bytes(3, "big") + payload


def verify_round_trip(raw: bytes, compress_large: bool = False) -> bytes:
    encoded = encode_entry(raw, compress_large)
    if decode_entry(encoded) != raw:
        raise ValueError("PAC codec round-trip mismatch")
    return encoded


def replace_pac_entries(
    source: bytes,
    replacements: dict[int, bytes],
    compress_large: bool = False,
    sparse_entries: set[int] | None = None,
    sparse_block_size: int = 3500,
    sparse_block_sizes: dict[int, int] | None = None,
    sparse_first_block_sizes: dict[int, int] | None = None,
) -> tuple[bytes, list[dict[str, int]]]:
    sparse_entries = sparse_entries or set()
    sparse_block_sizes = sparse_block_sizes or {}
    sparse_first_block_sizes = sparse_first_block_sizes or {}
    source_entries = split_pac(source)
    entries = []
    stats = []
    for index, compressed in enumerate(source_entries):
        source_raw = decode_entry(compressed)
        replacement = replacements.get(index)
        if replacement is None:
            encoded = compressed
            output_raw = source_raw
        else:
            output_raw = replacement
            if index in sparse_entries:
                encoded = encode_entry_sparse(
                    output_raw,
                    block_size=sparse_block_sizes.get(index, sparse_block_size),
                    first_block_size=sparse_first_block_sizes.get(index),
                )
            else:
                encoded = verify_round_trip(output_raw, compress_large)
        entries.append(encoded)
        stats.append(
            {
                "entry": index,
                "source_compressed_bytes": len(compressed),
                "output_compressed_bytes": len(encoded),
                "source_decoded_bytes": len(source_raw),
                "output_decoded_bytes": len(output_raw),
            }
        )
    return build_pac(entries), stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_bytes(verify_round_trip(args.input.read_bytes()))
