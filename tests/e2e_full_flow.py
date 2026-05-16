#!/usr/bin/env python3
"""Full bi-directional flow test:

  1. CREATE several issues in Plane.so (via API).
  2. MIRROR them into the local Grava Dolt DB (insert rows + plane:<seq> labels).
  3. MUTATE grava status on each issue.
  4. SIGNAL — invoke grava_plane_sync.py once per issue (what each agent would do).
  5. VERIFY — fetch each Plane work item and confirm state has propagated.

Unlike e2e_grava_plane_sync.py, this script does NOT delete the Plane work items
on exit, so you can open them in the Plane UI and see the synced status.

Run:  python3 tests/e2e_full_flow.py
      python3 tests/e2e_full_flow.py --cleanup        # delete Plane issues at end
      python3 tests/e2e_full_flow.py --count 5        # change number of issues
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

# ─── Plane wiring ────────────────────────────────────────────────────────────

PLANE_HOST = "https://api.plane.so"
PLANE_WORKSPACE = "stellar-sandbox"
PLANE_PROJECT_ID = "cec88b42-b47c-4f1c-bfdf-a882c490a784"
PLANE_PROJECT_IDENTIFIER = "STELL"
PLANE_TASK_TYPE_ID = "bfa0b143-b4a5-42f9-b460-3c333ee03d5b"
# Hard-coded test API key (overrides ~/.config/plane/config.json when env not set).
PLANE_API_TOKEN = "plane_api_6ad1c033c54146e0a09cc6e7eaf884f3"

# Per-state UUIDs in the Stellar-Sandbox project (cached for easy display).
PLANE_STATES = {
    "Backlog":     "c81ef0d0-94c8-42cf-8cca-fccc07b12c34",
    "Todo":        "293ca4c1-dad0-4dc8-a67a-ea838743274d",
    "In Progress": "8cd744b2-6104-49bb-998f-5e91ae2088c9",
    "Done":        "12fc0d73-f7b9-475a-b2f5-b14a5c0e97f9",
    "Cancelled":   "2984876f-ce67-4d11-ab30-1fe19771d838",
}
PLANE_STATE_NAME_BY_UUID = {v: k for k, v in PLANE_STATES.items()}

# ─── Local paths ─────────────────────────────────────────────────────────────

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

# Map each test issue to the grava status we'll move it to.
DEFAULT_STATUS_PLAN = ["in_progress", "closed", "open"]

# ─── tiny logger ─────────────────────────────────────────────────────────────


def log(msg: str, level: str = "INFO") -> None:
    colour = {"INFO": "\033[36m", "OK": "\033[32m", "ERR": "\033[31m",
              "STEP": "\033[35m", "URL": "\033[34m"}.get(level, "")
    print(f"{colour}[{level}]\033[0m {msg}", flush=True)


class FlowFail(Exception):
    pass


# ─── Plane API ───────────────────────────────────────────────────────────────


def _plane_token() -> str:
    # Precedence: env var > hard-coded test token > config.json.
    env = os.environ.get("PLANE_API_TOKEN")
    if env:
        return env
    if PLANE_API_TOKEN:
        return PLANE_API_TOKEN
    return json.loads(
        (Path.home() / ".config" / "plane" / "config.json").read_text()
    )["token"]


def _plane(method: str, path: str, **kw) -> dict | list:
    headers = {"X-API-Key": _plane_token(), "Content-Type": "application/json"}
    url = f"{PLANE_HOST}/api/v1/workspaces/{PLANE_WORKSPACE}/{path.lstrip('/')}"
    resp = requests.request(method, url, headers=headers, timeout=30, **kw)
    if resp.status_code >= 400:
        raise FlowFail(f"Plane {method} {path} → {resp.status_code}: {resp.text[:300]}")
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def create_plane_issue(title: str) -> dict:
    return _plane("POST", f"projects/{PLANE_PROJECT_ID}/work-items/", json={
        "name": title,
        "type_id": PLANE_TASK_TYPE_ID,
        "description_html": "<p>created by e2e_full_flow.py</p>",
    })


def get_plane_issue(work_id: str) -> dict:
    return _plane("GET", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/")


def delete_plane_issue(work_id: str) -> None:
    _plane("DELETE", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/")


def plane_url(seq_id: int | str) -> str:
    return f"https://app.plane.so/{PLANE_WORKSPACE}/browse/{PLANE_PROJECT_IDENTIFIER}-{seq_id}/"


# ─── Dolt helpers ────────────────────────────────────────────────────────────


def dolt_sql(query: str) -> list[dict]:
    res = subprocess.run(
        ["dolt", "sql", "-q", query, "--result-format", "json"],
        cwd=str(DOLT_DIR), capture_output=True, text=True, timeout=15,
    )
    if res.returncode != 0:
        raise FlowFail(f"dolt sql failed: {res.stderr[:200]}")
    raw = res.stdout.strip()
    if not raw:
        return []
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        return parsed.get("rows", [])
    return parsed


def dolt_exec(query: str) -> None:
    res = subprocess.run(
        ["dolt", "sql", "-q", query],
        cwd=str(DOLT_DIR), capture_output=True, text=True, timeout=15,
    )
    if res.returncode != 0:
        raise FlowFail(f"dolt exec failed: {res.stderr[:200]}")


def reset_dolt() -> None:
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


# ─── Sync runner (simulates a Grava agent's post-signal hook) ───────────────


def run_sync(issue_id: str | None = None, label: str = "") -> int:
    cmd = ["python3", str(SYNC_SCRIPT)]
    if issue_id:
        cmd.append(issue_id)
    cmd += [
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
    if proc.stderr.strip():
        for line in proc.stderr.strip().splitlines():
            log(f"  {label} | {line}", "INFO")
    return proc.returncode


# ─── Flow ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=3,
                    help="Number of Plane issues to create (default 3).")
    ap.add_argument("--cleanup", action="store_true",
                    help="Delete Plane issues at end (default: keep for inspection).")
    args = ap.parse_args()

    if not SYNC_SCRIPT.exists():
        log(f"sync script missing at {SYNC_SCRIPT}", "ERR")
        return 1

    # Plan: cycle through DEFAULT_STATUS_PLAN, one target status per issue.
    status_plan = [DEFAULT_STATUS_PLAN[i % len(DEFAULT_STATUS_PLAN)]
                   for i in range(args.count)]
    expected_plane_state = {
        "open": "Todo",
        "in_progress": "In Progress",
        "closed": "Done",
    }

    write_system_yaml()
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    reset_dolt()

    created: list[dict] = []  # {grava_id, plane_id, seq_id, target_status}

    try:
        # ── Step 1: create Plane issues ────────────────────────────────────
        log(f"Step 1 — Create {args.count} Plane issues", "STEP")
        ts = int(time.time())
        for i in range(args.count):
            title = f"e2e-flow-{ts}-{i+1}"
            wi = create_plane_issue(title)
            created.append({
                "grava_id": f"grava-flow-{ts}-{i+1}",
                "plane_id": wi["id"],
                "seq_id": wi["sequence_id"],
                "target_status": status_plan[i],
                "title": title,
            })
            log(f"  Plane {wi['sequence_id']:>3}  id={wi['id']}  title={title}", "OK")
            log(f"  URL: {plane_url(wi['sequence_id'])}", "URL")

        # ── Step 2: mirror Plane issues → grava Dolt DB ────────────────────
        log("Step 2 — Mirror Plane issues into local Grava Dolt DB", "STEP")
        for c in created:
            dolt_exec(
                f"INSERT INTO issues (id, title, status) VALUES "
                f"('{c['grava_id']}', '{c['title']}', 'open')"
            )
            dolt_exec(
                f"INSERT INTO issue_labels (issue_id, label) VALUES "
                f"('{c['grava_id']}', 'plane:{c['seq_id']}')"
            )
            log(f"  dolt INSERT {c['grava_id']}  ←  plane:{c['seq_id']}", "OK")

        # ── Step 3: mutate grava status (operator / coding agent) ──────────
        log("Step 3 — Change Grava status (simulating agent work)", "STEP")
        for c in created:
            dolt_exec(
                f"UPDATE issues SET status='{c['target_status']}' "
                f"WHERE id='{c['grava_id']}'"
            )
            log(f"  {c['grava_id']}  status → {c['target_status']}", "OK")

        # ── Step 4: signal — invoke sync (what each agent's post-signal hook does)
        log("Step 4 — Trigger grava_plane_sync.py (one call per issue)", "STEP")
        for c in created:
            rc = run_sync(c["grava_id"], label=c["grava_id"])
            mark = "OK" if rc == 0 else "ERR"
            log(f"  sync({c['grava_id']}) → exit {rc}", mark)

        # ── Step 5: verify Plane states have propagated ────────────────────
        log("Step 5 — Verify each Plane work item now reflects the grava status", "STEP")
        verified = 0
        for c in created:
            item = get_plane_issue(c["plane_id"])
            expected_state_name = expected_plane_state[c["target_status"]]
            expected_uuid = PLANE_STATES[expected_state_name]
            actual_uuid = item.get("state")
            actual_name = PLANE_STATE_NAME_BY_UUID.get(actual_uuid, actual_uuid)
            ok = actual_uuid == expected_uuid
            mark = "OK" if ok else "ERR"
            log(
                f"  {c['grava_id']}  grava='{c['target_status']}'  →  "
                f"plane='{actual_name}'  (expected '{expected_state_name}')  {mark}",
                mark,
            )
            log(f"  URL: {plane_url(c['seq_id'])}", "URL")
            if ok:
                verified += 1

        log(f"Verified {verified}/{len(created)} state propagations", "STEP")

        # ── Summary ────────────────────────────────────────────────────────
        log("Open these URLs in Plane to confirm visually:", "STEP")
        for c in created:
            log(f"  {plane_url(c['seq_id'])}", "URL")

        if verified != len(created):
            log("FAIL — at least one state did not propagate", "ERR")
            return 1
        log("ALL STATE PROPAGATIONS VERIFIED", "OK")
        return 0

    finally:
        if args.cleanup:
            log("Cleanup — deleting Plane issues + truncating dolt", "STEP")
            for c in created:
                try:
                    delete_plane_issue(c["plane_id"])
                    log(f"  deleted plane {c['plane_id']}", "OK")
                except FlowFail as exc:
                    log(f"  cleanup delete failed: {exc}", "ERR")
            reset_dolt()
            if STATE_FILE.exists():
                STATE_FILE.unlink()


if __name__ == "__main__":
    sys.exit(main())
