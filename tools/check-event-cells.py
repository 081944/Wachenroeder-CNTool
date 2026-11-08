#!/usr/bin/env python3
"""Validate every translated text layer against the runtime cell model.

This is the Python half of the translation gate.  ``validate-translation.ps1``
keeps the encoding checks; this script owns the *cell roles*, so extraction,
validation and writeback read one rule set (``event_cells``) instead of three
slightly different ones.

Hard failures (exit 1):
  * a control tag in the translation no longer matches the source sequence
  * an id/window cell sits outside the leading position
  * a glyph code falls outside the built font

Reported, not fatal:
  * anchor drift: the number of visible cells before a control cell changed
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from event_cells import (  # noqa: E402
    GLYPH_MAX,
    GLYPH_MIN,
    CellError,
    anchors,
    assert_body,
    control_signature,
    control_signature_prose,
    parse_text,
    parse_tactical_text,
    parse_tactical_words,
    dialogue_layout_errors,
)

POINTER_KINDS = ("pointer-block", "pointer-text-block")
VALUE_SEPARATOR = " | "


def load_font_codes(path: Path) -> set[int]:
    codes: set[int] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            text = (row.get("TextCode") or "").strip()
            if not text:
                continue
            try:
                codes.add(int(text, 16))
            except ValueError:
                pass
    return codes


def split_target(line: str) -> str | None:
    first = line.find(VALUE_SEPARATOR)
    if first < 0:
        return None
    second = line.find(VALUE_SEPARATOR, first + len(VALUE_SEPARATOR))
    if second < 0:
        return None
    return line[second + len(VALUE_SEPARATOR):]


def line_lengths(cells) -> list[int]:
    """Visible cells per displayed line.

    0x0000 breaks the line, 0x0001 starts a new page and 0x0002-0x0005 end the
    message; all of them close the current line.
    """
    counts: list[int] = []
    width = 0
    for cell in cells:
        if cell.kind == "G":
            width += 1
        elif cell.kind == "C" and cell.code is not None and cell.code <= 5:
            counts.append(width)
            width = 0
    if width:
        counts.append(width)
    return counts


def overwide(counts: list[int], line_max: int = 15, third_max: int = 14) -> list[str]:
    problems: list[str] = []
    for index, width in enumerate(counts, 1):
        limit = third_max if (len(counts) == 3 and index == 3) else line_max
        if width > limit:
            problems.append(f"line {index}={width}>{limit}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--catalog", default="work/text/catalog/japan")
    parser.add_argument("--events", default="work/translation/events")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--drift-limit", type=int, default=15)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--line-width", action="store_true",
                        help="reject dialogue page/row overflow, including tactical dialogue")
    args = parser.parse_args()

    root = args.root.resolve()
    font_path = root / "work/fonts/chinese/font-map-chinese.csv"
    if not font_path.exists():
        font_path = root / "analysis/decoded-samples/font-map.csv"
    font_codes = load_font_codes(font_path)

    errors: list[str] = []
    drift_rows: list[tuple] = []
    wide_rows: list[tuple] = []
    wide_by_file = Counter()
    checked = 0
    translated = 0
    drift_total = 0
    reflowed = 0
    by_file = Counter()

    catalog_dir = root / args.catalog
    events_dir = root / args.events
    for catalog_path in sorted(catalog_dir.glob("*.json")):
        name = catalog_path.stem
        template = events_dir / f"{name}.txt"
        if not template.exists():
            continue
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        lines = template.read_text(encoding="utf-8-sig").splitlines()
        rows = [line for line in lines if line and not line.startswith("#")]
        records = catalog["records"]
        if len(rows) != len(records):
            errors.append(f"{name}: {len(rows)} rows for {len(records)} records")
            continue
        for index, (record, line) in enumerate(zip(records, rows)):
            if record.get("kind") not in POINTER_KINDS:
                continue
            target = split_target(line)
            if not target or not target.strip():
                continue
            checked += 1
            translated += 1
            label = f"{name}[{record['id']}] line {index + 2}"
            source_text = record.get("source_text") or ""
            try:
                if name == 'TACTEVT.PAC':
                    src = parse_tactical_words([int(x, 16) for x in record['source_codes'].split()])
                    dst = parse_tactical_text(target)
                else:
                    src = parse_text(source_text)
                    dst = parse_text(target)
            except CellError as error:
                errors.append(f"{label}: {error}")
                continue

            try:
                assert_body(dst, label)
            except CellError as error:
                errors.append(str(error))
                continue

            src_sig = control_signature(src)
            dst_sig = control_signature(dst)
            # Line breaks are a pure cursor move, so a translation is allowed
            # to use fewer (or more) rows than the Japanese source.  Everything
            # else -- speaker cell, 0001-0007 and their operands -- must match
            # in order and value.
            if control_signature_prose(src) != control_signature_prose(dst):
                errors.append(
                    f"{label}: control sequence changed\n"
                    f"    source {src_sig}\n"
                    f"    target {dst_sig}"
                )
                continue
            if src_sig != dst_sig:
                reflowed += 1

            bad = [
                cell.code
                for cell in dst
                if cell.kind == "G"
                and cell.code is not None
                and not (GLYPH_MIN <= cell.code <= GLYPH_MAX)
            ]
            if bad:
                errors.append(f"{label}: glyph codes outside the font: {[hex(c) for c in bad]}")
                continue
            unknown = [
                cell.code
                for cell in dst
                if cell.kind == "G" and cell.code is not None and cell.code not in font_codes
            ]
            if unknown:
                errors.append(
                    f"{label}: glyph codes with no font slot: {[hex(c) for c in unknown]}"
                )
                continue

            src_anchor = anchors(src)
            dst_anchor = anchors(dst)
            drift = [
                (a[0], a[1], a[2], b[2])
                for a, b in zip(src_anchor, dst_anchor)
                if a[2] != b[2]
            ]
            if drift:
                drift_total += len(drift)
                by_file[name] += 1
                if len(drift_rows) < 200:
                    drift_rows.append((label, drift[:4], target[:60]))

            # The 15/15/14 model describes the dialogue window.  Status bars and
            # save-slot prompts are whole-line fields the engine draws itself,
            # so enforce it on EVTTK pointer blocks and tactical dialogue.
            if args.line_width and (record.get("kind") == "pointer-block" or name == 'TACTEVT.PAC'):
                problems = dialogue_layout_errors(src, dst)
                if problems:
                    wide_rows.append((label, ", ".join(problems), target[:60]))
                    wide_by_file[name] += 1
                    errors.append(f"{label}: " + ', '.join(problems))

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["label", "control", "source_anchor", "target_anchor"])
            for label, drift, _ in drift_rows:
                for kind, code, src_anchor, dst_anchor in drift:
                    writer.writerow([label, f"{kind}:{code:04X}", src_anchor, dst_anchor])

    if not args.quiet:
        print(
            f"cell check: {translated} translated pointer rows, "
            f"{len(errors)} hard error(s), {drift_total} anchor drift(s) "
            f"in {sum(by_file.values())} row(s), {reflowed} row(s) with reflowed line breaks"
        )
        if args.line_width:
            print(f"  over-wide lines: {len(wide_rows)} row(s) {wide_by_file.most_common(8)}")
            for label, detail, snippet in wide_rows[:12]:
                print(f"  {label}: {detail}  | {snippet}")
        if drift_rows:
            print("  anchor drift by file:", by_file.most_common(8))
            for label, drift, snippet in drift_rows[: args.drift_limit]:
                desc = ", ".join(f"{k}:{c:04X} {s}->{t}" for k, c, s, t in drift)
                print(f"  {label}: {desc}  | {snippet}")
        for error in errors[:40]:
            print(f"ERROR {error}")
        if len(errors) > 40:
            print(f"... {len(errors) - 40} more error(s)")

    for error in errors:
        print(f"::error::{error.splitlines()[0]}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
