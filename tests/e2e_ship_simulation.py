#!/usr/bin/env python3
"""End-to-end test that simulates a `/ship` pipeline run and verifies the
Plane work item's state advances at each phase.

`/ship` orchestrates three agents in sequence:

    coder    →  reviewer  →  pr-creator   (then handoff to pr-merge-watcher)

Each agent (after the `grava_plane_sync.py` hook was added to coder.md,
reviewer.md, pr-creator.md) calls the sync script right after emitting its
`grava signal`. This test simulates that exact sequence — without running
real code work, real Claude agents, or real GitHub PRs — to confirm the
sync hook fires correctly at every phase.

Sequence:

  Phase 0: Create Plane issue + mirror to grava + apply plane:<seq> label.
  Phase 1: `grava start`               — coder Step 1 (== `grava claim`).
            sync → Plane = In Progress.
  Phase 2: `grava signal CODER_DONE`   — coder Step N (last action).
            sync → no state change (still in_progress).
  Phase 3: `grava label code_review`   — reviewer-side label.
           `grava signal REVIEWER_APPROVED`.
            sync → no state change.
  Phase 4: `grava label pr-created`    — pr-creator-side label.
           `grava signal PR_CREATED`.
            sync → no state change.
  Phase 5: `grava close --force`       — pr-merge-watcher closes after merge.
            sync → Plane = Done.

Each `sync` step runs the EXACT command the corresponding agent runs.

Run:
    python3 tests/e2e_ship_simulation.py
    python3 tests/e2e_ship_simulation.py --cleanup
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

# ─── Configuration ───────────────────────────────────────────────────────────

PLANE_HOST = "https://api.plane.so"
PLANE_WORKSPACE = "stellar-sandbox"
PLANE_PROJECT_ID = "cec88b42-b47c-4f1c-bfdf-a882c490a784"
PLANE_PROJECT_IDENTIFIER = "STELL"
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
              "PHASE": "\033[35m", "URL": "\033[34m", "CMD": "\033[33m"}.get(level, "")
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


def plane_create(name: str) -> dict:
    return _plane("POST", f"projects/{PLANE_PROJECT_ID}/work-items/", json={
        "name": name,
        "type_id": PLANE_TYPE_TASK,
        "description_html": "<p>e2e_ship_simulation</p>",
    })


def plane_get(work_id: str) -> dict:
    return _plane("GET", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/")


def plane_delete(work_id: str) -> None:
    _plane("DELETE", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/")


def plane_url(seq: int | str) -> str:
    return f"https://app.plane.so/{PLANE_WORKSPACE}/browse/{PLANE_PROJECT_IDENTIFIER}-{seq}/"


# ─── grava wrapper ───────────────────────────────────────────────────────────


def grava(*args: str, json_out: bool = False) -> str | dict | list:
    cmd = ["grava", *args]
    if json_out:
        cmd.append("--json")
    log(f"  $ {' '.join(cmd)}", "CMD")
    res = subprocess.run(cmd, cwd=str(SANDBOX_ROOT), capture_output=True,
                         text=True, timeout=30)
    if res.returncode != 0:
        raise FlowFail(f"grava {args[0]} failed: {(res.stderr or res.stdout)[:200]}")
    out = (res.stdout or "").strip()
    if not json_out or not out:
        return out
    # grava may print log lines (e.g. "Using config file: ...") BEFORE the JSON
    # block. Scan top-down for the first '{' or '[' and parse from there.
    lines = out.splitlines()
    for i, line in enumerate(lines):
        s = line.lstrip()
        if s.startswith(("{", "[")):
            body = "\n".join(lines[i:])
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                continue
    raise FlowFail(f"grava {args[0]}: cannot parse JSON from output:\n{out[:300]}")


# ─── system.yaml ─────────────────────────────────────────────────────────────


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


# ─── Sync hook (the literal call from each agent's post-signal block) ────────


def agent_sync_hook(issue_id: str, label: str) -> int:
    """The exact command that coder.md / reviewer.md / pr-creator.md run
    after their `grava signal` line. Returns sync exit code.
    """
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
        if "PATCH" in line or "first seen" in line or "no state" in line:
            log(f"  sync[{label}] | {line.split('grava_plane_sync: ')[-1]}", "INFO")
    return proc.returncode


def verify_plane(plane_id: str, expected_state_name: str, label: str) -> bool:
    item = plane_get(plane_id)
    actual_uuid = item.get("state")
    actual_name = PLANE_STATE_NAME_BY_UUID.get(actual_uuid, "?")
    expected_uuid = PLANE_STATES[expected_state_name]
    ok = actual_uuid == expected_uuid
    log(
        f"  plane[{label}] state='{actual_name}'  (expected '{expected_state_name}')  "
        f"{'✓' if ok else '✗'}",
        "OK" if ok else "ERR",
    )
    return ok


# ─── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()

    if not (SANDBOX_ROOT / ".grava.yaml").exists():
        log(f"`.grava.yaml` missing — run `cd {SANDBOX_ROOT} && grava init` first.", "ERR")
        return 1
    if not SYNC_SCRIPT.exists():
        log(f"sync script missing at {SYNC_SCRIPT}", "ERR")
        return 1

    ts = int(time.time())
    title = f"ship-sim-{ts}"
    write_system_yaml()
    if STATE_FILE.exists():
        STATE_FILE.unlink()

    plane_id = ""
    grava_id = ""
    failures = 0

    try:
        # ── Phase 0 ────────────────────────────────────────────────────────
        log("Phase 0 — Provision Plane work item + grava issue + plane:<seq> label", "PHASE")
        wi = plane_create(title)
        plane_id, seq_id = wi["id"], wi["sequence_id"]
        log(f"  Plane created: STELL-{seq_id}", "OK")

        resp = grava("create", "-t", title, "--type", "task", json_out=True)
        grava_id = resp["id"]
        log(f"  Grava created: {grava_id}", "OK")
        grava("label", grava_id, "--add", f"plane:{seq_id}", json_out=True)

        # Initial sync to record the open state in the cache.
        agent_sync_hook(grava_id, "init")
        verify_plane(plane_id, "Todo", "init")

        # ── Phase 1: coder claims → in_progress ────────────────────────────
        log("Phase 1 — coder agent: claims issue (grava start ≅ grava claim)", "PHASE")
        grava("start", grava_id)
        rc = agent_sync_hook(grava_id, "coder.start")
        if rc != 0:
            log(f"  sync exit={rc}", "ERR"); failures += 1
        if not verify_plane(plane_id, "In Progress", "after-claim"):
            failures += 1

        # ── Phase 2: coder signals CODER_DONE ──────────────────────────────
        log("Phase 2 — coder agent: emits CODER_DONE, then sync hook", "PHASE")
        grava("signal", "CODER_DONE", "--issue", grava_id, "--payload", "sha-deadbeef")
        rc = agent_sync_hook(grava_id, "coder.done")
        if rc != 0:
            log(f"  sync exit={rc}", "ERR"); failures += 1
        # Grava status is still in_progress → Plane state stays In Progress.
        if not verify_plane(plane_id, "In Progress", "after-CODER_DONE"):
            failures += 1

        # ── Phase 3: reviewer ──────────────────────────────────────────────
        log("Phase 3 — reviewer agent: labels code_review, signals REVIEWER_APPROVED", "PHASE")
        grava("label", grava_id, "--add", "code_review", json_out=True)
        grava("signal", "REVIEWER_APPROVED", "--issue", grava_id)
        rc = agent_sync_hook(grava_id, "reviewer.approved")
        if rc != 0:
            log(f"  sync exit={rc}", "ERR"); failures += 1
        if not verify_plane(plane_id, "In Progress", "after-REVIEWER_APPROVED"):
            failures += 1

        # ── Phase 4: pr-creator ────────────────────────────────────────────
        # `grava signal PR_CREATED` enforces preconditions: `pr_number` and
        # `pr_awaiting_merge_since` wisps must be written first. In production,
        # `scripts/agent-bot/finalize-pr.sh` writes these before signalling.
        log("Phase 4 — pr-creator agent: writes wisps, labels, signals PR_CREATED", "PHASE")
        grava("wisp", "write", grava_id, "pr_number", "42")
        grava("wisp", "write", grava_id, "pr_url",
              "https://github.com/org/repo/pull/42")
        grava("wisp", "write", grava_id, "pr_awaiting_merge_since",
              str(int(time.time())))
        grava("label", grava_id, "--add", "pr-created", json_out=True)
        grava("signal", "PR_CREATED", "--issue", grava_id,
              "--payload", "https://github.com/org/repo/pull/42")
        rc = agent_sync_hook(grava_id, "pr-creator.created")
        if rc != 0:
            log(f"  sync exit={rc}", "ERR"); failures += 1
        if not verify_plane(plane_id, "In Progress", "after-PR_CREATED"):
            failures += 1

        # ── Phase 5: PR-merge-watcher closes the issue after PR merges ────
        log("Phase 5 — pr-merge-watcher: grava close (mimics PR merge handoff)", "PHASE")
        grava("close", grava_id, "--force")
        rc = agent_sync_hook(grava_id, "watcher.close")
        if rc != 0:
            log(f"  sync exit={rc}", "ERR"); failures += 1
        if not verify_plane(plane_id, "Done", "after-close"):
            failures += 1

        log("Pipeline summary", "PHASE")
        log(f"  Plane work item: {plane_url(seq_id)}", "URL")
        log("  Phase progression:  Todo → In Progress → … → Done", "INFO")

        if failures:
            log(f"FAILED: {failures} verification(s)", "ERR")
            return 1
        log("ALL /ship PHASES PROPAGATED CORRECTLY TO PLANE", "OK")
        return 0

    finally:
        if args.cleanup:
            log("Cleanup", "PHASE")
            if plane_id:
                try:
                    plane_delete(plane_id)
                    log(f"  plane delete {plane_id}", "OK")
                except FlowFail as exc:
                    log(f"  plane delete failed: {exc}", "ERR")
            if grava_id:
                try:
                    grava("drop", grava_id, "--force")
                    log(f"  grava drop {grava_id}", "OK")
                except FlowFail as exc:
                    log(f"  grava drop failed: {exc}", "ERR")
            if STATE_FILE.exists():
                STATE_FILE.unlink()


if __name__ == "__main__":
    sys.exit(main())
