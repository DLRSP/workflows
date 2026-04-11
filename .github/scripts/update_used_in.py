#!/usr/bin/env python3
"""
Update auto-generated 'Used in' sections:

  central — DLRSP/workflows README (repos that reference reusable workflows).

  consumer — Caller repo must contain ``.github/used-in.yaml`` with ``code_search_queries``.
  Only files that include the HTML marker pairs are rewritten (README bullets + optional docs tables).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any

import yaml
from github import Github

WORKFLOWS_REF_PATTERN = re.compile(r"DLRSP/workflows/\.github/workflows/[^@\s]+@")

MARKER_README_START = "<!-- used-in:auto-start -->"
MARKER_README_END = "<!-- used-in:auto-end -->"
MARKER_TABLE_START = "<!-- used-in:auto-table-start -->"
MARKER_TABLE_END = "<!-- used-in:auto-table-end -->"

CONFIG_REL = os.path.join(".github", "used-in.yaml")


def _repo_meta(repo: Any) -> dict[str, Any]:
    desc = (repo.description or "").strip().replace("\n", " ").replace("\r", "")
    return {
        "name": repo.name,
        "full_name": repo.full_name,
        "stars": repo.stargazers_count,
        "description": desc,
        "url": repo.html_url,
    }


def find_workflows_consumers(g: Github, org_name: str, workflows_repo: str) -> list[dict[str, Any]]:
    org = g.get_organization(org_name)
    repos_found: dict[str, dict[str, Any]] = {}
    for repo in org.get_repos(type="all"):
        if repo.full_name == workflows_repo or repo.private:
            continue
        try:
            contents = repo.get_contents(".github/workflows")
            if not isinstance(contents, list):
                continue
        except Exception:
            continue
        for content_file in contents:
            if not content_file.name.endswith((".yaml", ".yml")):
                continue
            try:
                text = content_file.decoded_content.decode("utf-8")
                if WORKFLOWS_REF_PATTERN.search(text):
                    repos_found[repo.full_name] = _repo_meta(repo)
                    break
            except Exception:
                continue
    return sorted(repos_found.values(), key=lambda x: x["stars"], reverse=True)


def find_consumers_by_queries(
    g: Github,
    queries: list[str],
    exclude_full_name: str,
) -> list[dict[str, Any]]:
    exclude_l = exclude_full_name.strip().lower()
    by_full: dict[str, dict[str, Any]] = {}
    for q in queries:
        q = str(q).strip()
        if not q:
            continue
        try:
            for hit in g.search_code(q):
                repo = hit.repository
                if repo.private:
                    continue
                rf = repo.full_name
                if rf.lower() == exclude_l:
                    continue
                if rf not in by_full:
                    by_full[rf] = _repo_meta(repo)
        except Exception as exc:
            print(f"warning: code search {q!r}: {exc}", file=sys.stderr)
    return sorted(by_full.values(), key=lambda x: x["stars"], reverse=True)


def _workflows_used_in_block(repos: list[dict[str, Any]]) -> str:
    lines = [
        "## Used in",
        "",
        "Check these projects to get real-life examples of usage and inspiration:",
        "",
    ]
    for repo in repos:
        owner, repo_name = repo["full_name"].split("/", 1)
        badge = (
            f"https://img.shields.io/github/stars/{owner}/{repo_name}"
            f"?label=%E2%AD%90&style=flat-square"
        )
        line = f"- ![GitHub stars]({badge}) [{repo_name}]({repo['url']}#readme)"
        if repo["description"]:
            line += f" - {repo['description']}"
        lines.append(line)
    lines.extend(
        [
            "",
            "Feel free to send a PR to add your project in this list if you are relying on these scripts.",
        ]
    )
    return "\n".join(lines)


def update_workflows_readme(readme_path: str, repos: list[dict[str, Any]]) -> bool:
    block = _workflows_used_in_block(repos)
    with open(readme_path, encoding="utf-8") as f:
        content = f.read()
    pattern = (
        r"(## Used in\n.*?"
        r"\nFeel free to send a PR to add your project in this list if you are relying on these scripts\.)"
    )
    if re.search(pattern, content, re.DOTALL):
        new_content = re.sub(pattern, block, content, count=1, flags=re.DOTALL)
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return True
    if "## Changelog" in content:
        new_content = content.replace(
            "## Changelog",
            f"## Changelog\n\n{block}\n\n",
            1,
        )
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return True
    return False


def _safe_table_cell(text: str) -> str:
    return text.replace("|", " ").replace("\n", " ").strip()


def _readme_bullets(
    repos: list[dict[str, Any]],
    empty_message: str,
) -> str:
    lines: list[str] = []
    for repo in repos:
        owner, repo_name = repo["full_name"].split("/", 1)
        badge = (
            f"https://img.shields.io/github/stars/{owner}/{repo_name}"
            f"?label=%E2%AD%90&style=flat-square"
        )
        line = f"- ![GitHub stars]({badge}) [{repo_name}]({repo['url']}#readme)"
        if repo["description"]:
            line += f" — {repo['description']}"
        lines.append(line)
    if not lines:
        lines.append(empty_message)
    return "\n".join(lines)


def _markdown_table(
    repos: list[dict[str, Any]],
    empty_cell: str,
    default_role: str,
    table_header: tuple[str, str],
) -> str:
    col1, col2 = table_header
    rows = [
        f"| {col1} | {col2} |",
        f"| {'-' * max(3, len(col1))} | {'-' * max(3, len(col2))} |",
    ]
    if not repos:
        rows.append(f"| — | {empty_cell} |")
    for repo in repos:
        desc = repo["description"] or default_role
        rows.append(
            f"| [{repo['name']}]({repo['url']}) | {_safe_table_cell(desc)} |"
        )
    return "\n".join(rows)


def _replace_markers(content: str, start: str, end: str, inner: str) -> str | None:
    if start not in content or end not in content:
        return None
    pre, rest = content.split(start, 1)
    _old_inner, post = rest.split(end, 1)
    return pre + start + "\n" + inner.strip() + "\n" + end + post


def load_consumer_config(root: str) -> dict[str, Any] | None:
    path = os.path.join(root, CONFIG_REL)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        print(f"Invalid {CONFIG_REL}: root must be a mapping", file=sys.stderr)
        return None
    return data


def run_consumer(root: str, g: Github) -> int:
    cfg = load_consumer_config(root)
    if cfg is None:
        print(f"No {CONFIG_REL}; skipping marker sync (not an error).")
        return 0

    queries = cfg.get("code_search_queries")
    if not queries or not isinstance(queries, list):
        print(
            f"{CONFIG_REL} must define a non-empty list 'code_search_queries'",
            file=sys.stderr,
        )
        return 1

    exclude = (cfg.get("exclude_repository") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if not exclude:
        print(
            "Set exclude_repository in config or GITHUB_REPOSITORY in the environment",
            file=sys.stderr,
        )
        return 1

    repos = find_consumers_by_queries(g, [str(q) for q in queries], exclude)

    empty_readme = str(cfg.get("empty_readme_message", "_No public repositories matched the latest scan._"))
    empty_table = str(cfg.get("empty_table_message", "_No public repositories matched the latest scan._"))
    default_role = str(
        cfg.get(
            "default_table_role",
            "Listed via configured code search (automated).",
        )
    )
    th = cfg.get("table_columns")
    if isinstance(th, list) and len(th) == 2:
        table_header = (str(th[0]), str(th[1]))
    else:
        table_header = ("Repository", "Role")

    bullets = _readme_bullets(repos, empty_readme)
    table_body = _markdown_table(repos, empty_table, default_role, table_header)

    changed: list[str] = []
    update_readme = cfg.get("update_readme", True)
    readme_rel = str(cfg.get("readme_path", "README.md"))

    if update_readme:
        readme_path = os.path.join(root, readme_rel)
        if os.path.isfile(readme_path):
            with open(readme_path, encoding="utf-8") as f:
                body = f.read()
            new_body = _replace_markers(body, MARKER_README_START, MARKER_README_END, bullets)
            if new_body is not None and new_body != body:
                with open(readme_path, "w", encoding="utf-8") as f:
                    f.write(new_body)
                changed.append(readme_rel)

    table_files = cfg.get("table_marker_files")
    if isinstance(table_files, list):
        for rel in table_files:
            rel_s = str(rel).strip().lstrip("/")
            if not rel_s:
                continue
            doc_path = os.path.join(root, rel_s)
            if not os.path.isfile(doc_path):
                continue
            with open(doc_path, encoding="utf-8") as f:
                body = f.read()
            new_body = _replace_markers(
                body, MARKER_TABLE_START, MARKER_TABLE_END, table_body
            )
            if new_body is not None and new_body != body:
                with open(doc_path, "w", encoding="utf-8") as f:
                    f.write(new_body)
                changed.append(rel_s)

    if changed:
        print(f"Updated marker sections in: {', '.join(changed)} ({len(repos)} repos in index)")
    else:
        print(
            "No marker updates (missing <!-- used-in:auto-* --> markers, or content unchanged)"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p0 = sub.add_parser("central", help="Update DLRSP/workflows README")
    p0.add_argument("--root", default=".", help="Workflows repository root")

    p1 = sub.add_parser(
        "consumer",
        help="Update README/docs using .github/used-in.yaml and marker blocks",
    )
    p1.add_argument("--root", default=".", help="Repository root (caller)")

    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN not set", file=sys.stderr)
        return 1

    g = Github(token)

    if args.command == "central":
        repos = find_workflows_consumers(g, "DLRSP", "DLRSP/workflows")
        if not repos:
            print("No repositories found; skipping README update")
            return 0
        readme = os.path.join(args.root, "README.md")
        if not update_workflows_readme(readme, repos):
            print("Could not update workflows README (missing sections?)", file=sys.stderr)
            return 1
        print(f"Updated workflows README with {len(repos)} repositories")
        return 0

    if args.command == "consumer":
        return run_consumer(args.root, g)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
