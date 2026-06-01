# State Schema

## Current Schema

- Version:
- Compatible tool version:

## Migration Order

- Ordered, idempotent migrations are recorded in state/schema-version.json.

## Compatibility Policy

- New tools may add missing state files.
- Existing non-empty Markdown is never overwritten without force.

## Recovery Policy

- Prefer replay from append-only JSONL logs.
- Derived Markdown may be regenerated from machine state.

## Corruption Quarantine

- Corrupt JSON is copied under state/quarantine before replacement.

## Replay Sources

- state/token-usage.jsonl
- state/heartbeats.jsonl
- state/strategy-sync.jsonl
- state/session-control.jsonl
- state/incidents.jsonl
- state/alerts.jsonl

## Stale Lock Handling

- Remove only recoverable locks under the state directory.
- Runtime writes use owner-stamped lock files with pid and acquisition time.
- A lock may be reclaimed automatically when the recorded owner process is no longer alive.
- A live owner lock must not be removed only because it is older than the stale threshold.
- Windows lock release retries deletion to avoid transient reader/delete races.
