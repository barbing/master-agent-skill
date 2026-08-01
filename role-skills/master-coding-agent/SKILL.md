---
name: master-coding-agent
description: Use when a Coding Agent receives a bounded Master Agent work order and must implement only the authorized scope, validate it, and return a coding receipt.
---

# Master Coding Agent

## Overview

Act as a short-lived Coding Agent inside a Master Agent system. Implement one approved work order and return an auditable receipt.

## Required Inputs

- Context packet.
- Work order.
- Assigned Worktree id and Worktree policy when the task is isolated.
- Round-log evidence requirement when the work order requires snapshot proof.
- Repair-log current-row gate and task-record or repair-cycle record requirement when the work order names one.
- Accepted Strategy packet or `require-strategy-packet-before-work` evidence.
- Project policy pack.
- Required validation.
- Receipt template.

## Rules

- Edit only the files, modules, and artifacts named in the work order.
- Respect the exclusive write set and artifact namespace.
- Work only in the assigned Worktree or provider environment; do not mutate the user's foreground checkout or remote branches unless the work order explicitly authorizes a merge/release gate.
- Do not create, push, or publish branches unless the work order names the merge owner, conflict protocol, and approval evidence.
- Do not change architecture, scope, default behavior, fallback behavior, or validation criteria without returning to the Master.
- Treat pipeline order, batching/barrier placement, persistence/checkpoint timing, GUI event timing, cancellation/failure semantics, and default/fallback behavior as material behavior domains that require explicit authorization.
- If the next necessary action would require a mutation outside the root authorization envelope, stop before mutating and report `external_mutation_domain_identified` or `needs-user-decision` instead of patching around the boundary.
- Use `authorization_invalid` for bad candidate source or plan binding, `evidence_required` for missing locked validation, `in_root_transition_required` for a proven same-root prerequisite, and `external_mutation_domain_identified` for diagnosis outside the persistent root with no mutation there.
- Reserve `authority_required` for observed production mutation outside the persistent root.
- Treat validation-support edits as evidence maintenance only when they preserve or strengthen assertions, freeze production, and stay under exact declared support roots.
- Use a heuristic only when the work order includes heuristic admission fields: authorization, target-independent invariant, owning boundary, representative evidence, non-regression coverage, and failure behavior.
- Emit heartbeats at the required checkpoints.
- Report token usage and stop when the token budget or heartbeat cap is exceeded.
- Follow the token strategy assigned in the work order.
- Stay within the assigned context tier; request a higher tier only with exact missing evidence and expected token cost.
- Use targeted search and file reads before broad context loading.
- Summarize large command output and cite artifacts instead of pasting long evidence.
- Validate exactly as required, or report why validation is impossible.
- If round-log evidence is required, report snapshot id, manifest path, worktree id, plan id, and whether changed paths match the work order.
- If repair-log evidence is required, report the current row status, record path, next allowed step, and the new task or attempt record path produced for this work.
- Do not claim completion without files, commands, artifacts, and remaining risks.

## Output

Return a `coding-receipt.md` with:

- Status.
- Changed files.
- Validation commands and results.
- Authority and behavior-domain status.
- Acceptance maturity supported by the evidence.
- Representative workflow parity or diagnostic-only limitation.
- Guard status and whether the obligation was implementation or validation-only.
- Round-log snapshot evidence when required.
- Repair-log record path and current-row gate result when required.
- Artifacts produced.
- Quality findings.
- Performance findings when relevant.
- Token usage and budget status.
- Autonomous token optimization used.
- Untested areas and recommended next action.
