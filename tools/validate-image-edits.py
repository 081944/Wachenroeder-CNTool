#!/usr/bin/env python3
"""Validate edited PNGs against the real image writeback pipeline."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
SKIPPED_EDIT_DIRECTORIES = frozenset({"menu-sprites"})


def load_build_module(root: Path) -> Any:
    tools_dir = root / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    module_path = tools_dir / "build-translation-pacs.py"
    spec = importlib.util.spec_from_file_location("build_translation_pacs", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def patch_label(patch: dict[str, object]) -> str:
    png = Path(str(patch["png"]))
    kind = str(patch.get("kind", "unknown"))
    fields = [kind]
    for key in ("file", "entry", "top_entry", "group", "inner_entry", "sprite"):
        if key in patch and patch[key] not in (None, ""):
            fields.append(f"{key}={patch[key]}")
    if "row" in patch:
        row = patch["row"]
        if isinstance(row, dict):
            fields = [str(row.get("Kind", "title-image"))]
            for key in ("File", "TopEntry", "InnerEntry", "Sprite"):
                if row.get(key) not in (None, ""):
                    fields.append(f"{key}={row[key]}")
    return f"{png} ({', '.join(fields)})"


def add_issue(
    issues: list[dict[str, str]],
    phase: str,
    reason: str,
    patch: dict[str, object] | None = None,
) -> None:
    item = {
        "phase": phase,
        "path": str(patch["png"]) if patch is not None else "",
        "target": patch_label(patch) if patch is not None else "image set",
        "reason": " ".join(str(reason).splitlines()).strip(),
    }
    if item not in issues:
        issues.append(item)


def check_one_patch(
    build: Any,
    root: Path,
    source_dir: Path,
    patch: dict[str, object],
) -> None:
    if "row" in patch:
        build.apply_title_image_patches(
            root,
            source_dir,
            {},
            patches=[patch],
        )
    else:
        build.apply_image_patches(
            root,
            source_dir,
            {},
            {},
            {},
            patches=[patch],
        )


def check_patch_group(
    build: Any,
    root: Path,
    source_dir: Path,
    patches: list[dict[str, object]],
    title: bool,
) -> None:
    if not patches:
        return
    if title:
        build.apply_title_image_patches(root, source_dir, {}, patches=patches)
    else:
        build.apply_image_patches(root, source_dir, {}, {}, {}, patches=patches)


def patch_group_key(patch: dict[str, object], title: bool) -> tuple[str, ...]:
    if title:
        row = patch["row"]
        assert isinstance(row, dict)
        return (
            "title",
            str(row.get("TopEntry", "")),
            str(row.get("InnerEntry", "")),
        )
    kind = str(patch.get("kind", ""))
    if kind == "menu-sprite":
        return (kind, str(patch.get("file", "")), str(patch.get("top_entry", "")))
    if kind == "bup-entry02-sprite":
        return (
            kind,
            str(patch.get("file", "")),
            str(patch.get("entry", "")),
            str(patch.get("group", "")),
        )
    return (
        kind,
        str(patch.get("file", "")),
        str(patch.get("entry", patch.get("top_entry", ""))),
    )


def is_skipped_edit_path(path: Path, edited_root: Path) -> bool:
    """Exclude duplicate image sets that are not part of validation."""
    try:
        relative = path.resolve().relative_to(edited_root.resolve())
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] in SKIPPED_EDIT_DIRECTORIES


def filter_skipped_patches(
    patches: list[dict[str, object]], edited_root: Path
) -> list[dict[str, object]]:
    return [
        patch
        for patch in patches
        if not is_skipped_edit_path(Path(str(patch["png"])), edited_root)
    ]


def validate_group_recursive(
    build: Any,
    root: Path,
    source_dir: Path,
    patches: list[dict[str, object]],
    title: bool,
    issues: list[dict[str, str]],
) -> None:
    """Validate a shared resource once, splitting only when that group fails."""
    try:
        check_patch_group(build, root, source_dir, patches, title)
        return
    except Exception as exc:
        group_error = repr(exc)

    if len(patches) == 1:
        add_issue(issues, "image", group_error, patches[0])
        return

    before = len(issues)
    midpoint = len(patches) // 2
    validate_group_recursive(
        build, root, source_dir, patches[:midpoint], title, issues
    )
    validate_group_recursive(
        build, root, source_dir, patches[midpoint:], title, issues
    )
    if len(issues) == before:
        target = patch_label(patches[0])
        add_issue(
            issues,
            "shared-image-state",
            f"{target} and {len(patches) - 1} other images: {group_error}",
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check edited image PNGs without building PAC or disc images."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=SCRIPT_ROOT,
        help="project root (default: repository containing this script)",
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="also write the validation report as UTF-8 JSON",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    source_dir = root / "unpacked" / "japan"
    issues: list[dict[str, str]] = []
    normal_patches: list[dict[str, object]] = []
    title_patches: list[dict[str, object]] = []

    try:
        build = load_build_module(root)
    except Exception as exc:
        add_issue(issues, "load-builder", repr(exc))
        build = None

    if build is not None:
        for phase, loader, target in (
            ("load-standard-images", build.load_image_patches, normal_patches),
            ("load-candidate-images", build.load_candidate_image_patches, normal_patches),
            ("load-title-images", build.load_title_image_patches, title_patches),
        ):
            try:
                target.extend(loader(root))
            except Exception as exc:
                add_issue(issues, phase, repr(exc))

    patches = normal_patches + title_patches
    edited_root = root / "work" / "imageedit"
    imageori_root = root / "work" / "imageori"
    all_edited_paths = {
        path.resolve()
        for path in edited_root.rglob("*.png")
        if path.is_file()
    } if edited_root.is_dir() else set()
    edited_paths = {
        path
        for path in all_edited_paths
        if not is_skipped_edit_path(path, edited_root)
    }
    skipped_edited_paths = all_edited_paths - edited_paths
    normal_patches = filter_skipped_patches(normal_patches, edited_root)
    title_patches = filter_skipped_patches(title_patches, edited_root)
    patches = normal_patches + title_patches
    mapped_paths = {Path(str(patch["png"])).resolve() for patch in patches}

    for edited in sorted(edited_paths):
        relative = edited.relative_to(edited_root.resolve())
        original = imageori_root / relative
        if not original.is_file():
            add_issue(
                issues,
                "mapping",
                f"edited PNG has no imageori counterpart: {original}",
                {"png": edited, "kind": "mapping"},
            )
        elif edited not in mapped_paths:
            add_issue(
                issues,
                "mapping",
                "edited PNG is not consumed by any image manifest",
                {"png": edited, "kind": "mapping"},
            )

    if build is not None:
        normal_groups: dict[tuple[str, ...], list[dict[str, object]]] = {}
        for patch in normal_patches:
            normal_groups.setdefault(patch_group_key(patch, title=False), []).append(patch)
        for group in normal_groups.values():
            validate_group_recursive(build, root, source_dir, group, False, issues)

        title_groups: dict[tuple[str, ...], list[dict[str, object]]] = {}
        for patch in title_patches:
            title_groups.setdefault(patch_group_key(patch, title=True), []).append(patch)
        for group in title_groups.values():
            validate_group_recursive(build, root, source_dir, group, True, issues)

    valid_patch_count = sum(
        1
        for patch in patches
        if not any(issue["path"] == str(patch["png"]) for issue in issues)
    )
    report = {
        "root": str(root),
        "edited_pngs": len(edited_paths),
        "skipped_pngs": len(skipped_edited_paths),
        "skipped_directories": sorted(SKIPPED_EDIT_DIRECTORIES),
        "mapped_images": len(patches),
        "valid_images": valid_patch_count,
        "invalid_images": len(issues),
        "issues": issues,
    }

    print(
        f"Checked {len(edited_paths)} edited PNGs; "
        f"skipped {len(skipped_edited_paths)} duplicate PNGs; "
        f"mapped {len(patches)}; "
        f"valid {valid_patch_count}; "
        f"issues {len(issues)}."
    )
    for issue in issues:
        print(f"INVALID [{issue['phase']}] {issue['target']}")
        print(f"  {issue['reason']}")

    if args.json:
        output = args.json.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote report: {output}")

    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
