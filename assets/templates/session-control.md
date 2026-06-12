# Session Control

## Provider Boundary

- Provider: file | codex | manual-provider
- Confirmed provider state is authoritative over requested state.
- File provider: local JSON session mock for tests and offline runs.
- Codex provider: requires --provider-command or MASTER_AGENT_SESSION_PROVIDER for create, send, read, archive, reconcile, and rotation.
- Provider command execution: parsed as argv; not executed through a shell.
- Provider command input: JSON request on stdin with event, agent id, role, provider session id, context packet, predecessor, message when relevant, and requested time.
- Provider command output: JSON object with provider_session_id, status, provider_session_path evidence, and messages for read operations.
- Manual provider: pending manual action until reconciled with external evidence.

## Session Lifecycle

- Create:
- Send:
- Read:
- Archive:
- Rotate:
- Terminate:
- Reconcile:

## Context Injection

- Context packet:
- Accepted plan id:
- Predecessor agent:
- Inheritance reason:
- Save-state request:
- Predecessor state packet:

## Status Reconciliation

- Active:
- Stale:
- Archived:
- Missing provider session:

## Termination And Archive

- Graceful stop:
- Archive path:
- Retention:

## Failure Handling

- Provider unavailable:
- Pending manual provider action:
- Reconciliation failure:
- Live provider without confirmed active state:

## Audit Trail

- Append state/session-control.jsonl for every requested and confirmed lifecycle event.
