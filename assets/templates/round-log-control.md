# Round Log Control

## Purpose

- Optional local history evidence for Codex implementation rounds.
- Complements Git status; it does not replace Git boundary enforcement.
- Helps the Master correlate concurrent sub-agent work with per-round snapshot evidence.

## Provider

- Status:
- Repo root:
- Round log root: .codex-round-log
- Latest snapshot id:
- Snapshot count:
- Plugin command:

## Evidence Binding

- Latest agent id:
- Latest plan id:
- Latest worktree id:
- Latest evidence snapshot id:
- Latest manifest path:
- Required before receipt acceptance: yes

## Snapshot Policy

- Inspect status before accepting concurrent implementation receipts.
- Record snapshot id, manifest path, agent id, plan id, and worktree id when available.
- Treat manifest paths as evidence, not implementation authority.
- Use Git boundary enforcement for current write-set blocking.

## Export Policy

- Export only when readable review evidence is needed.
- Export path:
- Export must not mutate source snapshots or project code.

## Restore Policy

- Restore requires explicit human decision.
- Prefer restore dry-run before any real restore.
- Preserve unrelated local work and review the safety snapshot first.
