"""Upsert per-repository compatibility milestones from the shared timeline.

Reads the Python/Django release and EOL dates from ``compat-timeline.yaml`` and
ensures one milestone per still-relevant version on the target repository, with
``due_on`` set to the version EOL date. Milestones stay open until their EOL and
are closed afterwards, so adaptation PRs/commits remain linked to the version
they target.

Environment:
    GH_TOKEN       installation token with repo issues:write (milestones)
    REPOSITORY     owner/repo to sync (e.g. DLRSP/django-hashtag)
    TIMELINE_REF   ref of DLRSP/workflows to read the timeline from (default main)
    TIMELINE_URL   optional explicit raw URL override
    DRY_RUN        when "1", report actions without writing
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

import yaml

API = "https://api.github.com"
DEFAULT_TIMELINE = (
    "https://raw.githubusercontent.com/DLRSP/workflows/{ref}/.github/compat-timeline.yaml"
)


def _request(method, url, token, data=None):
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def _fetch_timeline(ref, url_override):
    url = url_override or DEFAULT_TIMELINE.format(ref=ref)
    with urllib.request.urlopen(url) as resp:
        return yaml.safe_load(resp.read().decode())


def _existing_milestones(repo, token):
    milestones = {}
    page = 1
    while True:
        url = f"{API}/repos/{repo}/milestones?state=all&per_page=100&page={page}"
        batch = _request("GET", url, token)
        if not batch:
            break
        for milestone in batch:
            milestones[milestone["title"]] = milestone
        if len(batch) < 100:
            break
        page += 1
    return milestones


def _desired(timeline, today):
    rows = []
    for kind, label in (("python", "Python"), ("django", "Django")):
        for entry in timeline.get(kind, []):
            version = entry["version"]
            eol = dt.date.fromisoformat(entry["eol"])
            release = entry.get("release", "")
            rows.append(
                {
                    "title": f"compat: {label} {version}",
                    # Noon UTC: GitHub renders the milestone due date in a
                    # timezone behind UTC and would roll midnight back to the
                    # previous day; noon keeps the EOL date intact everywhere.
                    "due_on": f"{entry['eol']}T12:00:00Z",
                    "state": "open" if eol >= today else "closed",
                    "description": (
                        f"Track compatibility for {label} {version}. "
                        f"Released {release}; end of life {entry['eol']}. "
                        "Keep open until EOL; link version-adaptation PRs here."
                    ),
                }
            )
    return rows


def main():
    token = os.environ["GH_TOKEN"]
    repo = os.environ["REPOSITORY"]
    ref = os.environ.get("TIMELINE_REF", "main")
    url_override = os.environ.get("TIMELINE_URL", "")
    dry_run = os.environ.get("DRY_RUN") == "1"

    timeline = _fetch_timeline(ref, url_override)
    today = dt.date.today()
    existing = _existing_milestones(repo, token)

    created = updated = skipped = 0
    for want in _desired(timeline, today):
        current = existing.get(want["title"])
        if current is None:
            if want["state"] == "closed":
                # Do not materialise milestones for versions already past EOL.
                skipped += 1
                continue
            if dry_run:
                print(f"[dry-run] create {want['title']} (due {want['due_on']})")
            else:
                _request("POST", f"{API}/repos/{repo}/milestones", token, want)
            created += 1
            continue

        drift = (
            current.get("state") != want["state"]
            or (current.get("due_on") or "")[:10] != want["due_on"][:10]
            or current.get("description") != want["description"]
        )
        if not drift:
            skipped += 1
            continue
        if dry_run:
            print(f"[dry-run] update {want['title']} -> {want['state']}")
        else:
            number = current["number"]
            _request(
                "PATCH",
                f"{API}/repos/{repo}/milestones/{number}",
                token,
                want,
            )
        updated += 1

    print(
        f"compat milestones on {repo}: created={created} "
        f"updated={updated} unchanged={skipped}"
    )


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:  # surface API errors clearly in CI
        print(f"::error::GitHub API {exc.code}: {exc.read().decode()}", file=sys.stderr)
        sys.exit(1)
