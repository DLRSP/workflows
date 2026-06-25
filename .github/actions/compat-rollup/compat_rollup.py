"""Aggregate per-version compatibility across published packages as sub-issues.

Milestones cannot nest, so the org-level rollup uses GitHub sub-issues: one
parent issue per still-supported Python/Django version, with one sub-issue per
published package. A package's sub-issue is closed when the package already
declares support for that version (via pyproject classifiers) and open when it
still needs adaptation, so the parent's native sub-issue progress reads as
"compatible packages / total".

The per-repo ``compat:`` milestones are untouched: they keep the native pull
request link and the EOL countdown inside each repository.

Environment:
    GH_TOKEN       installation token with issues:write on the hub repo
    ORG            organization login (e.g. DLRSP)
    HUB_REPO       owner/repo that holds the parent + sub-issues
    TIMELINE_REF   ref of DLRSP/workflows to read the timeline from (default main)
    PACKAGES       optional CSV of repo names to override discovery
    DRY_RUN        when "1", report actions without writing
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request

import yaml

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com/{repo}/HEAD/pyproject.toml"
TIMELINE_URL = (
    "https://raw.githubusercontent.com/DLRSP/workflows/"
    "{ref}/.github/compat-timeline.yaml"
)
LABEL = "compat-rollup"
EOL_SOON = "eol-soon"
EOL_WARN_DAYS = 180
TEMPLATE_REPO = "django-pkg"


def _request(method, url, token, data=None, accept=None):
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept or "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def _paginated(path, token):
    items, page = [], 1
    sep = "&" if "?" in path else "?"
    while True:
        url = f"{API}{path}{sep}per_page=100&page={page}"
        batch = _request("GET", url, token)
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


def _fetch_text(url):
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.read().decode()
    except urllib.error.URLError:
        return None


def _active_versions(ref):
    text = _fetch_text(TIMELINE_URL.format(ref=ref))
    if text is None:
        raise RuntimeError("could not read compat-timeline.yaml")
    timeline = yaml.safe_load(text)
    today = dt.date.today()
    rows = []
    for key, label in (("python", "Python"), ("django", "Django")):
        for entry in timeline.get(key, []):
            # Django: LTS only (packages declare support for LTS, not for the
            # fast-churning feature releases). Python has no LTS concept.
            if key == "django" and not entry.get("lts"):
                continue
            eol = dt.date.fromisoformat(entry["eol"])
            if eol < today:
                continue
            rows.append(
                {
                    "ecosystem": label,
                    "version": str(entry["version"]),
                    "release": entry.get("release", ""),
                    "eol": entry["eol"],
                    "days_to_eol": (eol - today).days,
                }
            )
    return rows


def _classifiers(repo_full):
    text = _fetch_text(RAW.format(repo=repo_full))
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    py, dj = set(), set()
    for item in project.get("classifiers", []):
        head, _, tail = item.rpartition("::")
        tail = tail.strip()
        if item.startswith("Programming Language :: Python :: ") and "." in tail:
            py.add(tail)
        elif item.startswith("Framework :: Django :: ") and "." in tail:
            dj.add(tail)
    return {"Python": py, "Django": dj}


def _discover_packages(org, token, override):
    if override:
        names = [n.strip() for n in override.split(",") if n.strip()]
        return [{"name": n, "full": f"{org}/{n}"} for n in names]
    packages = []
    for repo in _paginated(f"/orgs/{org}/repos?type=all", token):
        name = repo["name"]
        if repo.get("archived") or not name.startswith("django-"):
            continue
        if name == TEMPLATE_REPO:
            continue
        if _classifiers(repo["full_name"]) is None:
            continue
        packages.append({"name": name, "full": repo["full_name"]})
    return packages


def _ensure_label(repo, token, name, color, description, dry_run):
    if dry_run:
        return
    try:
        _request(
            "POST",
            f"{API}/repos/{repo}/labels",
            token,
            {"name": name, "color": color, "description": description},
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 422:  # already exists
            raise


def _add_labels(repo, token, number, labels, dry_run):
    if not labels:
        return
    if dry_run:
        print(f"[dry-run] add labels {labels} to #{number}")
        return
    _request(
        "POST",
        f"{API}/repos/{repo}/issues/{number}/labels",
        token,
        {"labels": labels},
    )


def _existing_issues(repo, token):
    issues = {}
    for issue in _paginated(f"/repos/{repo}/issues?state=all&labels={LABEL}", token):
        if "pull_request" in issue:
            continue
        issues[issue["title"]] = issue
    return issues


def _ensure_issue(repo, token, title, body, want_state, existing, dry_run, labels=None):
    labels = labels or [LABEL]
    issue = existing.get(title)
    if issue is None:
        if dry_run:
            print(f"[dry-run] create {want_state:6} issue '{title}'")
            return None
        issue = _request(
            "POST",
            f"{API}/repos/{repo}/issues",
            token,
            {"title": title, "body": body, "labels": labels},
        )
    if issue.get("state") != want_state:
        if dry_run:
            print(f"[dry-run] set '{title}' -> {want_state}")
        else:
            data = {"state": want_state}
            if want_state == "closed":
                data["state_reason"] = "completed"
            _request(
                "PATCH", f"{API}/repos/{repo}/issues/{issue['number']}", token, data
            )
    return issue


def _linked_child_ids(repo, token, parent_number):
    url = f"{API}/repos/{repo}/issues/{parent_number}/sub_issues"
    try:
        return {child["id"] for child in _request("GET", url, token)}
    except urllib.error.HTTPError:
        return set()


def _link_sub_issue(repo, token, parent_number, child_id, linked, dry_run):
    if child_id in linked:
        return
    if dry_run:
        print(f"[dry-run] link sub-issue {child_id} -> #{parent_number}")
        return
    try:
        _request(
            "POST",
            f"{API}/repos/{repo}/issues/{parent_number}/sub_issues",
            token,
            {"sub_issue_id": child_id},
        )
    except urllib.error.HTTPError as exc:
        if exc.code not in (409, 422):  # already linked / not allowed
            raise


def _close_stale(repo, token, existing, active_titles, dry_run):
    """Close rollup issues no longer in the active set (version past EOL)."""
    closed = 0
    for title, issue in existing.items():
        if title in active_titles or issue.get("state") == "closed":
            continue
        if dry_run:
            print(f"[dry-run] close stale '{title}'")
        else:
            _request(
                "PATCH",
                f"{API}/repos/{repo}/issues/{issue['number']}",
                token,
                {"state": "closed", "state_reason": "completed"},
            )
        closed += 1
    return closed


def main():
    token = os.environ["GH_TOKEN"]
    org = os.environ["ORG"]
    hub = os.environ.get("HUB_REPO", f"{org}/compatibility")
    ref = os.environ.get("TIMELINE_REF", "main")
    override = os.environ.get("PACKAGES", "")
    dry_run = os.environ.get("DRY_RUN") == "1"

    versions = _active_versions(ref)
    packages = _discover_packages(org, token, override)
    support = {pkg["name"]: _classifiers(pkg["full"]) or {} for pkg in packages}
    print(
        f"rollup: {len(versions)} active version(s) x {len(packages)} package(s) "
        f"on {hub}"
    )

    _ensure_label(
        hub,
        token,
        LABEL,
        "1d76db",
        "Org compatibility rollup (parent/sub-issues)",
        dry_run,
    )
    _ensure_label(
        hub,
        token,
        EOL_SOON,
        "d93f0b",
        f"End of life within {EOL_WARN_DAYS} days",
        dry_run,
    )
    existing = {} if dry_run else _existing_issues(hub, token)

    active_titles = set()
    parents = subs = supported = soon = 0
    for ver in versions:
        eco, num = ver["ecosystem"], ver["version"]
        near = ver["days_to_eol"] <= EOL_WARN_DAYS
        ptitle = f"compat: {eco} {num}"
        active_titles.add(ptitle)
        countdown = (
            f"EOL in {ver['days_to_eol']} days." if near else f"EOL {ver['eol']}."
        )
        pbody = (
            f"Tracks {eco} {num} compatibility across published DLRSP packages.\n\n"
            f"Released {ver['release'] or 'n/a'}; {countdown}\n\n"
            "Each sub-issue is one package: closed = already declares support, "
            "open = still needs adaptation."
        )
        plabels = [LABEL, EOL_SOON] if near else [LABEL]
        parent = _ensure_issue(
            hub, token, ptitle, pbody, "open", existing, dry_run, plabels
        )
        parents += 1
        if near:
            soon += 1
            current = existing.get(ptitle)
            if current is not None:
                have = {lbl["name"] for lbl in current.get("labels", [])}
                if EOL_SOON not in have:
                    _add_labels(hub, token, current["number"], [EOL_SOON], dry_run)
        linked = (
            set()
            if dry_run or parent is None
            else _linked_child_ids(hub, token, parent["number"])
        )
        for pkg in packages:
            sets = support.get(pkg["name"], {})
            ok = num in sets.get(eco, set())
            supported += 1 if ok else 0
            stitle = f"{pkg['name']}: {eco} {num}"
            sbody = (
                f"{pkg['name']} compatibility with {eco} {num}.\n\n"
                f"Status from pyproject classifiers: "
                f"{'declared' if ok else 'not declared yet'}.\n\n"
                f"Repo https://github.com/{pkg['full']} - "
                f"milestones https://github.com/{pkg['full']}/milestones"
            )
            active_titles.add(stitle)
            want = "closed" if ok else "open"
            child = _ensure_issue(hub, token, stitle, sbody, want, existing, dry_run)
            subs += 1
            if child is not None and parent is not None:
                _link_sub_issue(
                    hub, token, parent["number"], child["id"], linked, dry_run
                )

    closed = _close_stale(hub, token, existing, active_titles, dry_run)
    print(
        f"compat rollup on {hub}: parents={parents} sub-issues={subs} "
        f"declared-supported={supported} eol-soon={soon} retired={closed}"
    )


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        print(f"::error::GitHub API {exc.code}: {exc.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        sys.exit(1)
