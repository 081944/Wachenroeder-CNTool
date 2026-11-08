#!/usr/bin/env python3
"""Authoritative cell model for the Wachenroeder text layers.

Text layers are streams of 16-bit words. The event-body decoder dispatches on
word value; TACTEVT additionally has one unconditional leading speaker word
(use parse_tactical_words/parse_tactical_text for that resource):

* ``0x0000``-``0x0007``  commands; ``0x0006``/``0x0007`` consume the next word
  as an operand that is never drawn.
* ``0x0020``-``0x7FFF``  glyph cells.  The renderer looks the value up in the
  text-code font and draws the bitmap it finds there.
* ``0x0008``-``0x001F`` and ``0x8000``-  speaker/window cells.  Legal only as
  the first word of a block (or as a 6/7 operand).  Anywhere else the renderer
  treats them as glyph cells, finds no bitmap, and draws garbage -- this is the
  "control code shows up as text" failure.

The first word is special only for the two id ranges.  A block whose first word
is already a glyph cell simply has no speaker header and starts with text.

Every other tool imports this module so extraction, validation and writeback
cannot drift apart again.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CMD_MAX = 0x07
MARKER_COMMANDS = (0x06, 0x07)
GLYPH_MIN = 0x20
GLYPH_MAX = 0xD72
ID_LOW_MAX = 0x1F
ID_HIGH_MIN = 0x8000

TAG = re.compile(r"<([HCPG]):([0-9A-F]{4})>|<([0-9A-F]{4})>|([\s\S])")

# Exported labels map onto the same four runtime roles.  H and I both mean
# "identifier cell"; the letter only records where the exporter saw it.
LABEL_KIND = {"H": "I", "I": "I", "C": "C", "P": "P", "G": "G"}


class CellError(ValueError):
    """A layer violates the runtime cell rules."""


@dataclass(frozen=True)
class Cell:
    kind: str          # 'I' identifier, 'C' command, 'P' operand, 'G' glyph
    code: int | None   # None for a literal character resolved through the font map
    text: str          # the source spelling, for error messages

    @property
    def visible(self) -> bool:
        return self.kind == "G"


def is_id_word(word: int) -> bool:
    return word <= ID_LOW_MAX or word >= ID_HIGH_MIN


def is_glyph_word(word: int) -> bool:
    return GLYPH_MIN <= word < ID_HIGH_MIN


def parse_words(words: list[int] | tuple[int, ...], *, mm_pointer_text: bool = False) -> list[Cell]:
    """Classify a stored word stream.

    Command range wins over the leading-position rule: 0x0000-0x0007 are
    unambiguous commands, and several layers open with <0000> (a line break)
    rather than a speaker header.
    """
    cells: list[Cell] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word <= CMD_MAX:
            cells.append(Cell("C", word, f"<{word:04X}>"))
            if word in MARKER_COMMANDS and not mm_pointer_text and index + 1 < len(words):
                index += 1
                operand = words[index]
                cells.append(Cell("P", operand, f"<{operand:04X}>"))
            index += 1
            continue
        if index == 0 and is_id_word(word):
            cells.append(Cell("I", word, f"<{word:04X}>"))
            index += 1
            continue
        if is_glyph_word(word):
            cells.append(Cell("G", word, f"<{word:04X}>"))
            index += 1
            continue
        raise CellError(f"word {index} is <{word:04X}>, an id cell outside the leading position")
    return cells


def parse_words_marker_aware(words: list[int] | tuple[int, ...]) -> list[Cell]:
    """Classify a stream where 6/7 consume their operand in place."""
    return parse_words(words)


def parse_tactical_words(words: list[int] | tuple[int, ...]) -> list[Cell]:
    """TACTEVT has an unconditional speaker word followed by event commands."""
    if not words:
        raise CellError('empty tactical block')
    return [Cell('I', words[0], f'<H:{words[0]:04X}>')] + parse_words(words[1:])


def dialogue_pages(cells: list[Cell]) -> list[list[int]]:
    """Visible widths by page; 0001 resets the display row to the first row."""
    pages: list[list[int]] = []
    rows = [0]
    for cell in cells:
        if cell.kind == 'G':
            rows[-1] += 1
        elif cell.kind == 'C':
            if cell.code == 0:
                rows.append(0)
            elif cell.code in (1, 2, 3, 4, 5):
                pages.append(rows)
                rows = [0]
    if any(rows):
        pages.append(rows)
    return pages


def dialogue_layout_errors(source: list[Cell], target: list[Cell]) -> list[str]:
    original, translated = dialogue_pages(source), dialogue_pages(target)
    errors = []
    if len(original) != len(translated):
        errors.append(f'page count {len(translated)} differs from source {len(original)}')
    for page, (old, new) in enumerate(zip(original, translated), 1):
        if len(new) > 3:
            errors.append(f'page {page} has {len(new)} rows, maximum 3')
        budget = max(15, max(old))
        for row, width in enumerate(new, 1):
            limit = budget - 1 if row == 3 else budget
            if width > limit:
                errors.append(f'page {page} row {row}={width}>{limit}')
    return errors


def parse_tactical_text(text: str) -> list[Cell]:
    """Require explicit operands; never silently consume a translated glyph."""
    match = TAG.match(text)
    if not match:
        raise CellError('empty tactical block')
    header = parse_text(match.group())[0]
    if header.code is None:
        raise CellError('tactical speaker must be an explicit word')
    cells = [Cell('I', header.code, header.text)] + parse_text(text[match.end():])
    for i, cell in enumerate(cells[1:], 1):
        if cell.kind == 'C' and cell.code in MARKER_COMMANDS:
            if i + 1 >= len(cells) or cells[i + 1].kind != 'P':
                raise CellError(f'{cell.text} lacks an explicit <P:XXXX> operand before text')
    return cells


def parse_text(text: str) -> list[Cell]:
    """Classify a translation line.

    Literal characters become glyph cells with ``code=None``; the caller
    resolves them through the font map when a concrete code is needed.  A 6/7
    marker consumes the following ``<XXXX>`` tag as its operand, and a bare tag
    in the leading position is an identifier cell, so a translation that copies
    the exported source spelling classifies exactly like the stored words.
    """
    raw: list[tuple[str, int | None, str]] = []
    for match in TAG.finditer(text):
        label, prefixed, bare, char = match.groups()
        if label:
            raw.append((LABEL_KIND[label], int(prefixed, 16), match.group(0)))
        elif bare:
            raw.append(("?", int(bare, 16), match.group(0)))
        else:
            raw.append(("G", None, char))

    cells: list[Cell] = []
    pending_operand = False
    for index, (kind, code, spelling) in enumerate(raw):
        if pending_operand:
            # Only a bare tag can be a marker operand.  A literal character, or
            # an explicitly prefixed <G:XXXX>, means the marker did not consume
            # one -- the export uses that same distinction to decide whether
            # the following word is drawn or swallowed.
            if kind == "?":
                pending_operand = False
                cells.append(Cell("P", code, spelling))
                continue
            pending_operand = False
        if kind == "?":
            assert code is not None
            if code <= CMD_MAX:
                cells.append(Cell("C", code, spelling))
                if code in MARKER_COMMANDS:
                    pending_operand = True
            elif index == 0 and is_id_word(code):
                cells.append(Cell("I", code, spelling))
            elif is_glyph_word(code):
                cells.append(Cell("G", code, spelling))
            else:
                cells.append(Cell("!", code, spelling))
            continue
        cells.append(Cell(kind, code, spelling))
        if kind == "C" and code in MARKER_COMMANDS:
            pending_operand = True
    return cells


def control_signature(cells: list[Cell]) -> list[tuple[str, int]]:
    """The identifier/command/operand sequence, with operands kept distinct."""
    return [(c.kind, c.code) for c in cells if c.kind != "G" and c.code is not None]


def control_signature_prose(cells: list[Cell]) -> list[tuple[str, int]]:
    """Control sequence with line breaks removed.

    ``0x0000`` only moves the cursor to column 0 on the next text row; it holds
    no counter and sets no status bit.  Chinese is shorter than Japanese, so a
    translation often needs fewer rows than the source, and padding a sentence
    with a break it does not need is exactly what makes a box read like broken
    speech.  Every other control cell still has to match one for one -- dropping
    0x0001-0x0007 or the speaker cell would change how the block advances.
    """
    return [
        (c.kind, c.code)
        for c in cells
        if c.kind != "G" and c.code is not None and not (c.kind == "C" and c.code == 0)
    ]


def anchors(cells: list[Cell]) -> list[tuple[str, int, int]]:
    """Each control cell with the number of visible cells that precede it."""
    seen = 0
    result: list[tuple[str, int, int]] = []
    for cell in cells:
        if cell.kind == "G":
            seen += 1
        elif cell.code is not None:
            result.append((cell.kind, cell.code, seen))
    return result


def assert_body(cells: list[Cell], label: str) -> None:
    """Reject id/window cells that drifted out of the leading position."""
    for index, cell in enumerate(cells):
        if cell.kind != "!":
            continue
        raise CellError(
            f"{label}: cell {index + 1} is <{cell.code:04X}>, an id/window code; "
            "only the leading cell or a 6/7 operand may hold it"
        )
    if cells and cells[0].kind == "P":
        raise CellError(f"{label}: block starts with a marker operand")


def glyph_codes(cells: list[Cell]) -> list[int]:
    return [c.code for c in cells if c.kind == "G" and c.code is not None]


def literal_characters(cells: list[Cell]) -> list[str]:
    return [c.text for c in cells if c.kind == "G" and c.code is None]
