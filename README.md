# Master Agent System

Project-neutral Codex skill pack for coordinating long-running, multi-agent project work through a non-implementing Master Agent control plane.

The system is designed to keep project continuity outside chat history by using structured ledgers, packets, heartbeats, role contracts, token budgets, runtime supervision, and review gates.

## What This Provides

- A main `master-agent-system` Codex skill.
- Role skills for Strategy, Coding, Review, and Policy Review agents.
- State-pack templates under `assets/templates/`.
- CLI helpers under `scripts/` for bootstrapping, validation, Strategy-packet gating, session control, Worktree control, strict session rotation, role governance, heartbeat monitoring, token tracking, supervisor lifecycle, Master-boundary enforcement, parallelism assessment, soak validation, release validation, and recovery.
- A provider-command reference adapter at `scripts/file_session_provider.py`.
- CI and pre-commit examples for release and Master-boundary enforcement.
- Regression tests for safety-critical behavior.
- A detailed reference manual at `references/master-agent-system.md`.

## Core Model

The Master Agent is a control-plane role. It may update project ledgers, packets, plans, state documents, and policy packs, but it must not edit production code.

Implementation work is delegated to bounded sub-agents through explicit work orders. Strategy, coding, review, policy, and custom-role agents return structured packets that the Master Agent accepts, rejects, or escalates.

Conversation is not treated as durable state. Accepted decisions and current project status must be written into the project state pack.

## Repository Layout

```text
.
├── SKILL.md
├── agents/
│   └── openai.yaml
├── assets/
│   └── templates/
├── references/
│   ├── master-agent-system.md
│   └── provider-command-adapter.md
├── role-skills/
│   ├── master-coding-agent/
│   ├── master-policy-review-agent/
│   ├── master-review-agent/
│   └── master-strategy-agent/
├── scripts/
│   ├── bootstrap_project_state.py
│   ├── file_session_provider.py
│   ├── master_agent_tool.py
│   ├── release_validate.py
│   ├── soak_validate.py
│   ├── state_io.py
│   └── validate_state_pack.py
└── tests/
```

## Install For Codex

Copy this folder into your Codex skills directory:

```bash
~/.codex/skills/master-agent-system
```

On Windows, the equivalent default location is:

```powershell
$HOME\.codex\skills\master-agent-system
```

The role skills can also be installed separately if you want them to trigger directly:

```text
role-skills/master-strategy-agent
role-skills/master-coding-agent
role-skills/master-review-agent
role-skills/master-policy-review-agent
```

Restart Codex after installing or updating skills.

## Quick Start

From the installed skill folder or repository root:

```bash
python scripts/master_agent_tool.py init --project-root <project-root>
python scripts/master_agent_tool.py validate --state-dir <project-root>/docs/master-agent --strict
```

This creates a project state pack at:

```text
<project-root>/docs/master-agent
```

Read `references/master-agent-system.md` before deploying the workflow on a real project.

## Runtime Notes

- The local file provider is for offline testing and state simulation.
- `provider=codex` is the provider-command automation path. It requires `--provider-command` or `MASTER_AGENT_SESSION_PROVIDER`.
- `scripts/file_session_provider.py` is a reusable reference implementation of the provider-command JSON contract.
- `provider=codex-app` is the Codex desktop tool-mediated path. Use Codex thread tools for create/send/read/archive, then record evidence with `session-confirm-create`, `session-confirm-send`, `session-confirm-read`, and `session-confirm-archive`.
- Worktree control is also evidence-based. Use `worktree-plan`, `worktree-confirm-create`, `worktree-assign-session`, `worktree-reconcile`, `worktree-close`, and `worktree-confirm-close` so sub-agents work in isolated Worktrees instead of mutating the user's foreground checkout or remote branches.
- Run `validate-worktreeinclude` before relying on ignored local files in Codex-managed Worktrees. `.worktreeinclude` must list only intentionally ignored local setup files; tracked files, broad patterns, repository escapes, and symlinks are release blockers.
- `accept-strategy` validates Strategy packets before they become current plan state. Coding, Review, and Policy Review registration/session launch require the current plan to carry that validation evidence.
- Use `strategy-packet-lint` for preflight diagnostics and `require-strategy-packet-before-work` as an explicit gate before issuing sub-agent work from an accepted Strategy packet.
- Use `request-rotation`, `validate-predecessor-state`, and `rotate-session` to replace overloaded agents. Normal rotation requires a validated predecessor-state packet; emergency rotation requires `--emergency-without-predecessor-state`.
- Use `enforce-master-boundary` before completing Master-led work, and `assess-parallelism` before running multiple sub-agents.
- Long-running supervision should be launched through the operating system scheduler or service wrapper appropriate for the deployment environment.
- Custom roles must have explicit approval evidence, scope, positive token budget, heartbeat cap, and deactivation conditions before activation.

## Validation

Run the core checks from the repository root:

```bash
python -m unittest discover -s tests -v
python -m py_compile scripts/bootstrap_project_state.py scripts/file_session_provider.py scripts/validate_state_pack.py scripts/master_agent_tool.py scripts/state_io.py scripts/release_validate.py scripts/soak_validate.py tests/test_master_agent_system.py
python scripts/validate_state_pack.py assets/templates
python scripts/soak_validate.py --quick
python scripts/master_agent_tool.py validate-worktreeinclude --state-dir <project-root>/docs/master-agent --project-root <project-root>
```

Validate the skill metadata with Codex's skill validator:

```bash
python <path-to-skill-creator>/scripts/quick_validate.py <path-to-this-skill>
```

Run the release gate:

```bash
python scripts/release_validate.py --quick-validate <path-to-skill-creator>/scripts/quick_validate.py --installed-skill-dir <installed-master-agent-system>
```

`release_validate.py` runs the quick soak by default. It skips `plugin-eval` when unavailable, or treats it as a release blocker when `--require-plugin-eval` is supplied.

The tests are not required for minimal runtime installation, but they are recommended for source releases because they protect the safety-critical behavior of the skill pack.

## License

This project is released under the MIT License. See `LICENCE`.
