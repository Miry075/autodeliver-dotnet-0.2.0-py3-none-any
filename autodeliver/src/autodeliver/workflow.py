"""Work item status handling: the item and its child tasks move through the board together.

Three moments, all configurable:
  start     (on_start)    the item and its open children go To Do -> in progress (Doing / Active / In Progress)
  success   (on_success)  after the draft PR exists, open children follow the item to the dev-done state
  complete  (on_complete) `autodeliver complete`, run by a human after the merge: item and children -> Done / Resolved

Nothing here ever writes Closed, and a failure to write a state never stops or escalates a run: it is logged and
shown in the evidence pack.
"""
from __future__ import annotations

from typing import Any

# States that mean "finished or abandoned": never moved by a cascade.
TERMINAL = {"done", "closed", "resolved", "completed", "removed", "cut"}
NEVER = {"closed"}


def pick_state(available: list[str], preferred: list[str]) -> str | None:
    """First preferred state that the work item type really has (case-insensitive), as the process spells it."""
    lookup = {s.lower(): s for s in available}
    for want in preferred:
        if want.lower() in lookup and want.lower() not in NEVER:
            return lookup[want.lower()]
    return None


def children_of(wi: Any) -> list[dict[str, str]]:
    return [c for c in (wi.linked or []) if c.get("relation") == "child"]


class Mover:
    """Plans and applies state changes for a work item and its children against an Azure DevOps client."""

    def __init__(self, ado: Any):
        self.ado = ado
        self._states: dict[str, list[str]] = {}

    def states(self, work_item_type: str) -> list[str]:
        if work_item_type not in self._states:
            self._states[work_item_type] = list(self.ado.type_states(work_item_type))
        return self._states[work_item_type]

    def plan(self, wi: Any, preferred: list[str], *, cascade: bool, include_item: bool = True,
             child_current: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        """-> (changes, notes). A change is {id, type, title, from, to}. Items already there or finished are skipped."""
        changes: list[dict[str, Any]] = []
        notes: list[str] = []
        targets: list[tuple[str, str, str, str]] = []
        if include_item:
            targets.append((str(wi.id), wi.type, wi.title, wi.state))
        if cascade:
            for c in children_of(wi):
                state = (child_current or {}).get(str(c["id"]), c.get("state", ""))
                targets.append((str(c["id"]), c.get("type", ""), c.get("title", ""), state))
        for wid, wtype, title, current in targets:
            if not wtype:
                notes.append(f"{wid}: type unknown, left alone")
                continue
            to = pick_state(self.states(wtype), preferred)
            if to is None:
                notes.append(f"{wid} ({wtype}): none of {preferred} exists for this type {self.states(wtype)}; left alone")
            elif current.lower() == to.lower():
                continue
            elif current.lower() in TERMINAL:
                notes.append(f"{wid}: already {current}, left alone")
            else:
                changes.append({"id": int(wid), "type": wtype, "title": title, "from": current, "to": to})
        return changes, notes

    def apply(self, changes: list[dict[str, Any]]) -> list[str]:
        """Write each change on its own; returns the problems. A change that worked gets `done: True`."""
        problems: list[str] = []
        for ch in changes:
            try:
                self.ado.set_state(ch["id"], ch["to"])
                ch["done"] = True
            except Exception as e:  # noqa: BLE001 - a rejected transition must not stop the delivery
                ch["done"] = False
                problems.append(f"{ch['id']} {ch['from']} -> {ch['to']} rejected: {str(e)[:160]}")
        return problems

    def revert(self, changes: list[dict[str, Any]]) -> list[str]:
        problems: list[str] = []
        for ch in changes:
            if not ch.get("done") or ch.get("reverted"):
                continue
            try:
                self.ado.set_state(ch["id"], ch["from"])
                ch["reverted"] = True
            except Exception as e:  # noqa: BLE001
                problems.append(f"{ch['id']} could not go back to {ch['from']}: {str(e)[:160]}")
        return problems


def describe(changes: list[dict[str, Any]]) -> list[str]:
    return [f"AB#{c['id']} ({c['type']}) {c['from']} -> {c['to']}" + ("" if c.get("done", True) else " [FAILED]")
            for c in changes]
