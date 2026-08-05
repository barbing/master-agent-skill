# Repair Log Control

## Purpose

- Project-local document memory for bounded tasks and repeated repair cycles.
- Keeps continuity in repository docs instead of raw conversation history.
- Complements Master ledgers, guard obligations, round-log evidence, and Git status.

## Provider

- Repair log root:
- Status: missing | initialized | active | blocked | partial
- Latest index:
- Latest row status:
- Latest row next allowed step:

## Task Record Lane

- Task records root: docs/repair-execution-log/task-records
- Task index: docs/repair-execution-log/task-records/plan-index.md
- One-off task records do not authorize autonomous loops.
- A paused, rollback-only, superseded, or no-further-action task row blocks successor work.

## Lineage And Guard Independence

- Record lineage independent from guard mode: yes
- Record identity fields: objective, original target error, controlling plan/spec
- Logging arms guard: no
- Guard not required status: loop_guard_not_required
- Originating bounded record preserved when cycle opens: yes
- Reciprocal cycle links required: yes
- Shared index contention blocks work: no

## Repair Cycle Lane

- Cycle index: docs/repair-execution-log/plan-index.md
- Active cycle:
- Active cycle plan:
- Active cycle record:
- Repair cycles require explicit current user, goal, or approved-plan authority.

## Current Row Gate

- Required before launching or accepting sub-agent work when prior document memory exists.
- The current row must name status, record path, next allowed step, and escalation trigger.
- Blocked statuses: paused, blocked, not-ready, repair-cycle-needed, rollback-only, no-further-action, superseded, complete, accepted.
- Allowed statuses default to active, continue, in-progress, ready.

## Record Policy

- Record task outcomes that affect future work.
- Open a repair cycle for repeated failure classes, rollback/retry sequences, or autonomous iteration.
- Do not treat task records, repair records, or plan-index rows as root authorization.
- Do not create command-level record spam; consolidate when record volume grows.

## Audit Trail

- Events file: state/repair-log-events.jsonl
- Latest event:
- Latest event at:
- Latest record path:
