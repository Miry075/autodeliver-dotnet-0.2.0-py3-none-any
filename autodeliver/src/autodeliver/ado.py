"""Azure DevOps REST client (api-version 7.1) plus HTML -> criteria extraction.

Auth: SYSTEM_ACCESSTOKEN (pipeline, Bearer) or AZURE_DEVOPS_PAT (local, Basic).

Fetch follows the azure-workitem-flow convention: fields, the full comment thread (requirements often live
there), the Security Criteria field when the template has one, and one level of linked items.
Writes are limited to: comment, tags, state, Completed/Remaining work, draft PR + PR thread. The client
has no method to edit the description, approve, or complete a PR.
"""
from __future__ import annotations

import base64
import html
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import requests

API = "api-version=7.1"
COMMENTS_API = "api-version=7.1-preview.4"
LINK_TYPES = {
    "System.LinkTypes.Hierarchy-Reverse": "parent",
    "System.LinkTypes.Hierarchy-Forward": "child",
    "System.LinkTypes.Related": "related",
}
COMPLETED = "Microsoft.VSTS.Scheduling.CompletedWork"
REMAINING = "Microsoft.VSTS.Scheduling.RemainingWork"


@dataclass
class WorkItem:
    id: int
    type: str
    state: str
    title: str
    description_text: str
    acceptance_text: str
    acceptance_html: str
    tags: list[str]
    security_html: str = ""
    comments: list[str] = field(default_factory=list)          # verbatim text, oldest first
    linked: list[dict[str, str]] = field(default_factory=list)  # {id, relation, type, title, state}
    completed_work: float | None = None
    remaining_work: float | None = None


class _ListParser(HTMLParser):
    """Collects <li> items; falls back to block-level lines when the field has no list."""

    def __init__(self) -> None:
        super().__init__()
        self.items: list[str] = []
        self.lines: list[str] = [""]
        self._li_depth = 0
        self._li_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "li":
            self._li_depth += 1
            self._li_buf = []
        if tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.lines.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._li_depth:
            self._li_depth -= 1
            self.items.append(" ".join("".join(self._li_buf).split()))
        if tag in ("p", "div", "li", "tr"):
            self.lines.append("")

    def handle_data(self, data: str) -> None:
        if self._li_depth:
            self._li_buf.append(data)
        self.lines[-1] += data


_PREFIX = re.compile(r"^\s*(?:(?:AC|SC)\s*-?\s*\d+\s*[:.)-]?|\d+\s*[.)]|[-*•]|\[\s?[xX ]?\s?\])\s*")


def html_to_text(value: str) -> str:
    p = _ListParser()
    p.feed(value or "")
    return "\n".join(" ".join(line.split()) for line in p.lines if line.strip())


def extract_criteria(field_html: str, prefix: str = "AC") -> list[dict[str, str]]:
    """Deterministic split. Ids are assigned here (AC-1..n / SC-1..n) and never by an agent."""
    p = _ListParser()
    p.feed(field_html or "")
    raw = p.items or [l for l in p.lines]
    items = [_PREFIX.sub("", html.unescape(" ".join(r.split()))).strip() for r in raw]
    items = [i for i in items if len(i) >= 8]
    source = "security" if prefix == "SC" else "acceptance"
    return [{"id": f"{prefix}-{n}", "text": text, "source": source} for n, text in enumerate(items, start=1)]


def extract_acceptance_criteria(acceptance_html: str) -> list[dict[str, str]]:
    return extract_criteria(acceptance_html, "AC")


def _security_field(fields: dict[str, Any], configured: str) -> str:
    if configured:
        return fields.get(configured, "") or ""
    for name, value in fields.items():
        if name.lower().endswith("securitycriteria") and isinstance(value, str):
            return value
    return ""


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class AzureDevOps:
    def __init__(self, cfg: dict):
        ado = cfg["azure_devops"]
        self.org = ado["organization_url"].rstrip("/")
        self.project = ado["project"]
        self.repository = ado["repository"]
        self.security_field = ado.get("security_criteria_field", "")
        self.session = requests.Session()
        token = os.environ.get("SYSTEM_ACCESSTOKEN")
        pat = os.environ.get("AZURE_DEVOPS_PAT")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        elif pat:
            self.session.headers["Authorization"] = "Basic " + base64.b64encode(f":{pat}".encode()).decode()
        else:
            raise RuntimeError("set SYSTEM_ACCESSTOKEN (pipeline) or AZURE_DEVOPS_PAT (local)")
        self._repo_id: str | None = None

    def _url(self, path: str, api: str = API) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self.org}/{self.project}/_apis/{path}{sep}{api}"

    def _check(self, r: requests.Response) -> dict:
        if r.status_code >= 400:
            raise RuntimeError(f"Azure DevOps {r.request.method} {r.url.split('?')[0]} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    # --- work items -----------------------------------------------------------------------------
    def get_work_item(self, wid: int) -> WorkItem:
        data = self._check(self.session.get(self._url(f"wit/workitems/{wid}?$expand=all"), timeout=30))
        f = data["fields"]
        ac_html = f.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or ""
        return WorkItem(
            id=wid,
            type=f.get("System.WorkItemType", ""),
            state=f.get("System.State", ""),
            title=f.get("System.Title", ""),
            description_text=html_to_text(f.get("System.Description", "") or f.get("Microsoft.VSTS.TCM.ReproSteps", "") or ""),
            acceptance_text=html_to_text(ac_html),
            acceptance_html=ac_html,
            tags=[t.strip() for t in (f.get("System.Tags", "") or "").split(";") if t.strip()],
            security_html=_security_field(f, self.security_field),
            comments=self._comments(wid),
            linked=self._linked(data.get("relations") or []),
            completed_work=_num(f.get(COMPLETED)),
            remaining_work=_num(f.get(REMAINING)),
        )

    def _comments(self, wid: int) -> list[str]:
        out: list[str] = []
        url = self._url(f"wit/workItems/{wid}/comments?order=asc&$top=200", COMMENTS_API)
        while url:
            data = self._check(self.session.get(url, timeout=30))
            out += [html_to_text(c.get("text", "")) for c in data.get("comments", []) if c.get("text")]
            token = data.get("continuationToken")
            url = self._url(f"wit/workItems/{wid}/comments?order=asc&$top=200&continuationToken={token}", COMMENTS_API) if token else ""
        return out

    def _linked(self, relations: list[dict]) -> list[dict[str, str]]:
        wanted = [(r["url"].rstrip("/").rsplit("/", 1)[-1], LINK_TYPES[r["rel"]]) for r in relations if r.get("rel") in LINK_TYPES]
        if not wanted:
            return []
        ids = ",".join(i for i, _ in wanted[:50])
        fields = "System.Id,System.WorkItemType,System.Title,System.State"
        data = self._check(self.session.get(self._url(f"wit/workitems?ids={ids}&fields={fields}"), timeout=30))
        by_id = {str(w["id"]): w["fields"] for w in data.get("value", [])}
        return [{"id": i, "relation": rel, "type": by_id.get(i, {}).get("System.WorkItemType", ""),
                 "title": by_id.get(i, {}).get("System.Title", ""), "state": by_id.get(i, {}).get("System.State", "")}
                for i, rel in wanted]

    def type_states(self, work_item_type: str) -> list[str]:
        data = self._check(self.session.get(self._url(f"wit/workitemtypes/{requests.utils.quote(work_item_type)}/states"), timeout=30))
        return [s["name"] for s in data.get("value", [])]

    def next_eligible(self, required_tag: str, blocked_tag: str, types: list[str]) -> int | None:
        """Oldest work item carrying the opt-in tag and not blocked (dispatcher mode)."""
        esc = lambda v: v.replace("'", "''")
        type_list = ", ".join(f"'{esc(t)}'" for t in types)
        wiql = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
                f"AND [System.Tags] CONTAINS '{esc(required_tag)}' AND NOT [System.Tags] CONTAINS '{esc(blocked_tag)}' "
                f"AND [System.WorkItemType] IN ({type_list}) ORDER BY [System.ChangedDate] ASC")
        data = self._check(self.session.post(self._url("wit/wiql?$top=1"), json={"query": wiql}, timeout=30))
        items = data.get("workItems", [])
        return int(items[0]["id"]) if items else None

    def comment(self, wid: int, html_body: str) -> None:
        self._check(self.session.post(self._url(f"wit/workItems/{wid}/comments", COMMENTS_API),
                                      json={"text": html_body}, timeout=30))

    def set_tags(self, wid: int, tags: list[str]) -> None:
        self._patch(wid, [{"op": "add", "path": "/fields/System.Tags", "value": "; ".join(sorted(set(tags)))}])

    def set_state(self, wid: int, state: str) -> None:
        self._patch(wid, [{"op": "add", "path": "/fields/System.State", "value": state}])

    def update_fields(self, wid: int, values: dict[str, Any]) -> None:
        """One PATCH; ops in the given order (hours before state: many templates require hours to resolve)."""
        self._patch(wid, [{"op": "add", "path": f"/fields/{k}", "value": v} for k, v in values.items()])

    def _patch(self, wid: int, ops: list[dict]) -> None:
        self._check(self.session.patch(self._url(f"wit/workitems/{wid}"), json=ops, timeout=30,
                                       headers={"Content-Type": "application/json-patch+json"}))

    # --- pull requests ----------------------------------------------------------------------------
    def repo_id(self) -> str:
        if not self._repo_id:
            self._repo_id = self._check(self.session.get(self._url(f"git/repositories/{self.repository}"), timeout=30))["id"]
        return self._repo_id

    def create_pr(self, source: str, target: str, title: str, description: str, wid: int, draft: bool) -> dict:
        body = {
            "sourceRefName": f"refs/heads/{source}",
            "targetRefName": f"refs/heads/{target}",
            "title": title[:400],
            "description": description[:4000],          # Azure DevOps hard limit
            "isDraft": draft,
            "workItemRefs": [{"id": str(wid)}],
        }
        return self._check(self.session.post(self._url(f"git/repositories/{self.repo_id()}/pullrequests"), json=body, timeout=30))

    def add_pr_thread(self, pr_id: int, markdown: str) -> None:
        # status "closed": informational, so it never blocks a "resolve all comments" policy
        body = {"comments": [{"parentCommentId": 0, "content": markdown, "commentType": 1}], "status": "closed"}
        self._check(self.session.post(self._url(f"git/repositories/{self.repo_id()}/pullRequests/{pr_id}/threads"),
                                      json=body, timeout=30))

    def pr_web_url(self, pr: dict) -> str:
        return f"{self.org}/{self.project}/_git/{self.repository}/pullrequest/{pr['pullRequestId']}"


class OfflineAzureDevOps:
    """Dry-run / test stand-in: reads a work item from JSON, records every write instead of sending it."""

    def __init__(self, story: dict):
        self.story = story
        self.writes: list[tuple[str, Any]] = []

    def get_work_item(self, wid: int) -> WorkItem:
        s = self.story

        def as_list(key_html: str, key_list: str) -> str:
            raw = s.get(key_html) or "".join(f"<li>{html.escape(a)}</li>" for a in s.get(key_list, []))
            return f"<ul>{raw}</ul>" if "<li>" in raw and "<ul>" not in raw else raw

        ac_html = as_list("acceptance_html", "acceptance_criteria")
        return WorkItem(id=wid, type=s.get("type", "User Story"), state=s.get("state", "Active"), title=s["title"],
                        description_text=s.get("description", ""), acceptance_text=html_to_text(ac_html),
                        acceptance_html=ac_html, tags=s.get("tags", []),
                        security_html=as_list("security_html", "security_criteria"),
                        comments=list(s.get("comments", [])), linked=list(s.get("linked", [])),
                        completed_work=_num(s.get("completed_work")), remaining_work=_num(s.get("remaining_work")))

    def type_states(self, work_item_type: str) -> list[str]:
        return list(self.story.get("type_states", ["New", "Active", "Resolved", "Closed"]))

    def comment(self, wid: int, html_body: str) -> None:
        self.writes.append(("comment", html_body))

    def set_tags(self, wid: int, tags: list[str]) -> None:
        self.writes.append(("tags", tags))

    def set_state(self, wid: int, state: str) -> None:
        self.writes.append(("state", state))

    def update_fields(self, wid: int, values: dict[str, Any]) -> None:
        self.writes.append(("fields", dict(values)))

    def create_pr(self, source: str, target: str, title: str, description: str, wid: int, draft: bool) -> dict:
        self.writes.append(("pr", {"source": source, "target": target, "title": title, "description": description,
                                   "work_item": wid, "draft": draft}))
        return {"pullRequestId": 0}

    def add_pr_thread(self, pr_id: int, markdown: str) -> None:
        self.writes.append(("pr_thread", markdown))

    def pr_web_url(self, pr: dict) -> str:
        return "(offline: no PR created)"
