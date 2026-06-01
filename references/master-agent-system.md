# Master Agent System Reference

## Purpose

The Master Agent System coordinates long-running project work without making one conversational session responsible for all memory, reasoning, implementation, and review.

The system separates four concerns:

1. Project state lives in files.
2. Reasoning happens in short-lived Strategy sessions.
3. Implementation happens in bounded Coding sessions.
4. Evidence checks happen in separate Review or Policy Review sessions.

The Master Agent is the control plane. It routes work, monitors heartbeats, enforces boundaries, accepts or rejects packets, and updates the ledger. It does not implement production changes.

## Design Goals

- Preserve project continuity across session resets and compression.
- Reduce attention loss by keeping long history out of the active conversation.
- Prevent reward hacking by making acceptance depend on evidence and explicit gates.
- Prevent runaway token usage by requiring budgets, usage records, heartbeat caps, escalation thresholds, and token-saving strategy recommendations.
- Keep project-specific policy outside the reusable skill.
- Allow the Master Agent to decide when parallel sub-agents are safe.
- Allow project-specific roles only through a governed role catalog.
- Make drift auditable by comparing actions against context packets.

## Non-Goals

- Do not make the Master Agent an all-purpose Discussion Agent.
- Do not make the Master Agent a production implementation worker.
- Do not encode project-specific architecture into the generic skill.
- Do not use heartbeat text as a substitute for validation evidence.
- Do not accept strategy recommendations automatically.

## System Architecture

```text
authoritative project docs
-> project policy pack
-> master ledger
-> role catalog
-> context packet
-> active role session
-> return packet
-> Master acceptance or rejection
-> ledger and event log update
```

The Master Agent reads the current ledger and project policy pack first. It reads the event log only when it needs history to resolve an ambiguity.

## Persistent Artifacts

| Artifact | Purpose |
| --- | --- |
| `master-ledger.md` | Current project state, not full history. |
| `project-policy-pack.md` | Project adapter: authority docs, boundaries, validation rules, forbidden shortcuts. |
| `running-agents.md` | Current sub-agent registry and heartbeat status. |
| `role-catalog.md` | Active, proposed, and inactive roles governed by the Master Agent. |
| `role-proposal.md` | Proposal template for creating or changing project-specific roles. |
| `strategy-sync.md` | Current accepted strategy plan and Master awareness state. |
| `anomaly-log.md` | Detected loops, plan drift, scope drift, reward-hacking, and token-risk anomalies. |
| `event-log.md` | Append-only accepted events and rejected packets. |
| `context-packet.md` | Task-specific context sent to a sub-agent. |
| `heartbeat-packet.md` | Structured progress update from a running sub-agent. |
| `remediation-packet.md` | Safety-checked context reinforcement, successor handoff, split-task, or stop packet. |
| `strategy-packet.md` | Strategy recommendation and proposed work order. |
| `work-order.md` | Bounded implementation assignment. |
| `token-strategy.md` | Master constraints and sub-agent self-optimization rules for token use. |
| `runtime-supervisor.md` | Continuous supervisor policy, poll cadence, recovery limits, quiet periods, and handoff rules. |
| `runtime-status.md` | Current supervisor state, last checks, active interventions, next wakeup, and operator handoff. |
| `runtime-deployment.md` | Windows startup, process identity, crash recovery, stop/status commands, and production limits. |
| `session-control.md` | Provider-neutral session lifecycle contract and audit trail rules. |
| `incident-log.md` | Open and resolved incident summaries, severity levels, remediation, and operator handoff. |
| `alert-queue.md` | Pending alerts, severity, acknowledgement, suppression, and escalation status. |
| `state-schema.md` | Current schema version, migration order, compatibility policy, recovery policy, and stale lock handling. |
| `coding-receipt.md` | Implementation result and validation evidence. |
| `review-verdict.md` | Independent review result. |
| `policy-verdict.md` | Authority and policy compliance verdict. |
| `state/agents.json` | Machine-readable running-agent registry. |
| `state/roles.json` | Machine-readable role registry used by `register-agent` and validation. |
| `state/heartbeats.jsonl` | Append-only heartbeat history. |
| `state/strategy-sync.jsonl` | Append-only accepted strategy plan history. |
| `state/anomalies.jsonl` | Append-only anomaly detection history. |
| `state/budget.json` | Machine-readable project and per-agent token budget state. |
| `state/token-usage.jsonl` | Append-only token usage records. |
| `state/runtime.json` | Supervisor loop state, recovery counts, breach counts, and next wakeup. |
| `state/session-control.jsonl` | Append-only requested and confirmed provider session events. |
| `state/incidents.jsonl` | Append-only incident records. |
| `state/alerts.jsonl` | Append-only alert-opened and acknowledgement records. |
| `state/schema-version.json` | Current schema version, migration history, and compatible tool version. |

## Operational CLI

Use `scripts/master_agent_tool.py` for the working control surface.

Bootstrap a project:

```bash
python scripts/master_agent_tool.py init --project-root <project-root>
```

Run structural validation:

```bash
python scripts/master_agent_tool.py validate --state-dir <project-root>/docs/master-agent
```

Run readiness validation after filling the ledger and policy pack:

```bash
python scripts/master_agent_tool.py validate --state-dir <project-root>/docs/master-agent --strict
```

Install role skills into a Codex skills directory:

```bash
python scripts/master_agent_tool.py install-system --skills-dir <codex-skills-dir>
python scripts/master_agent_tool.py install-role-skills --skills-dir <codex-skills-dir>
```

Use `install-system` for a full installation of the root skill plus role skills. Use `install-role-skills` only when the root skill is already installed.

Create a packet from a template:

```bash
python scripts/master_agent_tool.py new-packet --state-dir <state-dir> --template work-order
```

Govern roles:

```bash
python scripts/master_agent_tool.py list-roles --state-dir <state-dir>
python scripts/master_agent_tool.py define-role --state-dir <state-dir> --role "Domain Research" --purpose "Collect bounded project evidence" --allowed-work "Read docs and artifacts" --forbidden-work "Production implementation" --return-packet role-receipt.md --scope docs/research --token-budget 6000 --max-heartbeats 3 --approval "accepted role-proposal.md" --activate
python scripts/master_agent_tool.py deactivate-role --state-dir <state-dir> --role "Domain Research" --reason "No longer needed"
python scripts/master_agent_tool.py activate-role --state-dir <state-dir> --role "Domain Research" --reason "New evidence pass approved" --approval "accepted role-proposal.md"
python scripts/master_agent_tool.py scaffold-role-skill --state-dir <state-dir> --role "Domain Research" --skills-dir <codex-skills-dir>
```

`register-agent` rejects undefined, proposed, or inactive roles. `validate` also rejects existing running-agent records that point to undefined or inactive roles.

Synchronize accepted strategy:

```bash
python scripts/master_agent_tool.py accept-strategy --state-dir <state-dir> --packet packets/strategy-packet.md --plan-id PLAN-1 --summary "Approved bounded plan"
python scripts/master_agent_tool.py strategy-sync-status --state-dir <state-dir>
python scripts/master_agent_tool.py require-plan --state-dir <state-dir> --plan-id PLAN-1
```

When a strategy plan is active, `register-agent` must include the current `--plan-id`. This prevents Coding, Review, Policy, or custom role sessions from continuing a stale or superseded plan.

Register and monitor a sub-agent:

```bash
python scripts/master_agent_tool.py register-agent --state-dir <state-dir> --agent-id coding-1 --role Coding --task-id TASK-1 --objective "Implement the approved work order" --scope "app/module" --plan-id PLAN-1
python scripts/master_agent_tool.py heartbeat --state-dir <state-dir> --agent-id coding-1 --state active --current app/module/file.py --last-action "Patched parser" --next-action "Run tests" --scope-status yes --confidence medium
python scripts/master_agent_tool.py audit-agent --state-dir <state-dir> --agent-id coding-1
python scripts/master_agent_tool.py remediate-agent --state-dir <state-dir> --agent-id coding-1 --action reinforce-context
python scripts/master_agent_tool.py supervise --state-dir <state-dir> --poll-seconds 60 --max-cycles 1
python scripts/master_agent_tool.py supervise --state-dir <state-dir> --poll-seconds 60 --run-until-stopped
python scripts/master_agent_tool.py supervisor-start --state-dir <state-dir> --poll-seconds 60 --spawn
python scripts/master_agent_tool.py supervisor-status --state-dir <state-dir>
python scripts/master_agent_tool.py supervisor-stop --state-dir <state-dir>
python scripts/master_agent_tool.py supervisor-recover --state-dir <state-dir>
python scripts/master_agent_tool.py session-create --state-dir <state-dir> --agent-id strategy-1 --role Strategy --context-packet packets/context-packet.md --provider file
python scripts/master_agent_tool.py session-create --state-dir <state-dir> --agent-id strategy-live --role Strategy --context-packet packets/context-packet.md --provider codex --provider-command "<provider command>"
python scripts/master_agent_tool.py session-send --state-dir <state-dir> --agent-id strategy-1 --message "Please return a strategy packet."
python scripts/master_agent_tool.py session-send --state-dir <state-dir> --agent-id strategy-live --message "Please return a strategy packet." --provider-command "<provider command>"
python scripts/master_agent_tool.py session-read --state-dir <state-dir> --agent-id strategy-1
python scripts/master_agent_tool.py session-read --state-dir <state-dir> --agent-id strategy-live --provider-command "<provider command>"
python scripts/master_agent_tool.py session-archive --state-dir <state-dir> --agent-id strategy-1
python scripts/master_agent_tool.py session-archive --state-dir <state-dir> --agent-id strategy-live --provider-command "<provider command>"
python scripts/master_agent_tool.py session-reconcile --state-dir <state-dir>
python scripts/master_agent_tool.py session-reconcile --state-dir <state-dir> --provider-command "<provider command>"
python scripts/master_agent_tool.py record-incident --state-dir <state-dir> --severity critical --summary "Safety breach" --source supervisor
python scripts/master_agent_tool.py alert-status --state-dir <state-dir>
python scripts/master_agent_tool.py acknowledge-alert --state-dir <state-dir> --alert-id <alert-id> --note "operator reviewed"
python scripts/master_agent_tool.py telemetry-summary --state-dir <state-dir>
python scripts/master_agent_tool.py schema-status --state-dir <state-dir>
python scripts/master_agent_tool.py migrate-state --state-dir <state-dir>
python scripts/master_agent_tool.py recover-state --state-dir <state-dir> --from-logs
python scripts/master_agent_tool.py recover-locks --state-dir <state-dir> --stale-seconds 600
python scripts/master_agent_tool.py check-heartbeats --state-dir <state-dir> --stale-minutes 30
python scripts/master_agent_tool.py watch-heartbeats --state-dir <state-dir> --stale-minutes 30 --poll-seconds 60
```

`check-heartbeats` and `watch-heartbeats` exit `1` when any monitored agent is stale. Use that non-zero exit as the Master Agent's escalation trigger.

`supervise` is the durable runtime loop for unattended monitoring. Each cycle validates the state pack, checks stale heartbeats, checks token budget pressure, reads strategy sync, audits monitored agents, applies safety-envelope remediation, and renders `runtime-status.md`. Repeated identical remediation for the same agent stops the agent and creates a Strategy review packet. Critical safety breaches stop the affected agent immediately. Quiet periods defer noncritical work until the next wakeup.

Use `supervisor-start --spawn`, `supervisor-status`, `supervisor-stop`, and `supervisor-recover` as the process lifecycle surface around `supervise`. On Windows, the deployment model is intentionally dependency-light: use Task Scheduler or a service wrapper to launch the command from the project root, write logs to a known path, and use `supervisor-stop` for graceful shutdown rather than killing unrelated processes. `supervisor-status` verifies the recorded PID, supervisor identity nonce, lock file, crash marker, stop request, and heartbeat freshness before reporting the supervisor as running. `supervisor-recover` refuses to clear a live supervisor unless `--force` is supplied; use forced recovery only after independently confirming the recorded process must be overridden.

Use `session-create`, `session-send`, `session-read`, `session-archive`, and `session-reconcile` as a provider-neutral adapter. The file provider is a local mock for testing and offline use. The `codex` provider requires `--provider-command` or `MASTER_AGENT_SESSION_PROVIDER` for every live operation; the command receives a JSON request on stdin and must return JSON with confirmed session state plus provider evidence. Provider commands are parsed as argv, not executed through a shell. Unsupported providers stay in `pending-manual-provider` rather than silently reporting success.

Use `record-incident`, `alert-status`, `acknowledge-alert`, and `telemetry-summary` for production observability. Critical incidents automatically open alerts, and supervisor-detected critical safety breaches or repeated remediation failures create incidents so they cannot pass silently. Acknowledgement appends an audit record instead of deleting the original alert.

Use `schema-status`, `migrate-state`, `recover-state --from-logs`, and `recover-locks` to keep long-lived state packs upgradeable. Recovery quarantines corrupt JSON before replacement, rebuilds derived budget state from append-only usage logs, and removes only recoverable lock files inside the state directory. Normal writes use owner-stamped lock files and atomic replacements; a lock can be reclaimed automatically only when its recorded owner is no longer alive or it exceeds the configured stale threshold with no live owner.

Manage and optimize token budgets:

```bash
python scripts/master_agent_tool.py set-budget --state-dir <state-dir> --project-budget 100000 --warning-percent 80 --hard-percent 100
python scripts/master_agent_tool.py register-agent --state-dir <state-dir> --agent-id strategy-1 --role Strategy --task-id TASK-2 --objective "Resolve boundary" --scope docs/master-agent --token-budget 12000 --max-heartbeats 4
python scripts/master_agent_tool.py record-usage --state-dir <state-dir> --agent-id strategy-1 --tokens-used 2400 --note "initial recommendation"
python scripts/master_agent_tool.py check-budget --state-dir <state-dir>
python scripts/master_agent_tool.py budget-status --state-dir <state-dir>
python scripts/master_agent_tool.py recommend-token-strategy --state-dir <state-dir> --agent-id strategy-1 --expected-tokens 4000 --task-complexity medium
```

`check-budget` exits `1` for warning-level budget pressure and `2` for hard limits. Treat either non-zero exit as a Master Agent stop/escalation condition.

`recommend-token-strategy` exits `0` for `continue`, `1` for `compress-and-narrow`, and `2` for `stop-or-request-budget`. Run it before spawning a sub-agent, before approving a broad continuation, and whenever the next action is expected to consume a material share of the remaining budget.

## Dynamic Role Governance

The default roles are Strategy, Coding, Review, and Policy Review. The Master may create project-specific roles when the current project has a recurring or specialized responsibility that does not fit those defaults.

Do not create a new role just because a task is large. First try to narrow the assignment into a default role. Create a role only when the role has a stable purpose, clear allowed work, forbidden work, return packet, scope, token budget, heartbeat cap, activation approval, and deactivation condition.

Use this gate:

```text
project need -> role-proposal.md -> Master acceptance or user approval -> define-role with bounds -> activate-role --approval -> register-agent
```

Role state has two surfaces:

- `role-catalog.md`: human-readable current catalog.
- `state/roles.json`: machine-readable registry used by CLI validation.

Role lifecycle:

1. Perceive a gap from ledger state, policy constraints, repeated Strategy packets, or user direction.
2. Fill `role-proposal.md`, including why existing roles are insufficient.
3. Define the role with `define-role`; leave it proposed unless approval is already explicit.
4. Activate with `activate-role --approval <evidence>` only after accepted role proposal, Master acceptance, or direct user approval.
5. Register sub-agents with the new role.
6. Deactivate roles that are stale, overlapping, too broad, or no longer needed.

Optional role skills:

- Use `scaffold-role-skill` when the custom role will recur enough to justify a Codex skill.
- Keep the scaffolded skill short and subordinate to the role catalog.
- Do not put project memory into the role skill; project state stays in the ledger and policy pack.
- Installing a role skill does not activate the role. Activation remains controlled by `state/roles.json`.

## Strategy-Master Synchronization

The Master records accepted Strategy packets in `strategy-sync.md` and `state/strategy-sync.jsonl`. Raw discussion with a Strategy Agent does not become the current plan. Only `accept-strategy` makes a plan current.

Use this gate:

```text
strategy packet -> accept-strategy -> strategy-sync.md -> register-agent --plan-id <current>
```

The Master must check strategy sync before issuing implementation or review work. If `strategy-sync-status` reports a stale plan, the Master should request a fresh Strategy packet, ask the user for confirmation, or issue only narrow maintenance work that does not depend on the stale plan.

## Anomaly Detection

Use `audit-agent` after heartbeats, validation, or suspicious progress reports. The audit checks for:

- Repeated identical next actions across three heartbeats.
- Scope status of `no` or `unsure`.
- Plan alignment of `no` or a plan id that differs from the accepted plan.
- Complete status without commands, artifacts, changed files, or concrete evidence.
- Budget pressure combined with broad continuation language.

Detected anomalies are appended to `state/anomalies.jsonl` and rendered in `anomaly-log.md`. An anomaly is not a final verdict; it is a Master control signal to stop, reinforce context, request review, or create a remediation packet.

## Autonomous Remediation

Use `remediate-agent` only after a heartbeat, anomaly, stale-agent result, or budget warning gives a concrete reason to intervene. Each remediation action runs through the safety envelope first.

Supported actions:

- `reinforce-context`: create a narrowed context packet for the same agent.
- `spawn-successor`: create a successor-agent handoff when attention drift or looping is likely.
- `split-task`: create a split-task packet when scope is too large.
- `stop-agent`: mark the agent stopping and require review before continuation.

If safety returns a hard block, no remediation packet is created. If safety returns an internal-review condition, the packet is created but should be reviewed before spawning more work.

## Token Optimization Strategy

Token control has two layers:

1. Master constraints: project budget, per-agent budget, heartbeat cap, session creation cap, context tier, allowed evidence paths, and stop thresholds.
2. Sub-agent autonomous strategies: targeted search, selective file reads, summarized tool output, artifact references instead of pasted evidence, compression requests before large loops, and early stop when the next step no longer fits the budget.

Use `token-strategy.md` as the durable policy for both layers. The Master updates it when budget pressure, project complexity, or sub-agent behavior changes.

Use context tiers to avoid accidental overloading:

| Tier | Typical contents | Use |
| --- | --- | --- |
| Minimal | Current packet, ledger excerpt, exact authority section | Narrow coding, review, or policy check |
| Focused | Minimal plus directly cited files or artifacts | Diagnosis, implementation, evidence review |
| Expanded | Focused plus one compact prior summary | Strategy work that truly depends on history |

The Master should impose a lower tier when budget pressure rises. A sub-agent may request a higher tier, but must name the exact missing evidence and expected token cost.

At warning threshold, compress and narrow before continuing. At hard threshold, stop or request explicit user approval for a new budget. If token usage is unknown, require a usage report before further work.

The strategy is research-informed but project-neutral. Patterns to adapt, not copy, include per-agent usage accounting, token-limited message trimming, relevant-code maps under a budget, dual compression thresholds, autonomous context compression, and budget-aware multi-agent topology selection. Keep external project code, prompts, and templates out of this skill unless the user explicitly approves a separate licensing review.

Primary references for refreshing the principles later:

- AutoGen usage tracking: `https://autogenhub.github.io/autogen/docs/notebooks/agentchat_cost_token_tracking/`
- LangChain token-limited message trimming: `https://reference.langchain.com/v0.3/python/core/messages/langchain_core.messages.utils.trim_messages.html`
- Aider repository map token budget: `https://aider.chat/docs/repomap.html`
- Hermes Agent context compression and caching: `https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/context-compression-and-caching.md`
- Active Context Compression paper: `https://arxiv.org/abs/2601.07190`
- AgentBalance paper: `https://arxiv.org/abs/2512.11426`

## Master Agent Contract

The Master Agent may:

- Maintain ledgers, event logs, policy packs, context packets, and work orders.
- Decide whether to use Strategy, Coding, Review, or Policy Review agents.
- Decide whether multiple sub-agents can run in parallel.
- Define, activate, deactivate, and scaffold project-specific roles through role governance.
- Impose token constraints and context tiers on each sub-agent.
- Require sub-agents to use autonomous token-saving strategies.
- Ask the user for approval when authority is ambiguous.
- Pause or stop a sub-agent when drift is detected.
- Pause or stop a sub-agent when token budget, heartbeat cap, or session cap is exceeded.
- Accept or reject structured return packets.

The Master Agent must not:

- Patch production code.
- Quietly change runtime behavior.
- Turn a strategy recommendation into project state without acceptance.
- Merge a coding receipt without checking required review gates.
- Treat conversation history as authoritative state.
- Continue execution when authority docs and user direction conflict.
- Continue a sub-agent past hard token limits without explicit budget approval.
- Register an agent with an undefined, proposed, or inactive role.

## Strategy Agent Contract

Use a Strategy Agent for solution development, architecture reasoning, diagnosis, tradeoff analysis, postmortems, and proposed work-order drafting.

The Strategy Agent returns a `strategy-packet.md` with:

- The question being answered.
- Authority docs and current plan used.
- Proposed plan id and plan-sync impact.
- Current diagnosis.
- Options considered.
- Recommended decision.
- Rejected alternatives.
- Proposed next work order.
- Affected modules or ownership boundaries.
- Forbidden shortcuts.
- Validation required.
- Token budget, context tier, and compression recommendation.
- Confidence and open risks.

A Strategy Agent recommendation is not project state until the Master accepts it into the ledger.

## Coding Agent Contract

Use a Coding Agent only after the Master issues a work order. The work order must include:

- Objective.
- Allowed files or modules.
- Out-of-scope files or modules.
- Architecture or policy constraints.
- Required validation.
- Token budget and heartbeat cap.
- Context tier and autonomous token-saving requirements.
- Expected artifacts.
- Stop conditions.
- Receipt format.

The Coding Agent returns a `coding-receipt.md`. The receipt must list changed files, commands run, artifacts produced, validation status, quality findings, performance findings if relevant, and remaining risks.
It must also report token usage and budget status.

## Review Agent Contract

Use a Review Agent after implementation or when evidence requires independent scrutiny. The Review Agent checks:

- Whether the diff matches the work order.
- Whether changed files stayed inside scope.
- Whether validation was sufficient.
- Whether artifacts support the claimed result.
- Whether tests, logs, screenshots, outputs, or visual evidence reveal a blocker.
- Whether the receipt is credible.
- Whether token usage stayed within the work order budget.
- Whether the sub-agent followed the assigned token strategy.

The Review Agent returns a `review-verdict.md` with one verdict:

- `pass`
- `pass-with-risks`
- `fail`
- `inconclusive`
- `blocked`

## Policy Review Agent Contract

Use a Policy Review Agent when a task may touch architecture, roadmap, ownership boundaries, validation gates, default behavior, fallback behavior, security, compliance, or release criteria.

The Policy Review Agent returns a `policy-verdict.md` with one verdict:

- `allowed`
- `allowed-with-conditions`
- `needs-user-decision`
- `rejected`
- `blocked`

The Policy Review Agent does not own product direction. It only checks whether a proposal is consistent with the named authority.

## Heartbeat Monitoring

Every running sub-agent must report structured heartbeats. The Master should request a heartbeat:

- At startup acknowledgement.
- Before risky edits or broad exploration.
- After any validation command.
- After producing or inspecting artifacts.
- When blocked.
- At a time interval appropriate to the environment.

Record heartbeats with `scripts/master_agent_tool.py heartbeat`. Check staleness with `scripts/master_agent_tool.py check-heartbeats`. The file-backed registry updates `running-agents.md`, `state/agents.json`, and `state/heartbeats.jsonl`.

Record token usage with `scripts/master_agent_tool.py record-usage`. Check budget pressure with `scripts/master_agent_tool.py check-budget`. Recommend the next token action with `scripts/master_agent_tool.py recommend-token-strategy`.

The heartbeat must include:

- Task id.
- Current objective.
- Current file, module, artifact, or question.
- Last completed action.
- Next planned action.
- Files changed.
- Validation run.
- Artifacts produced.
- Blocker or risk.
- Confidence.
- Whether the agent is still inside scope.

## Heartbeat Failure Patterns

Pause or stop the agent when:

- Heartbeats omit concrete files, artifacts, or commands.
- The next action changes scope without approval.
- The agent repeats a failed patch cycle.
- The agent reports success without required validation.
- The agent omits token usage or exceeds heartbeat cap.
- The agent starts solving a different problem.
- The agent invents a project mode, fallback, or architecture layer.
- The agent cannot identify the owner of the bug or decision.

## Parallel Sub-Agent Rules

The Master Agent may enable parallel sub-agents only when the tasks are independent.

Allow parallelism when:

- Write sets are disjoint.
- Artifacts are written to separate paths.
- Work orders name an exclusive write set, artifact namespace, merge owner, and conflict protocol.
- Work orders name a token budget and heartbeat cap for each sub-agent.
- Validation commands do not mutate shared state in conflicting ways.
- Results can be accepted independently.
- The tasks do not require a shared unresolved architecture decision.

Do not allow parallelism when:

- Two agents might edit the same files.
- One agent needs another agent's result before proceeding.
- Validation output paths collide.
- The work touches a fragile ownership boundary.
- A policy question is unresolved.

## Ledger Update Rules

The ledger records current state only. Do not use it as a diary.

Update the ledger after:

- Strategy packet accepted.
- Work order issued.
- Coding receipt accepted.
- Review verdict accepted.
- Policy verdict accepted.
- User decision changes scope or authority.
- A blocker becomes active or resolved.

Each ledger update should answer:

- What changed?
- Why is it authoritative?
- What evidence supports it?
- What token usage or budget pressure changed?
- What token strategy, context tier, or compression action changed?
- What is the next action?
- What remains unvalidated?

## Event Log Rules

The event log is append-only. Use it for audit history:

- Accepted packets.
- Rejected packets.
- Drift interventions.
- User decisions.
- Validation milestones.
- Blocker transitions.

Keep event entries short. Put detailed evidence in artifacts and link to those artifacts.

## Session Reset Protocol

When starting a new Master Agent session:

1. Read the project policy pack.
2. Read the master ledger.
3. Read `role-catalog.md`.
4. Read `running-agents.md` and close or reconcile stale agents.
5. Read only the latest event-log entries needed to understand the current objective.
6. Confirm the next action.
7. Ask the user only if authority or state is ambiguous.

When starting a new Strategy, Coding, Review, or Policy session, provide a context packet instead of conversation history.

Use the role skills under `role-skills/` for short-lived role sessions:

- `master-strategy-agent`
- `master-coding-agent`
- `master-review-agent`
- `master-policy-review-agent`

For custom roles, provide the context packet plus the role catalog entry. Use a scaffolded role skill only when one exists.

## Handling User and Strategy Conversation

The user may spend most of the project discussion with a Strategy Agent. That is allowed.

The Strategy Agent must periodically emit a strategy packet. The Master accepts only structured decisions, not the raw discussion.

Use this acceptance gate:

```text
Raw discussion -> strategy packet -> Master review -> accepted ledger state
```

If the user and Strategy Agent agree on a direction that changes architecture, validation, or scope, the Master must record the user decision and update the policy pack or ledger before issuing implementation work.

## Work Order Quality Bar

A good work order is narrow enough that a Coding Agent can complete it without guessing. It names:

- Current code path or current artifact path.
- Intended code path or intended artifact state.
- Allowed edit scope.
- Forbidden shortcuts.
- Validation commands.
- Required inspection or evidence.
- Stop conditions.
- Receipt expectations.

Avoid vague work orders such as:

- "finish the phase"
- "generalize this"
- "clean it up"
- "make it robust"
- "fix all remaining issues"

Rewrite vague goals into one bounded task.

## Acceptance and Rejection

Accept a return packet only when:

- It answers the assigned task.
- It stayed inside scope.
- It names evidence.
- It completed required validation or explains why validation was impossible.
- It identifies remaining risk.
- It does not conflict with authority docs or accepted decisions.

Reject or request clarification when:

- The agent changed scope.
- Validation is missing.
- Evidence does not support the claim.
- The agent inserted unauthorized fallback or heuristic behavior.
- The receipt is too vague to audit.
- The work depends on an unapproved policy decision.

## Project Policy Adapter

The generic skill must stay project-neutral. Put project-specific rules in `project-policy-pack.md`.

A policy pack should name:

- Authority docs.
- Active phase or objective.
- Default behavior.
- Module ownership boundaries.
- Validation gates.
- Forbidden shortcuts.
- Performance or quality thresholds.
- Stop-and-ask conditions.
- Project-specific receipt requirements.

For example, one project may have ingestion, processing, review, and publishing boundaries. Another may have frontend, backend, database, and deployment boundaries. The Master Agent should not know either domain directly; it should load the adapter.

## Anti-Patterns

| Anti-pattern | Why it fails | Replacement |
| --- | --- | --- |
| Master as Discussion Agent | Long-context drift returns. | Use short-lived Strategy Agents. |
| Master as Coding Agent | Control plane loses neutrality. | Issue work orders to Coding Agents. |
| Raw chat as state | Compression and attention loss corrupt continuity. | Accept structured packets into ledger. |
| Reviewer as product owner | Review becomes redesign. | Reviewer gives evidence verdict only. |
| Policy agent as permanent memory | Creates another continuity problem. | Use policy packs plus short-lived Policy Review. |
| Parallel by default | Creates conflicting edits and artifacts. | Parallel only after independence check. |
| Untracked custom role | The Master cannot enforce boundaries or validation. | Define and activate the role in `role-catalog.md` and `state/roles.json`. |
| No token budget | Sessions multiply without a stop signal. | Set project and per-agent budgets before spawning. |
| No token strategy | Agents stay under budget by luck or manual supervision. | Assign Master constraints plus sub-agent self-optimization rules. |
| Success from metrics alone | Misses unrecorded failures. | Require the validation evidence named by project policy. |

## Minimal Operating Loop

Use this loop when the project is moving quickly:

1. Master reads ledger and policy pack.
2. Master asks Strategy for a decision packet if solution is unclear.
3. Master accepts or rejects the strategy packet.
4. Master issues one work order.
5. Coding Agent returns receipt.
6. Review Agent returns verdict.
7. Master updates ledger.
8. Master derives the next action.

Do not skip packet acceptance just because everyone agrees in conversation. Agreement becomes durable only when the ledger changes.
