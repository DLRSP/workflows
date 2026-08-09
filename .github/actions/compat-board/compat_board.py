"""Sync an org-level GitHub Project v2 roadmap from the compatibility rollup.

This is the aggregated, cross-repository dashboard for the DLRSP fleet. It tracks
the rollup parent issues (one per still-supported Python/Django version, held in
the hub repository) as Project v2 items, so the roadmap covers the whole fleet at
once instead of mirroring per-repo milestones one module at a time.

Each parent issue carries Released/EOL dates (from the shared timeline) for the
roadmap bars, plus the native sub-issue progress field that reads as
"compatible packages / total". Drilling into a parent issue lists the per-package
sub-issues. Only parents are added as items, so the roadmap stays readable; the
per-package detail lives in each parent's sub-issue list and in the hub repo.

Parent completion is mirrored onto the built-in Status field: Done when every
package sub-issue is complete (parent issue closed), Todo when any package still
needs adaptation (parent open). Fully-adapted versions stay on the board until
EOL so the Released→EOL bars remain visible.

Projects v2 is only reachable through GraphQL with an org-scoped token that has
``organization-projects: write`` (the repository GITHUB_TOKEN cannot access it).

Environment:
    GH_TOKEN       org-scoped App installation token (org Projects RW + issues read)
    ORG            organization login (e.g. DLRSP)
    HUB_REPO       owner/repo holding the rollup parent + sub-issues
    PROJECT_TITLE  Project v2 title to find (fallback if PROJECT_NUMBER unset)
    PROJECT_NUMBER Project v2 number to target directly (preferred)
    TIMELINE_REF   ref of DLRSP/workflows to read the timeline from (default main)
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
GRAPHQL = f"{API}/graphql"

ROLLUP_LABEL = "compat-rollup"
PARENT_PREFIX = "compat: "

FIELDS = {
    "Ecosystem": "SINGLE_SELECT",
    "Version": "TEXT",
    "Released": "DATE",
    "EOL": "DATE",
}
TIMELINE_URL = (
    "https://raw.githubusercontent.com/DLRSP/workflows/"
    "{ref}/.github/compat-timeline.yaml"
)
ECOSYSTEM_OPTIONS = [
    {"name": "Python", "color": "BLUE", "description": "CPython runtime"},
    {"name": "Django", "color": "GREEN", "description": "Django framework"},
]
# Built-in Status option names (project defaults vary slightly across templates).
STATUS_DONE = ("Done", "Complete", "Completed")
STATUS_TODO = ("Todo", "To do", "Backlog", "Ready")
STATUS_ACTIVE = ("In Progress", "In progress", "Started")


def _rest(method, path, token):
    url = path if path.startswith("http") else f"{API}{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(req) as resp:
        link = resp.headers.get("Link", "")
        body = resp.read().decode()
    return (json.loads(body) if body else []), link


def _rest_paginated(path, token):
    items = []
    url = f"{API}{path}"
    while url:
        batch, link = _rest("GET", url, token)
        items.extend(batch)
        url = ""
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part[part.find("<") + 1 : part.find(">")]
    return items


def _graphql(query, token, variables=None):
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(GRAPHQL, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read().decode())
    if body.get("errors"):
        raise RuntimeError(json.dumps(body["errors"]))
    return body["data"]


def _list_org_projects(org, token):
    data = _graphql(
        "query($login:String!){organization(login:$login){"
        "projectsV2(first:100){nodes{id title number}}}}",
        token,
        {"login": org},
    )
    return data["organization"]["projectsV2"]["nodes"]


def _find_project(org, title, token):
    for node in _list_org_projects(org, token):
        if node["title"] == title:
            return node
    return None


def _get_project_by_number(org, number, token):
    data = _graphql(
        "query($login:String!,$n:Int!){organization(login:$login){"
        "projectV2(number:$n){id title number}}}",
        token,
        {"login": org, "n": number},
    )
    return data["organization"]["projectV2"]


def _project_fields(project_id, token):
    data = _graphql(
        "query($p:ID!){node(id:$p){... on ProjectV2{fields(first:50){nodes{"
        "__typename ... on ProjectV2FieldCommon{id name}"
        " ... on ProjectV2SingleSelectField{id name options{id name}}}}}}}",
        token,
        {"p": project_id},
    )
    out = {}
    for node in data["node"]["fields"]["nodes"]:
        if not node:
            continue
        out[node["name"]] = node
    return out


def _create_field(project_id, name, dtype, token):
    if dtype == "SINGLE_SELECT":
        data = _graphql(
            "mutation($p:ID!,$name:String!,"
            "$opts:[ProjectV2SingleSelectFieldOptionInput!]!){"
            "createProjectV2Field(input:{projectId:$p,"
            "dataType:SINGLE_SELECT,name:$name,"
            "singleSelectOptions:$opts}){projectV2Field{"
            "... on ProjectV2SingleSelectField{"
            "id name options{id name}}}}}",
            token,
            {"p": project_id, "name": name, "opts": ECOSYSTEM_OPTIONS},
        )
        return data["createProjectV2Field"]["projectV2Field"]
    data = _graphql(
        "mutation($p:ID!,$name:String!,$dt:ProjectV2CustomFieldType!){"
        "createProjectV2Field(input:{projectId:$p,dataType:$dt,name:$name}){"
        "projectV2Field{... on ProjectV2FieldCommon{id name}}}}",
        token,
        {"p": project_id, "name": name, "dt": dtype},
    )
    return data["createProjectV2Field"]["projectV2Field"]


def _ensure_fields(project_id, token):
    fields = _project_fields(project_id, token)
    for name, dtype in FIELDS.items():
        if name in fields:
            continue
        print(f"creating field '{name}' ({dtype})")
        try:
            _create_field(project_id, name, dtype, token)
        except RuntimeError as exc:
            if "reserved" in str(exc).lower():
                print(f"::warning::field '{name}' is reserved by GitHub; skipping")
                continue
            raise
    return _project_fields(project_id, token)


def _existing_items(project_id, token):
    """Return current project items with their content type and node id."""
    items = []
    cursor = None
    while True:
        data = _graphql(
            "query($p:ID!,$c:String){node(id:$p){"
            "... on ProjectV2{items(first:100,after:$c){"
            "pageInfo{hasNextPage endCursor} nodes{id content{__typename "
            "... on Issue{id title} ... on DraftIssue{title}}}}}}}",
            token,
            {"p": project_id, "c": cursor},
        )
        block = data["node"]["items"]
        for node in block["nodes"]:
            content = node.get("content") or {}
            items.append(
                {
                    "item_id": node["id"],
                    "type": content.get("__typename"),
                    "content_id": content.get("id"),
                    "title": content.get("title"),
                }
            )
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
    return items


def _add_item(project_id, content_id, token):
    data = _graphql(
        "mutation($p:ID!,$c:ID!){addProjectV2ItemById("
        "input:{projectId:$p,contentId:$c}){item{id}}}",
        token,
        {"p": project_id, "c": content_id},
    )
    return data["addProjectV2ItemById"]["item"]["id"]


def _delete_item(project_id, item_id, token):
    _graphql(
        "mutation($p:ID!,$i:ID!){deleteProjectV2Item("
        "input:{projectId:$p,itemId:$i}){deletedItemId}}",
        token,
        {"p": project_id, "i": item_id},
    )


def _set_text(project_id, item_id, field_id, value, token):
    _graphql(
        "mutation($p:ID!,$i:ID!,$f:ID!,$v:String!){"
        "updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,"
        "fieldId:$f,value:{text:$v}}){projectV2Item{id}}}",
        token,
        {"p": project_id, "i": item_id, "f": field_id, "v": value},
    )


def _set_date(project_id, item_id, field_id, value, token):
    _graphql(
        "mutation($p:ID!,$i:ID!,$f:ID!,$v:Date!){"
        "updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,"
        "fieldId:$f,value:{date:$v}}){projectV2Item{id}}}",
        token,
        {"p": project_id, "i": item_id, "f": field_id, "v": value},
    )


def _set_select(project_id, item_id, field_id, option_id, token):
    _graphql(
        "mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue("
        "input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){"
        "projectV2Item{id}}}",
        token,
        {"p": project_id, "i": item_id, "f": field_id, "o": option_id},
    )


def _option_id(field, option_name):
    for opt in field.get("options", []):
        if opt["name"] == option_name:
            return opt["id"]
    return None


def _option_id_any(field, names):
    for name in names:
        opt = _option_id(field, name)
        if opt:
            return opt, name
    return None, None


def _status_option(field, complete, in_progress):
    """Map rollup readiness onto the built-in Status single-select."""
    if field is None:
        return None, None
    if complete:
        return _option_id_any(field, STATUS_DONE)
    if in_progress:
        opt, name = _option_id_any(field, STATUS_ACTIVE)
        if opt:
            return opt, name
    return _option_id_any(field, STATUS_TODO)


def _load_timeline(ref):
    try:
        with urllib.request.urlopen(TIMELINE_URL.format(ref=ref)) as resp:
            return yaml.safe_load(resp.read().decode())
    except (urllib.error.URLError, yaml.YAMLError) as exc:
        print(f"::warning::could not read timeline ({exc}); dates omitted")
        return {}


def _version_map(timeline):
    """Map (ecosystem label, version) -> {release, eol} from the shared timeline."""
    out = {}
    for key, label in (("python", "Python"), ("django", "Django")):
        for entry in (timeline or {}).get(key, []):
            out[(label, str(entry["version"]))] = {
                "release": entry.get("release", ""),
                "eol": entry.get("eol", ""),
            }
    return out


def _active_versions(timeline):
    """Active rollup versions: eol >= today; Django LTS only (matches compat-rollup)."""
    today = dt.date.today()
    active = set()
    for key, label in (("python", "Python"), ("django", "Django")):
        for entry in (timeline or {}).get(key, []):
            if key == "django" and not entry.get("lts"):
                continue
            eol = dt.date.fromisoformat(entry["eol"])
            if eol < today:
                continue
            active.add((label, str(entry["version"])))
    return active


def _discover(hub, token, version_map, active):
    """Yield desired board items from active rollup parents (open or complete)."""
    rows = []
    # state=all: fully-adapted parents are closed but must stay on the roadmap
    # until EOL so Released→EOL bars remain visible.
    issues = _rest_paginated(
        f"/repos/{hub}/issues?state=all&labels={ROLLUP_LABEL}", token
    )
    for issue in issues:
        if "pull_request" in issue:
            continue
        title = issue.get("title", "")
        if not title.startswith(PARENT_PREFIX):
            continue
        label = title[len(PARENT_PREFIX) :].strip()
        ecosystem, _, version = label.partition(" ")
        key = (ecosystem, version)
        if key not in active:
            continue
        dates = version_map.get(key, {})
        complete = issue.get("state") == "closed"
        rows.append(
            {
                "content_id": issue["node_id"],
                "title": title,
                "ecosystem": ecosystem,
                "version": version,
                "start": dates.get("release", ""),
                "eol": dates.get("eol", ""),
                # Parent issue state already mirrors sub-issue completion
                # (compat-rollup): closed = all packages done, open = work left.
                "complete": complete,
                "in_progress": False,
            }
        )
    return rows


def main():
    token = os.environ["GH_TOKEN"]
    org = os.environ["ORG"]
    hub = os.environ.get("HUB_REPO", f"{org}/compatibility")
    project_title = os.environ.get("PROJECT_TITLE", "Compatibility Roadmap")
    project_number = os.environ.get("PROJECT_NUMBER", "").strip()
    timeline_ref = os.environ.get("TIMELINE_REF", "main")
    dry_run = os.environ.get("DRY_RUN") == "1"

    timeline = _load_timeline(timeline_ref)
    desired = _discover(hub, token, _version_map(timeline), _active_versions(timeline))
    print(f"discovered {len(desired)} rollup parent issue(s) on {hub}")

    project = None
    if project_number:
        project = _get_project_by_number(org, int(project_number), token)
        if project:
            print(
                f"resolved project #{project['number']} '{project['title']}' by number"
            )
    if project is None:
        project = _find_project(org, project_title, token)

    if dry_run:
        for row in desired:
            state = "Done" if row["complete"] else "open"
            print(
                f"[dry-run] track '{row['title']}' status={state} "
                f"(released {row['start'] or 'n/a'} -> EOL {row['eol'] or 'n/a'})"
            )
        print(f"compat board: desired={len(desired)} (dry-run, no writes)")
        return

    if project is None:
        visible = _list_org_projects(org, token)
        listing = ", ".join(f"#{p['number']} '{p['title']}'" for p in visible) or "none"
        print(f"::warning::App sees these org projects: {listing}")
        print(
            f"::error::Project '{project_title}' not found. Ensure it is org-owned "
            "(orgs/<org>/projects), titled exactly "
            f"'{project_title}' or addressed via PROJECT_NUMBER, then re-run."
        )
        sys.exit(1)

    project_id = project["id"]
    fields = _ensure_fields(project_id, token)
    existing = _existing_items(project_id, token)
    status_field = fields.get("Status")

    desired_ids = set()
    tracked = done = 0
    for row in desired:
        item_id = _add_item(project_id, row["content_id"], token)
        desired_ids.add(row["content_id"])
        tracked += 1
        eco = fields.get("Ecosystem")
        eco_opt = _option_id(eco, row["ecosystem"]) if eco else None
        if eco_opt:
            _set_select(project_id, item_id, eco["id"], eco_opt, token)
        if "Version" in fields:
            _set_text(
                project_id, item_id, fields["Version"]["id"], row["version"], token
            )
        if row["start"] and "Released" in fields:
            _set_date(
                project_id, item_id, fields["Released"]["id"], row["start"], token
            )
        if row["eol"] and "EOL" in fields:
            _set_date(project_id, item_id, fields["EOL"]["id"], row["eol"], token)
        status_opt, status_name = _status_option(
            status_field, row["complete"], row["in_progress"]
        )
        if status_opt:
            _set_select(project_id, item_id, status_field["id"], status_opt, token)
            if row["complete"]:
                done += 1
            print(f"status '{row['title']}' -> {status_name}")

    # Remove anything that is not a current rollup parent: stale draft items from
    # the previous per-repo board and issues for versions now past EOL.
    removed = 0
    for item in existing:
        keep = item["type"] == "Issue" and item.get("content_id") in desired_ids
        if keep:
            continue
        _delete_item(project_id, item["item_id"], token)
        removed += 1

    print(
        f"compat board #{project['number']}: tracked={tracked} complete={done} "
        f"removed-stale={removed} total={len(desired)}"
    )


if __name__ == "__main__":
    try:
        main()
    except (urllib.error.HTTPError, RuntimeError) as exc:
        detail = (
            exc.read().decode() if isinstance(exc, urllib.error.HTTPError) else str(exc)
        )
        print(f"::error::compat board sync failed: {detail}", file=sys.stderr)
        sys.exit(1)
