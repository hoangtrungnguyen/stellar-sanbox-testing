#!/usr/bin/env python3
"""End-to-end test with a dependency graph.

Creates the following structure in Plane (Stellar-Sandbox, project STELL):

        ┌──── Epic E "Auth Feature"
        │
   ┌────┴────┐
   │         │
  Story S1   Story S2          (parent = E)
   │         │
  ┌┴─┐       │
  │  │       │
  T1 T2 ──▶ T3                  T2 BLOCKS T3
  │  │      │
  └──┴──────┘
   (parent = S1 / S2)

Then mirrors the whole tree into the sandbox Grava Dolt DB with:
  - `plane:<seq>` labels per issue
  - parent-child rows in `dependencies` table (type='parent-child')
  - T2→T3 blocking row in `dependencies` table (type='blocks')

Then executes a realistic close-from-the-leaves workflow:

  T1: open → in_progress → closed   (sync after each transition)
  T2: open → in_progress → closed
  T3: open → in_progress → closed
  S1: open → in_progress → closed
  S2: open → in_progress → closed
   E: open → in_progress → closed

After every grava UPDATE the test fires `grava_plane_sync.py <id>` (simulating
a Grava agent's post-`grava signal` hook) and verifies the Plane work item's
state has propagated.

Run:
    python3 tests/e2e_dependency_flow.py             # keeps Plane issues
    python3 tests/e2e_dependency_flow.py --cleanup   # deletes Plane issues
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

# ─── Configuration ───────────────────────────────────────────────────────────

PLANE_HOST = "https://api.plane.so"
PLANE_WORKSPACE = "stellar-sandbox"
PLANE_PROJECT_ID = "cec88b42-b47c-4f1c-bfdf-a882c490a784"
PLANE_PROJECT_IDENTIFIER = "STELL"
PLANE_TYPE_EPIC = "4972657b-6486-422d-ab85-e689d6ad1284"
PLANE_TYPE_STORY = "c91cae6a-89b9-48f4-b585-e99fe5bcf75b"
PLANE_TYPE_TASK = "bfa0b143-b4a5-42f9-b460-3c333ee03d5b"
PLANE_API_TOKEN = "plane_api_6ad1c033c54146e0a09cc6e7eaf884f3"

PLANE_STATES = {
    "Backlog":     "c81ef0d0-94c8-42cf-8cca-fccc07b12c34",
    "Todo":        "293ca4c1-dad0-4dc8-a67a-ea838743274d",
    "In Progress": "8cd744b2-6104-49bb-998f-5e91ae2088c9",
    "Done":        "12fc0d73-f7b9-475a-b2f5-b14a5c0e97f9",
    "Cancelled":   "2984876f-ce67-4d11-ab30-1fe19771d838",
}
PLANE_STATE_NAME_BY_UUID = {v: k for k, v in PLANE_STATES.items()}
GRAVA_TO_PLANE = {"open": "Todo", "in_progress": "In Progress", "closed": "Done"}

SANDBOX_ROOT = Path("/Users/trungnguyenhoang/IdeaProjects/stellar-sand-box")
DOLT_DIR = SANDBOX_ROOT / ".grava" / "dolt"
STATE_FILE = SANDBOX_ROOT / "tests" / ".sync-state.json"
SYSTEM_YAML = SANDBOX_ROOT / "tests" / "system.yaml"

STELLAR_ENGINE = Path(
    os.environ.get(
        "STELLAR_ENGINE_HOME",
        "/Users/trungnguyenhoang/IdeaProjects/stellar-engine/.claude/worktrees/"
        "infallible-dirac-c1d4e5",
    )
)
SYNC_SCRIPT = STELLAR_ENGINE / "agents" / "task-generator" / "cli" / "grava_plane_sync.py"


# ─── Logging ─────────────────────────────────────────────────────────────────


def log(msg: str, level: str = "INFO") -> None:
    colour = {"INFO": "\033[36m", "OK": "\033[32m", "ERR": "\033[31m",
              "STEP": "\033[35m", "URL": "\033[34m"}.get(level, "")
    print(f"{colour}[{level}]\033[0m {msg}", flush=True)


class FlowFail(Exception):
    pass


# ─── Plane API ───────────────────────────────────────────────────────────────


def _plane_token() -> str:
    env = os.environ.get("PLANE_API_TOKEN")
    if env:
        return env
    return PLANE_API_TOKEN


def _plane(method: str, path: str, **kw) -> dict | list:
    headers = {"X-API-Key": _plane_token(), "Content-Type": "application/json"}
    url = f"{PLANE_HOST}/api/v1/workspaces/{PLANE_WORKSPACE}/{path.lstrip('/')}"
    resp = requests.request(method, url, headers=headers, timeout=30, **kw)
    if resp.status_code >= 400:
        raise FlowFail(f"Plane {method} {path} → {resp.status_code}: {resp.text[:300]}")
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def plane_create(name: str, type_id: str, parent: str | None = None) -> dict:
    payload = {
        "name": name,
        "type_id": type_id,
        "description_html": "<p>created by e2e_dependency_flow.py</p>",
    }
    if parent:
        payload["parent"] = parent
    return _plane("POST", f"projects/{PLANE_PROJECT_ID}/work-items/", json=payload)


def plane_get(work_id: str) -> dict:
    return _plane("GET", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/")


def plane_delete(work_id: str) -> None:
    _plane("DELETE", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/")


def plane_add_blocks(src_id: str, dst_id: str) -> Any:
    """Post a `blocking` relation from src to dst."""
    return _plane(
        "POST",
        f"projects/{PLANE_PROJECT_ID}/issues/{src_id}/relations/",
        json={"relation_type": "blocking", "issues": [dst_id]},
    )


def plane_url(seq: int | str) -> str:
    return f"https://app.plane.so/{PLANE_WORKSPACE}/browse/{PLANE_PROJECT_IDENTIFIER}-{seq}/"


# ─── Dolt helpers ────────────────────────────────────────────────────────────


def dolt_exec(query: str) -> None:
    res = subprocess.run(
        ["dolt", "sql", "-q", query],
        cwd=str(DOLT_DIR), capture_output=True, text=True, timeout=15,
    )
    if res.returncode != 0:
        raise FlowFail(f"dolt exec failed: {res.stderr[:200]}")


def reset_dolt() -> None:
    # Order matters — FK constraints. Wipe child tables first.
    dolt_exec("DELETE FROM dependencies")
    dolt_exec("DELETE FROM issue_comments")
    dolt_exec("DELETE FROM issue_labels")
    dolt_exec("DELETE FROM issues")


# ─── system.yaml fixture ─────────────────────────────────────────────────────


def write_system_yaml() -> None:
    SYSTEM_YAML.write_text(
        "projects:\n"
        f'  "{PLANE_PROJECT_ID}":\n'
        '    repo_name: stellar-sandbox\n'
        f'    workspace_prefix: {PLANE_PROJECT_IDENTIFIER}\n'
        '\n'
        'plane_state_map:\n'
        f'  "{PLANE_PROJECT_ID}":\n'
        '    open:        "Todo"\n'
        '    in_progress: "In Progress"\n'
        '    closed:      "Done"\n'
    )


# ─── Sync trigger (simulates the agent post-signal hook) ─────────────────────


def run_sync(issue_id: str, label: str = "") -> int:
    cmd = [
        "python3", str(SYNC_SCRIPT), issue_id,
        "--project-id", PLANE_PROJECT_ID,
        "--grava-repo", str(SANDBOX_ROOT),
        "--state-file", str(STATE_FILE),
        "--system-yaml", str(SYSTEM_YAML),
        "--log-level", "INFO",
    ]
    env = {
        **os.environ,
        "PLANE_HOST": PLANE_HOST,
        "PLANE_WORKSPACE": PLANE_WORKSPACE,
        "PLANE_API_TOKEN": _plane_token(),
    }
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    for line in (proc.stderr or "").strip().splitlines():
        if "PATCH" in line or "first seen" in line or "fallback" in line:
            log(f"  {label} | {line.split('grava_plane_sync: ')[-1]}", "INFO")
    return proc.returncode


# ─── Data model ──────────────────────────────────────────────────────────────


@dataclass
class Node:
    grava_id: str
    title: str
    type_id: str
    parent_grava_id: str | None = None   # parent within the tree
    plane_id: str = ""                   # set after Plane create
    seq_id: int = 0                      # set after Plane create


# ─── Main flow ───────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cleanup", action="store_true",
                    help="Delete all Plane work items + truncate dolt at end.")
    args = ap.parse_args()

    if not SYNC_SCRIPT.exists():
        log(f"sync script missing at {SYNC_SCRIPT}", "ERR")
        return 1

    ts = int(time.time())
    write_system_yaml()
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    reset_dolt()

    # ── Define the tree (creation order is parent-first for parent UUID resolution).
    tree: list[Node] = [
        Node(grava_id=f"grava-e-{ts}",       title=f"E:Auth-{ts}",         type_id=PLANE_TYPE_EPIC),
        Node(grava_id=f"grava-e-{ts}.1",     title=f"S1:Login-{ts}",       type_id=PLANE_TYPE_STORY, parent_grava_id=f"grava-e-{ts}"),
        Node(grava_id=f"grava-e-{ts}.2",     title=f"S2:Logout-{ts}",      type_id=PLANE_TYPE_STORY, parent_grava_id=f"grava-e-{ts}"),
        Node(grava_id=f"grava-e-{ts}.1.1",   title=f"T1:API-{ts}",         type_id=PLANE_TYPE_TASK,  parent_grava_id=f"grava-e-{ts}.1"),
        Node(grava_id=f"grava-e-{ts}.1.2",   title=f"T2:UI-{ts}",          type_id=PLANE_TYPE_TASK,  parent_grava_id=f"grava-e-{ts}.1"),
        Node(grava_id=f"grava-e-{ts}.2.1",   title=f"T3:Integration-{ts}", type_id=PLANE_TYPE_TASK,  parent_grava_id=f"grava-e-{ts}.2"),
    ]
    by_grava: dict[str, Node] = {n.grava_id: n for n in tree}

    try:
        # ── Step 1: create the work-item tree in Plane ─────────────────────
        log(f"Step 1 — Create {len(tree)} Plane work items (epic / story / task)", "STEP")
        for n in tree:
            parent_plane = by_grava[n.parent_grava_id].plane_id if n.parent_grava_id else None
            try:
                created = plane_create(n.title, n.type_id, parent_plane)
            except FlowFail as exc:
                # Parent linking on epic/story sometimes rejected — retry without parent.
                if n.parent_grava_id:
                    log(f"  parent link rejected, retry without parent: {exc}", "INFO")
                    created = plane_create(n.title, n.type_id, None)
                else:
                    raise
            n.plane_id = created["id"]
            n.seq_id = created["sequence_id"]
            parent_label = f" parent={by_grava[n.parent_grava_id].seq_id}" if n.parent_grava_id else ""
            log(f"  Plane STELL-{n.seq_id:>3}  {n.title}{parent_label}", "OK")

        # ── Step 2: post blocking relation T2 → T3 in Plane ────────────────
        log("Step 2 — Plane relation: T2 blocks T3", "STEP")
        t2 = by_grava[f"grava-e-{ts}.1.2"]
        t3 = by_grava[f"grava-e-{ts}.2.1"]
        try:
            plane_add_blocks(t2.plane_id, t3.plane_id)
            log(f"  STELL-{t2.seq_id} blocks STELL-{t3.seq_id}", "OK")
        except FlowFail as exc:
            log(f"  WARN: Plane relation post failed: {exc}", "INFO")

        # ── Step 3: mirror everything into local Grava Dolt DB ─────────────
        log("Step 3 — Mirror full tree into local Grava Dolt DB", "STEP")
        for n in tree:
            safe_title = n.title.replace("'", "''")
            dolt_exec(
                f"INSERT INTO issues (id, title, status, issue_type) VALUES "
                f"('{n.grava_id}', '{safe_title}', 'open', "
                f"'{'epic' if n.type_id == PLANE_TYPE_EPIC else 'story' if n.type_id == PLANE_TYPE_STORY else 'task'}')"
            )
            dolt_exec(
                f"INSERT INTO issue_labels (issue_id, label) VALUES "
                f"('{n.grava_id}', 'plane:{n.seq_id}')"
            )
            log(f"  dolt {n.grava_id}  ←  plane:{n.seq_id}", "OK")

        # ── Step 4: insert dependency rows in grava ────────────────────────
        log("Step 4 — Write dependency edges into grava `dependencies` table", "STEP")
        # parent-child rows
        for n in tree:
            if n.parent_grava_id:
                dolt_exec(
                    f"INSERT INTO dependencies (from_id, to_id, type) VALUES "
                    f"('{n.parent_grava_id}', '{n.grava_id}', 'parent-child')"
                )
                log(f"  {n.parent_grava_id}  parent-child  {n.grava_id}", "OK")
        # T2 blocks T3
        dolt_exec(
            f"INSERT INTO dependencies (from_id, to_id, type) VALUES "
            f"('{t2.grava_id}', '{t3.grava_id}', 'blocks')"
        )
        log(f"  {t2.grava_id}  blocks  {t3.grava_id}", "OK")

        # ── Step 5: status workflow — leaves first, root last ──────────────
        log("Step 5 — Status workflow + sync per transition", "STEP")
        # Topological order: tasks → stories → epic.
        order = [
            by_grava[f"grava-e-{ts}.1.1"],  # T1
            by_grava[f"grava-e-{ts}.1.2"],  # T2
            by_grava[f"grava-e-{ts}.2.1"],  # T3 (after T2)
            by_grava[f"grava-e-{ts}.1"],    # S1
            by_grava[f"grava-e-{ts}.2"],    # S2
            by_grava[f"grava-e-{ts}"],      # E
        ]
        failures = 0
        for n in order:
            for grava_status in ("in_progress", "closed"):
                dolt_exec(
                    f"UPDATE issues SET status='{grava_status}' "
                    f"WHERE id='{n.grava_id}'"
                )
                rc = run_sync(n.grava_id, label=f"STELL-{n.seq_id}")
                if rc != 0:
                    log(f"  sync({n.grava_id}) exit={rc}", "ERR")
                    failures += 1
                    continue
                # Verify Plane immediately.
                item = plane_get(n.plane_id)
                expected_name = GRAVA_TO_PLANE[grava_status]
                expected_uuid = PLANE_STATES[expected_name]
                actual_uuid = item.get("state")
                actual_name = PLANE_STATE_NAME_BY_UUID.get(actual_uuid, "?")
                ok = actual_uuid == expected_uuid
                mark = "OK" if ok else "ERR"
                if not ok:
                    failures += 1
                log(
                    f"  STELL-{n.seq_id:>3} {n.title[:24]:<24}  grava='{grava_status}'  "
                    f"plane='{actual_name}'  {'✓' if ok else '✗'}",
                    mark,
                )

        # ── Step 6: final inventory ────────────────────────────────────────
        log("Step 6 — Final inventory (all six work items should be Done)", "STEP")
        for n in order:
            item = plane_get(n.plane_id)
            actual_name = PLANE_STATE_NAME_BY_UUID.get(item.get("state"), "?")
            mark = "OK" if actual_name == "Done" else "ERR"
            log(f"  STELL-{n.seq_id:>3} {n.title[:30]:<30}  → {actual_name}", mark)
            log(f"     {plane_url(n.seq_id)}", "URL")

        if failures:
            log(f"FAILED: {failures} sync/verify mismatches", "ERR")
            return 1
        log("ALL TRANSITIONS PROPAGATED CORRECTLY", "OK")
        return 0

    finally:
        if args.cleanup:
            log("Cleanup — delete Plane issues + truncate dolt", "STEP")
            # Delete leaf-first so Plane doesn't choke on parent constraints.
            for n in reversed(tree):
                if n.plane_id:
                    try:
                        plane_delete(n.plane_id)
                        log(f"  deleted plane {n.plane_id}", "OK")
                    except FlowFail as exc:
                        log(f"  delete failed: {exc}", "ERR")
            reset_dolt()
            if STATE_FILE.exists():
                STATE_FILE.unlink()


if __name__ == "__main__":
    sys.exit(main())
