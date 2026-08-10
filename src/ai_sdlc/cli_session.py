"""CLI surface for interactive (IDE / slash-command) mode.

These commands expose the orchestrator's decisions one at a time so an
interactive Claude Code session can perform the work while Python keeps the
state machine: it picks the task, counts retries, verifies the result,
commits, and writes the audit log. Kept separate from cli.py, which already
carries the headless surface.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from ai_sdlc.governance.branching import checkout_branch, current_branch
from ai_sdlc.observability.audit import AuditLog
from ai_sdlc.orchestrator.engine import recommend_branch
from ai_sdlc.state.plan import PlanDocument
from ai_sdlc.workspace import Workspace


def _require_workspace(root: Path) -> Workspace:
    ws = Workspace(root)
    if not ws.exists():
        raise SystemExit(f"error: {ws.state_dir} not found; run: ai-sdlc init --workspace {root}")
    return ws


def _new_audit(ws: Workspace) -> AuditLog:
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    return AuditLog(ws.runs_dir, run_id)


def _emit(payload: dict, as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        for line in lines:
            print(line)


def cmd_branch(args) -> int:
    ws = _require_workspace(args.workspace)
    doc = PlanDocument.load(ws.plan_path)
    recommended, source = recommend_branch(ws)
    payload = {
        "recommended": recommended,
        "source": source,
        "pinned": doc.meta.get("branch"),
        "current": current_branch(ws.root),
        "created": False,
    }

    if not args.use:
        _emit(
            payload,
            args.json,
            [
                f"recommended: {recommended}  ({source})",
                f"pinned in plan: {payload['pinned'] or '(none)'}",
                f"currently checked out: {payload['current'] or '(none)'}",
                "",
                f"to use it: ai-sdlc branch --use {recommended}",
            ],
        )
        return 0

    if not (ws.root / ".git").is_dir():
        print("error: workspace is not a git repository", file=sys.stderr)
        return 1

    audit = _new_audit(ws)
    reason = (
        "recommended default accepted by human"
        if args.use == recommended
        else "chosen by human"
    )
    ok = checkout_branch(ws.root, args.use)
    audit.event("decision", subject="branch_selection", choice=args.use, reasons=[reason])
    audit.event("branch", name=args.use, ok=ok)
    if not ok:
        print(f"error: could not check out branch {args.use}", file=sys.stderr)
        return 1
    doc.set_meta(branch=args.use)
    doc.save()
    payload.update({"pinned": args.use, "current": current_branch(ws.root), "created": True})
    _emit(payload, args.json, [f"branch {args.use} checked out and pinned in the plan"])
    return 0


def cmd_remote(args) -> int:
    from ai_sdlc.governance.branching import remote_url, set_remote

    ws = _require_workspace(args.workspace)
    if not (ws.root / ".git").is_dir():
        print("error: workspace is not a git repository", file=sys.stderr)
        return 1
    if not args.set:
        url = remote_url(ws.root)
        _emit(
            {"origin": url, "changed": False},
            args.json,
            [
                f"origin: {url}" if url else "origin: (none configured)",
                "" if url else "set one with: ai-sdlc remote --set <repository-url>",
            ],
        )
        return 0
    audit = _new_audit(ws)
    ok = set_remote(ws.root, args.set)
    audit.event(
        "decision",
        subject="remote_set",
        choice=args.set,
        reasons=["human supplied the remote url"],
    )
    audit.event("remote", url=args.set, ok=ok)
    if not ok:
        print(f"error: could not set origin to {args.set}", file=sys.stderr)
        return 1
    _emit({"origin": args.set, "changed": True}, args.json, [f"origin set to {args.set}"])
    return 0
