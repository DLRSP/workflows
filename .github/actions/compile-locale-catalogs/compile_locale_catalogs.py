#!/usr/bin/env python3
"""Compile gettext .po catalogs under src/**/locale to .mo via GNU msgfmt.

No Django or settings module required. Safe to run when a module has no locale
trees (no-op). Used by DLRSP/workflows CI release builds and tox commands_pre.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def find_po_files(root: Path) -> list[Path]:
    src = root / "src"
    if not src.is_dir():
        return []
    return sorted(
        p
        for p in src.rglob("*.po")
        if "locale" in p.parts and p.parent.name == "LC_MESSAGES"
    )


def compile_catalogs(root: Path) -> int:
    po_files = find_po_files(root)
    if not po_files:
        print("ℹ️  No locale catalogs under src/**/locale; skipping compile")
        return 0

    msgfmt = shutil.which("msgfmt")
    if not msgfmt:
        print(
            "❌ msgfmt not found; install GNU gettext (e.g. apt install gettext)",
            file=sys.stderr,
        )
        return 1

    for po in po_files:
        mo = po.with_suffix(".mo")
        subprocess.run(
            [msgfmt, "--check-format", "-o", str(mo), str(po)],
            check=True,
        )
    print(f"✅ Compiled {len(po_files)} locale catalog(s) under {root / 'src'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: current directory)",
    )
    args = parser.parse_args(argv)
    return compile_catalogs(args.root.resolve())


if __name__ == "__main__":
    sys.exit(main())
