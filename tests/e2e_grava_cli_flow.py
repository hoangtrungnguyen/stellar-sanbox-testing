#!/usr/bin/env python3
"""End-to-end test driven by the real `grava` CLI.

Unlike the other e2e scripts in this folder, this one does NOT insert dolt rows
directly. Every Grava-side mutation goes through the actual `grava` command:

    grava create -t <title> --type <epic|story|task> [--parent <id>]
    grava label  <id> --add plane:<seq>
    grava dep    <from> <to> --type blocks
    grava update <id> --status <open|in_progress|closed>
    grava assign <id> --actor <name>
    grava comment <id> -m "<message>"

After each grava mutation, `grava_plane_sync.py <id>` is invoked exactly as a
Grava agent would invoke it from its post-`grava signal` hook. The script then
fetches the Plane work item via the public API and asserts the state, assignee,
and comment count have propagated.

Pre-conditions:
  * `grava` on $PATH.
  * `/Users/trungnguyenhoang/IdeaProjects/stellar-sand-box/` is the sandbox
    repo, with `grava init` already run (creates `.grava/`, `.grava.yaml`,
    git hooks). Run `grava init` once before invoking this test.

Run:
    python3 tests/e2e_grava_cli_flow.py                # keeps Plane items
    python3 tests/e2e_grava_cli_flow.py --cleanup      # deletes them
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

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
PLANE_TEST_MEMBER = "hoangtrungnguyen18102000"
PLANE_TEST_MEMBER_UUID = "32684b53-1ce0-4c16-9113-1443a5b63b20"

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
              "STEP": "\033[35m", "URL": "\033[34m", "CMD": "\033[33m"}.get(level, "")
    print(f"{colour}[{level}]\033[0m {msg}", flush=True)


class FlowFail(Exception):
    pass


# ─── Plane API ───────────────────────────────────────────────────────────────


def _plane(method: str, path: str, **kw) -> dict | list:
    headers = {"X-API-Key": PLANE_API_TOKEN, "Content-Type": "application/json"}
    url = f"{PLANE_HOST}/api/v1/workspaces/{PLANE_WORKSPACE}/{path.lstrip('/')}"
    resp = requests.request(method, url, headers=headers, timeout=30, **kw)
    if resp.status_code >= 400:
        raise FlowFail(f"Plane {method} {path} → {resp.status_code}: {resp.text[:300]}")
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def plane_create(name: str, type_id: str, parent: str | None = None) -> dict:
    payload = {"name": name, "type_id": type_id,
               "description_html": "<p>e2e_grava_cli_flow.py</p>"}
    if parent:
        payload["parent"] = parent
    return _plane("POST", f"projects/{PLANE_PROJECT_ID}/work-items/", json=payload)


def plane_get(work_id: str) -> dict:
    return _plane("GET", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/")


def plane_get_comments(work_id: str) -> list:
    data = _plane("GET", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/comments/")
    if isinstance(data, list):
        return data
    return data.get("results", []) if isinstance(data, dict) else []


def plane_delete(work_id: str) -> None:
    _plane("DELETE", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/")


def plane_url(seq: int | str) -> str:
    return f"https://app.plane.so/{PLANE_WORKSPACE}/browse/{PLANE_PROJECT_IDENTIFIER}-{seq}/"


def _assignee_ids(item: dict) -> set[str]:
    out: set[str] = set()
    for a in item.get("assignees") or []:
        if isinstance(a, dict):
            uid = a.get("id")
            if uid:
                out.add(uid)
        elif isinstance(a, str):
            out.add(a)
    return out


# ─── grava CLI wrapper ───────────────────────────────────────────────────────


def grava(*args: str, json_out: bool = False, capture: bool = True) -> str | dict | list:
    """Run `grava <args>` in the sandbox repo root. Optionally parse JSON output."""
    cmd = ["grava", *args]
    if json_out:
        cmd.append("--json")
    log(f"  $ {' '.join(cmd)}", "CMD")
    res = subprocess.run(
        cmd, cwd=str(SANDBOX_ROOT),
        capture_output=capture, text=True, timeout=30,
    )
    if res.returncode != 0:
        raise FlowFail(
            f"grava {args[0]} failed (exit {res.returncode}): "
            f"{(res.stderr or res.stdout)[:200]}"
        )
    out = (res.stdout or "").strip()
    if not json_out or not out:
        return out
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # Grava may print log lines before the JSON. Find the first '{' or '['
        # and parse from there (handles multi-line JSON objects).
        lines = out.splitlines()
        for i, line in enumerate(lines):
            s = line.lstrip()
            if s.startswith(("{", "[")):
                try:
                    return json.loads("\n".join(lines[i:]))
                except json.JSONDecodeError:
                    continue
        raise FlowFail(f"grava {args[0]} non-JSON: {out[:200]}")


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


# ─── Sync trigger (== the agent post-signal hook) ────────────────────────────


def signal_sync(issue_id: str, label: str = "") -> int:
    cmd = [
        "python3", str(SYNC_SCRIPT), issue_id,
        "--project-id", PLANE_PROJECT_ID,
        "--grava-repo", str(SANDBOX_ROOT),
        "--state-file", str(STATE_FILE),
        "--system-yaml", str(SYSTEM_YAML),
        "--log-level", "INFO",
    ]
    env = {**os.environ, "PLANE_HOST": PLANE_HOST,
           "PLANE_WORKSPACE": PLANE_WORKSPACE,
           "PLANE_API_TOKEN": PLANE_API_TOKEN}
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    for line in (proc.stderr or "").strip().splitlines():
        if "PATCH" in line or "first seen" in line or "fallback" in line:
            log(f"  sync({label}) | {line.split('grava_plane_sync: ')[-1]}", "INFO")
    return proc.returncode


# ─── Data model ──────────────────────────────────────────────────────────────


@dataclass
class Node:
    plane_type_id: str
    title: str
    parent_key: str | None = None    # logical tree key (E, S1, T1, ...)
    grava_id: str = ""
    plane_id: str = ""
    seq_id: int = 0


# ─── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cleanup", action="store_true",
                    help="Delete Plane work items + drop grava issues at end.")
    args = ap.parse_args()

    if not (SANDBOX_ROOT / ".grava.yaml").exists():
        log(f"`.grava.yaml` missing in {SANDBOX_ROOT}. Run `cd {SANDBOX_ROOT} && grava init` first.", "ERR")
        return 1
    if not SYNC_SCRIPT.exists():
        log(f"sync script missing at {SYNC_SCRIPT}", "ERR")
        return 1

    ts = int(time.time())
    write_system_yaml()
    if STATE_FILE.exists():
        STATE_FILE.unlink()

    # ── Tree (5 issues: 1 epic, 2 stories, 2 tasks; T1 blocks T2) ──────────
    nodes: dict[str, Node] = {
        "E":  Node(PLANE_TYPE_EPIC,  f"E-cli-{ts}",       parent_key=None),
        "S1": Node(PLANE_TYPE_STORY, f"S1-cli-{ts}",      parent_key="E"),
        "S2": Node(PLANE_TYPE_STORY, f"S2-cli-{ts}",      parent_key="E"),
        "T1": Node(PLANE_TYPE_TASK,  f"T1-cli-{ts}",      parent_key="S1"),
        "T2": Node(PLANE_TYPE_TASK,  f"T2-cli-{ts}",      parent_key="S2"),
    }
    created_order = ["E", "S1", "S2", "T1", "T2"]
    failures = 0

    try:
        # Step 1: create work items in Plane (parent-first).
        log("Step 1 — Create work items in Plane (POST /work-items/)", "STEP")
        for key in created_order:
            n = nodes[key]
            parent_uuid = nodes[n.parent_key].plane_id if n.parent_key else None
            wi = plane_create(n.title, n.plane_type_id, parent_uuid)
            n.plane_id = wi["id"]
            n.seq_id = wi["sequence_id"]
            ptag = f" (parent={nodes[n.parent_key].seq_id})" if n.parent_key else ""
            log(f"  Plane STELL-{n.seq_id:>3}  {key}={n.title}{ptag}", "OK")

        # Step 2: create grava issues via `grava create` (one-by-one, parent-first).
        log("Step 2 — Create grava issues via `grava create` CLI", "STEP")
        for key in created_order:
            n = nodes[key]
            grava_type = (
                "epic" if n.plane_type_id == PLANE_TYPE_EPIC else
                "story" if n.plane_type_id == PLANE_TYPE_STORY else "task"
            )
            create_args = ["create", "-t", n.title, "--type", grava_type, "-d",
                           f"plane: {plane_url(n.seq_id)}"]
            if n.parent_key:
                create_args += ["--parent", nodes[n.parent_key].grava_id]
            resp = grava(*create_args, json_out=True)
            n.grava_id = resp["id"]
            log(f"  grava {n.grava_id} = {key}", "OK")

        # Step 3: label each grava issue with plane:<seq>.
        log("Step 3 — Apply plane:<seq> labels via `grava label`", "STEP")
        for key in created_order:
            n = nodes[key]
            grava("label", n.grava_id, "--add", f"plane:{n.seq_id}", json_out=True)
            log(f"  grava label {n.grava_id} --add plane:{n.seq_id}", "OK")

        # Step 4: T1 blocks T2 via `grava dep`.
        log("Step 4 — Dependency: T1 blocks T2 via `grava dep`", "STEP")
        grava("dep", nodes["T1"].grava_id, nodes["T2"].grava_id,
              "--type", "blocks", json_out=True)
        log(f"  grava dep {nodes['T1'].grava_id} {nodes['T2'].grava_id} --type blocks", "OK")

        # Step 5: status workflow + sync per transition.
        #
        # Grava enforces a state machine — transitions use dedicated commands:
        #   open → in_progress    via  `grava start <id>`
        #   in_progress → closed  via  `grava close <id> --force`
        #
        # Grava also cascades: when ALL children of a parent are closed, the
        # parent auto-closes. After closing T1 (the only task under S1), S1
        # auto-closes too. So we only drive the *leaves* (T1, T2) and sync the
        # ancestors after each leaf transition to observe the cascade in Plane.
        log("Step 5 — Status workflow (drive leaves, observe cascade)", "STEP")

        def verify_plane(n: Node, expected_status: str) -> bool:
            item = plane_get(n.plane_id)
            actual_uuid = item.get("state")
            actual_name = PLANE_STATE_NAME_BY_UUID.get(actual_uuid, "?")
            expected_name = GRAVA_TO_PLANE[expected_status]
            ok = actual_uuid == PLANE_STATES[expected_name]
            log(
                f"    STELL-{n.seq_id:>3} {[k for k,v in nodes.items() if v is n][0]:<2} "
                f"grava='{expected_status}'  →  plane='{actual_name}'  "
                f"{'✓' if ok else '✗'}",
                "OK" if ok else "ERR",
            )
            return ok

        def sync_and_verify(n: Node, expected_status: str) -> None:
            nonlocal failures
            rc = signal_sync(n.grava_id, label=f"STELL-{n.seq_id}")
            if rc != 0:
                log(f"    sync exit={rc}", "ERR")
                failures += 1
                return
            if not verify_plane(n, expected_status):
                failures += 1

        # ── T1: full lifecycle (start → assign → comment → unassign → close) ──
        log("  T1.start  → grava in_progress", "STEP")
        grava("start", nodes["T1"].grava_id)
        sync_and_verify(nodes["T1"], "in_progress")

        # ── Step 6: assignee — `grava assign` → sync → verify Plane assignees.
        log("Step 6 — Assignee: grava assign T1 --actor <member>", "STEP")
        t1 = nodes["T1"]
        grava("assign", t1.grava_id, "--actor", PLANE_TEST_MEMBER)
        rc = signal_sync(t1.grava_id, label=f"STELL-{t1.seq_id}")
        if rc != 0:
            log(f"  sync exit={rc}", "ERR")
            failures += 1
        item = plane_get(t1.plane_id)
        if PLANE_TEST_MEMBER_UUID in _assignee_ids(item):
            log(f"  Plane STELL-{t1.seq_id} assignees include {PLANE_TEST_MEMBER}", "OK")
        else:
            log(f"  Plane STELL-{t1.seq_id} assignees missing test member: "
                f"{item.get('assignees')}", "ERR")
            failures += 1

        # ── Step 7: comment — `grava comment` → sync → verify Plane comment.
        log("Step 7 — Comment: grava comment T1 -m \"...\"", "STEP")
        marker = f"cli-marker-{ts}"
        before = len(plane_get_comments(t1.plane_id))
        grava("comment", t1.grava_id, "-m", f"hello plane {marker}")
        rc = signal_sync(t1.grava_id, label=f"STELL-{t1.seq_id}")
        if rc != 0:
            log(f"  sync exit={rc}", "ERR")
            failures += 1
        after = plane_get_comments(t1.plane_id)
        if len(after) == before + 1 and any(marker in (c.get("comment_html") or "")
                                            for c in after):
            log(f"  Plane STELL-{t1.seq_id} comment with marker {marker!r} posted", "OK")
        else:
            log(f"  Plane comment not found. before={before} after={len(after)}", "ERR")
            failures += 1

        log("  T1.close  → grava closed; cascade should close S1", "STEP")
        grava("close", nodes["T1"].grava_id, "--force")
        sync_and_verify(nodes["T1"], "closed")
        sync_and_verify(nodes["S1"], "closed")    # observe cascade

        # ── T2: simple start → close (cascades S2 → closed, then E → closed) ──
        log("  T2.start  → grava in_progress", "STEP")
        grava("start", nodes["T2"].grava_id)
        sync_and_verify(nodes["T2"], "in_progress")

        log("  T2.close  → grava closed; cascade should close S2 and E", "STEP")
        grava("close", nodes["T2"].grava_id, "--force")
        sync_and_verify(nodes["T2"], "closed")
        sync_and_verify(nodes["S2"], "closed")
        sync_and_verify(nodes["E"],  "closed")

        # ── Final inventory ────────────────────────────────────────────────
        log("Step 8 — Final inventory (open URLs to inspect)", "STEP")
        for key in created_order:
            n = nodes[key]
            item = plane_get(n.plane_id)
            actual_name = PLANE_STATE_NAME_BY_UUID.get(item.get("state"), "?")
            log(f"  STELL-{n.seq_id:>3} {key:<2} {n.title:<25}  → {actual_name}", "OK")
            log(f"     {plane_url(n.seq_id)}", "URL")

        if failures:
            log(f"FAILED: {failures} verification(s)", "ERR")
            return 1
        log("ALL CLI-DRIVEN TRANSITIONS PROPAGATED CORRECTLY", "OK")
        return 0

    finally:
        if args.cleanup:
            log("Cleanup — delete Plane issues + drop grava issues", "STEP")
            for key in reversed(created_order):
                n = nodes[key]
                if n.plane_id:
                    try:
                        plane_delete(n.plane_id)
                        log(f"  plane delete {n.plane_id}", "OK")
                    except FlowFail as exc:
                        log(f"  plane delete failed: {exc}", "ERR")
                if n.grava_id:
                    try:
                        grava("drop", n.grava_id, "--force")
                        log(f"  grava drop {n.grava_id}", "OK")
                    except FlowFail as exc:
                        log(f"  grava drop failed: {exc}", "ERR")
            if STATE_FILE.exists():
                STATE_FILE.unlink()


if __name__ == "__main__":
    sys.exit(main())
