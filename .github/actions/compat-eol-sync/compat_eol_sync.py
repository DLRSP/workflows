"""Verify compat-timeline.yaml against endoflife.date and update on drift.

endoflife.date publishes machine-readable release / end-of-life dates for
Python and Django. This keeps the curated ``compat-timeline.yaml`` correct:
when an upstream date differs from a version we already track, the date is
patched in place (comments and structure preserved via line edits) and the
caller opens a pull request. New upstream cycles we do not yet track are only
reported, never auto-added, because adding a version is a curation decision
(notably the Django ``lts`` flag).

Environment:
    TIMELINE_FILE   path to compat-timeline.yaml to verify / patch
    SUMMARY_FILE    optional path to write a markdown drift summary
    GITHUB_OUTPUT   optional; receives ``changed=true|false``
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request

import yaml

ENDOFLIFE = "https://endoflife.date/api/{product}.json"
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
VERSION_LINE = re.compile(r'^\s*-\s*version:\s*"?([^"\s]+)"?\s*$')
SECTION_LINE = re.compile(r"^(python|django):\s*$")
FIELD_LINE = re.compile(r'^(\s*)(release|eol):\s*"?([^"\s]+)"?\s*$')
UPDATED_LINE = re.compile(r'^updated:\s*"?[^"\s]+"?\s*$')


def _fetch(product):
    url = ENDOFLIFE.format(product=product)
    try:
        with urllib.request.urlopen(url) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError):
        return None


def _upstream(product, today):
    """Map cycle -> {release, eol, active} from endoflife.date.

    ``active`` is True when the cycle is still supported (eol in the future or
    no fixed eol), so callers can ignore long-dead historical versions.
    """
    data = _fetch(product)
    if data is None:
        return None
    out = {}
    for row in data:
        cycle = str(row.get("cycle", ""))
        if not cycle:
            continue
        info = {"active": True}
        rel = row.get("releaseDate")
        if isinstance(rel, str) and DATE.fullmatch(rel):
            info["release"] = rel
        eol = row.get("eol")
        if isinstance(eol, str) and DATE.fullmatch(eol):
            info["eol"] = eol
            info["active"] = dt.date.fromisoformat(eol) >= today
        out[cycle] = info
    return out


def _drift(timeline, upstream):
    """Return changes {(section, version, field): (old, new)} and new cycles."""
    changes = {}
    new_cycles = {}
    for section in ("python", "django"):
        up = upstream.get(section) or {}
        tracked = set()
        for entry in timeline.get(section, []):
            version = str(entry["version"])
            tracked.add(version)
            ref = up.get(version)
            if not ref:
                continue
            for field in ("release", "eol"):
                old = str(entry.get(field, ""))
                new = ref.get(field, "")
                if new and old and new != old:
                    changes[(section, version, field)] = (old, new)
        extra = [
            cycle for cycle in up if cycle not in tracked and up[cycle].get("active")
        ]
        if extra:
            new_cycles[section] = sorted(extra, key=_sortable)
    return changes, new_cycles


def _sortable(cycle):
    return tuple(int(p) if p.isdigit() else p for p in cycle.split("."))


def _apply(lines, changes):
    """Patch release/eol dates in place, preserving comments and layout."""
    section = None
    version = None
    out = []
    for line in lines:
        sec = SECTION_LINE.match(line)
        if sec:
            section = sec.group(1)
            version = None
            out.append(line)
            continue
        ver = VERSION_LINE.match(line)
        if ver:
            version = ver.group(1)
            out.append(line)
            continue
        field = FIELD_LINE.match(line)
        if field and section and version:
            indent, name, _ = field.groups()
            change = changes.get((section, version, name))
            if change:
                out.append(f'{indent}{name}: "{change[1]}"\n')
                continue
        out.append(line)
    return out


def _touch_updated(lines, today):
    return [
        f'updated: "{today}"\n' if UPDATED_LINE.match(line) else line for line in lines
    ]


def _summary(changes, new_cycles):
    rows = ["## Timeline sync from endoflife.date", ""]
    if changes:
        rows.append("Updated dates:")
        rows.append("")
        for (section, version, field), (old, new) in sorted(changes.items()):
            rows.append(f"- {section} {version} {field}: {old} -> {new}")
        rows.append("")
    for section, cycles in new_cycles.items():
        listed = ", ".join(cycles)
        rows.append(
            f"New upstream {section} cycle(s) not tracked yet "
            f"(review/add manually): {listed}"
        )
    return "\n".join(rows).rstrip() + "\n"


def _emit_changed(changed):
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as handle:
        handle.write(f"changed={'true' if changed else 'false'}\n")


def main():
    path = os.environ["TIMELINE_FILE"]
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()
    timeline = yaml.safe_load(raw)

    today = dt.date.today()
    upstream = {}
    for section in ("python", "django"):
        data = _upstream(section, today)
        if data is None:
            print(f"::warning::endoflife.date {section} unreachable; skipping")
            _emit_changed(False)
            return
        upstream[section] = data

    changes, new_cycles = _drift(timeline, upstream)
    summary_file = os.environ.get("SUMMARY_FILE")
    if summary_file:
        with open(summary_file, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_summary(changes, new_cycles))

    if not changes:
        note = ""
        if new_cycles:
            note = f" ({sum(len(v) for v in new_cycles.values())} new cycle)"
        print(f"compat eol sync: timeline matches endoflife.date{note}")
        _emit_changed(False)
        return

    lines = raw.splitlines(keepends=True)
    lines = _apply(lines, changes)
    lines = _touch_updated(lines, today.isoformat())
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(lines))

    print(f"compat eol sync: patched {len(changes)} date(s) in {path}")
    for (section, version, field), (old, new) in sorted(changes.items()):
        print(f"  {section} {version} {field}: {old} -> {new}")
    _emit_changed(True)


if __name__ == "__main__":
    try:
        main()
    except OSError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        sys.exit(1)
