# Provider-Command Adapter Contract

Use this reference when implementing an unattended provider for `provider=codex`.

The Master CLI invokes the provider command as argv, not through a shell. The provider receives one JSON object on stdin and must return one JSON object on stdout. Any nonzero exit, invalid JSON, missing active status, or missing evidence path is treated as a failed provider operation.

Worktree lifecycle may be implemented by a future provider-command adapter, but it must follow the same evidence rule: the provider must not mutate the user's foreground checkout, push branches, create pull requests, or report Worktree success without durable provider evidence.

## Required Events

The provider must accept these `event` values:

- `session-create`
- `session-send`
- `session-read`
- `session-archive`
- `session-reconcile`

Future Worktree-capable providers should also expose equivalent evidence for:

- `worktree-create`
- `worktree-reconcile`
- `worktree-close`

## Request Shape

Common fields:

```json
{
  "event": "session-create",
  "provider": "codex",
  "agent_id": "coding-1",
  "role": "Coding",
  "provider_session_id": "",
  "provider_session_path": "",
  "context_packet": "docs/master-agent/packets/context-packet.md",
  "message": "Return a packet.",
  "requested_at": "2026-06-01T00:00:00+00:00"
}
```

Only some fields are present for each event. A provider must ignore unknown fields and must not infer success without durable evidence.

## Response Shape

Every successful operation should return:

```json
{
  "provider_session_id": "provider-specific-id",
  "provider_session_path": "docs/master-agent/state/provider-sessions.json",
  "provider_session_ref": "provider-specific-reference",
  "status": "active",
  "messages": [
    {
      "sender": "provider",
      "message": "ready"
    }
  ]
}
```

`status` must be `active` for create, send, read, and reconcile success. Use `archived` after archive, and `missing`, `stale`, or `dead` when reconcile cannot prove the session is alive.

`provider_session_path` must point to a durable evidence file that exists after the provider returns. The Master uses this path for reconciliation and release diagnostics.

## Reference Adapter

Use `scripts/file_session_provider.py` as the minimal reference implementation:

```bash
python scripts/master_agent_tool.py session-create \
  --state-dir docs/master-agent \
  --agent-id strategy-live \
  --role Strategy \
  --context-packet docs/master-agent/packets/context-packet.md \
  --provider codex \
  --provider-command "python scripts/file_session_provider.py --state-file docs/master-agent/state/provider-sessions.json"
```

The file adapter is not a model runner. It exists to prove the provider-command contract, session lifecycle, reconciliation, and release gates before a project attaches a live automation backend.

## Hard Requirements For Live Providers

- Do not report success until the external session or thread actually exists.
- Do not hide failed sends or reads behind an `active` status.
- Preserve enough provider evidence to debug stale sessions.
- Keep provider state under the project state directory when possible.
- Return bounded transcripts or summaries; do not dump unlimited conversation history into stdout.
- Treat archive as an auditable state transition, not deletion.
- Keep Worktree state isolated from the user's local checkout and remote branches until the Master records an explicit merge or release gate.
