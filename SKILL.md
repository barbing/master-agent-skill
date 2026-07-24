---
name: master-agent-system
description: Use when coordinating multiple Codex sessions or sub-agents across a project, designing a non-implementing master agent, maintaining project ledgers, issuing work orders, monitoring heartbeats, running a runtime supervisor, rotating overloaded sessions into successor agents, managing token budgets, optimizing sub-agent token use, distilling corrections into governed learning updates, defining or activating project-specific agent roles, enforcing Master write boundaries, assessing parallel sub-agent safety, or standardizing strategy, coding, review, policy, and learning handoffs.
---

# Master Agent System

## Overview

Use this skill to run a project-neutral Master Agent control system. The Master Agent is a non-implementing coordination layer: it may update docs and state ledgers, but it must not patch production code.

The system keeps long project continuity outside conversation history by using ledgers, context packets, heartbeats, work orders, return packets, and review verdicts.

## Load Order

Read `references/master-agent-system.md` when setting up the system, designing a new project adapter, or resolving a coordination ambiguity.

Use `scripts/master_agent_tool.py` as the primary tool. It bootstraps state, validates readiness, registers agents, governs roles, lints and accepts strategy plans, requires the current Strategy packet before work, records heartbeats, audits anomalies, creates remediation packets, requests strict rotation state, rotates overloaded sessions into successor agents, records Codex app session confirmations, enforces Master boundaries, assesses parallelism, runs supervisor cycles, tracks token budgets, recommends token-saving constraints, records learning corrections, lints and accepts learning proposals, detects stale or over-budget agents, creates packet files, and installs role skills.

Use `scripts/soak_validate.py --quick` before release or after runtime-control changes. Use `scripts/file_session_provider.py` as the provider-command reference adapter when testing unattended `provider=codex` flows.

Read `references/provider-command-adapter.md` before building a live provider-command adapter. Copy templates from `assets/templates/` when a single artifact is needed without bootstrapping the full state pack.

Use active roles from `role-catalog.md` when launching short-lived sessions. Use role skills from `role-skills/` for the default Strategy, Coding, Review, and Policy Review roles, or scaffold a custom role skill when a project-defined role becomes reusable.

## Core Rules

| Rule | Meaning |
| --- | --- |
| Master does not implement | The Master Agent does not edit production code, tests, runtime config, migrations, or behavior. It may edit ledgers, docs, plans, work orders, and policy packs. |
| Conversation is not state | Decisions become project state only after the Master accepts a structured packet into the ledger. |
| Authority is explicit | Every task packet names the authority docs, accepted decisions, allowed boundaries, forbidden changes, validation, and stop conditions. |
| Agents are short-lived | Strategy, Coding, Review, Policy Review, and custom role agents should complete one bounded assignment and return a packet. |
| Roles are governed | Register agents only with active roles from `role-catalog.md`; undefined, proposed, or inactive roles are invalid. |
| Heartbeats are required | Any running sub-agent must emit structured progress packets on checkpoint, before risky edits, after validation, and when blocked. |
| Token strategy is required | Every project should set a project budget, per-agent budget when possible, heartbeat/session caps, and a token strategy before spawning sub-agents. |
| Worktrees isolate implementation | Plan and confirm a Worktree before implementation sessions when isolation is needed; do not mutate the user's local checkout or remote branches without an explicit merge/release gate. |
| Parallelism is conditional | Run multiple sub-agents only when their write sets, artifacts, and acceptance criteria are independent. |
| Root authority is non-escalating | A plan or repair record may narrow or sequence work, but it cannot widen the original user grant, approved owners, file scopes, or material behavior domains. |
| Material behavior is declared | Every context packet, work order, and receipt declares whether it touches pipeline order, batching/barriers, persistence/checkpoints, GUI timing, cancellation/failure semantics, or default/fallback behavior. |
| Heuristics require admission | A heuristic is allowed only when the packet names authorization, target-independent invariant, owning boundary, representative evidence, non-regression coverage, and failure behavior. |
| Acceptance maturity is monotonic | Do not claim a higher maturity gate until all lower gates are recorded as passed for the same scope. |
| Authority stops are explicit | When the next action needs a wider grant or user decision, mark the agent `authority_required` and record a governance event. |
| Review is separate | Coding receipts are not accepted until reviewed, unless the user explicitly chooses to skip review. |
| Rotation is strict | Launch a successor only from a validated predecessor-state packet, except explicit emergency recovery. |
| Learning is governed | Corrections become durable behavior only through a correction record, learning cycle, linted learning proposal, acceptance event, and recurrence check. |

## Roles

Default active roles:

| Role | Allowed work | Not allowed |
| --- | --- | --- |
| Master Agent | Ledger, routing, heartbeat monitoring, work orders, stop/go decisions, state docs | Production implementation |
| Strategy Agent | Diagnosis, architecture discussion, options, recommendation, proposed work order | Production implementation unless separately assigned as Coding Agent |
| Coding Agent | One bounded implementation task with scoped files and validation | Changing architecture or scope without returning to Master |
| Review Agent | Diff review, artifact review, validation evidence, verdict | Product direction or broad redesign |
| Policy Review Agent | Check proposed work against authority docs and project policy | Implementation or final product decision |
| Learning Distiller Agent | Mine corrections, incidents, failed reviews, and anomalies into learning proposals | Production implementation or unreviewed self-modification |

Define custom roles only when a project has a recurring or specialized responsibility that does not fit the default roles. Capture the need in `role-proposal.md`, define it with positive token and heartbeat bounds, activate it with `--approval` evidence, and register agents only after it is active.

## Standard Workflow

1. Bootstrap the project state pack with `scripts/master_agent_tool.py init --project-root <project-root>`.
2. Fill `master-ledger.md` and `project-policy-pack.md`, then run strict validation.
3. Set token budgets and session caps with `set-budget`, per-agent registration fields, and `token-strategy.md`.
4. Check `role-catalog.md` and decide whether the next step fits an active role, needs a role proposal, or should remain direct ledger maintenance.
5. Accept only complete Strategy packets. `accept-strategy` runs Strategy packet validation and records validation evidence; `require-strategy-packet-before-work` verifies that evidence before Coding, Review, or Policy Review work.
6. For implementation work, plan an isolated Worktree with `worktree-plan`, validate `.worktreeinclude` when ignored local files are needed, confirm provider evidence with `worktree-confirm-create`, and bind the session with `worktree-assign-session`.
7. Create a context packet or work order with `new-packet`, fill the root authorization envelope, material behavior domains, representative workflow fields, acceptance gates, and heuristic admission fields, then run `governance-lint`.
8. Run `record-acceptance-gate` as gates pass, preserving monotonic maturity for the task scope.
9. Run `recommend-token-strategy` before launching or continuing a sub-agent whose next step has a material token cost.
10. Register the running sub-agent and require heartbeats plus token usage reports.
11. Monitor with `supervise`, `check-heartbeats`, `watch-heartbeats`, `check-budget`, `worktree-reconcile`, and `recommend-token-strategy` until the agent completes, blocks, drifts, or needs authority.
12. Use `record-authority-required` when the next necessary action exceeds the root authorization envelope.
13. Accept, reject, or request clarification on the return packet.
14. Record material user corrections, failed reviews, and repeated agent mistakes with `record-learning-correction`.
15. Periodically start a learning cycle, lint proposals, accept only reviewed learning updates, and track recurrence.
16. Update the master ledger and event log only after acceptance.
17. Derive the next action from the updated ledger, not from conversational momentum.

## Agent Selection

| Situation | Use |
| --- | --- |
| Need architecture diagnosis, tradeoff analysis, or solution design | Strategy Agent |
| Need production code, test, or config changes | Coding Agent |
| Need to check a diff, artifact, log, screenshot, or validation claim | Review Agent |
| Need to check roadmap, policy, boundaries, or acceptance criteria | Policy Review Agent |
| Need to convert repeated corrections or agent mistakes into durable behavior updates | Learning Distiller Agent |
| Need a recurring specialized responsibility not covered by active roles | Draft `role-proposal.md`, then define and activate a custom role |
| Need to reconcile accepted packets, update next action, or stop drift | Master Agent only |
| Need to replace an overloaded or looping sub-agent without losing continuity | `rotate-session` |

## Parallelism Gate

Allow parallel sub-agents only when all are true:

- Tasks have separate ownership boundaries and likely disjoint write sets.
- Outputs cannot overwrite each other.
- Each task has an independent acceptance criterion.
- The Master can merge the results without resolving architecture ambiguity.
- At least one non-blocking task can proceed while another runs.

If any condition is false, run one sub-agent at a time.

## Drift Stop Conditions

Stop or pause a sub-agent when it:

- Edits outside the authorized scope.
- Treats a prior phase as the complete plan.
- Adds fallback, advisory, heuristic, or compatibility behavior not authorized by the context packet.
- Skips required validation.
- Cannot identify the owning boundary for a bug.
- Reports progress without artifacts, files, commands, or evidence.
- Exceeds token budget, heartbeat cap, or session creation cap.
- Repeats the same failed approach after the allowed attempt count.
- Encounters a conflict between user direction and authority docs.

## State Pack

Default project state directory:

```bash
docs/master-agent
```

Bootstrap from the skill folder:

```bash
python scripts/master_agent_tool.py init --project-root <repo-or-project-root>
```

Validate:

```bash
python scripts/master_agent_tool.py validate --state-dir <repo-or-project-root>/docs/master-agent --strict
```

Install role skills:

```bash
python scripts/master_agent_tool.py install-system --skills-dir <codex-skills-dir>
python scripts/master_agent_tool.py install-role-skills --skills-dir <codex-skills-dir>
```

Register and monitor agents:

```bash
python scripts/master_agent_tool.py strategy-packet-lint --packet packets/strategy-packet.md
python scripts/master_agent_tool.py accept-strategy --state-dir <state-dir> --packet packets/strategy-packet.md --plan-id PLAN-1 --summary "Approved bounded plan"
python scripts/master_agent_tool.py strategy-sync-status --state-dir <state-dir>
python scripts/master_agent_tool.py require-plan --state-dir <state-dir> --plan-id PLAN-1
python scripts/master_agent_tool.py require-strategy-packet-before-work --state-dir <state-dir> --plan-id PLAN-1 --packet packets/strategy-packet.md
python scripts/master_agent_tool.py register-agent --state-dir <state-dir> --agent-id coding-1 --role Coding --task-id TASK-1 --objective "Execute approved work" --scope src/module --plan-id PLAN-1
python scripts/master_agent_tool.py session-create --state-dir <state-dir> --agent-id coding-1 --role Coding --context-packet packets/context-packet.md --provider file
python scripts/master_agent_tool.py register-agent --state-dir <state-dir> --agent-id strategy-1 --role Strategy --task-id TASK-2 --objective "Resolve next boundary" --scope docs/master-agent
python scripts/master_agent_tool.py heartbeat --state-dir <state-dir> --agent-id strategy-1 --state active --current strategy-packet.md --last-action "Read authority" --next-action "Draft packet" --scope-status yes --confidence high
python scripts/master_agent_tool.py audit-agent --state-dir <state-dir> --agent-id strategy-1
python scripts/master_agent_tool.py remediate-agent --state-dir <state-dir> --agent-id strategy-1 --action reinforce-context
python scripts/master_agent_tool.py supervise --state-dir <state-dir> --poll-seconds 60 --max-cycles 1
python scripts/master_agent_tool.py supervise --state-dir <state-dir> --poll-seconds 60 --run-until-stopped
python scripts/master_agent_tool.py supervisor-start --state-dir <state-dir> --poll-seconds 60 --spawn
python scripts/master_agent_tool.py supervisor-status --state-dir <state-dir>
python scripts/master_agent_tool.py supervisor-stop --state-dir <state-dir>
python scripts/master_agent_tool.py supervisor-recover --state-dir <state-dir>
python scripts/master_agent_tool.py session-create --state-dir <state-dir> --agent-id strategy-1 --role Strategy --context-packet packets/context-packet.md --provider file
python scripts/master_agent_tool.py session-create --state-dir <state-dir> --agent-id strategy-live --role Strategy --context-packet packets/context-packet.md --provider codex --provider-command "<provider command>"
python scripts/master_agent_tool.py session-create --state-dir <state-dir> --agent-id strategy-live --role Strategy --context-packet packets/context-packet.md --provider codex --provider-command "python scripts/file_session_provider.py --state-file <state-dir>/state/provider-sessions.json"
python scripts/master_agent_tool.py session-create --state-dir <state-dir> --agent-id strategy-app --role Strategy --context-packet packets/context-packet.md --provider codex-app
python scripts/master_agent_tool.py session-confirm-create --state-dir <state-dir> --agent-id strategy-app --thread-id <codex-thread-id>
python scripts/master_agent_tool.py session-send --state-dir <state-dir> --agent-id strategy-1 --message "Please return a strategy packet."
python scripts/master_agent_tool.py session-send --state-dir <state-dir> --agent-id strategy-live --message "Please return a strategy packet." --provider-command "<provider command>"
python scripts/master_agent_tool.py session-confirm-send --state-dir <state-dir> --agent-id strategy-app
python scripts/master_agent_tool.py session-read --state-dir <state-dir> --agent-id strategy-1
python scripts/master_agent_tool.py session-read --state-dir <state-dir> --agent-id strategy-live --provider-command "<provider command>"
python scripts/master_agent_tool.py session-confirm-read --state-dir <state-dir> --agent-id strategy-app --summary "Packet returned" --turn-count 2
python scripts/master_agent_tool.py session-archive --state-dir <state-dir> --agent-id strategy-1
python scripts/master_agent_tool.py session-archive --state-dir <state-dir> --agent-id strategy-live --provider-command "<provider command>"
python scripts/master_agent_tool.py session-confirm-archive --state-dir <state-dir> --agent-id strategy-app
python scripts/master_agent_tool.py session-reconcile --state-dir <state-dir>
python scripts/master_agent_tool.py session-reconcile --state-dir <state-dir> --provider-command "<provider command>"
python scripts/master_agent_tool.py worktree-plan --state-dir <state-dir> --worktree-id wt-task-1 --provider codex-app --base-branch main --purpose "Isolated Coding Agent task"
python scripts/master_agent_tool.py validate-worktreeinclude --state-dir <state-dir> --project-root <project-root>
python scripts/master_agent_tool.py worktree-confirm-create --state-dir <state-dir> --worktree-id wt-task-1 --thread-id <codex-thread-id>
python scripts/master_agent_tool.py session-create --state-dir <state-dir> --agent-id coding-wt-1 --role Coding --context-packet packets/context-packet.md --provider codex-app --worktree-id wt-task-1
python scripts/master_agent_tool.py session-confirm-create --state-dir <state-dir> --agent-id coding-wt-1 --thread-id <codex-thread-id> --worktree-id wt-task-1
python scripts/master_agent_tool.py worktree-assign-session --state-dir <state-dir> --worktree-id wt-task-1 --agent-id coding-wt-1
python scripts/master_agent_tool.py worktree-reconcile --state-dir <state-dir>
python scripts/master_agent_tool.py worktree-close --state-dir <state-dir> --worktree-id wt-task-1 --reason "task accepted"
python scripts/master_agent_tool.py worktree-confirm-close --state-dir <state-dir> --worktree-id wt-task-1
python scripts/master_agent_tool.py request-rotation --state-dir <state-dir> --agent-id coding-1 --successor-agent-id coding-2 --reason attention-drift
python scripts/master_agent_tool.py validate-predecessor-state --packet packets/coding-1-predecessor-state-packet.md
python scripts/master_agent_tool.py rotate-session --state-dir <state-dir> --agent-id coding-1 --successor-agent-id coding-2 --reason attention-drift --provider file --predecessor-state-packet packets/coding-1-predecessor-state-packet.md
python scripts/master_agent_tool.py rotate-session --state-dir <state-dir> --agent-id coding-app-1 --successor-agent-id coding-app-2 --reason attention-drift --provider codex-app --predecessor-state-packet packets/coding-app-1-predecessor-state-packet.md
python scripts/master_agent_tool.py enforce-master-boundary --project-root <project-root> --state-dir <state-dir>
python scripts/master_agent_tool.py assess-parallelism --state-dir <state-dir> --work-order packets/work-order-a.md --work-order packets/work-order-b.md --output packets/parallelism-verdict.md
python scripts/master_agent_tool.py governance-lint --packet packets/work-order-a.md --packet-type work-order
python scripts/master_agent_tool.py record-acceptance-gate --state-dir <state-dir> --scope-id TASK-1 --maturity diagnostic --status passed --evidence packets/task-record.md
python scripts/master_agent_tool.py record-authority-required --state-dir <state-dir> --agent-id coding-1 --reason "owner boundary exceeded" --evidence packets/obstacle-recovery-packet.md --required-user-decision "approve wider owner or narrow task"
python scripts/master_agent_tool.py record-incident --state-dir <state-dir> --severity critical --summary "Safety breach" --source supervisor
python scripts/master_agent_tool.py alert-status --state-dir <state-dir>
python scripts/master_agent_tool.py acknowledge-alert --state-dir <state-dir> --alert-id <alert-id> --note "operator reviewed"
python scripts/master_agent_tool.py telemetry-summary --state-dir <state-dir>
python scripts/master_agent_tool.py record-learning-correction --state-dir <state-dir> --project <project> --source <session-or-artifact> --task <task> --agent-behavior <behavior> --user-correction <correction> --evidence <evidence> --failure-mode <mode> --confidence high
python scripts/master_agent_tool.py learning-cycle-start --state-dir <state-dir> --window "last 7 days" --project <project>
python scripts/master_agent_tool.py learning-proposal-lint --proposal packets/learning-proposal.md
python scripts/master_agent_tool.py accept-learning-proposal --state-dir <state-dir> --proposal packets/learning-proposal.md --summary "Accepted correction distillation"
python scripts/master_agent_tool.py record-learning-effectiveness --state-dir <state-dir> --proposal-id <proposal-id> --status recurrence-prevented --evidence <evidence> --next-action "keep rule"
python scripts/master_agent_tool.py learning-summary --state-dir <state-dir>
python scripts/master_agent_tool.py schema-status --state-dir <state-dir>
python scripts/master_agent_tool.py migrate-state --state-dir <state-dir>
python scripts/master_agent_tool.py recover-state --state-dir <state-dir> --from-logs
python scripts/master_agent_tool.py recover-locks --state-dir <state-dir> --stale-seconds 600
python scripts/master_agent_tool.py check-heartbeats --state-dir <state-dir> --stale-minutes 30
python scripts/master_agent_tool.py watch-heartbeats --state-dir <state-dir> --stale-minutes 30 --poll-seconds 60
```

Production hardening checks:

```bash
python scripts/soak_validate.py --quick
python scripts/release_validate.py --quick-validate <quick_validate.py> --installed-skill-dir <installed-master-agent-system>
python scripts/release_validate.py --require-plugin-eval --plugin-eval-command "<plugin-eval command with {skill}>"
```

Govern roles:

```bash
python scripts/master_agent_tool.py list-roles --state-dir <state-dir>
python scripts/master_agent_tool.py define-role --state-dir <state-dir> --role "Domain Research" --purpose "Collect bounded project evidence" --allowed-work "Read docs and artifacts" --forbidden-work "Production implementation" --return-packet role-receipt.md --scope docs/research --token-budget 6000 --max-heartbeats 3 --approval "accepted role-proposal.md" --activate
python scripts/master_agent_tool.py deactivate-role --state-dir <state-dir> --role "Domain Research" --reason "No longer needed"
python scripts/master_agent_tool.py activate-role --state-dir <state-dir> --role "Domain Research" --reason "New evidence pass approved" --approval "accepted role-proposal.md"
python scripts/master_agent_tool.py scaffold-role-skill --state-dir <state-dir> --role "Domain Research" --skills-dir <codex-skills-dir>
```

Track and optimize token budgets:

```bash
python scripts/master_agent_tool.py set-budget --state-dir <state-dir> --project-budget 100000 --warning-percent 80 --hard-percent 100
python scripts/master_agent_tool.py record-usage --state-dir <state-dir> --agent-id strategy-1 --tokens-used 2500 --note "strategy pass"
python scripts/master_agent_tool.py check-budget --state-dir <state-dir>
python scripts/master_agent_tool.py budget-status --state-dir <state-dir>
python scripts/master_agent_tool.py recommend-token-strategy --state-dir <state-dir> --agent-id strategy-1 --expected-tokens 4000 --task-complexity medium
```

The state pack contains:

- `master-ledger.md`
- `project-policy-pack.md`
- `running-agents.md`
- `role-catalog.md`
- `role-proposal.md`
- `authority-envelope.md`
- `master-boundary.md`
- `strategy-sync.md`
- `anomaly-log.md`
- `event-log.md`
- `context-packet.md`
- `heartbeat-packet.md`
- `remediation-packet.md`
- `predecessor-state-packet.md`
- `obstacle-recovery-packet.md`
- `acceptance-gate.md`
- `task-record.md`
- `implementation-guard-adapter.md`
- `runtime-supervisor.md`
- `runtime-status.md`
- `runtime-deployment.md`
- `session-control.md`
- `worktree-control.md`
- `incident-log.md`
- `alert-queue.md`
- `state-schema.md`
- `correction-ledger.md`
- `learning-cycle.md`
- `learning-proposal.md`
- `learning-effectiveness.md`
- `strategy-packet.md`
- `work-order.md`
- `token-strategy.md`
- `coding-receipt.md`
- `review-verdict.md`
- `policy-verdict.md`
- `state/agents.json`
- `state/roles.json`
- `state/heartbeats.jsonl`
- `state/strategy-sync.jsonl`
- `state/anomalies.jsonl`
- `state/budget.json`
- `state/token-usage.jsonl`
- `state/runtime.json`
- `state/session-control.jsonl`
- `state/worktrees.jsonl`
- `state/incidents.jsonl`
- `state/alerts.jsonl`
- `state/learning-corrections.jsonl`
- `state/learning-cycles.jsonl`
- `state/learning-updates.jsonl`
- `state/learning-effectiveness.jsonl`
- `state/governance-events.jsonl`
- `state/acceptance-gates.jsonl`
- `state/schema-version.json`

## Acceptance Rule

Do not update the ledger from raw conversation. Update it only from an accepted packet, and record:

- What changed in project state.
- Which authority or decision justified it.
- Which evidence was reviewed.
- What remains blocked, risky, or unvalidated.
