#!/usr/bin/env python3
"""End-to-end test for grava_plane_sync.py against the real Stellar-Sandbox
Plane workspace.

What this exercises (network-bound):
  1. Create a fresh Plane work item via POST. Capture id + sequence_id.
  2. Seed a Grava issue in the local Dolt DB with a `plane:<seq>` label.
  3. Run grava_plane_sync.py → expect status patched to "Todo" (grava
     `open` maps via plane_state_map).
  4. Bump grava status to `in_progress` → re-run → expect Plane state
     "In Progress".
  5. Set grava assignee to a real member display_name → re-run →
     expect Plane `assignees` populated.
  6. Insert a grava comment → re-run → expect comment POSTed to Plane.
  7. Re-run with no grava changes → expect zero PATCH/POST (idempotent).
  8. Cleanup: delete the Plane work item; truncate dolt tables.

Pre-conditions:
  * `dolt` CLI on PATH.
  * `~/.config/plane/config.json` carries a token with access to workspace
    `stellar-sandbox`.
  * `/Users/trungnguyenhoang/IdeaProjects/stellar-sand-box/.grava/dolt` is a
    valid Dolt repo with the schema in `tests/schema.sql` applied.

Usage:
  python3 tests/e2e_grava_plane_sync.py [--no-cleanup]
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

# ─── Constants (Stellar-Sandbox-specific) ────────────────────────────────────

PLANE_HOST = "https://api.plane.so"
PLANE_WORKSPACE = "stellar-sandbox"
PLANE_PROJECT_ID = "cec88b42-b47c-4f1c-bfdf-a882c490a784"
PLANE_PROJECT_IDENTIFIER = "STELL"
PLANE_TASK_TYPE_ID = "bfa0b143-b4a5-42f9-b460-3c333ee03d5b"
PLANE_TEST_MEMBER_NAME = "hoangtrungnguyen18102000"
PLANE_API_TOKEN = "plane_api_6ad1c033c54146e0a09cc6e7eaf884f3"

EXPECTED_STATES = {
    "Backlog": "c81ef0d0-94c8-42cf-8cca-fccc07b12c34",
    "Todo": "293ca4c1-dad0-4dc8-a67a-ea838743274d",
    "In Progress": "8cd744b2-6104-49bb-998f-5e91ae2088c9",
    "Done": "12fc0d73-f7b9-475a-b2f5-b14a5c0e97f9",
    "Cancelled": "2984876f-ce67-4d11-ab30-1fe19771d838",
}

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

GRAVA_ISSUE_ID = "grava-e2e1"


# ─── Coloured logging ────────────────────────────────────────────────────────


def log(msg: str, level: str = "INFO") -> None:
    colour = {"INFO": "\033[36m", "PASS": "\033[32m", "FAIL": "\033[31m",
              "STEP": "\033[35m"}.get(level, "")
    print(f"{colour}[{level}]\033[0m {msg}", flush=True)


class TestFail(Exception):
    pass


def assert_eq(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise TestFail(f"{label}: expected {expected!r}, got {actual!r}")
    log(f"{label} == {expected!r}", "PASS")


# ─── Plane API helpers ───────────────────────────────────────────────────────


def _plane_token() -> str:
    env = os.environ.get("PLANE_API_TOKEN")
    if env:
        return env
    if PLANE_API_TOKEN:
        return PLANE_API_TOKEN
    cfg = json.loads((Path.home() / ".config" / "plane" / "config.json").read_text())
    return cfg["token"]


def _plane_url(path: str) -> str:
    return f"{PLANE_HOST}/api/v1/workspaces/{PLANE_WORKSPACE}/{path.lstrip('/')}"


def _plane(method: str, path: str, **kw) -> dict | list:
    headers = {"X-API-Key": _plane_token(), "Content-Type": "application/json"}
    resp = requests.request(method, _plane_url(path), headers=headers,
                            timeout=30, **kw)
    if resp.status_code >= 400:
        raise TestFail(f"Plane API {method} {path} → {resp.status_code}: {resp.text[:300]}")
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def create_plane_work_item(title: str) -> dict:
    return _plane("POST", f"projects/{PLANE_PROJECT_ID}/work-items/", json={
        "name": title,
        "type_id": PLANE_TASK_TYPE_ID,
        "description_html": "<p>created by e2e_grava_plane_sync.py</p>",
    })


def get_plane_work_item(work_id: str) -> dict:
    return _plane("GET", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/")


def get_plane_comments(work_id: str) -> list[dict]:
    data = _plane("GET", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/comments/")
    if isinstance(data, list):
        return data
    return data.get("results", []) if isinstance(data, dict) else []


def delete_plane_work_item(work_id: str) -> None:
    _plane("DELETE", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/")


# ─── Dolt helpers ────────────────────────────────────────────────────────────


def dolt_sql(query: str) -> list[dict]:
    result = subprocess.run(
        ["dolt", "sql", "-q", query, "--result-format", "json"],
        cwd=str(DOLT_DIR), capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise TestFail(f"dolt sql failed: {result.stderr[:300]}")
    raw = result.stdout.strip()
    if not raw:
        return []
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        return parsed.get("rows", [])
    return parsed


def dolt_exec(query: str) -> None:
    result = subprocess.run(
        ["dolt", "sql", "-q", query],
        cwd=str(DOLT_DIR), capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise TestFail(f"dolt exec failed: {result.stderr[:300]}")


def reset_dolt() -> None:
    dolt_exec("DELETE FROM issue_comments")
    dolt_exec("DELETE FROM issue_labels")
    dolt_exec("DELETE FROM issues")


# ─── Sync runner ─────────────────────────────────────────────────────────────


def run_sync(issue_id: str | None = None) -> int:
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
    log(f"  run: {' '.join(cmd[1:6])} ...", "INFO")
    env = {
        **os.environ,
        "PLANE_HOST": PLANE_HOST,
        "PLANE_WORKSPACE": PLANE_WORKSPACE,
        "PLANE_API_TOKEN": _plane_token(),
    }
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    if proc.stdout.strip():
        for line in proc.stdout.strip().splitlines():
            log(f"  stdout: {line}", "INFO")
    if proc.stderr.strip():
        for line in proc.stderr.strip().splitlines():
            log(f"  stderr: {line}", "INFO")
    return proc.returncode


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


# ─── Test scenarios ──────────────────────────────────────────────────────────


def test_silent_skip_when_no_plane_label() -> None:
    log("Scenario: grava issue WITHOUT plane:<seq> label → silent exit 2", "STEP")
    reset_dolt()
    dolt_exec(
        f"INSERT INTO issues (id, title, status) "
        f"VALUES ('{GRAVA_ISSUE_ID}', 'orphan', 'open')"
    )
    rc = run_sync(GRAVA_ISSUE_ID)
    assert_eq(rc, 2, "exit code (non-mirrored issue)")


def test_status_sync(seq_id: int, work_id: str) -> None:
    log("Scenario: status open → in_progress → closed propagates to Plane", "STEP")

    # First sync — grava status=open, Plane is on default "Backlog".
    # State map says open → Todo, so we expect PATCH to Todo.
    rc = run_sync(GRAVA_ISSUE_ID)
    assert_eq(rc, 0, "exit code (status sync — open→Todo)")
    item = get_plane_work_item(work_id)
    assert_eq(item.get("state"), EXPECTED_STATES["Todo"], "Plane state after open")

    # Move grava → in_progress.
    dolt_exec(
        f"UPDATE issues SET status='in_progress' WHERE id='{GRAVA_ISSUE_ID}'"
    )
    rc = run_sync(GRAVA_ISSUE_ID)
    assert_eq(rc, 0, "exit code (in_progress)")
    item = get_plane_work_item(work_id)
    assert_eq(item.get("state"), EXPECTED_STATES["In Progress"],
              "Plane state after in_progress")

    # Move grava → closed.
    dolt_exec(
        f"UPDATE issues SET status='closed' WHERE id='{GRAVA_ISSUE_ID}'"
    )
    rc = run_sync(GRAVA_ISSUE_ID)
    assert_eq(rc, 0, "exit code (closed)")
    item = get_plane_work_item(work_id)
    assert_eq(item.get("state"), EXPECTED_STATES["Done"], "Plane state after closed")


def test_idempotency(work_id: str) -> None:
    log("Scenario: re-run with no grava change → no PATCH, exit 0", "STEP")
    before = get_plane_work_item(work_id)
    rc = run_sync(GRAVA_ISSUE_ID)
    assert_eq(rc, 0, "exit code (idempotent re-run)")
    after = get_plane_work_item(work_id)
    assert_eq(after.get("state"), before.get("state"), "Plane state (idempotent)")
    assert_eq(after.get("updated_at"), before.get("updated_at"),
              "Plane updated_at (idempotent — must be unchanged)")


def _assignee_ids(item: dict) -> set[str]:
    """Plane sometimes returns assignees as [uuid, ...] and sometimes as
    [{id, display_name, ...}, ...]. Normalise both to a set of UUIDs."""
    out: set[str] = set()
    for a in item.get("assignees") or []:
        if isinstance(a, dict):
            uid = a.get("id")
            if uid:
                out.add(uid)
        elif isinstance(a, str):
            out.add(a)
    return out


def test_assignee_sync(work_id: str) -> None:
    log("Scenario: grava assignee → Plane assignees mapping by display_name", "STEP")
    member_uuid = "32684b53-1ce0-4c16-9113-1443a5b63b20"
    dolt_exec(
        f"UPDATE issues SET assignee='{PLANE_TEST_MEMBER_NAME}' "
        f"WHERE id='{GRAVA_ISSUE_ID}'"
    )
    rc = run_sync(GRAVA_ISSUE_ID)
    assert_eq(rc, 0, "exit code (assignee set)")
    item = get_plane_work_item(work_id)
    if member_uuid not in _assignee_ids(item):
        raise TestFail(
            f"Plane assignees missing test member {member_uuid!r}: "
            f"{item.get('assignees')}"
        )
    log(f"Plane assignees includes test member {member_uuid}", "PASS")

    # Unassign in grava → Plane should clear.
    dolt_exec(f"UPDATE issues SET assignee=NULL WHERE id='{GRAVA_ISSUE_ID}'")
    rc = run_sync(GRAVA_ISSUE_ID)
    assert_eq(rc, 0, "exit code (unassign)")
    item = get_plane_work_item(work_id)
    assert_eq(_assignee_ids(item), set(), "Plane assignees (after unassign)")


def test_comment_sync(work_id: str) -> None:
    log("Scenario: grava issue_comments row → Plane comment POSTed", "STEP")
    before_comments = get_plane_comments(work_id)
    n_before = len(before_comments)

    marker = f"e2e-marker-{int(time.time())}"
    dolt_exec(
        f"INSERT INTO issue_comments (issue_id, message, actor) VALUES "
        f"('{GRAVA_ISSUE_ID}', 'hello plane {marker}', 'coder-agent')"
    )
    rc = run_sync(GRAVA_ISSUE_ID)
    assert_eq(rc, 0, "exit code (comment sync)")

    after_comments = get_plane_comments(work_id)
    assert_eq(len(after_comments), n_before + 1, "Plane comment count")

    found = any(
        marker in (c.get("comment_html") or "")
        for c in after_comments
    )
    if not found:
        raise TestFail(
            f"Plane comment marker {marker!r} not found in any of "
            f"{[c.get('comment_html', '')[:80] for c in after_comments]}"
        )
    log(f"Plane comment contains marker {marker!r}", "PASS")

    # Re-run — no new comment expected (cursor advanced).
    rc = run_sync(GRAVA_ISSUE_ID)
    assert_eq(rc, 0, "exit code (re-run after comment)")
    repeat = get_plane_comments(work_id)
    assert_eq(len(repeat), n_before + 1,
              "Plane comment count (idempotent — no duplicate)")


# ─── Orchestrator ────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cleanup", action="store_true",
                    help="Skip Plane work-item DELETE + dolt truncate.")
    args = ap.parse_args()

    if not SYNC_SCRIPT.exists():
        log(f"sync script missing at {SYNC_SCRIPT}", "FAIL")
        return 1
    if not DOLT_DIR.exists():
        log(f"dolt dir missing at {DOLT_DIR}", "FAIL")
        return 1

    write_system_yaml()
    if STATE_FILE.exists():
        STATE_FILE.unlink()

    work_id: str | None = None
    seq_id: int | None = None
    failures = 0

    try:
        # Step 0: silent-skip on non-mirrored issue.
        try:
            test_silent_skip_when_no_plane_label()
        except TestFail as exc:
            log(str(exc), "FAIL")
            failures += 1

        # Step 1: provision Plane work item.
        log("Creating Plane work item ...", "STEP")
        created = create_plane_work_item(f"e2e-{int(time.time())}")
        work_id = created["id"]
        seq_id = created["sequence_id"]
        log(f"Plane work item: id={work_id} seq={seq_id}", "INFO")

        # Step 2: seed grava issue + plane:<seq> label.
        reset_dolt()
        dolt_exec(
            f"INSERT INTO issues (id, title, status) VALUES "
            f"('{GRAVA_ISSUE_ID}', 'e2e mirror', 'open')"
        )
        dolt_exec(
            f"INSERT INTO issue_labels (issue_id, label) VALUES "
            f"('{GRAVA_ISSUE_ID}', 'plane:{seq_id}')"
        )

        # Reset state file so the first sync sees a true first-run.
        if STATE_FILE.exists():
            STATE_FILE.unlink()

        # Step 3-6: run the four scenarios.
        for scenario in (
            lambda: test_status_sync(seq_id, work_id),
            lambda: test_idempotency(work_id),
            lambda: test_assignee_sync(work_id),
            lambda: test_comment_sync(work_id),
        ):
            try:
                scenario()
            except TestFail as exc:
                log(str(exc), "FAIL")
                failures += 1

    finally:
        if not args.no_cleanup:
            log("Cleanup ...", "STEP")
            try:
                if work_id:
                    delete_plane_work_item(work_id)
                    log(f"Deleted Plane work item {work_id}", "INFO")
            except TestFail as exc:
                log(f"Cleanup Plane delete failed: {exc}", "INFO")
            try:
                reset_dolt()
                log("Truncated dolt tables", "INFO")
            except TestFail as exc:
                log(f"Cleanup dolt failed: {exc}", "INFO")
            if STATE_FILE.exists():
                STATE_FILE.unlink()

    if failures:
        log(f"FAILED: {failures} scenario(s)", "FAIL")
        return 1
    log("ALL SCENARIOS PASSED", "PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
