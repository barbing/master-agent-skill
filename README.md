# Master Agent System

A runtime in which a non-implementing **Master Agent** runs a software project for you with a team of **module owners**, then hands you the integrated result to accept or reject.

- **Owners.** Each one is a persistent Codex or Claude Code session that may write only its own module's files.
- **Verifiers.** An independent verifier per module probes the code its owner delivers, and the Master rules on every failure it finds.
- **The host.** Plain Python, not a model. It enforces every scope, runs every check in a sandbox and journals every step in SQLite.
- **Your project is untouched.** You get an integrated copy and a report.

The runtime is the `master_agent_runtime/` package and uses only the Python standard library. It runs on macOS and Windows. This repository also carries an optional manual coordination mode, a file-based Codex skill pack that Codex runs only when you invoke it by name; see [Optional: manual coordination mode](#optional-manual-coordination-mode). The runtime neither uses nor activates it.

## How a project runs

You describe the work in one JSON **handoff**: the project directory and objective, its modules, the files each module owns, and the checks that decide acceptance. In operator mode, the default for `init`:

1. **`init` admits the handoff.** It validates the contract strictly and stores it with its runtime policy. No model is called.
2. **Preflight.** Before any agent session, the host makes three checks:
   - every check's executable exists;
   - every owned path can be created;
   - each check's result on the unchanged project, recorded as a baseline. A check that already passes there is reported, because it may not test the new work.
3. **The Master plans.** One turn expands the requirements into checkable obligations for each module, each quoting the requirement it comes from. The same turn drafts interface contracts for dependencies that have none.
4. **Owners build in parallel.** Each owner works in a fresh copy of the project in which only its module's paths are writable.
   - An owner whose dependency is covered by an interface contract starts at once. Its work is sealed and later rebased onto the provider's accepted code.
5. **Checks accept modules.**
   - **Sealing.** The host seals each submission: file hashes, bindings and command evidence.
   - **Checks.** It reruns the admitted checks and the owner's own tests in the validator sandbox.
   - **Failures** go straight back to the same owner, who keeps its session.
6. **Verifiers probe what was accepted.** A verifier is a readonly session that has never seen the plan.
   - It reads the module's actual code and writes executable probes against the requirements.
   - The Master upholds or dismisses each confirmed failure. Upheld failures go to the owner who must fix them, which is the provider's owner when the defect is in a dependency.
   - Every sealed probe re-runs on later versions.
7. **Integration.**
   - The host composes the accepted modules and runs the integration checks and every sealed probe.
   - The Master reviews the whole project.
   - Your private acceptance checks, which no agent can read, run last.
8. **You decide.** The run ends in `awaiting_acceptance` with the integrated files and a report. `accept` closes the project, and `reject` sends your feedback to the owners who must change.

**Questions during the run.** An owner blocked by a decision outside its module asks the Master instead of guessing.
- The Master answers within the brief.
- When the answer needs another module to change, the Master routes that change to that module's owner.
- It escalates to you only what is yours to decide: scope, acceptance criteria, external effects, spending beyond the envelope, and open product choices.

## How well it works

One historical task was used: make a real runtime CLI safe for inspection and automation, as a store module and a CLI module. Haiku 5.5 filled every role. Every delivered version passed the task's frozen acceptance oracle. Hidden defects were counted with a 13-class probe battery built from earlier blinded reviews.

| Configuration | Delivered | Hidden defects per version | Cost per run | Time per run |
| --- | --- | --- | --- | --- |
| One agent, no runtime | 3 of 3 | 6.0 | $0.67 | 11 min |
| Runtime, modules accepted on their checks alone | 3 of 3 | 3.0 | $0.48 | 16 min |
| Operator mode with probes the Master drafted from its plan | 4 of 7 | 5.25 | $0.69–0.96 | 10–15 min |
| Operator mode with independent verifiers (the current default) | 3 of 3 | 1.67 | $1.77–1.98 | 23–26 min |

**Why the earlier probes failed.** Probes drafted from the plan only re-tested the plan's cases; every hidden defect that survived was a case the plan had missed. That is why verifiers never see the plan.
- **The 4 of 7.** Of the three runs that did not deliver, one hit a bug since fixed, one ran out of tokens, and the evaluation harness stopped the third.
- **What verifiers changed.** In the current default, verifiers found and got fixed the journal-state defects that appeared in every other configuration.
- **Statistics.** The current default beat the plan-drafted probes in every run (exact permutation test, p ≈ 0.03). Its lead over checks-only acceptance is not significant at three runs each (p = 0.25).
- **Cost.** It costs about four times as much.

These are small samples on one task.

Earlier live runs exercised both providers.
- **Codex,** with `gpt-6-luna`. In a kill test, the owner's app-server was killed mid-turn. The runtime checkpointed the owner's files, resumed the same thread and was accepted 37 seconds later without operator action.
- **Claude Code.** A boundary probe confirmed its permission and sandbox denials.

## Requirements

- **macOS or Windows.** On Windows the target is Windows 11; its release validation is in [`references/windows-validation.md`](references/windows-validation.md). Linux shares the macOS code paths but has not been validated.
- **Python 3.12 or later.** Tested with 3.12 and 3.14. On Windows use a python.org install; a Microsoft Store app alias may not start inside the sandbox.
- **The Codex CLI, always,** because every check runs in its sandbox.
  - **Where the runtime looks.** `codex` on `PATH`, then the standard install locations: the ChatGPT and Codex app bundles on macOS; the standalone installer's folder and npm's packaged `codex.exe` on Windows. Pass `--codex PATH` to pin one.
  - **Windows.** The runtime runs `codex.exe` itself, never the npm `codex.cmd` shim, whose `cmd.exe` would re-parse the probe code and permission tables. Codex must use its MXC or elevated sandbox; the unelevated sandbox cannot enforce read restrictions, so the conformance probe refuses it.
  - For Codex actors it must offer the configured model. The runtime never substitutes a model or installs anything.
- **Claude Code 2.1.263 or later, logged in, for Claude actors, on macOS or Linux (including WSL2).** Check with `claude auth status`. It is found on `PATH` or at `~/.local/bin/claude`; pass `--claude PATH` to pin one.
  - **Native Windows.** Claude Code's Bash sandbox does not run there, so the runtime refuses Claude actors before any session. Run the runtime inside WSL2, or route those actors to Codex.
- **A runtime state directory** outside the project. Keep it under your home folder, not in a shared temp tree. On Windows also keep it out of OneDrive-synced folders, and keep its path short unless long paths are enabled.

**Check a machine first.** `doctor` runs every platform mechanism a run depends on against the installed CLIs, without a project or a model call, and leaves its probe evidence under `STATE/doctor/`:

```bash
python3 -m master_agent_runtime --state ~/runtime-state doctor
```

## Quick start

A handoff for a `store` module and a `cli` module that depends on it:

```json
{
  "root": "/absolute/path/to/project",
  "objective": "What the finished project must do, in your own words.",
  "token_budget": 30000000,
  "modules": [
    {
      "key": "store",
      "objective": "What this module must deliver.",
      "paths": ["app/store.py", "tests/owner_store_test.py"],
      "test_paths": ["tests/owner_store_test.py"],
      "validation": [{"argv": ["/absolute/path/to/python3", "{context}/checks/run.py", "{context}", "{candidate}", "store"], "timeout_seconds": 300}],
      "test_validation": [{"argv": ["/absolute/path/to/python3", "{context}/checks/run.py", "{context}", "{candidate}", "owner-store"], "timeout_seconds": 300}]
    },
    {
      "key": "cli",
      "objective": "What this module must deliver.",
      "paths": ["app/cli.py"],
      "dependencies": ["store"],
      "validation": [{"argv": ["/absolute/path/to/python3", "{context}/checks/run.py", "{context}", "{candidate}", "cli"], "timeout_seconds": 300}]
    }
  ],
  "integration_validation": [{"argv": ["/absolute/path/to/python3", "{context}/checks/run.py", "{context}", "{candidate}", "integration"], "timeout_seconds": 300}],
  "runtime_policy": {
    "provider": "claude",
    "master": {"provider": "claude", "model": "claude-haiku-5-5"},
    "modules": {"store": {"provider": "claude", "model": "claude-haiku-5-5"},
                "cli": {"provider": "claude", "model": "claude-haiku-5-5"}}
  }
}
```

**How the checks see the work.**
- For a module check, `{candidate}` holds only that module's sealed files, and `{context}` is a readonly copy of the project with the accepted dependencies in place. A small runner, like the `checks/run.py` above, overlays the candidate on a private copy of the context and runs the tests.
- For integration, `{candidate}` is the complete integrated project.
- Use absolute interpreter paths.

Then, from the repository root (the package is not installed anywhere):

```bash
python3 -m master_agent_runtime --state ~/runtime-state init handoff.json
python3 -m master_agent_runtime --state ~/runtime-state run PROJECT_ID
python3 -m master_agent_runtime --state ~/runtime-state health PROJECT_ID
python3 -m master_agent_runtime --state ~/runtime-state accept PROJECT_ID --authorization 'Checked the report and files'
```

- **`init`** prints the project id.
- **`run`** works until the project needs you or ends, and prints the outcome as JSON.
  - It can end in `awaiting_acceptance`, `awaiting_user` (a question waits for you), `budget_exhausted`, `integration_failed`, `cancelling` or `cancelled`, or `blocked_or_reconciliation_required`.
  - Each outcome comes with a remedy.
  - Run it again after acting; owners resume their own sessions.
- **`health`** always names the next command to run.

**On Windows**, run the same commands from PowerShell with `py -m master_agent_runtime --state $HOME\runtime-state ...`, and give the handoff Windows paths (`"root": "C:\\work\\project"`). Save the handoff as UTF-8 without a byte-order mark; Windows PowerShell 5.1's `Set-Content -Encoding utf8` adds one, which JSON does not allow. The report prints its accept and reject commands quoted for PowerShell.

## The handoff

**Project fields**

| Field | Default | Meaning |
| --- | --- | --- |
| `root` | required | Absolute path of an existing project directory. It is copied and never edited. |
| `objective` | required | What the project must achieve. Plans, reviews and verifiers judge against it. |
| `modules` | required | One or more modules, described below. |
| `token_budget` | required | Soft token envelope for the whole project, cached input included. |
| `integration_validation` | required | Checks on the integrated project. |
| `max_parallel` | 2 | Owners at work at once (1–16). |
| `turn_timeout_seconds` | 180 | Default deadline of one native turn (1–3600). |
| `max_revision_rounds` | 2 | Automatic code revisions per module before it blocks (0–10). |
| `max_evidence_rounds` | 2 | Evidence recoveries per module (0–10). |
| `model`, `effort` | none | Defaults for Codex actors. |
| `interfaces` | none | Contracts between modules: `name`, `owner`, `consumers` and the `contract` text. |
| `acceptance_directory`, `acceptance_checks` | none | Your private checks, described below. |
| `runtime_policy` | operator mode | How the runtime operates. It is kept beside the contract, so changing it never changes the contract's identity. |

**Module fields**

| Field | Default | Meaning |
| --- | --- | --- |
| `key` | required | Letters, digits, hyphens and underscores. |
| `objective` | required | What this module must deliver. |
| `paths` | required | The files and directories the owner may write. They are normalized, relative and unique, and may not overlap another module's. |
| `dependencies` | none | Modules whose accepted code this one builds on. The graph must be acyclic. |
| `validation` | none | Checks that decide the module's acceptance. |
| `test_paths`, `test_validation` | none | The owner's test files, inside `paths`, and the commands that rerun them. Give both or neither. |
| `token_reservation` | 20000 | Tokens reserved for each turn of this owner. |

**Checks and placeholders.** A check is a structured `argv`, never a shell string, with an optional `timeout_seconds` (default 60, at most 600).
- **Placeholders.** `{candidate}` and `{context}` expand as described in the quick start.
- **The sandbox.** Each check runs in the Codex sandbox and can read only those paths. Its working directory and `TMPDIR` are private, it gets a minimal environment, and it has no network.

**Interface contracts.** Owners see the contracts they provide and consume, and reviews cite them. Two kinds of module can be built in parallel against a contract: a consumer that does not depend on the provider, and, under `parallel_dependents: contract`, one that does. Integration checks prove the composition.

**Private acceptance checks.**
- **Where they live.** `acceptance_directory` must be outside the project. Each entry in `acceptance_checks` has a `name`, an `argv`, an optional `timeout_seconds` and a `description`. `{acceptance}` expands to a sealed copy of the directory.
- **Who sees them.** No agent can read the checks, their logs or the report.
- **When they run.** After the Master approves the integration, with results going only to you. Any failure stops the project for your decision, whatever `final_acceptance` says.

## Runtime policy

`init` applies the operator-mode defaults below wherever the handoff's `runtime_policy` is silent. A handoff's own values win. The library defaults apply when the policy is built directly through the Python API.

| Field | Library default | `init` default | Effect |
| --- | --- | --- | --- |
| `provider`, `master`, `modules` | `codex`, none, none | from the handoff | Provider (`codex` or `claude`), model and effort for the Master and per module. Verifiers use the Master's. |
| `admission_review` | true | false | A Master turn reviews the admission before any work. |
| `preflight` | false | true | The host-only preflight described above. |
| `planning`, `max_obligations` | false, 30 | true, 30 | The Master's planning turn and its obligation limit. |
| `parallel_dependents` | `off` | `contract` | Start dependents early when a contract covers the dependency. |
| `module_review`, `review_modules` | `always`, none | `checks_only`, none | `checks_only` accepts a module on its checks without a Master review turn. Modules listed in `review_modules` keep the review. |
| `structured_review` | true | true | Master rejections cite an admitted obligation with evidence. Advisories never block. |
| `fail_fast_failed_checks` | false | false | Failed checks go back to the owner without a Master turn. Under `checks_only` they always do. |
| `master_probes`, `max_probes` | false, 8 | true, 8 | Sealed executable probes, and the probes allowed per turn. |
| `probe_author` | `master` | `verifier` | `verifier`: an independent readonly session per module writes the probes, and the Master rules on each confirmed failure. `master`: the Master writes them, and they skip the ruling. |
| `probe_timing`, `max_probe_rounds` | `accepted`, 1 | `accepted`, 1 | Probe turns per module after acceptance. `draft`, for the Master as author only, writes black-box probes while the owner works, and they gate every submission. |
| `final_acceptance` | `master` | `user` | `user` stops a passing integration for your acceptance. |
| `max_decisions` | 3 | 3 | Owner questions per module the Master answers before further questions go to you. |
| `master_token_budget` | none | none | Master tokens after which reviews fall back to the checks, questions go to you, and you accept the integration. |
| `owner_turn_timeout_seconds`, `master_turn_timeout_seconds` | the contract's | the contract's | Turn deadlines for owners, and for the Master and verifiers. |
| `interrupt_grace_seconds` | 60 | 60 | Wait after an interrupt before the owned process is closed. |
| `max_continuations` | 2 | 2 | Continuations of a stopped owner turn before the module blocks. |
| `max_transient_retries`, `transient_backoff_seconds` | 3, 30 | 3, 30 | Bounded retries, with exponential backoff, for refused dispatches and unavailable turns. |
| `prices` | none | none | USD per million tokens per model (`input`, `cached_input`, `output`, optional `cache_write`), for cost estimates. |

**Lighter or heavier verification.**
- **The fastest and cheapest configuration measured:** `{"planning": false, "parallel_dependents": "off", "master_probes": false}`. That is checks-only acceptance with no plan and no probes, at 3.0 defects for $0.48 in the evaluation above.
- **Verification without verifier sessions:** keep probes but set `"probe_author": "master"`.

## Commands

Every command takes `--state DIR` before the command name, and prints JSON.

| Command | What it does |
| --- | --- |
| `init HANDOFF` | Validate and admit a handoff, and print its project id. No model calls. |
| `doctor [--codex PATH] [--claude PATH]` | Check this machine: Python, the project lease, owned process groups, the Codex CLI, its app-server and sandbox contract, the write and read conformance probes, and one sandboxed check. No project or model calls; exits 0 when ready. |
| `run PROJECT [--codex PATH] [--claude PATH]` | Run until the project needs you or ends. It exits 0 for `accepted` or `awaiting_acceptance` and 1 otherwise. Running an accepted project again returns the original delivery without native work. |
| `status PROJECT` | The recorded state: modules, operations, waiting questions, usage by role (`master`, `owners`, `verifiers`) and the cost estimate. |
| `health PROJECT` | Whether it is safe to run, what needs reconciling, and the next action, with the command to use. |
| `stop PROJECT` | Request a stop. Running turns are interrupted, and settlement waits for each owned process to exit. A project with no operations settles at once. |
| `reconcile PROJECT [--codex PATH] [--claude PATH]` | Settle operations whose outcome is uncertain from recorded native history, with no new model work. |
| `answer PROJECT --module KEY --text TEXT` | Answer an owner's question that waits for you. |
| `accept PROJECT` | Accept the integrated project. |
| `reject PROJECT --feedback TEXT [--module KEY ...]` | Send the work back. With `--module`, it goes to those modules and their dependents; without it, the Master maps the feedback to modules on the next run. |
| `rearm PROJECT --module KEY [--rounds N]` | Give a blocked module more attempts with the same owner and context. |
| `resolve PROJECT --operation ID [--outcome failed\|cancelled] [--rearm]` | Close an operation nobody can confirm, without inferring a result. |
| `admission PROJECT --decision rerun\|override` | Rerun or override a rejected Master admission. |
| `budget PROJECT --tokens N` | Change the token envelope. Incurred usage is never refunded. |
| `policy PROJECT --set JSON` | Revise the runtime policy. A bound actor's provider cannot change. |

**Operator commands.** That is the last eight in the table. They need `--authorization 'reason'`, which is journaled, and they are refused while a controller holds the project.

**Failures.** A failing command prints one JSON object with `error`, `detail` and `scope` to stderr and exits 1.

## What the runtime enforces

- **Write scope.** Every owner activation gets a fresh workspace. Only the module's admitted paths and a private `.tmp` are writable; the rest of the project, the oracle files and dependency code stay readonly.
  - **Sealing.** The host checks the workspace before sealing. Changes outside the grant, or to readonly context, are refused.
  - **Protected names.** Paths containing `.git`, `.gitignore`, `.codex`, `.agents`, `.aws`, `.claude` or `CLAUDE.md`, in any letter case, can never be admitted. On Windows, names the filesystem treats specially, such as drive-relative `D:x`, alternate streams `a:b` and device names like `nul`, are refused too.
- **Readonly roles.** The Master and the verifiers cannot write project files; each writes only scratch in its own workspace.
- **No external effects.** Git staging and commits, policy changes, publication, deployment, installs and credential changes cannot be admitted. Actors' commands and all checks run without network access.
- **Private storage.** Your acceptance checks, the sealed probes and the reports sit where no actor profile can read them. An owner gets a failing probe's behaviour and expected and observed results, never its code.
- **Your project is never edited.** Owners work on copies. The result is a separate integrated directory, named in `run`'s output and in the report.
- **Independent evidence.** Acceptance depends on checks the host reruns against the sealed files, never on what an owner reports.
  - **What a submission records:** file hashes, the dependency versions it was built on, its mandate revision and its native operation.
  - **The Master's view:** an index of the evidence.
- **One controller per project.** A lease refuses a second `run` while one is active.
- **Conformance before trust.** Before each actor binding and each check, a probe in the same sandbox and profile must write its own workspace (when writable), fail to write outside it, and fail to read two sentinels the host plants: one in a fresh shared temp folder, and one inside the control state, or beside a check's scratch. Any other outcome refuses the run with the evidence.
- **Owned processes.** Each actor process starts in its own process group, a job object on Windows, and the host ends every member of it when the actor closes.

**Codex actors** run through `codex app-server`, under least-privilege permission profiles that the host checks before any turn. Each activation resumes the same thread.

**Claude Code actors** run one non-interactive `claude -p` process per turn, on a host-chosen session id that later turns resume. They need macOS or Linux.
- **Tools.** The flags give an explicit tool list with no subagents, web access or MCP servers. Permission prompts are refused rather than asked.
- **Edits.** File edits are allowed only on the admitted paths and `.tmp`.
- **Bash** always runs in Claude Code's OS sandbox with the network off, and writes only where edits may. The kernel denies reads of the home folder, credential folders and the control state.
- **Environment.** Each process starts from a minimal environment, so a launching session's identity, proxies and API routing never reach it. `MASTER_AGENT_CLAUDE_ENV` names extra variables to forward, such as API-key authentication.
- **Temp folders.** Unlike the Codex profile, world-readable files in the shared temp folders stay readable, which is why runtime state belongs under your home folder.
- **Structured output.** Fields the host treats as optional stay optional for Claude actors, because Claude Code retries the whole turn when a required field is missing.

## Reliability and recovery

- **Stopped turns continue.** A deadline, budget stop, native failure, lost transport or missing receipt interrupts a turn.
  - **Recovery.** Once the owned process has exited, the host checkpoints the owner's files and sends a continuation to the same owner and context.
  - **Limits.** A module blocks only after `max_continuations`, and a budget stop does not use one up.
  - **Uncertain outcomes.** Without an observed exit, the operation is held for `reconcile` rather than retried blind.
- **Bounded rework.** Implementation revisions (`max_revision_rounds`) and evidence recoveries (`max_evidence_rounds`) are counted separately. A blocked module stops only its dependents, and `rearm` adds attempts.
- **Admission is never implied.** No module work starts before admission: a Master admission turn, or, in operator mode, a host approval once the preflight passes. A rejected admission cannot be bypassed by restarting.
- **Scheduling is event-driven.**
  - **Lanes.** The Master's turns form one lane, and host reviews, rebases and each verifier run in their own.
  - **Owners** fill `max_parallel` slots, starting with the modules that others depend on most.
- **Budget.** Each turn reserves tokens against the envelope. When the envelope is spent, turns stop and `run` ends in `budget_exhausted`; raise it with `budget`, and interrupted owners resume from their checkpoints.
- **Accounting.** The runtime records each provider's token breakdown: input, cached input, cache writes, output and reasoning.
  - **Cost.** It estimates cost from the provider or the `prices` table, and counts unpriced tokens separately.
  - **Actors.** Each actor records its CLI version and effective configuration.

## State directory

| Path under `--state` | Contents |
| --- | --- |
| `runtime.sqlite` | The journal: projects, actors, operations, candidates, messages and events. |
| `projects/<id>/public/` | What actors may read: the project base, sealed candidates, checkpoints, check evidence and the integrated directories. |
| `projects/<id>/private/` | Sealed acceptance checks, probes and probe results; no actor can read it. |
| `projects/<id>/reports/` | Your integration reports. |
| `projects/<id>/native-traces/` | Per-turn traces, including Claude Code's streams. |
| `projects/<id>/workspaces/` | Owner workspaces, one per activation, and the Master's and verifiers' scratch. |
| `doctor/<run>/` | Each `doctor` run's probe evidence and sandboxed-check log. |

## Testing and the release gate

```bash
python3 -m unittest discover -s tests
python3 -m unittest discover -s pilots -p 'owned_*_test.py'
python3 scripts/release_validate.py --quick-validate <path-to-skill-creator>/scripts/quick_validate.py
```

**`tests/`** holds 423 tests: 278 for the runtime and 145 for the skill pack's tool. `test_runtime_windows_compat.py` covers the Windows port in three layers: static guards that keep POSIX-only calls in `oscompat.py` and make every text read, write and pipe name its encoding; the Windows branches' logic, simulated on any OS; and native Windows behaviour, which runs only there.

**`pilots/`** keeps the frozen files of two completed runtime pilots byte for byte. The second command reruns the tests their module owners wrote, 20 executions; see `pilots/README.md`.

**The release gate** runs 19 steps:
- the unit suite;
- `py_compile`;
- the template state pack;
- a quick soak;
- the skill pack's control-surface checks;
- a fresh bootstrap and its validation;
- Codex's `quick_validate` on the root skill and each role skill;
- personal-path and secret scans;
- `plugin-eval`, which is skipped when unavailable unless `--require-plugin-eval` is given.

`quick_validate` needs PyYAML in the validating interpreter. `--installed-skill-dir` compares an installed copy of the skill pack with this source.

**Gate notes.**
- **Versions.** It passes on macOS with Python 3.12 and 3.14. The Windows run, and what to report from it, is in [`references/windows-validation.md`](references/windows-validation.md).
- **Temp folder.** The tests in `test_runtime_sandbox.py` create a directory in the shared temp root (`/private/tmp` on macOS), so they pass only where that is writable.
- **Not inside an agent sandbox.** Run the suite, the gate and `doctor` from an ordinary terminal. Inside Codex's or Claude Code's own sandbox, writes to Git metadata and shared temp folders are refused, and a nested sandbox cannot start.

**CI.** `assets/examples/release-validate.yml` is a GitHub Actions example. It runs the gate and the owner suite on Python 3.12 and 3.14. Copy it to `.github/workflows/` to activate it; this repository keeps `.github/` excluded.

## Repository layout

```text
.
├── master_agent_runtime/   the runtime
│   ├── __main__.py         command line
│   ├── contracts.py        handoff, module and policy contracts
│   ├── controller.py       scheduling, owner turns, plan, probes, verifiers, rulings, integration
│   ├── store.py            transactional SQLite journal and state transitions
│   ├── native.py           Codex app-server actors
│   ├── claude_native.py    Claude Code actors
│   ├── validation.py       sandboxed checks
│   ├── recovery.py         reconcile
│   ├── oscompat.py         macOS, Linux and Windows differences: lease, process groups, environments, links, CLIs
│   ├── doctor.py           machine readiness checks
│   └── …                   admission, artifacts, evidence, health, permissions, sandbox
├── tests/                  regression suites for the runtime and the skill pack's tool
├── pilots/                 frozen artifacts of two completed runtime pilots
├── SKILL.md                manual coordination mode: main skill
├── role-skills/            manual coordination mode: Strategy, Coding, Review, Policy Review and Learning Distiller roles
├── scripts/                manual coordination mode's tool, state pack, providers and soak; the release gate
├── assets/                 state-pack templates and CI / pre-commit examples
├── references/             the manual mode's reference and provider-command contract; the Windows validation plan
└── agents/openai.yaml      skill metadata (explicit invocation only)
```

## Optional: manual coordination mode

The repository also carries the system's earlier, file-based form: a Codex skill pack for coordinating agent sessions by hand. Its Master is again a non-implementing control plane, but project continuity lives in a **state pack** of ledgers, packets, receipts and verdicts, and agents are short-lived Codex sessions that you or a provider command launch. It is optional, and hidden by default: every skill in it sets `allow_implicit_invocation: false`, so Codex runs it only when you invoke it by name, as `$master-agent-system` or `$master-coding-agent`.

- **Install.** Copy this folder to `~/.codex/skills/master-agent-system` (on Windows, `$HOME\.codex\skills\master-agent-system`), and each folder under `role-skills/` beside it to invoke the roles directly; restart Codex.
- **Start.** `python scripts/master_agent_tool.py init --project-root <project-root>` creates the state pack at `<project-root>/docs/master-agent`.
- **Read first.** `SKILL.md` gives the load order and core rules, and `references/master-agent-system.md` is the full manual; `python scripts/master_agent_tool.py --help` lists every command.
- **Round-log evidence.** With the Change Round Logger plugin, the tool binds the logger's records (`.codex-round-log/snapshots/<id>/`) to agent receipts and refuses incomplete recordings as evidence.

## License

MIT; see `LICENCE`.
