"""Render the compatibility hub README from the shared timeline.

The hub repository (DLRSP/compatibility) holds the per-version parent issues
and per-package sub-issues. Its README must always mirror the tracked support
windows, so it is generated from ``compat-timeline.yaml`` instead of being
edited by hand: a Mermaid gantt plus Python/Django tables, with Django limited
to LTS releases (the policy the rollup and milestones follow).

The generated block lives between HTML markers, so any prose added outside the
markers is preserved on the next run.

Environment:
    TIMELINE_FILE  local path to compat-timeline.yaml (preferred)
    TIMELINE_URL   raw URL fallback when TIMELINE_FILE is unset
    TIMELINE_REF   ref used to build the default raw URL (default main)
    OUT            README path to write (default README.md)
    WARN_DAYS      EOL-soon threshold in days (default 180)
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import urllib.request

import yaml

BEGIN = "<!-- BEGIN compat-timeline (generated) -->"
END = "<!-- END compat-timeline (generated) -->"
DEFAULT_URL = (
    "https://raw.githubusercontent.com/DLRSP/workflows/"
    "{ref}/.github/compat-timeline.yaml"
)
PY_DOCS = "https://devguide.python.org/versions/"
DJ_DOCS = "https://www.djangoproject.com/download/"
EOL_DOCS = "https://endoflife.date/python"


def _load_timeline():
    path = os.environ.get("TIMELINE_FILE", "")
    if path:
        with open(path, encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    ref = os.environ.get("TIMELINE_REF", "main")
    url = os.environ.get("TIMELINE_URL") or DEFAULT_URL.format(ref=ref)
    with urllib.request.urlopen(url) as resp:
        return yaml.safe_load(resp.read().decode())


def _rows(timeline):
    """Return tracked rows: every Python minor and Django LTS only."""
    out = {"Python": [], "Django": []}
    for key, label in (("python", "Python"), ("django", "Django")):
        for entry in timeline.get(key, []):
            if key == "django" and not entry.get("lts"):
                continue
            out[label].append(
                {
                    "version": str(entry["version"]),
                    "release": entry.get("release", ""),
                    "eol": entry["eol"],
                    "lts": bool(entry.get("lts")),
                }
            )
    return out


def _status(row, today, warn_days):
    eol = dt.date.fromisoformat(row["eol"])
    release = row["release"]
    if eol < today:
        return "done", "EOL"
    if release and dt.date.fromisoformat(release) > today:
        return "future", "Scheduled"
    days = (eol - today).days
    if days <= warn_days:
        return "active", f"EOL in {days}d"
    return "active", "Supported"


def _gantt(rows, today, warn_days):
    lines = [
        "```mermaid",
        "gantt",
        "    title Supported Python & Django windows",
        "    dateFormat YYYY-MM-DD",
        "    axisFormat %Y",
        "    todayMarker on",
    ]
    for label in ("Python", "Django"):
        section = "Django LTS" if label == "Django" else label
        lines.append(f"    section {section}")
        for row in rows[label]:
            if not row["release"]:
                continue
            state, _ = _status(row, today, warn_days)
            name = f"{row['version']} LTS" if row["lts"] else row["version"]
            tag = "" if state == "future" else f"{state}, "
            lines.append(f"    {name} :{tag}{row['release']}, {row['eol']}")
    lines.append("```")
    return "\n".join(lines)


def _table(rows, today, warn_days, lts_col):
    head = "| Version | Released | End of life | Status |"
    sep = "| --- | --- | --- | --- |"
    if lts_col:
        head = "| Version | LTS | Released | End of life | Status |"
        sep = "| --- | --- | --- | --- | --- |"
    out = [head, sep]
    for row in rows:
        _, text = _status(row, today, warn_days)
        release = row["release"] or "n/a"
        if lts_col:
            lts = "yes" if row["lts"] else "no"
            out.append(
                f"| {row['version']} | {lts} | {release} " f"| {row['eol']} | {text} |"
            )
        else:
            out.append(f"| {row['version']} | {release} | {row['eol']} | {text} |")
    return "\n".join(out)


def _generated_block(timeline, today, warn_days):
    rows = _rows(timeline)
    updated = timeline.get("updated", today.isoformat())
    parts = [
        BEGIN,
        "",
        f"_Generated from `compat-timeline.yaml` (timeline updated "
        f"{updated}). Edit the timeline, not this block._",
        "",
        "### Support windows",
        "",
        _gantt(rows, today, warn_days),
        "",
        "### Python",
        "",
        _table(rows["Python"], today, warn_days, lts_col=False),
        "",
        "### Django (LTS only)",
        "",
        _table(rows["Django"], today, warn_days, lts_col=True),
        "",
        "Sources verified against "
        f"[Python devguide]({PY_DOCS}), "
        f"[Django download]({DJ_DOCS}) and "
        f"[endoflife.date]({EOL_DOCS}).",
        "",
        END,
    ]
    return "\n".join(parts)


def _template(block):
    return "\n".join(
        [
            "# DLRSP compatibility",
            "",
            "Org-wide Python and Django compatibility tracking hub for the "
            "published `django-*` packages.",
            "",
            "- **Parent issue per version** - one issue per active Python "
            "minor and Django LTS release.",
            "- **Sub-issue per package** - closed when the package declares "
            "support (pyproject classifiers), open when it still needs work.",
            "- **Native rollup** - the parent's sub-issue progress reads as "
            "compatible packages / total.",
            "",
            "Per-repository `compat:` milestones keep the pull-request link "
            "and EOL countdown inside each package.",
            "",
            block,
            "",
        ]
    )


def _render(existing, block):
    if existing and BEGIN in existing and END in existing:
        head, _, rest = existing.partition(BEGIN)
        _, _, tail = rest.partition(END)
        return f"{head}{block}{tail}"
    return _template(block)


def main():
    out_path = os.environ.get("OUT", "README.md")
    warn_days = int(os.environ.get("WARN_DAYS", "180"))
    today = dt.date.today()

    timeline = _load_timeline()
    block = _generated_block(timeline, today, warn_days)

    existing = ""
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as handle:
            existing = handle.read()

    rendered = _render(existing, block)
    if rendered == existing:
        print(f"compat readme: {out_path} already up to date")
        return

    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    print(f"compat readme: wrote {out_path}")


if __name__ == "__main__":
    try:
        main()
    except OSError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        sys.exit(1)
