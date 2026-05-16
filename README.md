# stellar-sand-box

End-to-end sandbox for `grava_plane_sync.py` (Grava → Plane mirror).

## Layout

```
stellar-sand-box/
├── .grava/dolt/           # local Dolt DB (grava-compatible schema)
├── tests/
│   ├── schema.sql         # CREATE TABLE for issues, issue_labels, issue_comments
│   ├── system.yaml        # plane_state_map (generated at test run)
│   └── e2e_grava_plane_sync.py   # the e2e harness
└── README.md
```

## Plane workspace

- Workspace: `stellar-sandbox`
- Project UUID: `cec88b42-b47c-4f1c-bfdf-a882c490a784`
- States: `Backlog` (backlog) · `Todo` (unstarted) · `In Progress` (started) · `Done` (completed) · `Cancelled` (cancelled)

## Run the e2e

```bash
# One-shot — creates a Plane work item, mirrors in Dolt, exercises every
# scenario, then deletes the Plane work item + truncates the Dolt tables.
python3 tests/e2e_grava_plane_sync.py

# Keep artefacts on Plane / Dolt for inspection:
python3 tests/e2e_grava_plane_sync.py --no-cleanup
```

## What it tests

| Scenario | Assertion |
|---|---|
| grava issue without `plane:<seq>` label | sync exits 2 silently |
| grava status `open` → `in_progress` → `closed` | Plane state follows via `plane_state_map` |
| Re-run with no grava change | no PATCH; `updated_at` unchanged (idempotent) |
| grava `assignee` set to a real Plane member display name | Plane `assignees` populated |
| grava `assignee` cleared | Plane assignees emptied |
| new `issue_comments` row | Plane comment POSTed with `[grava/<actor>]` prefix |
| Re-run after comment | comment cursor advances, no duplicate POST |

## Rebuild the Dolt DB from scratch

```bash
rm -rf .grava/dolt && mkdir -p .grava/dolt
( cd .grava/dolt && dolt init --name "Sandbox" --email sandbox@local )
( cd .grava/dolt && dolt sql < ../../tests/schema.sql )
```

## Prerequisites

- `dolt` on `$PATH` (tested with 1.82.0+)
- `~/.config/plane/config.json` with a token that has access to the
  `stellar-sandbox` workspace
- Python ≥ 3.10, `requests` package
