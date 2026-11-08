#!/usr/bin/env python3
"""Build a non-distributable SH-2 probe for the 8x8 ASCII tile writer."""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "unpacked/japan/0"
MAIN_FONT = ROOT / "work/fonts/chinese/ASCII.PAC.entry00.bin"
TILE_DELTAS = ROOT / "temp/packed-small-font/tile-deltas.bin"
EXECUTABLE_BASE = 0x06004000
ENTRY = 0x0600F2A8
LITERAL = 0x0600F2CC
WORD_ENTRY = 0x0602F124
WORD_LITERAL = 0x0602F154
RESERVED_MAIN_CODE = 0x0D33
MAIN_FONT_FIRST_CODE = 0x20
MAIN_FONT_SLOT_SIZE = 32
RUNTIME_TABLE = 0x00200000 + (RESERVED_MAIN_CODE - MAIN_FONT_FIRST_CODE) * MAIN_FONT_SLOT_SIZE
TILE_DELTA_COUNT = 0x400
RUNTIME_CODE = RUNTIME_TABLE + TILE_DELTA_COUNT

# The routine reads a signed one-byte tile delta for the logical glyph index
# and writes one pattern-name word to the same staging map as before.
ASCII_WRITER = bytes.fromhex(
    "644C 655C 666C 4608 4608 366C 4608 355C 365C "
    "D104 6043 021C 324C D103 6063 0125 000B 0009 "
    "0021A260 002E8000"
)
TRAMPOLINE = bytes.fromhex("D008 402B 0009")
WORD_WRITER = bytes.fromhex(
    "644D 655D 666D 9313 6243 2239 9311 2439 4608 4608 "
    "366C 4608 355C 365C D105 6043 031C 334C 323C D104 "
    "6063 0125 000B 0009 1000 03FF 0021A260 002E8000"
)
WORD_TRAMPOLINE = bytes.fromhex("D00B 402B 0009")
RUNTIME_WORD_CODE = RUNTIME_CODE + len(ASCII_WRITER)
DOUBLE_ENTRY = 0x06035798
DOUBLE_LITERAL = 0x060357DC
DOUBLE_TRAMPOLINE = bytes.fromhex("D010 402B 0009")
BLANK_TILE = 149
MENU_SELECTED_PALETTE_BASE = 0x1000
WORD_PALETTE_BASE = 0x1000
DOUBLE_PALETTE_BASE = 0x1000
# The small-font initializer uploads the menu text palette to CRAM index
# 0x011 (bus address 0x25F00022).  The original literal is the gray ink
# color used by the Japanese menu; translated menu glyphs should use white.
MENU_TEXT_PALETTE_OFFSET = 0x30F2A
MENU_TEXT_PALETTE_ORIGINAL = 0xB18C
MENU_TEXT_PALETTE_WHITE = 0x7FFF


def double_row_writer(address: int, fixed_palette_base: int | None = None) -> bytes:
    # The original double-row writer adds tile 0x100 (source slot 0x80)
    # before drawing. The packed table instead stores that physical tile.
    words: list[int | tuple[str, int, str]] = [0x644D, 0x655D, 0x666D, 0x2F46]
    if fixed_palette_base is None:
        # Preserve the original caller-selected palette for generic users of
        # the 8x16 writer. The status-screen hook uses the fixed variant.
        words.extend([("word", 1, "flags"), 0x6243, 0x2219])
    else:
        words.append(("word", 2, "palette_base"))
    words.extend([
        ("word", 1, "code_mask"), 0x2419,
        0x747F, 0x7401,
        0x4608, 0x4608, 0x366C, 0x4608, 0x355C, 0x365C,
        ("long", 1, "table"), 0x6043, 0x031C, 0x334C,
        0x332C,
        ("long", 1, "top"), 0x6063, 0x0135,
        ("word", 3, "blank"), 0x332C,
        ("long", 1, "bottom"), 0x0135,
        0xE701, 0x4718, 0x64F6,
        0x000B, 0x0009,
    ])
    result = bytearray(len(words) * 2)
    if len(result) % 4:
        result.extend(b"\0\0")
    literals: dict[str, int] = {}
    literal_values = [("code_mask", 0x01FF), ("blank", BLANK_TILE)]
    if fixed_palette_base is None:
        literal_values.insert(0, ("flags", 0xF000))
    else:
        literal_values.insert(0, ("palette_base", fixed_palette_base))
    for name, value in literal_values:
        literals[name] = address + len(result)
        result.extend(value.to_bytes(2, "big"))
    if len(result) % 4:
        result.extend(b"\0\0")
    for name, value in (("table", RUNTIME_TABLE), ("top", 0x002E8000),
                        ("bottom", 0x002E8080)):
        literals[name] = address + len(result)
        result.extend(value.to_bytes(4, "big"))
    for index, item in enumerate(words):
        current = address + index * 2
        if isinstance(item, int):
            opcode = item
        else:
            width, register, name = item
            base = current + 4 if width == "word" else (current + 4) & ~3
            stride = 2 if width == "word" else 4
            delta = literals[name] - base
            if delta < 0 or delta % stride or delta // stride > 255:
                raise ValueError(f"SH-2 literal {name} is out of range")
            opcode = (0x9000 if width == "word" else 0xD000) | register << 8 | delta // stride
        result[index * 2:index * 2 + 2] = opcode.to_bytes(2, "big")
    return bytes(result)


RUNTIME_DOUBLE_CODE = (RUNTIME_WORD_CODE + len(WORD_WRITER) + 3) & ~3
# The character-status description/equipment fields call the generic
# double-row writer and pass the original gray bank.  The translated packed
# glyphs use the white bank, so this relocated copy must replace the caller's
# palette bits as well.
DOUBLE_WRITER = double_row_writer(RUNTIME_DOUBLE_CODE, DOUBLE_PALETTE_BASE)
RUNTIME_STATUS_DOUBLE_CODE = (RUNTIME_DOUBLE_CODE + len(DOUBLE_WRITER) + 3) & ~3
STATUS_DOUBLE_WRITER = double_row_writer(
    RUNTIME_STATUS_DOUBLE_CODE, DOUBLE_PALETTE_BASE
)
MENU_ENTRY = 0x060068D8
MENU_LITERAL = 0x060068E0
MENU_RESUME = 0x060068EC
MENU_TRAMPOLINE = bytes.fromhex("D001 402B 0009")


def menu_writer(address: int) -> bytes:
    words: list[int | tuple[str, int, str]] = [
        0x671D, 0x627C, 0x727F, 0x7201,
        ("long", 1, "table"), 0x6023, 0x011C, 0x321C,
        ("word", 1, "palette_base"), 0x321C, 0x6033, 0x0525,
        ("word", 2, "blank"),
        ("long", 1, "resume"), 0x412B, 0x0009,
    ]
    result = bytearray(len(words) * 2)
    literals: dict[str, int] = {}
    palette_address = address + len(result)
    result.extend(MENU_SELECTED_PALETTE_BASE.to_bytes(2, "big"))
    blank_address = address + len(result)
    result.extend(BLANK_TILE.to_bytes(2, "big"))
    if len(result) % 4:
        result.extend(b"\0\0")
    table_address = address + len(result)
    result.extend(RUNTIME_TABLE.to_bytes(4, "big"))
    resume_address = address + len(result)
    result.extend(MENU_RESUME.to_bytes(4, "big"))
    literals.update({"palette_base": palette_address, "blank": blank_address,
                     "table": table_address, "resume": resume_address})
    for index, item in enumerate(words):
        current = address + index * 2
        if isinstance(item, int):
            opcode = item
        else:
            width, register, name = item
            base = current + 4 if width == "word" else (current + 4) & ~3
            stride = 2 if width == "word" else 4
            delta = literals[name] - base
            if delta < 0 or delta % stride or delta // stride > 255:
                raise ValueError(f"SH-2 literal {name} is out of range")
            opcode = (0x9000 if width == "word" else 0xD000) | register << 8 | delta // stride
        result[index * 2:index * 2 + 2] = opcode.to_bytes(2, "big")
    return bytes(result)


RUNTIME_MENU_CODE = (RUNTIME_STATUS_DOUBLE_CODE + len(STATUS_DOUBLE_WRITER) + 3) & ~3
MENU_WRITER = menu_writer(RUNTIME_MENU_CODE)
NAME_ENTRY = 0x06012EA2
NAME_LITERAL = 0x06012EAC
NAME_RESUME = 0x06012EBA
NAME_TRAMPOLINE = bytes.fromhex("D002 402B 0009")


def name_loop(address: int, writer: int) -> bytes:
    instructions: list[int | tuple[str, str] | tuple[str, int, str]] = [
        0x2FB6, 0xEB00,
        # Count escape prefixes before drawing. A right-aligned byte field
        # needs one extra blank on the left per two-byte displayed glyph.
        ("label", "scan"),
        0x6093, 0x018C, 0x611C, 0xE07F, 0x3100, ("bf", "scan_next"),
        0x7BFF, 0x7801,
        ("label", "scan_next"),
        0x7801, 0x38A7, ("bf", "scan"),
        0xE800, 0x2BB8, ("bt", "loop"),
        ("label", "leading_blank"),
        0x6583, 0x7508, 0xE48A, 0x644C, 0xE615,
        ("long", 1, "writer"), 0x410B, 0x0009,
        0x7801, 0x6183, 0x31BC, 0x2118, ("bf", "leading_blank"),
        0xE800,
        ("label", "loop"),
        0x6583, 0x7508, 0x35B8, 0x6093, 0x048C, 0x644C,
        0xE17F, 0x3410, ("bf", "plain"),
        0x7801, 0x7B01, 0x6093, 0x048C, 0x644C,
        0xE1FF, 0x611C, 0x341C,
        ("label", "plain"),
        0xE615, ("long", 1, "writer"), 0x410B, 0x0009,
        0x7801, 0x38A7, ("bf", "loop"),
        0x2BB8, ("bt", "done"),
        0x6583, 0x7508, 0x35B8,
        ("label", "clear"),
        0xE48A, 0x644C, 0xE615, ("long", 1, "writer"),
        0x410B, 0x0009, 0x7501, 0x4B10, ("bf", "clear"),
        ("label", "done"),
        0x6BF6, 0x6583,
        0x0018, ("long", 0, "resume"), 0x402B, 0x0009,
    ]
    labels: dict[str, int] = {}
    words = []
    for item in instructions:
        if isinstance(item, tuple) and item[0] == "label":
            labels[item[1]] = address + len(words) * 2
        else:
            words.append(item)
    result = bytearray(len(words) * 2)
    if len(result) % 4:
        result.extend(b"\0\0")
    literals = {}
    for name, value in (("writer", writer), ("resume", NAME_RESUME)):
        literals[name] = address + len(result)
        result.extend(value.to_bytes(4, "big"))
    for index, item in enumerate(words):
        current = address + index * 2
        if isinstance(item, int):
            opcode = item
        elif item[0] in {"bf", "bt"}:
            distance = labels[item[1]] - (current + 4)
            if distance % 2 or not -256 <= distance <= 254:
                raise ValueError(f"SH-2 branch to {item[1]} is out of range")
            opcode = (0x8B00 if item[0] == "bf" else 0x8900) | ((distance // 2) & 0xFF)
        else:
            _, register, name = item
            base = (current + 4) & ~3
            distance = literals[name] - base
            if distance < 0 or distance % 4 or distance // 4 > 255:
                raise ValueError(f"SH-2 literal {name} is out of range")
            opcode = 0xD000 | register << 8 | distance // 4
        result[index * 2:index * 2 + 2] = opcode.to_bytes(2, "big")
    return bytes(result)


RUNTIME_NAME_CODE = (RUNTIME_MENU_CODE + len(MENU_WRITER) + 3) & ~3
NAME_LOOP = name_loop(RUNTIME_NAME_CODE, RUNTIME_STATUS_DOUBLE_CODE)
BYTE_ENTRY = 0x0601080C
BYTE_LITERAL = 0x06010838
BYTE_RESUME = 0x06010822
BYTE_TRAMPOLINE = bytes.fromhex("D00A 402B 0009")


def fixed_byte_loop(address: int, writer: int) -> bytes:
    instructions: list[int | tuple[str, str] | tuple[str, int, str]] = [
        0xED00,
        ("label", "loop"),
        0x65C3, 0x358C, 0x35D8, 0x6093, 0x048C, 0x644C,
        0xE17F, 0x3410, ("bf", "plain"),
        0x7801, 0x7D01, 0x6093, 0x048C, 0x644C,
        0xE1FF, 0x611C, 0x341C,
        ("label", "plain"),
        0x66B3, ("long", 1, "writer"), 0x410B, 0x0009,
        0x7801, 0x38A3, ("bf", "loop"),
        0x2DD8, ("bt", "done"),
        0x65C3, 0x35AC, 0x35D8,
        ("label", "clear"),
        0xE48A, 0x644C, 0x66B3, ("long", 1, "writer"),
        0x410B, 0x0009, 0x7501, 0x4D10, ("bf", "clear"),
        ("label", "done"),
        ("long", 0, "resume"), 0x402B, 0x0009,
    ]
    labels: dict[str, int] = {}
    words = []
    for item in instructions:
        if isinstance(item, tuple) and item[0] == "label":
            labels[item[1]] = address + len(words) * 2
        else:
            words.append(item)
    result = bytearray(len(words) * 2)
    if len(result) % 4:
        result.extend(b"\0\0")
    literals = {}
    for name, value in (("writer", writer), ("resume", BYTE_RESUME)):
        literals[name] = address + len(result)
        result.extend(value.to_bytes(4, "big"))
    for index, item in enumerate(words):
        current = address + index * 2
        if isinstance(item, int):
            opcode = item
        elif item[0] in {"bf", "bt"}:
            distance = labels[item[1]] - (current + 4)
            if distance % 2 or not -256 <= distance <= 254:
                raise ValueError(f"SH-2 branch to {item[1]} is out of range")
            opcode = (0x8B00 if item[0] == "bf" else 0x8900) | ((distance // 2) & 0xFF)
        else:
            _, register, name = item
            base = (current + 4) & ~3
            distance = literals[name] - base
            if distance < 0 or distance % 4 or distance // 4 > 255:
                raise ValueError(f"SH-2 literal {name} is out of range")
            opcode = 0xD000 | register << 8 | distance // 4
        result[index * 2:index * 2 + 2] = opcode.to_bytes(2, "big")
    return bytes(result)


RUNTIME_BYTE_CODE = (RUNTIME_NAME_CODE + len(NAME_LOOP) + 3) & ~3
BYTE_LOOP = fixed_byte_loop(RUNTIME_BYTE_CODE, RUNTIME_STATUS_DOUBLE_CODE)
MENU_UNSELECTED_ENTRY = 0x06006882
MENU_UNSELECTED_LITERAL = 0x06006894
MENU_UNSELECTED_RESUME = 0x0600689C
MENU_UNSELECTED_TRAMPOLINE = bytes.fromhex("D104 412B 0009")


def menu_unselected_writer(address: int) -> bytes:
    words: list[int | tuple[str, int, str]] = [
        ("word", 1, "flags"), 0x6373, 0x2319,
        0x627C, 0x727F, 0x7201,
        ("long", 1, "table"), 0x6023, 0x011C, 0x321C,
        ("word", 1, "base"), 0x321C,
        0x50E7, 0x0B25,
        ("word", 1, "blank"), 0x6313,
        ("long", 1, "resume"), 0x412B, 0x0009,
    ]
    result = bytearray(len(words) * 2)
    literals: dict[str, int] = {}
    for name, value in (("flags", 0x0F00), ("base", 0x1000),
                        ("blank", 0x1000 + BLANK_TILE)):
        literals[name] = address + len(result)
        result.extend(value.to_bytes(2, "big"))
    if len(result) % 4:
        result.extend(b"\0\0")
    for name, value in (("table", RUNTIME_TABLE),
                        ("resume", MENU_UNSELECTED_RESUME)):
        literals[name] = address + len(result)
        result.extend(value.to_bytes(4, "big"))
    for index, item in enumerate(words):
        current = address + index * 2
        if isinstance(item, int):
            opcode = item
        else:
            width, register, name = item
            base = current + 4 if width == "word" else (current + 4) & ~3
            stride = 2 if width == "word" else 4
            distance = literals[name] - base
            if distance < 0 or distance % stride or distance // stride > 255:
                raise ValueError(f"SH-2 literal {name} is out of range")
            opcode = (0x9000 if width == "word" else 0xD000) | register << 8 | distance // stride
        result[index * 2:index * 2 + 2] = opcode.to_bytes(2, "big")
    return bytes(result)


RUNTIME_MENU_UNSELECTED_CODE = (RUNTIME_BYTE_CODE + len(BYTE_LOOP) + 3) & ~3
MENU_UNSELECTED_WRITER = menu_unselected_writer(RUNTIME_MENU_UNSELECTED_CODE)


def offset(address: int) -> int:
    return address - EXECUTABLE_BASE


def patch_executable(data: bytes) -> bytes:
    result = bytearray(data)
    palette = result[MENU_TEXT_PALETTE_OFFSET:MENU_TEXT_PALETTE_OFFSET + 2]
    if palette != MENU_TEXT_PALETTE_ORIGINAL.to_bytes(2, "big"):
        raise ValueError(
            "Menu text palette literal differs from the verified gray value"
        )
    result[MENU_TEXT_PALETTE_OFFSET:MENU_TEXT_PALETTE_OFFSET + 2] = (
        MENU_TEXT_PALETTE_WHITE.to_bytes(2, "big")
    )
    if result[offset(ENTRY) : offset(ENTRY) + 6] != bytes.fromhex("2FE6 6EF3 644C"):
        raise ValueError("8x8 ASCII writer entry differs from the verified program")
    if result[offset(LITERAL) : offset(LITERAL) + 4] != bytes.fromhex("002E8000"):
        raise ValueError("8x8 ASCII writer literal differs from the verified program")
    if result[offset(WORD_ENTRY) : offset(WORD_ENTRY) + 6] != bytes.fromhex("2FE6 6EF3 644D"):
        raise ValueError("8x8 word writer entry differs from the verified program")
    if result[offset(WORD_LITERAL) : offset(WORD_LITERAL) + 4] != bytes.fromhex("002E8000"):
        raise ValueError("8x8 word writer literal differs from the verified program")
    if result[offset(DOUBLE_ENTRY):offset(DOUBLE_ENTRY) + 6] != bytes.fromhex("2FE6 6EF3 644D"):
        raise ValueError("8x16 writer entry differs from the verified program")
    if result[offset(DOUBLE_LITERAL):offset(DOUBLE_LITERAL) + 4] != bytes.fromhex("002E8000"):
        raise ValueError("8x16 writer literal differs from the verified program")
    if result[offset(MENU_ENTRY):offset(MENU_ENTRY) + 6] != bytes.fromhex("671D 9152 6273"):
        raise ValueError("Main-menu tile writer differs from the verified program")
    if result[offset(MENU_LITERAL):offset(MENU_LITERAL) + 4] != bytes.fromhex("617C 311C"):
        raise ValueError("Main-menu tile writer literal slot differs")
    if result[offset(NAME_ENTRY):offset(NAME_ENTRY) + 6] != bytes.fromhex("6583 7508 6093"):
        raise ValueError("MM name loop differs from the verified program")
    if result[offset(BYTE_ENTRY):offset(BYTE_ENTRY) + 6] != bytes.fromhex("DD0A 65C3 358C"):
        raise ValueError("Fixed byte writer differs from the verified program")
    if result[offset(BYTE_LITERAL):offset(BYTE_LITERAL) + 4] != bytes.fromhex("06035798"):
        raise ValueError("Fixed byte writer literal differs from the verified program")
    if result[offset(MENU_UNSELECTED_ENTRY):offset(MENU_UNSELECTED_ENTRY) + 6] != bytes.fromhex("917E 6373 2319"):
        raise ValueError("Unselected menu writer differs from the verified program")
    if result[offset(MENU_UNSELECTED_LITERAL):offset(MENU_UNSELECTED_LITERAL) + 4] != bytes.fromhex("9076 330C"):
        raise ValueError("Unselected menu writer literal slot differs")
    result[offset(ENTRY) : offset(ENTRY) + len(TRAMPOLINE)] = TRAMPOLINE
    result[offset(LITERAL) : offset(LITERAL) + 4] = RUNTIME_CODE.to_bytes(4, "big")
    result[offset(WORD_ENTRY) : offset(WORD_ENTRY) + len(WORD_TRAMPOLINE)] = WORD_TRAMPOLINE
    result[offset(WORD_LITERAL) : offset(WORD_LITERAL) + 4] = RUNTIME_WORD_CODE.to_bytes(4, "big")
    result[offset(DOUBLE_ENTRY):offset(DOUBLE_ENTRY) + len(DOUBLE_TRAMPOLINE)] = DOUBLE_TRAMPOLINE
    result[offset(DOUBLE_LITERAL):offset(DOUBLE_LITERAL) + 4] = RUNTIME_DOUBLE_CODE.to_bytes(4, "big")
    result[offset(MENU_ENTRY):offset(MENU_ENTRY) + len(MENU_TRAMPOLINE)] = MENU_TRAMPOLINE
    result[offset(MENU_LITERAL):offset(MENU_LITERAL) + 4] = RUNTIME_MENU_CODE.to_bytes(4, "big")
    result[offset(NAME_ENTRY):offset(NAME_ENTRY) + len(NAME_TRAMPOLINE)] = NAME_TRAMPOLINE
    result[offset(NAME_LITERAL):offset(NAME_LITERAL) + 4] = RUNTIME_NAME_CODE.to_bytes(4, "big")
    result[offset(BYTE_ENTRY):offset(BYTE_ENTRY) + len(BYTE_TRAMPOLINE)] = BYTE_TRAMPOLINE
    result[offset(BYTE_LITERAL):offset(BYTE_LITERAL) + 4] = RUNTIME_BYTE_CODE.to_bytes(4, "big")
    result[offset(MENU_UNSELECTED_ENTRY):offset(MENU_UNSELECTED_ENTRY) + len(MENU_UNSELECTED_TRAMPOLINE)] = MENU_UNSELECTED_TRAMPOLINE
    result[offset(MENU_UNSELECTED_LITERAL):offset(MENU_UNSELECTED_LITERAL) + 4] = RUNTIME_MENU_UNSELECTED_CODE.to_bytes(4, "big")
    return bytes(result)


def patch_main_font(data: bytes, deltas: bytes) -> bytes:
    result = bytearray(data)
    start = RUNTIME_TABLE - 0x00200000
    if any(result[start:]):
        raise ValueError("Reserved main-font tail is already allocated")
    if len(deltas) != TILE_DELTA_COUNT:
        raise ValueError("Packed font tile-delta table has an unexpected length")
    result[start:start + len(deltas)] = deltas
    result[start + len(deltas):start + len(deltas) + len(ASCII_WRITER)] = ASCII_WRITER
    result[start + len(deltas) + len(ASCII_WRITER):
           start + len(deltas) + len(ASCII_WRITER) + len(WORD_WRITER)] = WORD_WRITER
    double_start = RUNTIME_DOUBLE_CODE - 0x00200000
    if double_start + len(DOUBLE_WRITER) > len(result):
        raise ValueError("Main-font tail cannot hold the double-row writer")
    result[double_start:double_start + len(DOUBLE_WRITER)] = DOUBLE_WRITER
    status_double_start = RUNTIME_STATUS_DOUBLE_CODE - 0x00200000
    if status_double_start + len(STATUS_DOUBLE_WRITER) > len(result):
        raise ValueError("Main-font tail cannot hold the status double-row writer")
    result[status_double_start:status_double_start + len(STATUS_DOUBLE_WRITER)] = STATUS_DOUBLE_WRITER
    menu_start = RUNTIME_MENU_CODE - 0x00200000
    if menu_start + len(MENU_WRITER) > len(result):
        raise ValueError("Main-font tail cannot hold the menu writer")
    result[menu_start:menu_start + len(MENU_WRITER)] = MENU_WRITER
    name_start = RUNTIME_NAME_CODE - 0x00200000
    if name_start + len(NAME_LOOP) > len(result):
        raise ValueError("Main-font tail cannot hold the MM name loop")
    result[name_start:name_start + len(NAME_LOOP)] = NAME_LOOP
    byte_start = RUNTIME_BYTE_CODE - 0x00200000
    if byte_start + len(BYTE_LOOP) > len(result):
        raise ValueError("Main-font tail cannot hold the fixed byte writer")
    result[byte_start:byte_start + len(BYTE_LOOP)] = BYTE_LOOP
    menu_unselected_start = RUNTIME_MENU_UNSELECTED_CODE - 0x00200000
    if menu_unselected_start + len(MENU_UNSELECTED_WRITER) > len(result):
        raise ValueError("Main-font tail cannot hold the unselected menu writer")
    result[menu_unselected_start:menu_unselected_start + len(MENU_UNSELECTED_WRITER)] = MENU_UNSELECTED_WRITER
    return bytes(result)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "temp/packed-small-font")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "0.ascii-probe").write_bytes(patch_executable(SOURCE.read_bytes()))
    (output / "ASCII.PAC.entry00.ascii-probe.bin").write_bytes(
        patch_main_font(MAIN_FONT.read_bytes(), TILE_DELTAS.read_bytes())
    )
    print(f"Wrote experimental 8x8 character writers and main-font payload: {output}")


if __name__ == "__main__":
    main()
