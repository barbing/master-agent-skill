# Worktree Control

## Purpose

- Goal: isolate sub-agent implementation work from the user's local checkout and remote branches.
- Master rule: plan and confirm Worktrees before assigning implementation sessions.

## Worktree Policy

- Default provider: codex-app
- Local checkout policy: do not mutate the user's foreground checkout.
- Remote policy: do not push branches, create pull requests, or update GitHub without an explicit release or merge gate.
- Branch policy: use detached or provider-managed Worktrees until a merge owner approves branch creation.
- Required preflight: validate-worktreeinclude before copying ignored local files into a managed Worktree.

## Managed Worktrees

| Worktree Id | Provider | Base Branch | Status | Purpose | Provider Ref |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

## Session Binding

- Each implementation session should name its Worktree Id.
- A Worktree may host only the sessions explicitly bound in state/worktrees.jsonl.
- Reconcile Worktree state before accepting coding receipts.

## Ignored Local Files

- Copy ignored local files only through an approved .worktreeinclude file.
- Do not list tracked files in .worktreeinclude.
- Do not list broad patterns, absolute paths, repository escapes, symlinks, or .git paths.
- AGENTS.override.md may be handled by Codex app behavior and does not need to be listed.

## Handoff And Merge

- Worktree-to-local handoff requires a recorded request and confirmation.
- Branch creation requires a merge owner and conflict protocol from the work order.
- Remote publication requires release validation and explicit approval evidence.

## Cleanup And Reconcile

- Reconcile active Worktrees with worktree-reconcile.
- Close completed or abandoned Worktrees with worktree-close, then record worktree-confirm-close after provider confirmation.
- Treat missing Worktree evidence as stale, not as success.

## Audit Trail

- Append all Worktree lifecycle events to state/worktrees.jsonl.
- Record linked session events in state/session-control.jsonl.
