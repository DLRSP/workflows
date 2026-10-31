"""Scan recent CI job logs for recurring packaging / deprecation warnings.

Opens or updates one tracking issue per (repository, warning-fingerprint) so
the same finding is not duplicated. Intended to run with an org-scoped
dlrsp-actions App token (issues:write, actions:read).

Environment:
    GH_TOKEN          installation token
    ORG               organization login (default DLRSP)
    FLEET_FILE        path to fleet-modules.yaml (optional)
    REPOS             optional CSV of repo names (overrides fleet file)
    RUNS_PER_REPO     completed CI runs to inspect (default 3)
    DRY_RUN           when "1", report without writing issues
    WORKFLOW_NAME     workflow name filter (default CI/CD)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml

API = "https://api.github.com"
FP_MARKER = "<!-- packaging-hygiene-fp:{fp} -->"
LABEL_HYGIENE = "packaging-hygiene"
LABEL_DEPRECATION = "deprecation-cleanup"


@dataclass(frozen=True)
class WarningHit:
    cls: str
    fingerprint: str
    title: str
    summary: str
    deprecation: bool
    sample: str


# Ordered matchers: first match wins for a log line.
# Fingerprints stay stable across wording churn by using class + short key.
_MATCHERS: list[tuple[str, re.Pattern[str], str, str, bool]] = [
    (
        "license-classifier-spdx",
        re.compile(
            r"License classifiers are deprecated|License :: OSI Approved",
            re.I,
        ),
        "License classifiers deprecated — prefer SPDX",
        "setuptools reports deprecated `License ::` classifiers; switch to an "
        "SPDX `license` expression in pyproject.toml and drop legacy classifiers.",
        True,
    ),
    (
        "node20-actions-deprecation",
        re.compile(r"Node\.js 20 is deprecated", re.I),
        "Node.js 20 Actions runtime deprecated",
        "A GitHub Action still targets Node.js 20; bump the action version "
        "(or pin) so CI runs on a supported Node runtime.",
        True,
    ),
    (
        "setuptools-experimental-distutils",
        re.compile(r"\[tool\.distutils\].*experimental", re.I),
        "setuptools `[tool.distutils]` still experimental",
        "pyproject.toml uses experimental `[tool.distutils]`; review before "
        "setuptools hardens or removes the table.",
        True,
    ),
    (
        "setuptools-field-overwrite",
        re.compile(r"SetuptoolsWarning: `.+` overwritten", re.I),
        "setuptools field overwritten by pyproject.toml",
        "setuptools warns that a setup.cfg / setup.py field is overwritten by "
        "pyproject.toml — consolidate metadata into one source.",
        False,
    ),
    (
        "pip-deprecation",
        re.compile(
            r"pip\._(internal|vendor).+DeprecationWarning|DEPRECATION: .+pip", re.I
        ),
        "pip deprecation warning in CI",
        "pip emitted a DeprecationWarning during install or build; upgrade pip "
        "or adjust the install invocation.",
        True,
    ),
    (
        "python-deprecation-warning",
        # Trailing ":" = emitted warning, not tox `-W error::DeprecationWarning`.
        re.compile(r"(?<!::)(DeprecationWarning|PendingDeprecationWarning)\s*:"),
        "Python DeprecationWarning in CI logs",
        "A DeprecationWarning / PendingDeprecationWarning appeared in CI job "
        "logs; triage the stack frame and remove the deprecated call path.",
        True,
    ),
    (
        "pkg-resources-deprecation",
        re.compile(r"pkg_resources is deprecated|PkgResourcesDeprecationWarning", re.I),
        "pkg_resources deprecated",
        "pkg_resources usage is deprecated; migrate to importlib.metadata / "
        "importlib.resources.",
        True,
    ),
    (
        "setuptools-deprecation",
        re.compile(r"SetuptoolsDeprecationWarning"),
        "setuptools deprecation warning",
        "setuptools emitted SetuptoolsDeprecationWarning; follow the linked "
        "migration guidance in the log excerpt.",
        True,
    ),
]


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward GitHub Authorization to Azure blob log redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        from_host = urllib.parse.urlparse(req.full_url).netloc
        to_host = urllib.parse.urlparse(new_req.full_url).netloc
        if from_host != to_host:
            for header in ("Authorization", "authorization"):
                if header in new_req.headers:
                    del new_req.headers[header]
        return new_req


_OPENER = urllib.request.build_opener(_StripAuthOnRedirect)


def _request(method, url, token, data=None, accept=None, raw=False):
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept or "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req) as resp:
            body = resp.read()
            if raw:
                return body
            text = body.decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc


def _unwrap_list(payload):
    """Normalize GitHub list payloads (bare array or wrapped collection)."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("workflow_runs", "jobs", "issues", "labels", "items"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def _paginated(path, token, max_pages=5):
    items: list = []
    page = 1
    sep = "&" if "?" in path else "?"
    while page <= max_pages:
        url = f"{API}{path}{sep}per_page=100&page={page}"
        batch = _unwrap_list(_request("GET", url, token))
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


def _load_repos(org: str) -> list[str]:
    override = os.environ.get("REPOS", "").strip()
    if override:
        return [n.strip() for n in override.split(",") if n.strip()]

    fleet = os.environ.get("FLEET_FILE", "").strip()
    if fleet:
        path = Path(fleet)
    else:
        # Default: sibling of action path → .github/fleet-modules.yaml
        here = Path(__file__).resolve()
        path = here.parents[2] / "fleet-modules.yaml"
    if not path.is_file():
        raise RuntimeError(f"fleet modules file missing: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    modules = data.get("modules") or []
    if not modules:
        raise RuntimeError(f"no modules listed in {path}")
    return [str(m).strip() for m in modules if str(m).strip()]


def _fingerprint(cls: str, key: str) -> str:
    raw = f"{cls}|{key}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _classify_line(line: str) -> WarningHit | None:
    stripped = line.strip()
    if not stripped:
        return None
    # Skip noisy "Collecting setuptools==" install lines unless they carry a warning.
    if "Collecting setuptools" in stripped and "Warning" not in stripped:
        return None
    for cls, pattern, title, summary, deprecation in _MATCHERS:
        if not pattern.search(stripped):
            continue
        # Class-stable fingerprint: one tracking issue per warning class per repo.
        fp = _fingerprint(cls, cls)
        sample = stripped[:500]
        return WarningHit(cls, fp, title, summary, deprecation, sample)
    return None


def _scan_log_text(text: str) -> dict[str, WarningHit]:
    found: dict[str, WarningHit] = {}
    for line in text.splitlines():
        hit = _classify_line(line)
        if hit is None or hit.fingerprint in found:
            continue
        found[hit.fingerprint] = hit
    return found


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _download_bytes(url: str, token: str | None = None) -> bytes:
    """GET bytes; do not forward Authorization onto Azure/S3 log redirects."""
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    auth = token
    for _ in range(5):
        req = urllib.request.Request(current, method="GET")
        if auth:
            req.add_header("Authorization", f"Bearer {auth}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
        try:
            with opener.open(req) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                detail = exc.read().decode(errors="replace")[:300]
                raise RuntimeError(f"GET {current} -> {exc.code}: {detail}") from exc
            location = exc.headers.get("Location")
            if not location:
                raise RuntimeError(
                    f"GET {current} -> {exc.code} without Location"
                ) from exc
            current = location
            auth = None  # blob URL must not receive the GitHub Bearer token
    raise RuntimeError(f"too many redirects for {url}")


def _job_log_text(repo: str, job_id: int, token: str) -> str:
    url = f"{API}/repos/{repo}/actions/jobs/{job_id}/logs"
    try:
        raw = _download_bytes(url, token=token)
    except RuntimeError as exc:
        print(f"  skip job {job_id} logs: {exc}")
        return ""
    if raw[:2] == b"PK":
        parts: list[str] = []
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                try:
                    parts.append(zf.read(name).decode(errors="replace"))
                except Exception:  # noqa: BLE001 — best-effort extract
                    continue
        return "\n".join(parts)
    return raw.decode(errors="replace")


def _recent_runs(repo: str, token: str, workflow_name: str, limit: int) -> list[dict]:
    path = (
        f"/repos/{repo}/actions/runs" f"?status=completed&per_page={max(limit * 3, 10)}"
    )
    runs = _paginated(path, token, max_pages=1)
    selected = []
    for run in runs:
        name = run.get("name") or ""
        if workflow_name and workflow_name.lower() not in name.lower():
            continue
        if run.get("event") in ("pull_request",) and len(selected) >= limit:
            continue
        selected.append(run)
        if len(selected) >= limit:
            break
    return selected


def _run_jobs(repo: str, run_id: int, token: str) -> list[dict]:
    return _paginated(f"/repos/{repo}/actions/runs/{run_id}/jobs", token, max_pages=2)


def _ensure_label(
    repo: str, token: str, name: str, color: str, description: str, dry_run: bool
):
    if dry_run:
        return
    try:
        _request(
            "POST",
            f"{API}/repos/{repo}/labels",
            token,
            {"name": name, "color": color, "description": description},
        )
    except RuntimeError as exc:
        if "422" not in str(exc):
            raise


def _list_hygiene_issues(repo: str, token: str) -> dict[str, dict]:
    """Map fingerprint -> issue for open packaging-hygiene issues."""
    by_fp: dict[str, dict] = {}
    for issue in _paginated(
        f"/repos/{repo}/issues?state=open&labels={LABEL_HYGIENE}", token
    ):
        if "pull_request" in issue:
            continue
        body = issue.get("body") or ""
        match = re.search(r"packaging-hygiene-fp:([a-f0-9]+)", body)
        if match:
            by_fp[match.group(1)] = issue
    return by_fp


def _issue_body(repo: str, hit: WarningHit, run_urls: list[str]) -> str:
    marker = FP_MARKER.format(fp=hit.fingerprint)
    runs = "\n".join(f"- {u}" for u in run_urls) or "- (no run URL captured)"
    return (
        f"{marker}\n"
        f"## Packaging hygiene: `{hit.cls}`\n\n"
        f"{hit.summary}\n\n"
        f"**Repository:** `{repo}`\n\n"
        f"### Sample log line\n\n"
        f"```text\n{hit.sample}\n```\n\n"
        f"### Recent CI evidence\n\n"
        f"{runs}\n\n"
        f"---\n"
        f"_Opened by the fleet packaging-hygiene scanner. "
        f"One issue per warning class; this issue is updated in place._\n"
    )


def _create_or_update_issue(
    repo: str,
    token: str,
    hit: WarningHit,
    run_urls: list[str],
    existing: dict[str, dict],
    dry_run: bool,
) -> str | None:
    labels = [LABEL_HYGIENE]
    if hit.deprecation:
        labels.append(LABEL_DEPRECATION)
    title = f"[packaging-hygiene] {hit.title}"
    body = _issue_body(repo, hit, run_urls)
    issue = existing.get(hit.fingerprint)
    if issue is None:
        if dry_run:
            print(f"[dry-run] create issue on {repo}: {title}")
            return None
        created = _request(
            "POST",
            f"{API}/repos/{repo}/issues",
            token,
            {"title": title, "body": body, "labels": labels},
        )
        url = created.get("html_url", "")
        print(f"created {url}")
        return url
    # Refresh body with latest evidence; keep title/labels.
    if dry_run:
        print(f"[dry-run] update {repo}#{issue['number']}: {title}")
        return issue.get("html_url")
    _request(
        "PATCH",
        f"{API}/repos/{repo}/issues/{issue['number']}",
        token,
        {"title": title, "body": body},
    )
    # Ensure labels (ignore if already present).
    try:
        _request(
            "POST",
            f"{API}/repos/{repo}/issues/{issue['number']}/labels",
            token,
            {"labels": labels},
        )
    except RuntimeError:
        pass
    url = issue.get("html_url", "")
    print(f"updated {url}")
    return url


def scan_repo(
    org: str, name: str, token: str, runs_per: int, workflow_name: str, dry_run: bool
) -> list[str]:
    repo = f"{org}/{name}"
    print(f"== {repo} ==")
    _ensure_label(
        repo,
        token,
        LABEL_HYGIENE,
        "1d76db",
        "Recurring packaging / metadata hygiene from CI logs",
        dry_run,
    )
    _ensure_label(
        repo,
        token,
        LABEL_DEPRECATION,
        "5319E7",
        "Remove deprecated APIs / classifiers / runtimes",
        dry_run,
    )

    hits: dict[str, WarningHit] = {}
    evidence: dict[str, list[str]] = {}
    runs = _recent_runs(repo, token, workflow_name, runs_per)
    if not runs:
        print("  no matching completed CI runs")
        return []

    jobs_per_run = int(os.environ.get("JOBS_PER_RUN", "8"))
    for run in runs:
        run_url = run.get("html_url") or ""
        jobs = [
            j
            for j in _run_jobs(repo, run["id"], token)
            if j.get("conclusion") in ("success", "failure")
        ]
        # Prefer longer / test jobs; packaging warnings usually appear there.
        jobs.sort(key=lambda j: (-(j.get("run_attempt") or 1), j.get("name") or ""))
        for job in jobs[:jobs_per_run]:
            text = _job_log_text(repo, job["id"], token)
            if not text:
                continue
            for fp, hit in _scan_log_text(text).items():
                hits[fp] = hit
                evidence.setdefault(fp, [])
                if run_url and run_url not in evidence[fp]:
                    evidence[fp].append(run_url)

    if not hits:
        print("  no matched warning classes")
        return []

    existing = {} if dry_run else _list_hygiene_issues(repo, token)
    urls: list[str] = []
    for fp, hit in sorted(hits.items(), key=lambda kv: kv[1].cls):
        url = _create_or_update_issue(
            repo, token, hit, evidence.get(fp, []), existing, dry_run
        )
        if url:
            urls.append(url)
    return urls


def main() -> int:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GH_TOKEN is required", file=sys.stderr)
        return 2
    org = os.environ.get("ORG", "DLRSP")
    dry_run = os.environ.get("DRY_RUN") == "1"
    runs_per = int(os.environ.get("RUNS_PER_REPO", "3"))
    workflow_name = os.environ.get("WORKFLOW_NAME", "CI/CD")
    repos = _load_repos(org)
    print(f"scanning {len(repos)} repos (runs_per={runs_per}, dry_run={dry_run})")
    all_urls: list[str] = []
    errors = 0
    for name in repos:
        try:
            all_urls.extend(
                scan_repo(org, name, token, runs_per, workflow_name, dry_run)
            )
        except Exception as exc:  # noqa: BLE001 — keep fleet scan going
            errors += 1
            print(f"ERROR {org}/{name}: {exc}", file=sys.stderr)
    print("---")
    print(f"issues touched: {len(all_urls)}")
    for u in all_urls:
        print(u)
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"issue-count={len(all_urls)}\n")
            # Multiline output for issue URLs
            fh.write("issue-urls<<EOF\n")
            fh.write("\n".join(all_urls))
            fh.write("\nEOF\n")
    return 1 if errors and not all_urls else 0


if __name__ == "__main__":
    raise SystemExit(main())
