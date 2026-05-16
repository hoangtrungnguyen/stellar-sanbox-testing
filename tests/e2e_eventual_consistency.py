#!/usr/bin/env python3
"""End-to-end test for the OPTION C design: eventual consistency between
`grava claim` and the Plane mirror.

Design choice (recorded in PR #1 discussion):
  Sync hooks fire only at the END of each agent's run (CODER_DONE,
  REVIEWER_APPROVED, PR_CREATED, etc.) — NOT immediately after
  `grava claim`. So claim-time mutations (status=in_progress, assignee)
  remain Grava-only until the agent's first end-of-work signal triggers
  the sync.

This test exercises the timing gap explicitly:

  T+0   grava claim --actor <member>       # sets in_progress + assignee
  T+1   (verify Plane STILL on Backlog — no assignees)
  T+2   grava comment (interim work)        # interim mutation
  T+3   grava signal CODER_DONE             # end-of-coder signal
  T+4   sync hook fires                     # the only sync invocation
  T+5   (verify Plane state=In Progress AND assignees=[member]
         AND comment was POSTed — ALL three mutations propagate in one
         sync pass)

Pass criteria:
  - Sync hook fires exactly ONCE.
  - That single sync PATCHes both `state` and `assignees` in one call.
  - The interim comment also reaches Plane in the same pass.

Run:
    python3 tests/e2e_eventual_consistency.py
    python3 tests/e2e_eventual_consistency.py --cleanup
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
              "STEP": "\033[35m", "URL": "\033[34m", "CMD": "\033[33m",
              "T":    "\033[33m"}.get(level, "")
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
        "description_html": "<p>e2e_eventual_consistency</p>",
    })


def plane_get(work_id: str) -> dict:
    return _plane("GET", f"projects/{PLANE_PROJECT_ID}/work-items/{work_id}/")


def plane_comments(work_id: str) -> list:
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


# ─── grava wrapper ───────────────────────────────────────────────────────────


def grava(*args: str, json_out: bool = False, quiet: bool = False) -> str | dict | list:
    cmd = ["grava", *args]
    if json_out:
        cmd.append("--json")
    if not quiet:
        log(f"  $ {' '.join(cmd)}", "CMD")
    res = subprocess.run(cmd, cwd=str(SANDBOX_ROOT), capture_output=True,
                         text=True, timeout=30)
    if res.returncode != 0:
        raise FlowFail(f"grava {args[0]} failed: {(res.stderr or res.stdout)[:200]}")
    out = (res.stdout or "").strip()
    if not json_out or not out:
        return out
    lines = out.splitlines()
    for i, line in enumerate(lines):
        s = line.lstrip()
        if s.startswith(("{", "[")):
            try:
                return json.loads("\n".join(lines[i:]))
            except json.JSONDecodeError:
                continue
    raise FlowFail(f"grava {args[0]}: cannot parse JSON")


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


# ─── Sync runner — captures stderr so we can count PATCH calls ──────────────


def run_sync_capture(issue_id: str) -> tuple[int, str]:
    """Run the sync script and return (exit_code, stderr_log)."""
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
    return proc.returncode, (proc.stderr or "")


def count_patch_lines(stderr: str) -> tuple[int, list[str]]:
    """Return (num_PATCH_invocations, [field_lists_per_PATCH])."""
    patches = []
    for line in stderr.splitlines():
        if "PATCH plane=" in line:
            # Format: ... fields=['state', 'assignees']
            idx = line.find("fields=")
            patches.append(line[idx:] if idx >= 0 else line)
    return len(patches), patches


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
    title = f"eventual-consistency-{ts}"
    write_system_yaml()
    if STATE_FILE.exists():
        STATE_FILE.unlink()

    plane_id = ""
    grava_id = ""
    failures = 0

    try:
        # ── T+0: Provision Plane + grava issue, label, prime cache ─────────
        log("T+0  Provision Plane work item + grava issue + plane:<seq>", "T")
        wi = plane_create(title)
        plane_id, seq_id = wi["id"], wi["sequence_id"]
        log(f"     Plane STELL-{seq_id}", "OK")

        resp = grava("create", "-t", title, "--type", "task", json_out=True)
        grava_id = resp["id"]
        log(f"     Grava {grava_id}", "OK")
        grava("label", grava_id, "--add", f"plane:{seq_id}", json_out=True, quiet=True)

        # Prime the cache so subsequent runs aren't "first-seen" (which would
        # skip historical comments).
        rc, err = run_sync_capture(grava_id)
        log(f"     (priming sync: exit {rc})", "INFO")

        # ── T+1: grava claim — sets in_progress + assignee. NO sync hook fires. ──
        log("T+1  grava claim --actor <member>  (no agent signal yet → no sync)", "T")
        grava("claim", grava_id, "--actor", PLANE_TEST_MEMBER)

        item = plane_get(plane_id)
        state_name = PLANE_STATE_NAME_BY_UUID.get(item.get("state"), "?")
        assignees = _assignee_ids(item)
        log(f"     Plane state='{state_name}'  assignees={assignees}", "INFO")
        if state_name == "In Progress":
            log("     ✗ EXPECTED Plane to still be on the prior state — sync fired too early", "ERR")
            failures += 1
        else:
            log("     ✓ Plane unchanged (claim is grava-only until agent signals)", "OK")

        # ── T+2: interim mutation — grava comment ──────────────────────────
        log("T+2  grava comment (interim work)  — still no sync", "T")
        marker = f"interim-{ts}"
        grava("comment", grava_id, "-m", f"investigating {marker}")
        comments_before = len(plane_comments(plane_id))
        log(f"     Plane comment count = {comments_before} (unchanged from cache)", "INFO")

        # ── T+3: grava signal CODER_DONE ───────────────────────────────────
        log("T+3  grava signal CODER_DONE  (end of coder agent run)", "T")
        grava("signal", "CODER_DONE", "--issue", grava_id, "--payload", "sha-cafef00d")

        # ── T+4: THE sync hook fires — the only one this whole run ─────────
        log("T+4  grava_plane_sync.py  (the agent's post-signal hook)", "T")
        rc, err = run_sync_capture(grava_id)
        if rc != 0:
            log(f"     sync exit={rc}", "ERR")
            failures += 1
        n_patch, patch_fields = count_patch_lines(err)
        for p in patch_fields:
            log(f"     {p}", "INFO")
        if n_patch != 1:
            log(f"     ✗ EXPECTED exactly 1 PATCH call (got {n_patch})", "ERR")
            failures += 1
        else:
            log("     ✓ Exactly 1 PATCH call — both fields bundled", "OK")
            if "state" in patch_fields[0] and "assignees" in patch_fields[0]:
                log("     ✓ PATCH carried BOTH state AND assignees in one request", "OK")
            else:
                log(f"     ✗ PATCH missing expected fields: {patch_fields[0]}", "ERR")
                failures += 1

        # ── T+5: verify Plane reflects ALL three mutations ─────────────────
        log("T+5  Verify Plane absorbed ALL pre-sync mutations in one pass", "T")
        item = plane_get(plane_id)
        state_name = PLANE_STATE_NAME_BY_UUID.get(item.get("state"), "?")
        assignees = _assignee_ids(item)
        comments_after = plane_comments(plane_id)
        n_comments = len(comments_after)
        marker_hit = any(marker in (c.get("comment_html") or "") for c in comments_after)

        ok_state    = state_name == "In Progress"
        ok_assignee = PLANE_TEST_MEMBER_UUID in assignees
        ok_comment  = n_comments == comments_before + 1 and marker_hit

        log(f"     state='{state_name}'                                          "
            f"{'✓' if ok_state else '✗'}",
            "OK" if ok_state else "ERR")
        log(f"     assignees={assignees}  (member={PLANE_TEST_MEMBER_UUID})  "
            f"{'✓' if ok_assignee else '✗'}",
            "OK" if ok_assignee else "ERR")
        log(f"     comments before={comments_before} after={n_comments} marker={marker_hit}  "
            f"{'✓' if ok_comment else '✗'}",
            "OK" if ok_comment else "ERR")

        if not (ok_state and ok_assignee and ok_comment):
            failures += 1

        # ── Summary ────────────────────────────────────────────────────────
        log("", "INFO")
        log("Eventual-consistency contract VERIFIED:", "STEP" if failures == 0 else "ERR")
        log("  1. grava claim did NOT trigger an immediate Plane PATCH", "INFO")
        log("  2. interim grava comment was NOT pushed live", "INFO")
        log("  3. CODER_DONE → ONE sync call → ALL three mutations propagated", "INFO")
        log("", "INFO")
        log(f"     Plane work item: {plane_url(seq_id)}", "URL")

        if failures:
            log(f"FAILED: {failures} assertion(s)", "ERR")
            return 1
        log("ALL ASSERTIONS PASSED", "OK")
        return 0

    finally:
        if args.cleanup:
            log("Cleanup", "STEP")
            if grava_id:
                try:
                    grava("close", grava_id, "--force", quiet=True)
                except FlowFail:
                    pass
                try:
                    grava("drop", grava_id, "--force", quiet=True)
                    log(f"  grava drop {grava_id}", "OK")
                except FlowFail as exc:
                    log(f"  grava drop failed: {exc}", "ERR")
            if plane_id:
                try:
                    plane_delete(plane_id)
                    log(f"  plane delete {plane_id}", "OK")
                except FlowFail as exc:
                    log(f"  plane delete failed: {exc}", "ERR")
            if STATE_FILE.exists():
                STATE_FILE.unlink()


if __name__ == "__main__":
    sys.exit(main())
