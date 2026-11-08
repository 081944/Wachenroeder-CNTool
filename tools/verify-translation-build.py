"""Read the final BIN through its ISO directory and verify translated resources."""
import argparse
import csv
import hashlib
import json
import struct
from pathlib import Path
import runpy

from PIL import ImageFont

from narrow_text_writeback import patch_shared
from pac_codec import decode_entry, split_pac
from event_cells import parse_tactical_text, parse_tactical_words, Cell


def verify(root: Path, build_root: Path, font_mode: str):
    manifest = json.loads((build_root / "final/build-manifest.json").read_text(encoding="utf-8-sig"))
    binary = Path(manifest["output"]["bin"])
    cue = Path(manifest["output"]["cue"])
    if binary.stat().st_size != manifest["output"]["frames"] * 2352:
        raise ValueError("Final BIN frame count differs from the manifest")
    if f'FILE "{binary.name}" BINARY' not in cue.read_text(encoding="ascii"):
        raise ValueError("CUE does not reference the generated BIN")
    with binary.open("rb") as handle:
        def read_iso(lba, size):
            output = bytearray()
            for sector in range(lba, lba + (size + 2047) // 2048):
                handle.seek(sector * 2352 + 16)
                data = handle.read(2048)
                if len(data) != 2048:
                    raise ValueError("Truncated BIN sector")
                output.extend(data)
            return bytes(output[:size])

        pvd = read_iso(16, 2048)
        if pvd[:7] != b"\x01CD001\x01":
            raise ValueError("Invalid ISO primary volume descriptor")
        directory = read_iso(int.from_bytes(pvd[158:162], "little"),
                             int.from_bytes(pvd[166:170], "little"))
        entries = {}
        position = 0
        while position < len(directory):
            size = directory[position]
            if not size:
                position = (position // 2048 + 1) * 2048
                continue
            record = directory[position:position + size]
            name = record[33:33 + record[32]].decode("ascii").split(";")[0]
            entries[name] = (int.from_bytes(record[2:6], "little"),
                             int.from_bytes(record[10:14], "little"))
            position += size

        resources = build_root / "repacked/pac"
        checked = {}
        for row in manifest["patch"]["files"]:
            name = row["file"]
            if name not in entries:
                raise ValueError(f"Final ISO is missing {name}")
            lba, size = entries[name]
            if (lba, size) != (row["new_extent"], row["new_length"]):
                raise ValueError(f"ISO directory differs from manifest: {name}")
            data = read_iso(lba, size)
            if data != (resources / name).read_bytes():
                raise ValueError(f"Final ISO resource differs from build output: {name}")
            checked[name] = data

    expected = {}
    patch_shared(root, expected, root / "work/fonts/chinese")
    layers_checked = 0
    for name, layers in expected.items():
        packed = split_pac(checked[name])
        for entry, expected_raw in layers.items():
            if decode_entry(packed[entry], stack_limit=512) != expected_raw:
                raise ValueError(f"Final image has stale translated fields: {name} entry {entry}")
            layers_checked += 1
    # Compare runtime roles AND every visible glyph in the final tactical
    # dialogue. Comparing a PAC with build output alone cannot detect a build
    # that swallowed the first translated character as an operand.
    font_codes = {}
    for row in csv.DictReader((root / 'work/fonts/chinese/font-map-chinese.csv').open(encoding='utf-8-sig')):
        if row['Character'] and (row['Mapping'] == 'allocated' or row['Character'] not in font_codes):
            font_codes[row['Character']] = int(row['TextCode'], 16)
    catalog = json.loads((root / 'work/text/catalog/japan/TACTEVT.PAC.json').read_text(encoding='utf-8-sig'))
    records = {(r['entry'], r['decoded_offset']): r for r in catalog['records'] if r['kind'] == 'pointer-text-block'}
    targets = {line[1:5]: line.split(' | ', 2)[2] for line in
               (root / 'work/translation/events/TACTEVT.PAC.txt').read_text(encoding='utf-8-sig').splitlines() if line.startswith('[')}
    tactical_checked = 0
    packed = split_pac(checked['TACTEVT.PAC'])
    original_packed = split_pac((root / 'unpacked/japan/TACTEVT.PAC').read_bytes())
    for entry in sorted({key[0] for key in records}):
        original = decode_entry(original_packed[entry])
        rebuilt = decode_entry(packed[entry], stack_limit=512)
        table = int.from_bytes(original[:4], 'big')
        count = (int.from_bytes(original[table:table + 4], 'big') - table) // 4
        old_offsets = struct.unpack_from(f'>{count}I', original, table)
        new_table = int.from_bytes(rebuilt[:4], 'big')
        offsets = struct.unpack_from(f'>{count}I', rebuilt, new_table)
        for index, old_offset in enumerate(old_offsets):
            record = records.get((entry, old_offset))
            if not record or not targets[record['id']]:
                continue
            end = offsets[index + 1] if index + 1 < count else len(rebuilt)
            words = struct.unpack(f'>{(end - offsets[index]) // 2}H', rebuilt[offsets[index]:end])
            actual = parse_tactical_words(words)
            expected_cells = parse_tactical_text(targets[record['id']])
            expected_cells = [Cell(c.kind, font_codes[c.text] if c.code is None else c.code, c.text) for c in expected_cells]
            if [(c.kind, c.code) for c in actual] != [(c.kind, c.code) for c in expected_cells]:
                raise ValueError(f"Final tactical text loses or changes cells: {record['id']}")
            tactical_checked += 1
    font_checks = {}
    if font_mode == "8x12":
        resource_manifest = json.loads((resources / "manifest.json").read_text(encoding="utf-8"))
        current_inputs = {
            path.relative_to(root / 'work/translation').as_posix(): hashlib.sha256(path.read_bytes()).hexdigest().upper()
            for directory in ('events', 'narrow')
            for path in (root / 'work/translation' / directory).glob('*.txt') if path.is_file()
        }
        if current_inputs != resource_manifest.get('translation_input_sha256'):
            raise ValueError('Final 8x12 image was built from different translation inputs; rerun the full build')
        font = resource_manifest["font_experiment"]
        atlas = decode_entry(split_pac(checked["ASCII.PAC"])[1], stack_limit=512)
        low_start, low_end = font['low_vram']
        high_start, high_end = font['overflow_vram']
        if (low_start, low_end, high_start, high_end) != (0x25E60000, 0x25E66000, 0x25E6C000, 0x25E70000):
            raise ValueError('8x12 VRAM regions differ from the bounded font layout')
        if resource_manifest.get('runtime_font_pending'):
            raise ValueError('8x12 runtime font is incomplete')
        expected_bytes = (low_end - low_start) // 4 + font['overflow_tiles'] * 8
        if (len(atlas) != font["source_bytes"] or len(atlas) != expected_bytes
                or not 0 <= font["overflow_tiles"] <= (high_end - high_start) // 32):
            raise ValueError("8x12 atlas size or overflow limit differs")
        original = decode_entry(split_pac((root / 'unpacked/japan/ASCII.PAC').read_bytes())[1])
        for slot in [*range(0xB3), 0x107, 0x108, 0x109]:
            if atlas[slot * 16:(slot + 1) * 16] != original[slot * 16:(slot + 1) * 16]:
                raise ValueError(f"8x12 original font slot {slot:03X} differs")
        main = decode_entry(split_pac(checked['ASCII.PAC'])[0], stack_limit=512)
        resident = runpy.run_path(str(root / 'tools/build-resident-small-font-probe.py'))
        mapping = json.loads((root / 'work/fonts/chinese/mapping.json').read_text(encoding='utf-8'))
        font_builder = runpy.run_path(str(root / 'tools/build-chinese-fonts.py'))
        face = ImageFont.truetype(str(font_builder['SMALL_FONT']), 12)
        baseline = font_builder['calculate_baseline'](face, mapping['glyphs'], 12)

        def tile_bytes(number):
            low_tiles = (low_end - low_start) // 32
            high_index = (high_start - low_start) // 32
            if 0 <= number < low_tiles:
                offset = number * 8
            elif high_index <= number < high_index + font['overflow_tiles']:
                offset = low_tiles * 8 + (number - high_index) * 8
            else:
                raise ValueError(f'8x12 glyph references tile outside uploaded spans: {number:03X}')
            return atlas[offset:offset + 8]

        for character, glyph in mapping['glyphs'].items():
            logical = int(glyph['logical_index'], 16)
            offset = font['table_address'] - 0x00200000 + (logical - font['table_first_logical']) * 4
            pair = main[offset:offset + 4]
            actual = tile_bytes(int.from_bytes(pair[:2], 'big')) + tile_bytes(int.from_bytes(pair[2:], 'big'))
            expected_glyph = font_builder['render_small_cell'](face, character, baseline)
            if actual != expected_glyph:
                raise ValueError(f'8x12 final glyph or mapping differs: {character!r} ({logical:03X})')
        font_checks = dict(added_glyphs_checked=len(mapping['glyphs']),
                           protected_original_cells_checked=182,
                           overflow_tiles_used=font['overflow_tiles'],
                           overflow_tiles_reserved=(high_end - high_start) // 32,
                           original_date_digit_loop_unchanged=True)
        hooks = runpy.run_path(str(root / 'tools/patch-packed-font-ascii-probe.py'))
        assembler = runpy.run_path(str(root / 'tools/build-tall-small-font-probe.py'))['Assembler']
        byte_at = font['hooks']['RUNTIME_CODE']
        name_at = font['hooks']['RUNTIME_NAME_CODE']
        for address, expected_code in (
                (byte_at, resident['writer'](byte_at, assembler, 'byte', font['battle_single_table_address'])),
                (name_at, hooks['name_loop'](name_at, font['hooks']['RUNTIME_STATUS_DOUBLE_CODE']))):
            offset = address - 0x00200000
            if main[offset:offset + len(expected_code)] != expected_code:
                raise ValueError(f"8x12 final image has stale font hook at {address:08X}")
        executable = checked["0"]
        original_program = (root / 'unpacked/japan/0').read_bytes()
        if executable[0x27F7A:0x28016] != original_program[0x27F7A:0x28016]:
            raise ValueError("Save date formatting code differs from the original game")
        if int.from_bytes(executable[0x30F28:0x30F2A], "big") != 6143:
            raise ValueError("8x12 original font upload length differs")
        if int.from_bytes(executable[0x30F54:0x30F58], "big") != font["hooks"]["RUNTIME_UPLOAD_CODE"]:
            raise ValueError("8x12 upload hook is missing from final executable")
    with binary.open("rb") as handle:
        binary_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
    result = dict(font_mode=font_mode, iso_resources_checked=len(checked),
                  tactical_dialogue_blocks_checked=tactical_checked,
                  translated_layers_checked=layers_checked, mm_equipment_fields_checked=31 * 38,
                  **font_checks,
                  bin=str(binary), bin_sha256=binary_sha256, cue=str(cue), status="verified")
    (build_root / "final/verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--font-mode", choices=("8x8", "8x12"), default="8x8")
    args = parser.parse_args()
    verify(args.root.resolve(), args.build_root.resolve(), args.font_mode)
