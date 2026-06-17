#!/usr/bin/env python3
"""Run a compact long-running-runtime soak for the Master Agent System."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "master_agent_tool.py"
PROVIDER = ROOT / "scripts" / "file_session_provider.py"


class StepFailure(RuntimeError):
    pass


def run_step(name: str, command: list[object], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    args = [sys.executable, *map(str, command)]
    result = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        detail = "\n".join(
            part
            for part in [
                f"step failed: {name}",
                f"command: {' '.join(args)}",
                "stdout:",
                result.stdout.strip(),
                "stderr:",
                result.stderr.strip(),
            ]
            if part
        )
        raise StepFailure(detail)
    print(f"PASS {name}")
    return result


def write_strategy_packet(path: Path, plan_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# Strategy Packet",
                "",
                "## Question",
                "",
                "- Strategy session id: strategy-soak-1",
                "- Question being answered: Can the runtime control loop proceed safely?",
                "- User decision requested: no",
                "",
                "## Authority",
                "",
                "- Authority docs consulted: project-policy-pack.md",
                "- Master ledger state used: bootstrap state",
                "- Policy pack sections used: default safety envelope",
                "",
                "## Plan Sync",
                "",
                f"- Proposed plan id: {plan_id}",
                "- Current accepted plan id: none",
                "- Plan version change: yes",
                "- Master ledger update required: strategy-sync.md",
                "- Resync trigger: initial runtime soak",
                "",
                "## Diagnosis",
                "",
                "- Current diagnosis: control plane is ready for bounded runtime exercise",
                "- Current code path or process path: state-pack commands",
                "- Intended code path or process path: strategy gate before work",
                "- First failing boundary: none observed",
                "",
                "## Options Considered",
                "",
                "| Option | Benefit | Cost | Risk | Decision |",
                "| --- | --- | --- | --- | --- |",
                "| bounded soak | verifies runtime controls | low | synthetic provider | accept |",
                "",
                "## Recommendation",
                "",
                "- Recommended decision: run bounded operating-system soak",
                "- Reason: verifies gates before release",
                "- Rejected alternatives: documentation-only claim",
                "- Confidence: high",
                "",
                "## Proposed Work Order",
                "",
                "- Proposed objective: run provider, heartbeat, boundary, rotation, and parallelism controls",
                "- Allowed scope: docs/master-agent and temporary provider state",
                "- Worktree mode: codex-app",
                "- Worktree id: wt-soak-app",
                "- Base branch: main",
                "- Local mutation policy: do not mutate local checkout",
                "- Remote mutation policy: do not push or create PR without release gate",
                "- Forbidden changes: production implementation",
                "- Validation required: command exits and release gate",
                "- Expected artifacts: runtime-status.md and session-control.jsonl",
                "- Stop conditions: any nonzero control-plane command",
                "",
                "## Open Risks",
                "",
                "- synthetic provider cannot prove external model quality",
                "",
                "## Token Impact",
                "",
                "- Estimated next-session token cost: 2000",
                "- Recommended sub-agent count: 1",
                "- Recommended heartbeat cap: 5",
                "- Recommended context tier: minimal",
                "- Recommended Master constraints: require strategy packet before work",
                "- Recommended sub-agent autonomous strategy: cite artifact paths instead of transcripts",
                "- Compression or narrowing trigger: repeated next action or stale heartbeat",
                "- Token risks: low in quick soak",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_predecessor_state_packet(path: Path, plan_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# Predecessor State Packet",
                "",
                "## Objective",
                "",
                "- Complete bounded coding soak task",
                "",
                "## Plan Id",
                "",
                f"- {plan_id}",
                "",
                "## Completed Work",
                "",
                "- Created initial session and emitted heartbeats",
                "",
                "## Changed Files And Artifacts",
                "",
                "- docs/master-agent/state/session-control.jsonl",
                "",
                "## Validation Evidence",
                "",
                "- strategy gate and provider-command flow passed",
                "",
                "## Known Failures",
                "",
                "- none",
                "",
                "## Risks",
                "",
                "- synthetic provider cannot prove external model quality",
                "",
                "## Next Safe Step",
                "",
                "- launch successor with compact inherited context",
                "",
                "## Forbidden Repeats",
                "",
                "- continue after repeated next-action loop without rotation",
                "",
                "## Token Usage",
                "",
                "- 1200 / 4000",
                "",
                "## Open Questions",
                "",
                "- none",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_work_order(path: Path, write_set: str, artifact_namespace: str, worktree_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# Work Order",
                "",
                "## Objective",
                "",
                "- Task id: SOAK",
                "- Coding Agent objective: bounded runtime soak task",
                "",
                "## Allowed Scope",
                "",
                f"- Files/modules/artifacts allowed: {write_set}",
                "",
                "## Parallel Safety",
                "",
                f"- Exclusive Write Set: {write_set}",
                f"- Artifact Namespace: {artifact_namespace}",
                "- Worktree Mode: codex-app",
                f"- Worktree Id: {worktree_id}",
                "- Base Branch: main",
                "- Local Mutation Policy: do not mutate local checkout",
                "- Remote Mutation Policy: do not push or create PR without release gate",
                "- Merge Owner: Master Agent",
                "- Conflict Protocol: stop and return to Master",
                "",
                "## Token Budget",
                "",
                "- Token budget: 3000",
                "- Maximum heartbeats: 3",
                "",
                "## Forbidden Changes",
                "",
                "- production implementation",
                "",
                "## Required Validation",
                "",
                "- Commands: control-plane command exits",
                "",
                "## Receipt Requirements",
                "",
                "- return compact receipt",
                "",
            ]
        ),
        encoding="utf-8",
    )


def run_soak(cycles: int, quick: bool) -> None:
    plan_id = "SOAK-PLAN-1"
    with tempfile.TemporaryDirectory(prefix="master-agent-soak-") as tmp:
        project = Path(tmp)
        state_dir = project / "docs" / "master-agent"
        packets = state_dir / "packets"
        provider_state = state_dir / "state" / "provider-sessions.json"
        provider_command = f"{sys.executable} {PROVIDER} --state-file {provider_state}"

        subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)

        run_step("bootstrap", [TOOL, "init", "--project-root", project])
        run_step("validate", [TOOL, "validate", "--state-dir", state_dir])
        run_step(
            "set budget",
            [
                TOOL,
                "set-budget",
                "--state-dir",
                state_dir,
                "--project-budget",
                "20000",
                "--warning-percent",
                "80",
                "--hard-percent",
                "100",
            ],
        )

        strategy_packet = packets / "strategy-packet.md"
        write_strategy_packet(strategy_packet, plan_id)
        run_step("strategy packet lint", [TOOL, "strategy-packet-lint", "--packet", strategy_packet])
        run_step(
            "accept strategy",
            [
                TOOL,
                "accept-strategy",
                "--state-dir",
                state_dir,
                "--packet",
                strategy_packet,
                "--plan-id",
                plan_id,
                "--summary",
                "Approved bounded runtime soak",
            ],
        )
        run_step(
            "strategy pre-work gate",
            [
                TOOL,
                "require-strategy-packet-before-work",
                "--state-dir",
                state_dir,
                "--plan-id",
                plan_id,
                "--packet",
                strategy_packet,
            ],
        )

        run_step(
            "register strategy agent",
            [
                TOOL,
                "register-agent",
                "--state-dir",
                state_dir,
                "--agent-id",
                "strategy-soak",
                "--role",
                "Strategy",
                "--task-id",
                "SOAK-STRATEGY",
                "--objective",
                "Maintain strategy sync during soak",
                "--scope",
                "docs/master-agent",
                "--plan-id",
                plan_id,
                "--token-budget",
                "6000",
                "--max-heartbeats",
                "8",
            ],
        )

        for index in range(cycles):
            run_step(
                f"heartbeat {index + 1}",
                [
                    TOOL,
                    "heartbeat",
                    "--state-dir",
                    state_dir,
                    "--agent-id",
                    "strategy-soak",
                    "--state",
                    "active",
                    "--current",
                    "strategy-packet.md",
                    "--last-action",
                    f"completed cycle {index + 1}",
                    "--next-action",
                    f"perform cycle {index + 2}",
                    "--scope-status",
                    "yes",
                    "--confidence",
                    "high",
                    "--commands",
                    "control-plane command",
                    "--plan-id",
                    plan_id,
                    "--plan-alignment",
                    "yes",
                    "--evidence-quality",
                    "concrete",
                ],
            )
            if index == 0 or not quick:
                run_step(
                    f"supervise cycle {index + 1}",
                    [
                        TOOL,
                        "supervise",
                        "--state-dir",
                        state_dir,
                        "--poll-seconds",
                        "0",
                        "--max-cycles",
                        "1",
                        "--stale-minutes",
                        "30",
                    ],
                )

        context_packet = packets / "context-packet.md"
        context_packet.write_text("# Context Packet\n\n## Objective\n\n- provider soak\n", encoding="utf-8")
        run_step(
            "provider session create",
            [
                TOOL,
                "session-create",
                "--state-dir",
                state_dir,
                "--agent-id",
                "provider-soak",
                "--role",
                "Strategy",
                "--context-packet",
                context_packet,
                "--provider",
                "codex",
                "--provider-command",
                provider_command,
            ],
        )
        run_step(
            "provider session send",
            [
                TOOL,
                "session-send",
                "--state-dir",
                state_dir,
                "--agent-id",
                "provider-soak",
                "--message",
                "Return a compact packet.",
                "--provider-command",
                provider_command,
            ],
        )
        run_step(
            "provider session read",
            [
                TOOL,
                "session-read",
                "--state-dir",
                state_dir,
                "--agent-id",
                "provider-soak",
                "--provider-command",
                provider_command,
            ],
        )
        run_step(
            "provider reconcile",
            [
                TOOL,
                "session-reconcile",
                "--state-dir",
                state_dir,
                "--provider-command",
                provider_command,
            ],
        )
        run_step(
            "provider archive",
            [
                TOOL,
                "session-archive",
                "--state-dir",
                state_dir,
                "--agent-id",
                "provider-soak",
                "--provider-command",
                provider_command,
            ],
        )

        run_step(
            "worktree plan",
            [
                TOOL,
                "worktree-plan",
                "--state-dir",
                state_dir,
                "--worktree-id",
                "wt-soak-app",
                "--provider",
                "codex-app",
                "--base-branch",
                "main",
                "--purpose",
                "Isolated soak session",
            ],
        )
        run_step(
            "worktree include validation",
            [
                TOOL,
                "validate-worktreeinclude",
                "--state-dir",
                state_dir,
                "--project-root",
                project,
            ],
        )
        run_step(
            "worktree confirm create",
            [
                TOOL,
                "worktree-confirm-create",
                "--state-dir",
                state_dir,
                "--worktree-id",
                "wt-soak-app",
                "--thread-id",
                "thread-soak-app",
            ],
        )
        run_step(
            "worktree codex app session request",
            [
                TOOL,
                "session-create",
                "--state-dir",
                state_dir,
                "--agent-id",
                "strategy-worktree-soak",
                "--role",
                "Strategy",
                "--context-packet",
                context_packet,
                "--provider",
                "codex-app",
                "--worktree-id",
                "wt-soak-app",
            ],
        )
        run_step(
            "worktree codex app session confirm",
            [
                TOOL,
                "session-confirm-create",
                "--state-dir",
                state_dir,
                "--agent-id",
                "strategy-worktree-soak",
                "--thread-id",
                "thread-soak-app",
                "--worktree-id",
                "wt-soak-app",
            ],
        )
        run_step(
            "worktree assign session",
            [
                TOOL,
                "worktree-assign-session",
                "--state-dir",
                state_dir,
                "--worktree-id",
                "wt-soak-app",
                "--agent-id",
                "strategy-worktree-soak",
            ],
        )
        run_step(
            "worktree session read confirmation",
            [
                TOOL,
                "session-confirm-read",
                "--state-dir",
                state_dir,
                "--agent-id",
                "strategy-worktree-soak",
                "--summary",
                "soak read evidence",
                "--turn-count",
                "1",
            ],
        )
        run_step("worktree reconcile", [TOOL, "worktree-reconcile", "--state-dir", state_dir])
        run_step(
            "worktree close request",
            [
                TOOL,
                "worktree-close",
                "--state-dir",
                state_dir,
                "--worktree-id",
                "wt-soak-app",
                "--reason",
                "soak complete",
            ],
        )
        run_step(
            "worktree close confirmation",
            [
                TOOL,
                "worktree-confirm-close",
                "--state-dir",
                state_dir,
                "--worktree-id",
                "wt-soak-app",
            ],
        )

        run_step(
            "register coding agent",
            [
                TOOL,
                "register-agent",
                "--state-dir",
                state_dir,
                "--agent-id",
                "coding-soak-1",
                "--role",
                "Coding",
                "--task-id",
                "SOAK-CODE",
                "--objective",
                "Execute bounded control-plane soak",
                "--scope",
                "docs/master-agent",
                "--plan-id",
                plan_id,
                "--token-budget",
                "4000",
                "--max-heartbeats",
                "4",
            ],
        )
        run_step(
            "coding file session",
            [
                TOOL,
                "session-create",
                "--state-dir",
                state_dir,
                "--agent-id",
                "coding-soak-1",
                "--role",
                "Coding",
                "--context-packet",
                context_packet,
                "--provider",
                "file",
            ],
        )
        predecessor_packet = packets / "coding-soak-1-predecessor-state-packet.md"
        write_predecessor_state_packet(predecessor_packet, plan_id)
        run_step(
            "predecessor state validation",
            [TOOL, "validate-predecessor-state", "--packet", predecessor_packet],
        )
        run_step(
            "strict rotation",
            [
                TOOL,
                "rotate-session",
                "--state-dir",
                state_dir,
                "--agent-id",
                "coding-soak-1",
                "--successor-agent-id",
                "coding-soak-2",
                "--reason",
                "attention-drift",
                "--provider",
                "file",
                "--predecessor-state-packet",
                predecessor_packet,
            ],
        )

        work_a = packets / "work-order-a.md"
        work_b = packets / "work-order-b.md"
        write_work_order(work_a, "docs/master-agent/a", "docs/master-agent/artifacts/a", "wt-soak-a")
        write_work_order(work_b, "docs/master-agent/b", "docs/master-agent/artifacts/b", "wt-soak-b")
        run_step(
            "parallelism gate",
            [
                TOOL,
                "assess-parallelism",
                "--state-dir",
                state_dir,
                "--work-order",
                work_a,
                "--work-order",
                work_b,
                "--output",
                packets / "parallelism-verdict.md",
            ],
        )
        run_step(
            "master boundary",
            [
                TOOL,
                "enforce-master-boundary",
                "--project-root",
                project,
                "--state-dir",
                state_dir,
            ],
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Master Agent operating-system soak.")
    parser.add_argument("--cycles", type=int, default=12)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    cycles = 3 if args.quick else args.cycles
    if cycles < 1:
        print("--cycles must be positive", file=sys.stderr)
        return 2
    try:
        run_soak(cycles=cycles, quick=args.quick)
    except (StepFailure, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Soak validation passed ({cycles} cycles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
