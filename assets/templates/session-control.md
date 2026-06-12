# Session Control

## Provider Boundary

- Provider: file | codex | codex-app | manual-provider
- Confirmed provider state is authoritative over requested state.
- File provider: local JSON session mock for tests and offline runs.
- Codex provider: requires --provider-command or MASTER_AGENT_SESSION_PROVIDER for create, send, read, archive, reconcile, and rotation.
- Codex app provider: Master uses Codex thread tools, then records confirmation commands.
- Codex app create confirmation: session-confirm-create after create_thread returns a thread id.
- Codex app send confirmation: session-confirm-send after send_message_to_thread completes.
- Codex app read confirmation: session-confirm-read after read_thread returns recent status.
- Codex app archive confirmation: session-confirm-archive after set_thread_archived completes.
- Provider command execution: parsed as argv; not executed through a shell.
- Provider command input: JSON request on stdin with event, agent id, role, provider session id, context packet, predecessor, message when relevant, and requested time.
- Provider command output: JSON object with provider_session_id, status, provider_session_path evidence, and messages for read operations.
- Manual provider: pending manual action until reconciled with external evidence.

## Session Lifecycle

- Create:
- Send:
- Read:
- Archive:
- Request rotation:
- Validate predecessor state:
- Rotate:
- Terminate:
- Reconcile:

## Context Injection

- Context packet:
- Accepted plan id:
- Predecessor agent:
- Inheritance reason:
- Save-state request:
- Predecessor state packet: required before normal successor launch.

## Status Reconciliation

- Active:
- Stale:
- Archived:
- Missing provider session:
- Missing Codex app read confirmation:

## Termination And Archive

- Graceful stop:
- Archive path:
- Retention:

## Failure Handling

- Provider unavailable:
- Pending manual provider action:
- Reconciliation failure:
- Live provider without confirmed active state:
- Strict rotation without predecessor state:

## Audit Trail

- Append state/session-control.jsonl for every requested and confirmed lifecycle event.
