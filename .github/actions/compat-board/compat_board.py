"""Sync an org-level GitHub Project v2 roadmap from per-repo compat milestones.

This is the aggregated, cross-repository dashboard for the DLRSP fleet
(Phase 2). It mirrors the native per-repo ``compat: <Ecosystem> <version>``
milestones onto a single organization Project v2, one draft item per
(repository x version), with fields Repo / Ecosystem / Version / EOL /
Compat state. A single org-level runner owns the board, so there is no race
to create the project (unlike a per-repo sync).

Onboarded repositories are discovered by the presence of ``compat:`` milestones
(no fleet registry is duplicated into CI). The board therefore grows naturally
as the milestone caller rolls out to more modules.

Projects v2 is only reachable through GraphQL with an org-scoped token that has
``organization-projects: write`` (the repository GITHUB_TOKEN cannot access it).

Environment:
    GH_TOKEN       org-scoped App installation token (org Projects RW + issues read)
    ORG            organization login (e.g. DLRSP)
    PROJECT_TITLE  Project v2 title to ensure/create
    DRY_RUN        when "1", report actions without writing
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
GRAPHQL = f"{API}/graphql"

FIELDS = {
    "Repo": "TEXT",
    "Ecosystem": "SINGLE_SELECT",
    "Version": "TEXT",
    "EOL": "DATE",
    "Compat state": "SINGLE_SELECT",
}
ECOSYSTEM_OPTIONS = [
    {"name": "Python", "color": "BLUE", "description": "CPython runtime"},
    {"name": "Django", "color": "GREEN", "description": "Django framework"},
]
STATE_OPTIONS = [
    {"name": "Active", "color": "GREEN", "description": "Supported, before EOL"},
    {"name": "EOL passed", "color": "RED", "description": "Past end of life"},
]


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


def _org_id(org, token):
    data = _graphql(
        "query($login:String!){organization(login:$login){id}}", token, {"login": org}
    )
    return data["organization"]["id"]


def _find_project(org, title, token):
    data = _graphql(
        "query($login:String!){organization(login:$login){"
        "projectsV2(first:100){nodes{id title number}}}}",
        token,
        {"login": org},
    )
    for node in data["organization"]["projectsV2"]["nodes"]:
        if node["title"] == title:
            return node
    return None


def _create_project(owner_id, title, token):
    data = _graphql(
        "mutation($owner:ID!,$title:String!){createProjectV2("
        "input:{ownerId:$owner,title:$title}){projectV2{id title number}}}",
        token,
        {"owner": owner_id, "title": title},
    )
    return data["createProjectV2"]["projectV2"]


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
        options = ECOSYSTEM_OPTIONS if name == "Ecosystem" else STATE_OPTIONS
        data = _graphql(
            "mutation($p:ID!,$name:String!,"
            "$opts:[ProjectV2SingleSelectFieldOptionInput!]!){"
            "createProjectV2Field(input:{projectId:$p,"
            "dataType:SINGLE_SELECT,name:$name,"
            "singleSelectOptions:$opts}){projectV2Field{"
            "... on ProjectV2SingleSelectField{"
            "id name options{id name}}}}}",
            token,
            {"p": project_id, "name": name, "opts": options},
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
        if name not in fields:
            _create_field(project_id, name, dtype, token)
    return _project_fields(project_id, token)


def _existing_items(project_id, token):
    items = {}
    cursor = None
    while True:
        data = _graphql(
            "query($p:ID!,$c:String){node(id:$p){"
            "... on ProjectV2{items(first:100,after:$c){"
            "pageInfo{hasNextPage endCursor} nodes{id content{__typename "
            "... on DraftIssue{title}}}}}}}",
            token,
            {"p": project_id, "c": cursor},
        )
        block = data["node"]["items"]
        for node in block["nodes"]:
            content = node.get("content") or {}
            title = content.get("title")
            if title:
                items[title] = node["id"]
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
    return items


def _add_draft(project_id, title, body, token):
    data = _graphql(
        "mutation($p:ID!,$t:String!,$b:String!){addProjectV2DraftIssue("
        "input:{projectId:$p,title:$t,body:$b}){projectItem{id}}}",
        token,
        {"p": project_id, "t": title, "b": body},
    )
    return data["addProjectV2DraftIssue"]["projectItem"]["id"]


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


def _discover(org, token):
    """Yield desired board items from every repo carrying compat: milestones."""
    rows = []
    repos = _rest_paginated(f"/orgs/{org}/repos?per_page=100&type=all", token)
    for repo in repos:
        full = repo["full_name"]
        try:
            milestones = _rest_paginated(
                f"/repos/{full}/milestones?state=all&per_page=100", token
            )
        except urllib.error.HTTPError:
            continue
        for milestone in milestones:
            title = milestone.get("title", "")
            if not title.startswith("compat: "):
                continue
            label = title[len("compat: ") :].strip()
            ecosystem, _, version = label.partition(" ")
            due = (milestone.get("due_on") or "")[:10]
            rows.append(
                {
                    "repo": repo["name"],
                    "full": full,
                    "ecosystem": ecosystem,
                    "version": version,
                    "eol": due,
                    "state": (
                        "EOL passed" if milestone.get("state") == "closed" else "Active"
                    ),
                    "title": f"{repo['name']} \u00b7 {ecosystem} {version}",
                }
            )
    return rows


def main():
    token = os.environ["GH_TOKEN"]
    org = os.environ["ORG"]
    project_title = os.environ.get("PROJECT_TITLE", "DLRSP Compatibility Roadmap")
    dry_run = os.environ.get("DRY_RUN") == "1"

    desired = _discover(org, token)
    print(f"discovered {len(desired)} compat milestone(s) across the fleet")

    project = _find_project(org, project_title, token)

    if dry_run:
        if project is None:
            print(
                f"[dry-run] create org Project v2 '{project_title}'"
                f" + fields {list(FIELDS)}"
            )
        for row in desired:
            print(
                f"[dry-run] upsert '{row['title']}' "
                f"(EOL {row['eol'] or 'n/a'}, {row['state']})"
            )
        print(f"compat board: desired={len(desired)} (dry-run, no writes)")
        return

    if project is None:
        try:
            project = _create_project(_org_id(org, token), project_title, token)
        except RuntimeError as exc:
            if "FORBIDDEN" in str(exc) or "create projects" in str(exc):
                print(
                    f"::error::Project '{project_title}' not found and the App "
                    "cannot create org projects. Create it once in the org "
                    "(Projects > New project), set its title to exactly "
                    f"'{project_title}', then re-run this workflow."
                )
                sys.exit(1)
            raise
        print(f"created org Project v2 #{project['number']} '{project_title}'")
    project_id = project["id"]
    fields = _ensure_fields(project_id, token)
    existing = _existing_items(project_id, token)

    created = updated = 0
    for row in desired:
        item_id = existing.get(row["title"])
        if item_id is None:
            body = (
                f"Compatibility tracking for {row['ecosystem']} {row['version']} "
                f"on {row['full']}. EOL {row['eol'] or 'n/a'}. "
                f"Repo milestone: https://github.com/{row['full']}/milestones"
            )
            item_id = _add_draft(project_id, row["title"], body, token)
            created += 1
        else:
            updated += 1
        _set_text(project_id, item_id, fields["Repo"]["id"], row["repo"], token)
        _set_text(project_id, item_id, fields["Version"]["id"], row["version"], token)
        eco_opt = _option_id(fields["Ecosystem"], row["ecosystem"])
        if eco_opt:
            _set_select(project_id, item_id, fields["Ecosystem"]["id"], eco_opt, token)
        state_opt = _option_id(fields["Compat state"], row["state"])
        if state_opt:
            _set_select(
                project_id, item_id, fields["Compat state"]["id"], state_opt, token
            )
        if row["eol"]:
            _set_date(project_id, item_id, fields["EOL"]["id"], row["eol"], token)

    print(
        f"compat board #{project['number']}: created={created} "
        f"updated={updated} total={len(desired)}"
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
