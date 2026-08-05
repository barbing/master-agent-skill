import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
TOOL = ROOT / "scripts" / "master_agent_tool.py"
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from state_io import append_jsonl_locked, atomic_write_json  # noqa: E402


def run_cmd(args, cwd=ROOT, check=True, env=None):
    result = subprocess.run(
        [PYTHON, *map(str, args)],
        cwd=cwd,
        text=True,
        capture_output=True,
        env=env,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed: {args}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def write_live_provider_script(path: Path, state_path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "import json, sys",
                "from pathlib import Path",
                "request = json.load(sys.stdin)",
                f"state_path = Path({str(state_path)!r})",
                "state_path.parent.mkdir(parents=True, exist_ok=True)",
                "state = {'messages': [], 'events': []}",
                "if state_path.exists():",
                "    state = json.loads(state_path.read_text(encoding='utf-8'))",
                "event = request['event']",
                "state['events'].append(event)",
                "if event == 'session-create':",
                "    state.update({",
                "        'provider_session_id': 'codex-session-live',",
                "        'status': 'active',",
                "        'provider_session_path': str(state_path),",
                "        'agent_id': request['agent_id'],",
                "        'role': request['role'],",
                "        'context_packet': request['context_packet'],",
                "    })",
                "    state['messages'] = [{'sender': 'provider', 'message': 'ready'}]",
                "elif event == 'session-send':",
                "    state['messages'].append({'sender': 'master', 'message': request['message']})",
                "    state['messages'].append({'sender': 'provider', 'message': 'ack:' + request['message']})",
                "elif event == 'session-read':",
                "    pass",
                "elif event == 'session-archive':",
                "    state['status'] = 'archived'",
                "elif event == 'session-reconcile':",
                "    pass",
                "state_path.write_text(json.dumps(state) + '\\n', encoding='utf-8')",
                "json.dump(state, sys.stdout)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def write_predecessor_state_packet(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "# Predecessor State Packet",
                "",
                "## Objective",
                "",
                "- Finish parser repair",
                "",
                "## Plan Id",
                "",
                "- PLAN-ROTATE",
                "",
                "## Completed Work",
                "",
                "- Patched parser state tracking",
                "",
                "## Changed Files And Artifacts",
                "",
                "- src/parser/tokenizer.py",
                "",
                "## Validation Evidence",
                "",
                "- python -m unittest tests.test_parser",
                "",
                "## Known Failures",
                "",
                "- none",
                "",
                "## Risks",
                "",
                "- attention drift after long session",
                "",
                "## Next Safe Step",
                "",
                "- inspect parser state transition test",
                "",
                "## Forbidden Repeats",
                "",
                "- continue patching parser without narrowing",
                "",
                "## Token Usage",
                "",
                "- 3200 / 8000",
                "",
                "## Open Questions",
                "",
                "- none",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def write_valid_strategy_packet(path: Path, plan_id: str = "PLAN-VALID") -> None:
    path.write_text(
        "\n".join(
            [
                "# Strategy Packet",
                "",
                "## Question",
                "",
                "- Strategy session id: strategy-test-1",
                "- Question being answered: Should the next bounded work order proceed?",
                "- User decision requested: no",
                "",
                "## Authority",
                "",
                "- Authority docs consulted: project-policy-pack.md",
                "- Master ledger state used: initial ledger",
                "- Policy pack sections used: safety envelope",
                "",
                "## Plan Sync",
                "",
                f"- Proposed plan id: {plan_id}",
                "- Current accepted plan id: none",
                "- Plan version change: yes",
                "- Master ledger update required: strategy-sync.md",
                "- Resync trigger: new accepted plan",
                "",
                "## Diagnosis",
                "",
                "- Current diagnosis: the work is bounded and ready for a Coding Agent",
                "- Current code path or process path: docs/master-agent",
                "- Intended code path or process path: gated work order",
                "- First failing boundary: none",
                "",
                "## Options Considered",
                "",
                "| Option | Benefit | Cost | Risk | Decision |",
                "| --- | --- | --- | --- | --- |",
                "| proceed | verifies gate | low | narrow | accept |",
                "",
                "## Recommendation",
                "",
                "- Recommended decision: proceed with bounded work order",
                "- Reason: all controls are named",
                "- Rejected alternatives: raw discussion handoff",
                "- Confidence: high",
                "",
                "## Proposed Work Order",
                "",
                "- Proposed objective: execute bounded implementation",
                "- Root authorization source: current-user-request",
                "- Root authorization grant id: grant-test-1",
                "- Allowed scope: docs/master-agent",
                "- Approved material behavior domains: none",
                "- Declared material behavior domains: none",
                "- Worktree mode: codex-app",
                "- Worktree id: wt-plan",
                "- Base branch: main",
                "- Local mutation policy: do not mutate local checkout",
                "- Remote mutation policy: do not push or create PR without release gate",
                "- Forbidden changes: production implementation",
                "- Acceptance maturity required: diagnostic",
                "- Representative workflow required: no",
                "- Heuristic admission required: no",
                "- Task record required: yes",
                "- Validation required: release gate",
                "- Expected artifacts: receipt and verdict",
                "- Stop conditions: scope drift or missing validation",
                "",
                "## Open Risks",
                "",
                "- synthetic test packet",
                "",
                "## Token Impact",
                "",
                "- Estimated next-session token cost: 2000",
                "- Recommended sub-agent count: 1",
                "- Recommended heartbeat cap: 3",
                "- Recommended context tier: minimal",
                "- Recommended Master constraints: require packet gate",
                "- Recommended sub-agent autonomous strategy: targeted reads only",
                "- Compression or narrowing trigger: repeated next action",
                "- Token risks: low",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def write_valid_learning_proposal(path: Path, proposal_id: str = "LP-1") -> None:
    path.write_text(
        "\n".join(
            [
                "# Learning Proposal",
                "",
                "## Trigger",
                "",
                f"- Proposal id: {proposal_id}",
                "- Source corrections: corr-1",
                "- Failure mode: evidence-free-success-claim",
                "- Evidence: review verdict rejected unsupported completion",
                "",
                "## Distilled Lesson",
                "",
                "- Lesson: completion claims must include direct evidence",
                "- Applies when: an agent reports complete status",
                "- Does not apply when: the user asks only for a brainstorming note",
                "- Evidence trigger: receipt lacks commands, artifacts, or changed files",
                "- Escape condition: direct validation evidence is supplied",
                "- Counterexample checked: small read-only answers need no artifacts",
                "",
                "## Target",
                "",
                "- Target type: template",
                "- Target path: assets/templates/coding-receipt.md",
                "- Change summary: require evidence-backed completion field",
                "- Implementation owner: Master Agent",
                "- Requires production code change: no",
                "",
                "## Safety Review",
                "",
                "- Anti-narrowing risk: could over-require evidence for casual answers; limited to receipts",
                "- Privacy or secret risk: none",
                "- Licensing risk: none",
                "- Policy review required: no",
                "",
                "## Validation",
                "",
                "- Required validation: learning-proposal-lint and state validate",
                "- Success metric: future receipts include evidence",
                "- Recurrence check: inspect next three completion receipts",
                "",
                "## Decision",
                "",
                "- Proposed decision: extend",
                "- Confidence: high",
                "- Open questions: none",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def accept_valid_strategy(state_dir: Path, tmp: Path, plan_id: str = "PLAN-1") -> Path:
    packet = tmp / f"strategy-packet-{plan_id}.md"
    write_valid_strategy_packet(packet, plan_id)
    run_cmd(
        [
            TOOL,
            "accept-strategy",
            "--state-dir",
            state_dir,
            "--packet",
            packet,
            "--plan-id",
            plan_id,
            "--summary",
            f"Accepted {plan_id}",
        ]
    )
    return packet


def write_round_log_snapshot(
    repo_root: Path,
    snapshot_id: str = "0001_20260730T000000",
    copied_paths: list[str] | None = None,
    deleted_paths: list[str] | None = None,
) -> Path:
    copied = copied_paths if copied_paths is not None else ["src/module.py"]
    deleted = deleted_paths if deleted_paths is not None else []
    log_root = repo_root / ".codex-round-log"
    snapshot_dir = log_root / snapshot_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": snapshot_id,
        "created_at": "2026-07-30T00:00:00+00:00",
        "mode": "checkpoint",
        "label": "test snapshot",
        "branch": "main",
        "previous_snapshot_id": "",
        "copied_paths": copied,
        "deleted_paths": deleted,
        "file_index_path": "files-index.html",
    }
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (snapshot_dir / "files-index.html").write_text("<html></html>\n", encoding="utf-8")
    (log_root / "state.json").write_text(
        json.dumps({"last_snapshot_id": snapshot_id}, indent=2),
        encoding="utf-8",
    )
    return snapshot_dir


class MasterAgentToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="master-agent-system-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.state_dir = self.tmp / "docs" / "master-agent"

    def test_all_cli_help_surfaces_parse_without_traceback(self):
        source = TOOL.read_text(encoding="utf-8")
        commands = sorted(set(re.findall(r'subparsers\.add_parser\("([^"]+)"', source)))
        self.assertGreaterEqual(len(commands), 40)

        for command in commands:
            with self.subTest(command=command):
                result = run_cmd([TOOL, command, "--help"], check=False)
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{command} --help failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
                )
                self.assertIn("usage:", result.stdout.lower())
                self.assertNotIn("Traceback", result.stderr)

    def test_init_validate_and_strict_validation(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        structural = run_cmd([TOOL, "validate", "--state-dir", self.state_dir])
        self.assertIn("State pack is valid", structural.stdout)

        strict = run_cmd(
            [TOOL, "validate", "--state-dir", self.state_dir, "--strict"],
            check=False,
        )
        self.assertNotEqual(strict.returncode, 0)
        self.assertIn("unfilled required field", strict.stderr)

        ledger = self.state_dir / "master-ledger.md"
        policy = self.state_dir / "project-policy-pack.md"
        ledger.write_text(
            ledger.read_text(encoding="utf-8")
            .replace("- Project:\n", "- Project: Sample Project\n")
            .replace(
                "- Current objective:\n",
                "- Current objective: Coordinate implementation sessions\n",
            )
            .replace(
                "- Project policy pack:\n",
                "- Project policy pack: docs/master-agent/project-policy-pack.md\n",
            )
            .replace("- Authority docs:\n", "- Authority docs: AGENTS.md\n")
            .replace("- Active plan:\n", "- Active plan: docs/plan.md\n"),
            encoding="utf-8",
        )
        policy.write_text(
            policy.read_text(encoding="utf-8")
            .replace("- \n", "- AGENTS.md\n", 1)
            .replace("- Objective:\n", "- Objective: Coordinate implementation sessions\n")
            .replace("- Acceptance criteria:\n", "- Acceptance criteria: Packets and heartbeats are enforced\n"),
            encoding="utf-8",
        )

        strict_after_fill = run_cmd(
            [TOOL, "validate", "--state-dir", self.state_dir, "--strict"]
        )
        self.assertIn("State pack is valid", strict_after_fill.stdout)

    def test_init_creates_safety_envelope(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        safety = (self.state_dir / "safety-envelope.md").read_text(encoding="utf-8")
        self.assertIn("# Safety Envelope", safety)
        self.assertIn("## Autonomous Authority", safety)
        self.assertIn("## Requires Human Decision", safety)
        self.assertIn("## Forbidden Autonomous Actions", safety)
        self.assertIn("## Budget And Role Limits", safety)
        self.assertIn("## Remediation Permissions", safety)
        self.assertIn("## Escalation Triggers", safety)

    def test_init_creates_worktree_control_state(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        worktree_control = (self.state_dir / "worktree-control.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("# Worktree Control", worktree_control)
        self.assertIn("## Worktree Policy", worktree_control)
        self.assertTrue((self.state_dir / "state" / "worktrees.jsonl").exists())

    def test_init_creates_round_log_control_state(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        round_log_control = (self.state_dir / "round-log-control.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("# Round Log Control", round_log_control)
        self.assertIn("## Evidence Binding", round_log_control)
        self.assertTrue((self.state_dir / "state" / "round-log-events.jsonl").exists())

    def test_init_creates_repair_log_control_state(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        repair_log_control = (self.state_dir / "repair-log-control.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("# Repair Log Control", repair_log_control)
        self.assertIn("## Current Row Gate", repair_log_control)
        self.assertTrue((self.state_dir / "state" / "repair-log-events.jsonl").exists())

    def test_init_creates_learning_layer_state(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        correction_ledger = (self.state_dir / "correction-ledger.md").read_text(
            encoding="utf-8"
        )
        proposal = (self.state_dir / "learning-proposal.md").read_text(
            encoding="utf-8"
        )
        effectiveness = (self.state_dir / "learning-effectiveness.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("# Correction Ledger", correction_ledger)
        self.assertIn("# Learning Proposal", proposal)
        self.assertIn("# Learning Effectiveness", effectiveness)
        self.assertTrue((self.state_dir / "state" / "learning-corrections.jsonl").exists())
        self.assertTrue((self.state_dir / "state" / "learning-cycles.jsonl").exists())
        self.assertTrue((self.state_dir / "state" / "learning-updates.jsonl").exists())
        self.assertTrue((self.state_dir / "state" / "learning-effectiveness.jsonl").exists())
        roles = json.loads((self.state_dir / "state" / "roles.json").read_text(encoding="utf-8"))
        self.assertEqual(roles["Learning Distiller"]["status"], "active")

    def test_init_creates_governance_optimization_state(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        for filename in [
            "authority-envelope.md",
            "obstacle-recovery-packet.md",
            "acceptance-gate.md",
            "task-record.md",
            "implementation-guard-adapter.md",
            "guard-obligation.md",
        ]:
            self.assertTrue((self.state_dir / filename).exists(), filename)
        self.assertTrue((self.state_dir / "state" / "governance-events.jsonl").exists())
        self.assertTrue((self.state_dir / "state" / "acceptance-gates.jsonl").exists())
        schema = json.loads(
            (self.state_dir / "state" / "schema-version.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["schema_version"], "1.5")

    def test_learning_correction_creates_cycle_and_summary(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        result = run_cmd(
            [
                TOOL,
                "record-learning-correction",
                "--state-dir",
                self.state_dir,
                "--project",
                "sample-project",
                "--source",
                "review-verdict.md",
                "--task",
                "verify completion claim",
                "--agent-behavior",
                "claimed ready without direct validation evidence",
                "--user-correction",
                "completion must be evidence-backed",
                "--evidence",
                "review found missing validation artifacts",
                "--failure-mode",
                "evidence-free-success-claim",
                "--confidence",
                "high",
                "--at",
                "2026-06-01T00:00:00+00:00",
            ]
        )
        self.assertIn("Recorded learning correction", result.stdout)

        corrections = [
            json.loads(line)
            for line in (self.state_dir / "state" / "learning-corrections.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(
            corrections[0]["failure_mode"], "evidence-free-success-claim"
        )
        ledger = (self.state_dir / "correction-ledger.md").read_text(encoding="utf-8")
        self.assertIn("evidence-free-success-claim", ledger)
        self.assertIn("review found missing validation artifacts", ledger)

        cycle = run_cmd(
            [
                TOOL,
                "learning-cycle-start",
                "--state-dir",
                self.state_dir,
                "--window",
                "last 7 days",
                "--project",
                "sample-project",
                "--cycle-id",
                "LC-1",
                "--at",
                "2026-06-01T01:00:00+00:00",
            ]
        )
        self.assertIn("Created learning cycle", cycle.stdout)
        cycle_packet = (
            self.state_dir / "packets" / "learning" / "LC-1" / "learning-cycle.md"
        )
        self.assertTrue(cycle_packet.exists())
        cycle_text = cycle_packet.read_text(encoding="utf-8")
        self.assertIn(corrections[0]["correction_id"], cycle_text)
        self.assertIn("needs triage", cycle_text)

        cycles = [
            json.loads(line)
            for line in (self.state_dir / "state" / "learning-cycles.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(cycles[-1]["corrections_considered"], 1)

        summary = run_cmd([TOOL, "learning-summary", "--state-dir", self.state_dir])
        self.assertIn("Corrections: 1", summary.stdout)
        self.assertIn("Learning cycles: 1", summary.stdout)
        self.assertIn("- evidence-free-success-claim: 1", summary.stdout)

    def test_learning_proposal_lint_accept_and_effectiveness(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        proposal = self.tmp / "learning-proposal.md"
        write_valid_learning_proposal(proposal, "LP-READY")

        lint = run_cmd([TOOL, "learning-proposal-lint", "--proposal", proposal])
        self.assertIn("Learning proposal is valid", lint.stdout)

        accepted = run_cmd(
            [
                TOOL,
                "accept-learning-proposal",
                "--state-dir",
                self.state_dir,
                "--proposal",
                proposal,
                "--summary",
                "Accepted evidence-backed completion learning",
                "--validation-evidence",
                "proposal lint passed",
                "--at",
                "2026-06-01T02:00:00+00:00",
            ]
        )
        self.assertIn("Accepted learning proposal LP-READY", accepted.stdout)

        updates = [
            json.loads(line)
            for line in (self.state_dir / "state" / "learning-updates.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        checks = [
            json.loads(line)
            for line in (self.state_dir / "state" / "learning-effectiveness.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(updates[-1]["proposal_id"], "LP-READY")
        self.assertEqual(checks[-1]["status"], "not-yet-measured")

        measured = run_cmd(
            [
                TOOL,
                "record-learning-effectiveness",
                "--state-dir",
                self.state_dir,
                "--proposal-id",
                "LP-READY",
                "--status",
                "recurrence-prevented",
                "--evidence",
                "next three receipts included validation evidence",
                "--next-action",
                "keep rule",
                "--at",
                "2026-06-02T00:00:00+00:00",
            ]
        )
        self.assertIn("Recorded learning effectiveness", measured.stdout)

        effectiveness = (self.state_dir / "learning-effectiveness.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("LP-READY", effectiveness)
        self.assertIn("recurrence-prevented", effectiveness)
        summary = run_cmd([TOOL, "learning-summary", "--state-dir", self.state_dir])
        self.assertIn("Accepted learning updates: 1", summary.stdout)
        self.assertIn("Recurrence detected: 0", summary.stdout)

    def test_learning_proposal_lint_blocks_production_code_changes(self):
        proposal = self.tmp / "bad-learning-proposal.md"
        write_valid_learning_proposal(proposal, "LP-BLOCKED")
        proposal.write_text(
            proposal.read_text(encoding="utf-8").replace(
                "- Requires production code change: no",
                "- Requires production code change: yes",
            ),
            encoding="utf-8",
        )

        result = run_cmd(
            [TOOL, "learning-proposal-lint", "--proposal", proposal],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "learning proposal cannot require production code change", result.stderr
        )

    def test_validate_rejects_missing_safety_envelope(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        (self.state_dir / "safety-envelope.md").unlink()

        result = run_cmd(
            [TOOL, "validate", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing file: safety-envelope.md", result.stderr)

    def test_master_can_autonomously_act_inside_safety_envelope(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        status = run_cmd([TOOL, "safety-status", "--state-dir", self.state_dir])
        self.assertIn("Safety envelope", status.stdout)
        self.assertIn("Autonomous authority", status.stdout)

        allowed = run_cmd(
            [
                TOOL,
                "check-safety",
                "--state-dir",
                self.state_dir,
                "--action",
                "update-ledger",
                "--role",
                "Master",
                "--scope",
                "docs/master-agent/master-ledger.md",
                "--budget-impact",
                "100",
            ]
        )
        self.assertIn("Safety: autonomous", allowed.stdout)

    def test_master_blocks_action_outside_safety_envelope(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        blocked = run_cmd(
            [
                TOOL,
                "check-safety",
                "--state-dir",
                self.state_dir,
                "--action",
                "edit-production-code",
                "--role",
                "Master",
                "--scope",
                "app/main.py",
                "--budget-impact",
                "100",
            ],
            check=False,
        )
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("Safety: human-decision-or-forbidden", blocked.stdout)
        self.assertIn("forbidden action", blocked.stdout.lower())

    def test_accept_strategy_updates_strategy_sync(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        packet = self.tmp / "strategy-packet.md"
        write_valid_strategy_packet(packet, "PLAN-1")

        result = run_cmd(
            [
                TOOL,
                "accept-strategy",
                "--state-dir",
                self.state_dir,
                "--packet",
                packet,
                "--plan-id",
                "PLAN-1",
                "--summary",
                "Approved bounded implementation sequence",
                "--at",
                "2026-06-01T00:00:00+00:00",
            ]
        )
        self.assertIn("Accepted strategy PLAN-1", result.stdout)

        sync = (self.state_dir / "strategy-sync.md").read_text(encoding="utf-8")
        self.assertIn("PLAN-1", sync)
        self.assertIn("Approved bounded implementation sequence", sync)

        sync_history = (self.state_dir / "state" / "strategy-sync.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertIn('"plan_id": "PLAN-1"', sync_history)
        self.assertIn('"strategy_packet_validated": true', sync_history)

        event_log = (self.state_dir / "event-log.md").read_text(encoding="utf-8")
        self.assertIn("strategy-accepted", event_log)
        self.assertIn("PLAN-1", event_log)

    def test_accept_strategy_rejects_incomplete_packet(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        packet = self.tmp / "strategy-packet.md"
        packet.write_text("# Strategy Packet\n", encoding="utf-8")

        result = run_cmd(
            [
                TOOL,
                "accept-strategy",
                "--state-dir",
                self.state_dir,
                "--packet",
                packet,
                "--plan-id",
                "PLAN-BAD",
                "--summary",
                "Incomplete packet should not become state",
            ],
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing heading", result.stderr)
        self.assertEqual(
            "",
            (self.state_dir / "state" / "strategy-sync.jsonl").read_text(
                encoding="utf-8"
            ),
        )

    def test_register_agent_requires_current_plan_when_strategy_sync_active(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        packet = self.tmp / "strategy-packet.md"
        write_valid_strategy_packet(packet, "PLAN-1")
        run_cmd(
            [
                TOOL,
                "accept-strategy",
                "--state-dir",
                self.state_dir,
                "--packet",
                packet,
                "--plan-id",
                "PLAN-1",
                "--summary",
                "Approved plan",
            ]
        )

        missing_plan = run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-PLAN",
                "--objective",
                "Implement approved work",
                "--scope",
                "src/module",
            ],
            check=False,
        )
        self.assertEqual(missing_plan.returncode, 1)
        self.assertIn("requires current plan", missing_plan.stderr.lower())

        wrong_plan = run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-PLAN",
                "--objective",
                "Implement approved work",
                "--scope",
                "src/module",
                "--plan-id",
                "PLAN-OLD",
            ],
            check=False,
        )
        self.assertEqual(wrong_plan.returncode, 1)
        self.assertIn("requires current plan", wrong_plan.stderr.lower())

        registered = run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-PLAN",
                "--objective",
                "Implement approved work",
                "--scope",
                "src/module",
                "--plan-id",
                "PLAN-1",
            ]
        )
        self.assertIn("Registered agent coding-1", registered.stdout)

    def test_register_agent_rejects_unvalidated_current_strategy_state(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        packet = self.tmp / "strategy-packet.md"
        write_valid_strategy_packet(packet, "PLAN-LEGACY")
        append_jsonl_locked(
            self.state_dir / "state" / "strategy-sync.jsonl",
            {
                "accepted_at": "2026-06-01T00:00:00+00:00",
                "packet": str(packet),
                "plan_id": "PLAN-LEGACY",
                "summary": "Legacy unvalidated state",
            },
        )

        result = run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-legacy",
                "--role",
                "Coding",
                "--task-id",
                "TASK-LEGACY",
                "--objective",
                "Must not run from unvalidated state",
                "--scope",
                "src/module",
                "--plan-id",
                "PLAN-LEGACY",
            ],
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("has not been validated", result.stderr)

    def test_stale_strategy_plan_is_reported(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        packet = self.tmp / "strategy-packet.md"
        write_valid_strategy_packet(packet, "PLAN-1")
        run_cmd(
            [
                TOOL,
                "accept-strategy",
                "--state-dir",
                self.state_dir,
                "--packet",
                packet,
                "--plan-id",
                "PLAN-1",
                "--summary",
                "Approved plan",
                "--at",
                "2026-06-01T00:00:00+00:00",
            ]
        )

        fresh = run_cmd(
            [
                TOOL,
                "strategy-sync-status",
                "--state-dir",
                self.state_dir,
                "--now",
                "2026-06-01T01:00:00+00:00",
                "--stale-hours",
                "24",
            ]
        )
        self.assertIn("Current plan: PLAN-1", fresh.stdout)
        self.assertIn("Plan status: current", fresh.stdout)

        stale = run_cmd(
            [
                TOOL,
                "strategy-sync-status",
                "--state-dir",
                self.state_dir,
                "--now",
                "2026-06-03T00:00:00+00:00",
                "--stale-hours",
                "24",
            ],
            check=False,
        )
        self.assertEqual(stale.returncode, 1)
        self.assertIn("Plan status: stale", stale.stdout)

    def test_strategy_packet_lint_rejects_unfilled_template(self):
        packet = self.tmp / "strategy-packet.md"
        packet.write_text(
            (ROOT / "assets" / "templates" / "strategy-packet.md").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        result = run_cmd(
            [TOOL, "strategy-packet-lint", "--packet", packet],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("unfilled field", result.stderr)

    def test_strategy_packet_lint_and_pre_work_gate_require_current_packet(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        packet = self.tmp / "strategy-packet.md"
        write_valid_strategy_packet(packet, "PLAN-GATED")

        lint = run_cmd([TOOL, "strategy-packet-lint", "--packet", packet])
        self.assertIn("Strategy packet is valid", lint.stdout)

        missing_plan = run_cmd(
            [
                TOOL,
                "require-strategy-packet-before-work",
                "--state-dir",
                self.state_dir,
                "--plan-id",
                "PLAN-GATED",
                "--packet",
                packet,
            ],
            check=False,
        )
        self.assertEqual(missing_plan.returncode, 1)
        self.assertIn("No current accepted strategy plan", missing_plan.stderr)

        run_cmd(
            [
                TOOL,
                "accept-strategy",
                "--state-dir",
                self.state_dir,
                "--packet",
                packet,
                "--plan-id",
                "PLAN-GATED",
                "--summary",
                "Accepted gated strategy packet",
            ]
        )

        gate = run_cmd(
            [
                TOOL,
                "require-strategy-packet-before-work",
                "--state-dir",
                self.state_dir,
                "--plan-id",
                "PLAN-GATED",
                "--packet",
                packet,
            ]
        )
        self.assertIn("Strategy pre-work gate passed: PLAN-GATED", gate.stdout)

        stale_packet = self.tmp / "stale-strategy-packet.md"
        write_valid_strategy_packet(stale_packet, "PLAN-GATED")
        stale = run_cmd(
            [
                TOOL,
                "require-strategy-packet-before-work",
                "--state-dir",
                self.state_dir,
                "--plan-id",
                "PLAN-GATED",
                "--packet",
                stale_packet,
            ],
            check=False,
        )
        self.assertEqual(stale.returncode, 1)
        self.assertIn("not the current accepted packet", stale.stderr)

    def test_audit_agent_detects_repeated_next_action_loop(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-REVIEW")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "review-1",
                "--role",
                "Review",
                "--task-id",
                "TASK-AUDIT",
                "--objective",
                "Review evidence",
                "--scope",
                "docs/master-agent",
                "--plan-id",
                "PLAN-REVIEW",
            ]
        )
        for index in range(3):
            run_cmd(
                [
                    TOOL,
                    "heartbeat",
                    "--state-dir",
                    self.state_dir,
                    "--agent-id",
                    "review-1",
                    "--state",
                    "active",
                    "--current",
                    "review-verdict.md",
                    "--last-action",
                    f"heartbeat {index}",
                    "--next-action",
                    "continue review",
                    "--scope-status",
                    "yes",
                    "--confidence",
                    "medium",
                ]
            )

        audit = run_cmd(
            [TOOL, "audit-agent", "--state-dir", self.state_dir, "--agent-id", "review-1"],
            check=False,
        )
        self.assertEqual(audit.returncode, 1)
        self.assertIn("repeated-next-action-loop", audit.stdout)
        anomaly_log = (self.state_dir / "anomaly-log.md").read_text(encoding="utf-8")
        self.assertIn("repeated-next-action-loop", anomaly_log)

    def test_audit_agent_detects_plan_mismatch(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        packet = self.tmp / "strategy-packet.md"
        write_valid_strategy_packet(packet, "PLAN-1")
        run_cmd(
            [
                TOOL,
                "accept-strategy",
                "--state-dir",
                self.state_dir,
                "--packet",
                packet,
                "--plan-id",
                "PLAN-1",
                "--summary",
                "Approved plan",
            ]
        )
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-AUDIT",
                "--objective",
                "Implement plan",
                "--scope",
                "src/module",
                "--plan-id",
                "PLAN-1",
            ]
        )
        run_cmd(
            [
                TOOL,
                "heartbeat",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--state",
                "active",
                "--current",
                "src/module/file.py",
                "--last-action",
                "read files",
                "--next-action",
                "patch file",
                "--scope-status",
                "yes",
                "--confidence",
                "medium",
                "--plan-alignment",
                "no",
            ]
        )

        audit = run_cmd(
            [TOOL, "audit-agent", "--state-dir", self.state_dir, "--agent-id", "coding-1"],
            check=False,
        )
        self.assertEqual(audit.returncode, 1)
        self.assertIn("plan-mismatch", audit.stdout)

    def test_audit_agent_detects_evidence_free_success_claim(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-AUDIT")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-AUDIT",
                "--objective",
                "Implement work",
                "--scope",
                "src/module",
                "--plan-id",
                "PLAN-AUDIT",
            ]
        )
        run_cmd(
            [
                TOOL,
                "heartbeat",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--state",
                "complete",
                "--current",
                "coding-receipt.md",
                "--last-action",
                "completed",
                "--next-action",
                "return receipt",
                "--scope-status",
                "yes",
                "--confidence",
                "high",
                "--evidence-quality",
                "missing",
            ]
        )

        audit = run_cmd(
            [TOOL, "audit-agent", "--state-dir", self.state_dir, "--agent-id", "coding-1"],
            check=False,
        )
        self.assertEqual(audit.returncode, 1)
        self.assertIn("evidence-free-success-claim", audit.stdout)

    def test_audit_agent_detects_scope_drift(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--role",
                "Strategy",
                "--task-id",
                "TASK-AUDIT",
                "--objective",
                "Analyze scope",
                "--scope",
                "docs/master-agent",
            ]
        )
        run_cmd(
            [
                TOOL,
                "heartbeat",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--state",
                "active",
                "--current",
                "strategy-packet.md",
                "--last-action",
                "expanded scope",
                "--next-action",
                "continue",
                "--scope-status",
                "no",
                "--confidence",
                "low",
            ]
        )

        audit = run_cmd(
            [TOOL, "audit-agent", "--state-dir", self.state_dir, "--agent-id", "strategy-1"],
            check=False,
        )
        self.assertEqual(audit.returncode, 1)
        self.assertIn("scope-drift", audit.stdout)

    def test_remediate_agent_creates_context_reinforcement_packet(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-REMEDIATE")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-REMEDIATE",
                "--objective",
                "Implement work",
                "--scope",
                "src/module",
                "--plan-id",
                "PLAN-REMEDIATE",
            ]
        )
        run_cmd(
            [
                TOOL,
                "heartbeat",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--state",
                "active",
                "--current",
                "src/module/file.py",
                "--last-action",
                "read work order",
                "--next-action",
                "continue",
                "--scope-status",
                "unsure",
                "--confidence",
                "low",
            ]
        )

        result = run_cmd(
            [
                TOOL,
                "remediate-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--action",
                "reinforce-context",
            ]
        )
        self.assertIn("Created remediation packet", result.stdout)
        packet = self.state_dir / "packets" / "remediation" / "coding-1-context-reinforcement.md"
        self.assertTrue(packet.exists())
        text = packet.read_text(encoding="utf-8")
        self.assertIn("## Context Reinforcement", text)
        self.assertIn("read work order", text)

    def test_remediate_agent_creates_successor_packet_for_attention_drift(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        packet = self.tmp / "strategy-packet.md"
        write_valid_strategy_packet(packet, "PLAN-1")
        run_cmd(
            [
                TOOL,
                "accept-strategy",
                "--state-dir",
                self.state_dir,
                "--packet",
                packet,
                "--plan-id",
                "PLAN-1",
                "--summary",
                "Approved plan",
            ]
        )
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-REMEDIATE",
                "--objective",
                "Implement work",
                "--scope",
                "src/module",
                "--plan-id",
                "PLAN-1",
                "--token-budget",
                "5000",
            ]
        )
        for index in range(3):
            run_cmd(
                [
                    TOOL,
                    "heartbeat",
                    "--state-dir",
                    self.state_dir,
                    "--agent-id",
                    "coding-1",
                    "--state",
                    "active",
                    "--current",
                    "src/module/file.py",
                    "--last-action",
                    f"attempt {index}",
                    "--next-action",
                    "continue patching",
                    "--scope-status",
                    "yes",
                    "--confidence",
                    "medium",
                    "--risk",
                    "attention drift",
                ]
            )

        result = run_cmd(
            [
                TOOL,
                "remediate-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--action",
                "spawn-successor",
            ]
        )
        self.assertIn("Created remediation packet", result.stdout)
        successor = self.state_dir / "packets" / "remediation" / "coding-1-successor-context.md"
        self.assertTrue(successor.exists())
        text = successor.read_text(encoding="utf-8")
        self.assertIn("Current plan id: PLAN-1", text)
        self.assertIn("Open risks: attention drift", text)
        self.assertIn("Forbidden repeats: continue patching", text)

    def test_remediate_agent_stops_when_safety_envelope_blocks_action(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-REMEDIATE")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-REMEDIATE",
                "--objective",
                "Implement work",
                "--scope",
                "src/module",
                "--plan-id",
                "PLAN-REMEDIATE",
            ]
        )
        blocked = run_cmd(
            [
                TOOL,
                "remediate-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--action",
                "spawn-successor",
                "--budget-impact",
                "25000",
            ],
            check=False,
        )
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("Safety blocked remediation", blocked.stdout)

    def test_state_json_write_is_atomic(self):
        target = self.tmp / "state" / "sample.json"
        atomic_write_json(target, {"before": True})
        atomic_write_json(target, {"after": True, "count": 2})

        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")),
            {"after": True, "count": 2},
        )
        leftovers = list(target.parent.glob("sample.json.tmp-*"))
        self.assertEqual(leftovers, [])

    def test_jsonl_append_uses_lock_file(self):
        target = self.tmp / "state" / "events.jsonl"
        append_jsonl_locked(target, {"index": 1})
        append_jsonl_locked(target, {"index": 2})

        rows = [
            json.loads(line)
            for line in target.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(rows, [{"index": 1}, {"index": 2}])
        self.assertFalse(target.with_suffix(target.suffix + ".lock").exists())

    def test_parallel_usage_records_do_not_lose_updates(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "set-budget",
                "--state-dir",
                self.state_dir,
                "--project-budget",
                "100000",
            ]
        )

        def record(index):
            return run_cmd(
                [
                    TOOL,
                    "record-usage",
                    "--state-dir",
                    self.state_dir,
                    "--agent-id",
                    "strategy-1",
                    "--tokens-used",
                    "10",
                    "--note",
                    f"parallel {index}",
                ]
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(20)))

        budget = json.loads(
            (self.state_dir / "state" / "budget.json").read_text(encoding="utf-8")
        )
        self.assertEqual(budget["project_used"], 200)
        self.assertEqual(budget["agents"]["strategy-1"]["tokens_used"], 200)
        usage_lines = (
            self.state_dir / "state" / "token-usage.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(usage_lines), 20)

    def test_heartbeat_lifecycle_detects_stale_agents(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--role",
                "Strategy",
                "--task-id",
                "TASK-1",
                "--objective",
                "Resolve the next implementation boundary",
                "--scope",
                "docs/master-agent",
                "--at",
                "2026-06-01T00:00:00+00:00",
            ]
        )
        run_cmd(
            [
                TOOL,
                "heartbeat",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--state",
                "active",
                "--current",
                "strategy-packet.md",
                "--last-action",
                "Read authority docs",
                "--next-action",
                "Draft recommendation",
                "--scope-status",
                "yes",
                "--confidence",
                "high",
                "--at",
                "2026-06-01T00:10:00+00:00",
            ]
        )

        healthy = run_cmd(
            [
                TOOL,
                "check-heartbeats",
                "--state-dir",
                self.state_dir,
                "--stale-minutes",
                "30",
                "--now",
                "2026-06-01T00:30:00+00:00",
            ]
        )
        self.assertIn("No stale agents", healthy.stdout)

        stale = run_cmd(
            [
                TOOL,
                "check-heartbeats",
                "--state-dir",
                self.state_dir,
                "--stale-minutes",
                "30",
                "--now",
                "2026-06-01T00:50:01+00:00",
            ],
            check=False,
        )
        self.assertEqual(stale.returncode, 1)
        self.assertIn("strategy-1", stale.stdout)
        self.assertIn("stale", stale.stdout.lower())

        running_agents = (self.state_dir / "running-agents.md").read_text(encoding="utf-8")
        self.assertIn("strategy-1", running_agents)
        self.assertIn("2026-06-01T00:10:00+00:00", running_agents)
        self.assertIn("## Token Controls", running_agents)
        self.assertIn("Active token strategy", running_agents)

        post_register_validation = run_cmd(
            [TOOL, "validate", "--state-dir", self.state_dir]
        )
        self.assertIn("State pack is valid", post_register_validation.stdout)

    def test_watch_heartbeats_can_run_one_poll_cycle(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-WATCH")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-2",
                "--objective",
                "Implement a scoped work order",
                "--scope",
                "src/module",
                "--plan-id",
                "PLAN-WATCH",
                "--at",
                "2026-06-01T00:00:00+00:00",
            ]
        )

        watch = run_cmd(
            [
                TOOL,
                "watch-heartbeats",
                "--state-dir",
                self.state_dir,
                "--stale-minutes",
                "30",
                "--poll-seconds",
                "0",
                "--max-checks",
                "1",
                "--now",
                "2026-06-01T00:10:00+00:00",
            ]
        )
        self.assertIn("Heartbeat watch check 1", watch.stdout)
        self.assertIn("No stale agents", watch.stdout)

    def test_token_budget_lifecycle_warns_and_blocks(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "set-budget",
                "--state-dir",
                self.state_dir,
                "--project-budget",
                "1000",
                "--warning-percent",
                "80",
                "--hard-percent",
                "100",
            ]
        )
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--role",
                "Strategy",
                "--task-id",
                "TASK-3",
                "--objective",
                "Draft a bounded recommendation",
                "--scope",
                "docs/master-agent",
                "--token-budget",
                "900",
                "--max-heartbeats",
                "2",
            ]
        )

        run_cmd(
            [
                TOOL,
                "record-usage",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--tokens-used",
                "100",
                "--note",
                "initial strategy pass",
            ]
        )
        healthy = run_cmd([TOOL, "check-budget", "--state-dir", self.state_dir])
        self.assertIn("within budget", healthy.stdout.lower())

        run_cmd(
            [
                TOOL,
                "record-usage",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--tokens-used",
                "750",
                "--note",
                "follow-up discussion",
            ]
        )
        warning = run_cmd(
            [TOOL, "check-budget", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(warning.returncode, 1)
        self.assertIn("warning", warning.stdout.lower())

        run_cmd(
            [
                TOOL,
                "record-usage",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--tokens-used",
                "200",
                "--note",
                "over budget",
            ]
        )
        blocked = run_cmd(
            [TOOL, "check-budget", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("hard limit", blocked.stdout.lower())

        budget_status = run_cmd([TOOL, "budget-status", "--state-dir", self.state_dir])
        self.assertIn("project used: 1050 / 1000", budget_status.stdout.lower())

    def test_usage_source_is_recorded(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        run_cmd(
            [
                TOOL,
                "record-usage",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--tokens-used",
                "120",
                "--source",
                "measured",
                "--confidence",
                "high",
                "--note",
                "provider usage report",
            ]
        )

        usage_line = (
            self.state_dir / "state" / "token-usage.jsonl"
        ).read_text(encoding="utf-8").splitlines()[-1]
        usage = json.loads(usage_line)
        self.assertEqual(usage["source"], "measured")
        self.assertEqual(usage["confidence"], "high")

        budget = json.loads(
            (self.state_dir / "state" / "budget.json").read_text(encoding="utf-8")
        )
        self.assertEqual(budget["usage_by_source"]["measured"], 120)
        self.assertEqual(
            budget["agents"]["strategy-1"]["usage_by_confidence"]["high"],
            120,
        )

    def test_unknown_usage_blocks_large_continuation(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--role",
                "Strategy",
                "--task-id",
                "TASK-UNKNOWN-USAGE",
                "--objective",
                "Continue a large strategy pass",
                "--scope",
                "docs/master-agent",
            ]
        )

        recommendation = run_cmd(
            [
                TOOL,
                "recommend-token-strategy",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--expected-tokens",
                "6000",
                "--task-complexity",
                "high",
            ],
            check=False,
        )

        self.assertEqual(recommendation.returncode, 1)
        self.assertIn("Action: compress-and-narrow", recommendation.stdout)
        self.assertIn("usage is unknown", recommendation.stdout.lower())
        self.assertIn("usage report", recommendation.stdout.lower())

    def test_estimated_usage_is_marked_separately_from_measured_usage(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        run_cmd(
            [
                TOOL,
                "record-usage",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--tokens-used",
                "100",
                "--source",
                "measured",
                "--confidence",
                "high",
            ]
        )
        run_cmd(
            [
                TOOL,
                "record-usage",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--tokens-used",
                "40",
                "--source",
                "estimated",
                "--confidence",
                "medium",
            ]
        )

        budget = json.loads(
            (self.state_dir / "state" / "budget.json").read_text(encoding="utf-8")
        )
        agent_budget = budget["agents"]["strategy-1"]
        self.assertEqual(budget["project_used"], 140)
        self.assertEqual(agent_budget["tokens_used"], 140)
        self.assertEqual(budget["usage_by_source"]["measured"], 100)
        self.assertEqual(budget["usage_by_source"]["estimated"], 40)
        self.assertEqual(agent_budget["usage_by_source"]["measured"], 100)
        self.assertEqual(agent_budget["usage_by_source"]["estimated"], 40)

    def test_heartbeat_cap_is_checked_as_budget_control(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-REVIEW")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "review-1",
                "--role",
                "Review",
                "--task-id",
                "TASK-4",
                "--objective",
                "Review evidence",
                "--scope",
                "docs/master-agent",
                "--plan-id",
                "PLAN-REVIEW",
                "--max-heartbeats",
                "1",
            ]
        )
        for index in range(2):
            run_cmd(
                [
                    TOOL,
                    "heartbeat",
                    "--state-dir",
                    self.state_dir,
                    "--agent-id",
                    "review-1",
                    "--state",
                    "active",
                    "--current",
                    "review-verdict.md",
                    "--last-action",
                    f"heartbeat {index}",
                    "--next-action",
                    "continue review",
                    "--scope-status",
                    "yes",
                    "--confidence",
                    "medium",
                ]
            )

        budget_check = run_cmd(
            [TOOL, "check-budget", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(budget_check.returncode, 1)
        self.assertIn("heartbeat cap", budget_check.stdout.lower())

    def test_recommend_token_strategy_changes_with_budget_pressure(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "set-budget",
                "--state-dir",
                self.state_dir,
                "--project-budget",
                "1000",
                "--warning-percent",
                "80",
                "--hard-percent",
                "100",
            ]
        )
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--role",
                "Strategy",
                "--task-id",
                "TASK-5",
                "--objective",
                "Produce a compact decision packet",
                "--scope",
                "docs/master-agent",
                "--token-budget",
                "1200",
                "--max-heartbeats",
                "3",
            ]
        )

        continue_plan = run_cmd(
            [
                TOOL,
                "recommend-token-strategy",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--expected-tokens",
                "100",
                "--task-complexity",
                "medium",
            ]
        )
        self.assertIn("Action: continue", continue_plan.stdout)
        self.assertIn("context tiers", continue_plan.stdout.lower())

        projected_warning = run_cmd(
            [
                TOOL,
                "recommend-token-strategy",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--expected-tokens",
                "850",
                "--task-complexity",
                "medium",
            ],
            check=False,
        )
        self.assertEqual(projected_warning.returncode, 1)
        self.assertIn("projected to reach warning threshold", projected_warning.stdout)

        run_cmd(
            [
                TOOL,
                "record-usage",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--tokens-used",
                "850",
            ]
        )
        compress_plan = run_cmd(
            [
                TOOL,
                "recommend-token-strategy",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--expected-tokens",
                "100",
                "--task-complexity",
                "medium",
            ],
            check=False,
        )
        self.assertEqual(compress_plan.returncode, 1)
        self.assertIn("Action: compress-and-narrow", compress_plan.stdout)
        self.assertIn("Sub-agent autonomous strategy", compress_plan.stdout)

        stop_plan = run_cmd(
            [
                TOOL,
                "recommend-token-strategy",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--expected-tokens",
                "300",
                "--task-complexity",
                "high",
            ],
            check=False,
        )
        self.assertEqual(stop_plan.returncode, 2)
        self.assertIn("Action: stop-or-request-budget", stop_plan.stdout)

    def test_token_strategy_template_is_present(self):
        strategy = (ROOT / "assets" / "templates" / "token-strategy.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Optimization Objective", strategy)
        self.assertIn("Master Constraints", strategy)
        self.assertIn("Sub-Agent Autonomous Strategies", strategy)
        self.assertIn("Context Tiers", strategy)
        self.assertIn("Compression Triggers", strategy)
        self.assertIn("Research Boundary", strategy)

    def test_token_strategy_is_documented_across_pack(self):
        root_skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        reference = (ROOT / "references" / "master-agent-system.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("recommend-token-strategy", root_skill)
        self.assertIn("token-strategy.md", root_skill)
        self.assertIn("recommend-token-strategy", reference)
        self.assertIn("Master constraints", reference)
        self.assertIn("Sub-agent autonomous strategies", reference)

        for folder in [
            "master-strategy-agent",
            "master-coding-agent",
            "master-review-agent",
            "master-policy-review-agent",
        ]:
            skill_text = (
                ROOT / "role-skills" / folder / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertIn("token", skill_text.lower())
            self.assertIn("strategy", skill_text.lower())

    def test_dynamic_role_lifecycle_controls_registration(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        role_catalog = (self.state_dir / "role-catalog.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("# Role Catalog", role_catalog)
        self.assertIn("Role Creation Rules", role_catalog)

        roles = json.loads(
            (self.state_dir / "state" / "roles.json").read_text(encoding="utf-8")
        )
        self.assertIn("Strategy", roles)
        self.assertIn("Coding", roles)

        undefined = run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "research-undefined",
                "--role",
                "Domain Research",
                "--task-id",
                "TASK-ROLE",
                "--objective",
                "Gather project evidence",
                "--scope",
                "docs/research",
            ],
            check=False,
        )
        self.assertEqual(undefined.returncode, 1)
        self.assertIn("undefined role", undefined.stderr.lower())

        define = run_cmd(
            [
                TOOL,
                "define-role",
                "--state-dir",
                self.state_dir,
                "--role",
                "Domain Research",
                "--purpose",
                "Collect bounded project evidence before strategy decisions",
                "--allowed-work",
                "Read authority docs, inspect artifacts, and return evidence packets",
                "--forbidden-work",
                "Production implementation or final product decisions",
                "--return-packet",
                "role-receipt.md",
                "--scope",
                "docs/research",
                "--role-skill",
                "master-domain-research-agent",
                "--token-budget",
                "6000",
                "--max-heartbeats",
                "3",
                "--approval",
                "accepted role-proposal.md",
                "--activate",
            ]
        )
        self.assertIn("Defined role Domain Research", define.stdout)

        list_roles = run_cmd([TOOL, "list-roles", "--state-dir", self.state_dir])
        self.assertIn("Domain Research", list_roles.stdout)
        self.assertIn("active", list_roles.stdout)

        registered = run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "research-1",
                "--role",
                "Domain Research",
                "--task-id",
                "TASK-ROLE",
                "--objective",
                "Gather project evidence",
                "--scope",
                "docs/research",
            ]
        )
        self.assertIn("Registered agent research-1", registered.stdout)

        budget_status = run_cmd([TOOL, "budget-status", "--state-dir", self.state_dir])
        self.assertIn("research-1: tokens=0 / 6000", budget_status.stdout)
        self.assertIn("heartbeats=0 / 3", budget_status.stdout)

        run_cmd(
            [
                TOOL,
                "deactivate-role",
                "--state-dir",
                self.state_dir,
                "--role",
                "Domain Research",
                "--reason",
                "Evidence pass complete",
            ]
        )
        inactive = run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "research-2",
                "--role",
                "Domain Research",
                "--task-id",
                "TASK-ROLE-2",
                "--objective",
                "Gather more evidence",
                "--scope",
                "docs/research",
            ],
            check=False,
        )
        self.assertEqual(inactive.returncode, 1)
        self.assertIn("inactive role", inactive.stderr.lower())

        run_cmd(
            [
                TOOL,
                "activate-role",
                "--state-dir",
                self.state_dir,
                "--role",
                "Domain Research",
                "--reason",
                "New evidence pass approved",
                "--approval",
                "accepted role-proposal.md",
            ]
        )
        active_again = run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "research-2",
                "--role",
                "Domain Research",
                "--task-id",
                "TASK-ROLE-2",
                "--objective",
                "Gather more evidence",
                "--scope",
                "docs/research",
            ]
        )
        self.assertIn("Registered agent research-2", active_again.stdout)

        valid = run_cmd([TOOL, "validate", "--state-dir", self.state_dir])
        self.assertIn("State pack is valid", valid.stdout)

    def test_custom_role_activation_requires_approval_scope_and_limits(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        unapproved = run_cmd(
            [
                TOOL,
                "define-role",
                "--state-dir",
                self.state_dir,
                "--role",
                "Domain Research",
                "--purpose",
                "Collect bounded project evidence",
                "--allowed-work",
                "Read docs and artifacts",
                "--forbidden-work",
                "Production implementation",
                "--return-packet",
                "role-receipt.md",
                "--scope",
                "docs/research",
                "--token-budget",
                "6000",
                "--max-heartbeats",
                "3",
                "--activate",
            ],
            check=False,
        )
        self.assertEqual(unapproved.returncode, 1)
        self.assertIn("activation approval", unapproved.stderr.lower())

        missing_limits = run_cmd(
            [
                TOOL,
                "define-role",
                "--state-dir",
                self.state_dir,
                "--role",
                "Domain Research",
                "--purpose",
                "Collect bounded project evidence",
                "--allowed-work",
                "Read docs and artifacts",
                "--forbidden-work",
                "Production implementation",
                "--return-packet",
                "role-receipt.md",
                "--approval",
                "accepted role-proposal.md",
                "--activate",
            ],
            check=False,
        )
        self.assertEqual(missing_limits.returncode, 1)
        self.assertIn("scope", missing_limits.stderr.lower())
        self.assertIn("token budget", missing_limits.stderr.lower())
        self.assertIn("heartbeat", missing_limits.stderr.lower())

        proposed = run_cmd(
            [
                TOOL,
                "define-role",
                "--state-dir",
                self.state_dir,
                "--role",
                "Domain Research",
                "--purpose",
                "Collect bounded project evidence",
                "--allowed-work",
                "Read docs and artifacts",
                "--forbidden-work",
                "Production implementation",
                "--return-packet",
                "role-receipt.md",
                "--scope",
                "docs/research",
                "--token-budget",
                "6000",
                "--max-heartbeats",
                "3",
            ]
        )
        self.assertIn("Defined role Domain Research (proposed)", proposed.stdout)

        activate_without_approval = run_cmd(
            [
                TOOL,
                "activate-role",
                "--state-dir",
                self.state_dir,
                "--role",
                "Domain Research",
            ],
            check=False,
        )
        self.assertEqual(activate_without_approval.returncode, 1)
        self.assertIn("activation approval", activate_without_approval.stderr.lower())

        activated = run_cmd(
            [
                TOOL,
                "activate-role",
                "--state-dir",
                self.state_dir,
                "--role",
                "Domain Research",
                "--approval",
                "accepted role-proposal.md",
            ]
        )
        self.assertIn("Activated role Domain Research", activated.stdout)

    def test_validation_rejects_agents_with_inactive_or_missing_roles(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "define-role",
                "--state-dir",
                self.state_dir,
                "--role",
                "Domain Research",
                "--purpose",
                "Collect bounded project evidence",
                "--allowed-work",
                "Read docs and artifacts",
                "--forbidden-work",
                "Production implementation",
                "--return-packet",
                "role-receipt.md",
            ]
        )

        agents_path = self.state_dir / "state" / "agents.json"
        agents_path.write_text(
            json.dumps(
                {
                    "research-1": {
                        "role": "Domain Research",
                        "task_id": "TASK-ROLE",
                        "status": "active",
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        inactive = run_cmd(
            [TOOL, "validate", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(inactive.returncode, 1)
        self.assertIn("inactive role", inactive.stderr.lower())

        agents_path.write_text(
            json.dumps(
                {
                    "unknown-1": {
                        "role": "Unknown Role",
                        "task_id": "TASK-ROLE",
                        "status": "active",
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        missing = run_cmd(
            [TOOL, "validate", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("undefined role", missing.stderr.lower())

    def test_validate_rejects_active_role_without_required_contract(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        roles_path = self.state_dir / "state" / "roles.json"
        roles = json.loads(roles_path.read_text(encoding="utf-8"))
        roles["Broken Role"] = {
            "status": "active",
            "role_type": "custom",
            "purpose": "Incomplete role",
            "allowed_work": "",
            "forbidden_work": "",
            "return_packet": "",
            "token_budget": -1,
        }
        roles_path.write_text(json.dumps(roles, indent=2) + "\n", encoding="utf-8")

        result = run_cmd(
            [TOOL, "validate", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Broken Role", result.stderr)
        self.assertIn("missing required contract field", result.stderr)
        self.assertIn("deactivation_condition", result.stderr)
        self.assertIn("scope", result.stderr)
        self.assertIn("max_heartbeats", result.stderr)
        self.assertIn("token_budget must be positive", result.stderr)

    def test_validate_rejects_work_order_without_active_role(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        packet_dir = self.state_dir / "packets"
        packet_dir.mkdir(parents=True, exist_ok=True)
        (packet_dir / "bad-work-order.md").write_text(
            "# Work Order\n\n## Objective\n\n- Assigned role: Missing Role\n",
            encoding="utf-8",
        )

        result = run_cmd(
            [TOOL, "validate", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("work order uses undefined role", result.stderr.lower())

    def test_validate_rejects_strategy_sync_plan_mismatch(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        packet = self.tmp / "strategy-packet.md"
        write_valid_strategy_packet(packet, "PLAN-CURRENT")
        run_cmd(
            [
                TOOL,
                "accept-strategy",
                "--state-dir",
                self.state_dir,
                "--packet",
                packet,
                "--plan-id",
                "PLAN-CURRENT",
                "--summary",
                "Current plan",
            ]
        )
        agents_path = self.state_dir / "state" / "agents.json"
        agents = {
            "coding-1": {
                "role": "Coding",
                "task_id": "TASK-MISMATCH",
                "status": "active",
                "plan_id": "PLAN-OLD",
            }
        }
        agents_path.write_text(json.dumps(agents, indent=2) + "\n", encoding="utf-8")

        result = run_cmd(
            [TOOL, "validate", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("current strategy plan", result.stderr.lower())

    def test_validate_rejects_safety_envelope_missing_for_autonomous_roles(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        (self.state_dir / "safety-envelope.md").unlink()

        result = run_cmd(
            [TOOL, "validate", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing file: safety-envelope.md", result.stderr)

    def test_upgrade_state_adds_missing_new_templates_without_overwriting_filled_files(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        ledger = self.state_dir / "master-ledger.md"
        ledger.write_text("# Master Ledger\n\nKEEP EXISTING LEDGER\n", encoding="utf-8")
        (self.state_dir / "safety-envelope.md").unlink()

        result = run_cmd([TOOL, "upgrade-state", "--state-dir", self.state_dir])
        self.assertIn("created:", result.stdout)
        self.assertTrue((self.state_dir / "safety-envelope.md").exists())
        self.assertEqual(
            ledger.read_text(encoding="utf-8"),
            "# Master Ledger\n\nKEEP EXISTING LEDGER\n",
        )

    def test_upgrade_state_initializes_missing_json_state(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        (self.state_dir / "state" / "roles.json").unlink()
        (self.state_dir / "state" / "anomalies.jsonl").unlink()

        result = run_cmd([TOOL, "upgrade-state", "--state-dir", self.state_dir])
        self.assertIn("state initialized", result.stdout.lower())
        self.assertTrue((self.state_dir / "state" / "roles.json").exists())
        self.assertTrue((self.state_dir / "state" / "anomalies.jsonl").exists())

    def test_upgrade_state_reports_manual_conflicts(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        safety = self.state_dir / "safety-envelope.md"
        safety.write_text("# Old Safety Notes\n\nCustom local policy\n", encoding="utf-8")

        result = run_cmd(
            [TOOL, "upgrade-state", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("conflicts:", result.stdout)
        self.assertIn("safety-envelope.md", result.stdout)
        self.assertIn("# Old Safety Notes", safety.read_text(encoding="utf-8"))

    def test_supervisor_templates_are_created(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        supervisor = (self.state_dir / "runtime-supervisor.md").read_text(
            encoding="utf-8"
        )
        status = (self.state_dir / "runtime-status.md").read_text(encoding="utf-8")
        self.assertIn("## Operating Mode", supervisor)
        self.assertIn("## Recovery Policy", supervisor)
        self.assertIn("## Supervisor State", status)
        self.assertTrue((self.state_dir / "state" / "runtime.json").exists())

    def test_supervise_runs_one_cycle_and_updates_runtime_status(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-SUPERVISE")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "review-1",
                "--role",
                "Review",
                "--task-id",
                "TASK-SUPERVISE",
                "--objective",
                "Review evidence",
                "--scope",
                "docs/master-agent",
                "--plan-id",
                "PLAN-SUPERVISE",
            ]
        )

        result = run_cmd(
            [
                TOOL,
                "supervise",
                "--state-dir",
                self.state_dir,
                "--poll-seconds",
                "0",
                "--max-cycles",
                "1",
                "--now",
                "2026-06-01T12:00:00+00:00",
            ]
        )
        self.assertIn("Supervisor cycle 1 complete", result.stdout)
        runtime_status = (self.state_dir / "runtime-status.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("## Last Check", runtime_status)
        self.assertIn("review-1", runtime_status)
        runtime = json.loads(
            (self.state_dir / "state" / "runtime.json").read_text(encoding="utf-8")
        )
        self.assertEqual(runtime["supervisor_state"], "idle")
        self.assertEqual(runtime["last_check_at"], "2026-06-01T12:00:00+00:00")

    def test_supervise_stops_after_repeated_same_remediation_limit(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-LOOP")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-LOOP",
                "--objective",
                "Implement work",
                "--scope",
                "docs/master-agent",
                "--plan-id",
                "PLAN-LOOP",
            ]
        )
        for index in range(3):
            run_cmd(
                [
                    TOOL,
                    "heartbeat",
                    "--state-dir",
                    self.state_dir,
                    "--agent-id",
                    "coding-1",
                    "--state",
                    "active",
                    "--current",
                    "work-order.md",
                    "--last-action",
                    f"attempt {index}",
                    "--next-action",
                    "continue same patch",
                    "--scope-status",
                    "yes",
                    "--confidence",
                    "medium",
                ]
            )
        runtime_path = self.state_dir / "state" / "runtime.json"
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        runtime["last_recoveries"] = {"coding-1:spawn-successor": 2}
        runtime_path.write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")

        run_cmd(
            [
                TOOL,
                "supervise",
                "--state-dir",
                self.state_dir,
                "--poll-seconds",
                "0",
                "--max-cycles",
                "1",
            ]
        )
        agents = json.loads(
            (self.state_dir / "state" / "agents.json").read_text(encoding="utf-8")
        )
        self.assertEqual(agents["coding-1"]["status"], "stopping")
        status = (self.state_dir / "runtime-status.md").read_text(encoding="utf-8")
        self.assertIn("coding-1 stopped after repeated remediation", status)

    def test_supervise_respects_quiet_period_for_noncritical_work(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-QUIET")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-QUIET",
                "--objective",
                "Implement work",
                "--scope",
                "docs/master-agent",
                "--plan-id",
                "PLAN-QUIET",
            ]
        )
        run_cmd(
            [
                TOOL,
                "heartbeat",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--state",
                "active",
                "--current",
                "work-order.md",
                "--last-action",
                "lost scope",
                "--next-action",
                "continue",
                "--scope-status",
                "unsure",
                "--confidence",
                "low",
            ]
        )

        run_cmd(
            [
                TOOL,
                "supervise",
                "--state-dir",
                self.state_dir,
                "--poll-seconds",
                "0",
                "--max-cycles",
                "1",
                "--now",
                "2026-06-01T12:00:00+00:00",
                "--quiet-start",
                "00:00",
                "--quiet-end",
                "23:59",
            ]
        )
        status = (self.state_dir / "runtime-status.md").read_text(encoding="utf-8")
        self.assertIn("Deferred actions", status)
        self.assertIn("coding-1", status)

    def test_supervise_escalates_unrecoverable_safety_breach(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-BREACH")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-BREACH",
                "--objective",
                "Implement work",
                "--scope",
                "docs/master-agent",
                "--plan-id",
                "PLAN-BREACH",
            ]
        )
        append_jsonl_locked(
            self.state_dir / "state" / "anomalies.jsonl",
            {
                "time": "2026-06-01T12:00:00+00:00",
                "agent_id": "coding-1",
                "type": "safety-breach",
                "severity": "critical",
                "evidence": "attempted forbidden production write",
                "recommended_action": "stop agent",
            },
        )

        result = run_cmd(
            [
                TOOL,
                "supervise",
                "--state-dir",
                self.state_dir,
                "--poll-seconds",
                "0",
                "--max-cycles",
                "1",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        agents = json.loads(
            (self.state_dir / "state" / "agents.json").read_text(encoding="utf-8")
        )
        self.assertEqual(agents["coding-1"]["status"], "stopping")
        self.assertIn("critical safety breach", result.stdout.lower())

    def test_runtime_deployment_template_is_created(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        deployment = (self.state_dir / "runtime-deployment.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("## Windows Startup", deployment)
        self.assertIn("## Process Identity", deployment)
        self.assertIn("## Crash Recovery", deployment)

    def test_supervisor_start_records_pid_and_lock(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        result = run_cmd(
            [
                TOOL,
                "supervisor-start",
                "--state-dir",
                self.state_dir,
                "--poll-seconds",
                "30",
                "--now",
                "2026-06-01T12:00:00+00:00",
            ]
        )
        self.assertIn("Supervisor start recorded", result.stdout)
        runtime = json.loads(
            (self.state_dir / "state" / "runtime.json").read_text(encoding="utf-8")
        )
        self.assertEqual(runtime["supervisor_state"], "running")
        self.assertEqual(runtime["poll_seconds"], 30)
        self.assertTrue(Path(runtime["lock_path"]).exists())
        self.assertGreater(int(runtime["pid"]), 0)

    def test_supervisor_status_reports_running_stale_or_stopped(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        lock_path = self.state_dir / "state" / "supervisor.lock"
        lock_path.write_text(f"pid={os.getpid()}\n", encoding="utf-8")
        atomic_write_json(
            self.state_dir / "state" / "runtime.json",
            {
                "supervisor_state": "running",
                "stop_requested": False,
                "pid": os.getpid(),
                "lock_path": str(lock_path),
                "started_at": "2026-06-01T12:00:00+00:00",
                "last_check_at": "2026-06-01T12:00:00+00:00",
            },
        )
        running = run_cmd(
            [
                TOOL,
                "supervisor-status",
                "--state-dir",
                self.state_dir,
                "--now",
                "2026-06-01T12:01:00+00:00",
                "--stale-seconds",
                "600",
            ]
        )
        self.assertIn("Supervisor status: running", running.stdout)

        stale = run_cmd(
            [
                TOOL,
                "supervisor-status",
                "--state-dir",
                self.state_dir,
                "--now",
                "2026-06-01T13:00:00+00:00",
                "--stale-seconds",
                "60",
            ],
            check=False,
        )
        self.assertEqual(stale.returncode, 1)
        self.assertIn("Supervisor status: stale", stale.stdout)

        run_cmd([TOOL, "supervisor-stop", "--state-dir", self.state_dir])
        stopped = run_cmd(
            [TOOL, "supervisor-status", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(stopped.returncode, 1)
        self.assertIn("Supervisor status: stop-requested", stopped.stdout)

    def test_supervisor_status_reports_dead_pid_as_not_running(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        lock_path = self.state_dir / "state" / "supervisor.lock"
        lock_path.write_text("pid=99999999\n", encoding="utf-8")
        atomic_write_json(
            self.state_dir / "state" / "runtime.json",
            {
                "supervisor_state": "running",
                "stop_requested": False,
                "pid": 99999999,
                "lock_path": str(lock_path),
                "started_at": "2026-06-01T12:00:00+00:00",
                "last_check_at": "2026-06-01T12:00:00+00:00",
            },
        )

        dead = run_cmd(
            [
                TOOL,
                "supervisor-status",
                "--state-dir",
                self.state_dir,
                "--now",
                "2026-06-01T12:01:00+00:00",
                "--stale-seconds",
                "600",
            ],
            check=False,
        )
        self.assertEqual(dead.returncode, 1)
        self.assertIn("Supervisor status: dead", dead.stdout)

    def test_supervisor_status_rejects_mismatched_lock_identity(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        lock_path = self.state_dir / "state" / "supervisor.lock"
        lock_path.write_text(
            f"pid={os.getpid()}\nsupervisor_id=other-owner\n",
            encoding="utf-8",
        )
        atomic_write_json(
            self.state_dir / "state" / "runtime.json",
            {
                "supervisor_state": "running",
                "stop_requested": False,
                "pid": os.getpid(),
                "supervisor_id": "expected-owner",
                "lock_path": str(lock_path),
                "started_at": "2026-06-01T12:00:00+00:00",
                "last_check_at": "2026-06-01T12:00:00+00:00",
            },
        )

        status = run_cmd(
            [
                TOOL,
                "supervisor-status",
                "--state-dir",
                self.state_dir,
                "--now",
                "2026-06-01T12:01:00+00:00",
            ],
            check=False,
        )
        self.assertEqual(status.returncode, 1)
        self.assertIn("Supervisor status: identity-mismatch", status.stdout)

    def test_spawned_supervisor_reports_running_between_cycles(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        try:
            run_cmd(
                [
                    TOOL,
                    "supervisor-start",
                    "--state-dir",
                    self.state_dir,
                    "--poll-seconds",
                    "1",
                    "--spawn",
                ]
            )
            time.sleep(2.5)
            status = run_cmd(
                [
                    TOOL,
                    "supervisor-status",
                    "--state-dir",
                    self.state_dir,
                    "--stale-seconds",
                    "10",
                ]
            )
            self.assertIn("Supervisor status: running", status.stdout)
        finally:
            run_cmd(
                [TOOL, "supervisor-stop", "--state-dir", self.state_dir],
                check=False,
            )
            runtime_path = self.state_dir / "state" / "runtime.json"
            if runtime_path.exists():
                runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
                pid = runtime.get("pid")
                if pid:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F"],
                        text=True,
                        capture_output=True,
                        check=False,
                    )

    def test_supervisor_stop_sets_stop_requested(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd([TOOL, "supervisor-start", "--state-dir", self.state_dir])

        result = run_cmd([TOOL, "supervisor-stop", "--state-dir", self.state_dir])
        self.assertIn("Supervisor stop requested", result.stdout)
        runtime = json.loads(
            (self.state_dir / "state" / "runtime.json").read_text(encoding="utf-8")
        )
        self.assertTrue(runtime["stop_requested"])

    def test_supervisor_recovers_after_crash_marker(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd([TOOL, "supervisor-start", "--state-dir", self.state_dir])
        runtime_path = self.state_dir / "state" / "runtime.json"
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        runtime["supervisor_state"] = "crashed"
        runtime["crash_marker"] = "simulated crash"
        runtime_path.write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")

        result = run_cmd([TOOL, "supervisor-recover", "--state-dir", self.state_dir])
        self.assertIn("Supervisor recovered", result.stdout)
        recovered = json.loads(runtime_path.read_text(encoding="utf-8"))
        self.assertEqual(recovered["supervisor_state"], "idle")
        self.assertFalse(Path(runtime["lock_path"]).exists())

    def test_supervisor_recover_refuses_live_process_without_force(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        lock_path = self.state_dir / "state" / "supervisor.lock"
        lock_path.write_text(f"pid={os.getpid()}\n", encoding="utf-8")
        runtime_path = self.state_dir / "state" / "runtime.json"
        atomic_write_json(
            runtime_path,
            {
                "supervisor_state": "running",
                "stop_requested": False,
                "pid": os.getpid(),
                "lock_path": str(lock_path),
                "started_at": "2026-06-01T12:00:00+00:00",
                "last_check_at": "2026-06-01T12:00:00+00:00",
            },
        )

        refused = run_cmd(
            [TOOL, "supervisor-recover", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("Refusing", refused.stderr)
        self.assertTrue(lock_path.exists())

        forced = run_cmd(
            [TOOL, "supervisor-recover", "--state-dir", self.state_dir, "--force"]
        )
        self.assertIn("Supervisor recovered", forced.stdout)
        self.assertFalse(lock_path.exists())

    def test_session_control_template_is_created(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        session_control = (self.state_dir / "session-control.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("## Provider Boundary", session_control)
        self.assertIn("## Session Lifecycle", session_control)
        self.assertTrue((self.state_dir / "state" / "session-control.jsonl").exists())

    def test_session_create_records_provider_session(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")

        result = run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--role",
                "Strategy",
                "--context-packet",
                context,
                "--provider",
                "file",
            ]
        )
        self.assertIn("Created session", result.stdout)
        events = (
            self.state_dir / "state" / "session-control.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        event = json.loads(events[-1])
        self.assertEqual(event["event"], "session-created")
        self.assertEqual(event["agent_id"], "strategy-1")
        self.assertTrue(Path(event["provider_session_path"]).exists())

    def test_coding_session_create_requires_registered_validated_agent(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-SESSION")
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")

        unregistered = run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--context-packet",
                context,
                "--provider",
                "file",
            ],
            check=False,
        )
        self.assertEqual(unregistered.returncode, 1)
        self.assertIn("requires registered agent", unregistered.stderr)

        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-SESSION",
                "--objective",
                "Launch only after registration",
                "--scope",
                "src/module",
                "--plan-id",
                "PLAN-SESSION",
            ]
        )
        created = run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--context-packet",
                context,
                "--provider",
                "file",
            ]
        )
        self.assertIn("Created session file:coding-1", created.stdout)

    def test_session_create_live_provider_requires_provider_command(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")

        result = run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-live",
                "--role",
                "Strategy",
                "--context-packet",
                context,
                "--provider",
                "codex",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Provider command is required", result.stderr)
        self.assertEqual(
            "",
            (self.state_dir / "state" / "session-control.jsonl").read_text(
                encoding="utf-8"
            ),
        )

    def test_session_create_live_provider_records_confirmed_session(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")
        provider_state = self.tmp / "provider-session.json"
        provider_script = self.tmp / "provider.py"
        provider_script.write_text(
            "\n".join(
                [
                    "import json, sys",
                    "payload = json.load(sys.stdin)",
                    f"path = {str(provider_state)!r}",
                    "session = {",
                    "    'provider_session_id': 'codex-session-1',",
                    "    'status': 'active',",
                    "    'provider_session_path': path,",
                    "    'agent_id': payload['agent_id'],",
                    "    'role': payload['role'],",
                    "    'context_packet': payload['context_packet'],",
                    "    'messages': [{'sender': 'provider', 'message': 'ready'}],",
                    "}",
                    "open(path, 'w', encoding='utf-8').write(json.dumps(session) + '\\n')",
                    "json.dump(session, sys.stdout)",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        result = run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-live",
                "--role",
                "Strategy",
                "--context-packet",
                context,
                "--provider",
                "codex",
                "--provider-command",
                f"{PYTHON} {provider_script}",
            ]
        )
        self.assertIn("Created session codex-session-1", result.stdout)
        event = json.loads(
            (self.state_dir / "state" / "session-control.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[-1]
        )
        self.assertEqual(event["provider"], "codex")
        self.assertEqual(event["provider_session_id"], "codex-session-1")
        self.assertEqual(event["provider_session_path"], str(provider_state))
        self.assertEqual(event["status"], "active")
        self.assertTrue(event["provider_confirmed"])

    def test_live_provider_send_read_archive_and_reconcile_use_provider_commands(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")
        provider_state = self.tmp / "provider-live.json"
        provider_script = self.tmp / "provider-live.py"
        write_live_provider_script(provider_script, provider_state)
        provider_command = f"{PYTHON} {provider_script}"

        run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-live",
                "--role",
                "Strategy",
                "--context-packet",
                context,
                "--provider",
                "codex",
                "--provider-command",
                provider_command,
            ]
        )
        run_cmd(
            [
                TOOL,
                "session-send",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-live",
                "--message",
                "Draft a bounded plan",
                "--provider-command",
                provider_command,
            ]
        )
        read = run_cmd(
            [
                TOOL,
                "session-read",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-live",
                "--provider-command",
                provider_command,
            ]
        )
        self.assertIn("ack:Draft a bounded plan", read.stdout)
        reconcile = run_cmd(
            [
                TOOL,
                "session-reconcile",
                "--state-dir",
                self.state_dir,
                "--provider-command",
                provider_command,
            ]
        )
        self.assertIn("No stale sessions", reconcile.stdout)
        run_cmd(
            [
                TOOL,
                "session-archive",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-live",
                "--provider-command",
                provider_command,
            ]
        )

        provider = json.loads(provider_state.read_text(encoding="utf-8"))
        self.assertEqual(
            provider["events"],
            [
                "session-create",
                "session-send",
                "session-read",
                "session-reconcile",
                "session-archive",
            ],
        )
        self.assertEqual(provider["status"], "archived")

    def test_bundled_file_session_provider_supports_provider_command_flow(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")
        provider_state = self.state_dir / "state" / "provider-sessions.json"
        provider_command = (
            f"{PYTHON} {ROOT / 'scripts' / 'file_session_provider.py'} "
            f"--state-file {provider_state}"
        )

        create = run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-live",
                "--role",
                "Strategy",
                "--context-packet",
                context,
                "--provider",
                "codex",
                "--provider-command",
                provider_command,
            ]
        )
        self.assertIn("Created session", create.stdout)

        run_cmd(
            [
                TOOL,
                "session-send",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-live",
                "--message",
                "Return a strategy packet.",
                "--provider-command",
                provider_command,
            ]
        )
        read = run_cmd(
            [
                TOOL,
                "session-read",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-live",
                "--provider-command",
                provider_command,
            ]
        )
        self.assertIn("ack:Return a strategy packet.", read.stdout)

        reconcile = run_cmd(
            [
                TOOL,
                "session-reconcile",
                "--state-dir",
                self.state_dir,
                "--provider-command",
                provider_command,
            ]
        )
        self.assertIn("No stale sessions", reconcile.stdout)

        run_cmd(
            [
                TOOL,
                "session-archive",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-live",
                "--provider-command",
                provider_command,
            ]
        )
        provider = json.loads(provider_state.read_text(encoding="utf-8"))
        self.assertEqual(provider["sessions"]["strategy-live"]["status"], "archived")

    def test_live_provider_operations_fail_without_provider_command(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")
        provider_state = self.tmp / "provider-live.json"
        provider_script = self.tmp / "provider-live.py"
        write_live_provider_script(provider_script, provider_state)
        run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-live",
                "--role",
                "Strategy",
                "--context-packet",
                context,
                "--provider",
                "codex",
                "--provider-command",
                f"{PYTHON} {provider_script}",
            ]
        )

        missing = run_cmd(
            [
                TOOL,
                "session-send",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-live",
                "--message",
                "Draft",
            ],
            check=False,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("Provider command is required", missing.stderr)

    def test_live_provider_command_does_not_use_shell_execution(self):
        source = (ROOT / "scripts" / "master_agent_tool.py").read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)

    def test_session_send_and_read_are_logged(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")
        run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--role",
                "Strategy",
                "--context-packet",
                context,
            ]
        )
        run_cmd(
            [
                TOOL,
                "session-send",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--message",
                "Draft a bounded plan",
            ]
        )
        read = run_cmd(
            [TOOL, "session-read", "--state-dir", self.state_dir, "--agent-id", "strategy-1"]
        )
        self.assertIn("Draft a bounded plan", read.stdout)
        events = [
            json.loads(line)["event"]
            for line in (self.state_dir / "state" / "session-control.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertIn("session-sent", events)
        self.assertIn("session-read", events)

    def test_session_reconcile_marks_missing_session_stale(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")
        run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--role",
                "Strategy",
                "--context-packet",
                context,
            ]
        )
        event = json.loads(
            (self.state_dir / "state" / "session-control.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[-1]
        )
        Path(event["provider_session_path"]).unlink()

        result = run_cmd([TOOL, "session-reconcile", "--state-dir", self.state_dir], check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("stale", result.stdout)

    def test_successor_session_inherits_context_packet(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-SUCCESSOR")
        context = self.tmp / "successor-context.md"
        context.write_text("# Successor Context\n\nInherited state\n", encoding="utf-8")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-2",
                "--role",
                "Coding",
                "--task-id",
                "TASK-SUCCESSOR",
                "--objective",
                "Continue inherited task",
                "--scope",
                "src/module",
                "--plan-id",
                "PLAN-SUCCESSOR",
            ]
        )

        run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-2",
                "--role",
                "Coding",
                "--context-packet",
                context,
                "--predecessor-agent-id",
                "coding-1",
                "--reason",
                "attention-drift",
            ]
        )
        event = json.loads(
            (self.state_dir / "state" / "session-control.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[-1]
        )
        self.assertEqual(event["predecessor_agent_id"], "coding-1")
        self.assertEqual(event["inheritance_reason"], "attention-drift")
        self.assertEqual(Path(event["context_packet"]).read_text(encoding="utf-8"), "# Successor Context\n\nInherited state\n")

    def test_codex_app_confirmation_events_and_reconcile(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")

        create = run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-app",
                "--role",
                "Strategy",
                "--context-packet",
                context,
                "--provider",
                "codex-app",
            ]
        )
        self.assertIn("Requested Codex app session", create.stdout)
        run_cmd(
            [
                TOOL,
                "session-confirm-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-app",
                "--thread-id",
                "thread-123",
            ]
        )
        run_cmd(
            [
                TOOL,
                "session-send",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-app",
                "--message",
                "Return a strategy packet",
            ]
        )
        run_cmd(
            [
                TOOL,
                "session-confirm-send",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-app",
            ]
        )
        run_cmd(
            [
                TOOL,
                "session-read",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-app",
            ]
        )
        run_cmd(
            [
                TOOL,
                "session-confirm-read",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-app",
                "--summary",
                "Strategy packet returned",
                "--turn-count",
                "2",
            ]
        )
        reconcile = run_cmd([TOOL, "session-reconcile", "--state-dir", self.state_dir])
        self.assertIn("No stale sessions", reconcile.stdout)
        run_cmd([TOOL, "session-archive", "--state-dir", self.state_dir, "--agent-id", "strategy-app"])
        run_cmd(
            [
                TOOL,
                "session-confirm-archive",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-app",
            ]
        )

        events = [
            json.loads(line)
            for line in (self.state_dir / "state" / "session-control.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        self.assertIn("session-create-requested", [event["event"] for event in events])
        self.assertIn("session-created", [event["event"] for event in events])
        self.assertIn("session-send-requested", [event["event"] for event in events])
        self.assertIn("session-sent", [event["event"] for event in events])
        self.assertIn("session-read", [event["event"] for event in events])
        self.assertEqual(
            next(event for event in events if event["event"] == "session-created")[
                "provider_session_ref"
            ],
            "codex-app:thread-123",
        )

    def test_codex_app_reconcile_requires_recent_read_confirmation(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")
        run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-app",
                "--role",
                "Strategy",
                "--context-packet",
                context,
                "--provider",
                "codex-app",
            ]
        )
        run_cmd(
            [
                TOOL,
                "session-confirm-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-app",
                "--thread-id",
                "thread-123",
            ]
        )

        reconcile = run_cmd([TOOL, "session-reconcile", "--state-dir", self.state_dir], check=False)
        self.assertEqual(reconcile.returncode, 1)
        self.assertIn("stale", reconcile.stdout)

    def test_codex_app_worktree_lifecycle_binds_session_and_reconciles(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")

        run_cmd(
            [
                TOOL,
                "worktree-plan",
                "--state-dir",
                self.state_dir,
                "--worktree-id",
                "wt-strategy",
                "--provider",
                "codex-app",
                "--base-branch",
                "main",
                "--purpose",
                "Isolated strategy work",
            ]
        )
        run_cmd(
            [
                TOOL,
                "worktree-confirm-create",
                "--state-dir",
                self.state_dir,
                "--worktree-id",
                "wt-strategy",
                "--thread-id",
                "thread-wt-1",
            ]
        )
        run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-wt",
                "--role",
                "Strategy",
                "--context-packet",
                context,
                "--provider",
                "codex-app",
                "--worktree-id",
                "wt-strategy",
            ]
        )
        run_cmd(
            [
                TOOL,
                "session-confirm-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-wt",
                "--thread-id",
                "thread-wt-1",
                "--worktree-id",
                "wt-strategy",
            ]
        )
        run_cmd(
            [
                TOOL,
                "worktree-assign-session",
                "--state-dir",
                self.state_dir,
                "--worktree-id",
                "wt-strategy",
                "--agent-id",
                "strategy-wt",
            ]
        )
        run_cmd(
            [
                TOOL,
                "session-confirm-read",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-wt",
                "--summary",
                "Worktree-bound session read",
                "--turn-count",
                "3",
            ]
        )

        reconcile = run_cmd([TOOL, "worktree-reconcile", "--state-dir", self.state_dir])
        self.assertIn("No stale worktrees", reconcile.stdout)
        run_cmd(
            [
                TOOL,
                "worktree-close",
                "--state-dir",
                self.state_dir,
                "--worktree-id",
                "wt-strategy",
                "--reason",
                "task accepted",
            ]
        )
        run_cmd(
            [
                TOOL,
                "worktree-confirm-close",
                "--state-dir",
                self.state_dir,
                "--worktree-id",
                "wt-strategy",
            ]
        )

        events = [
            json.loads(line)
            for line in (self.state_dir / "state" / "worktrees.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        event_names = [event["event"] for event in events]
        self.assertIn("worktree-planned", event_names)
        self.assertIn("worktree-created", event_names)
        self.assertIn("worktree-session-bound", event_names)
        self.assertIn("worktree-closed", event_names)

    def test_worktree_reconcile_marks_bound_codex_app_without_read_stale(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        context = self.tmp / "context-packet.md"
        context.write_text("# Context Packet\n", encoding="utf-8")
        run_cmd(
            [
                TOOL,
                "worktree-plan",
                "--state-dir",
                self.state_dir,
                "--worktree-id",
                "wt-stale",
                "--base-branch",
                "main",
                "--purpose",
                "Stale detection",
            ]
        )
        run_cmd(
            [
                TOOL,
                "worktree-confirm-create",
                "--state-dir",
                self.state_dir,
                "--worktree-id",
                "wt-stale",
                "--thread-id",
                "thread-stale",
            ]
        )
        run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-stale",
                "--role",
                "Strategy",
                "--context-packet",
                context,
                "--provider",
                "codex-app",
                "--worktree-id",
                "wt-stale",
            ]
        )
        run_cmd(
            [
                TOOL,
                "session-confirm-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-stale",
                "--thread-id",
                "thread-stale",
                "--worktree-id",
                "wt-stale",
            ]
        )
        run_cmd(
            [
                TOOL,
                "worktree-assign-session",
                "--state-dir",
                self.state_dir,
                "--worktree-id",
                "wt-stale",
                "--agent-id",
                "strategy-stale",
            ]
        )

        reconcile = run_cmd(
            [TOOL, "worktree-reconcile", "--state-dir", self.state_dir],
            check=False,
        )
        self.assertEqual(reconcile.returncode, 1)
        self.assertIn("stale worktrees", reconcile.stdout)

    def test_rotate_session_requires_predecessor_state_unless_emergency(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-ROTATE")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-ROTATE",
                "--objective",
                "Finish parser repair",
                "--scope",
                "src/parser",
                "--plan-id",
                "PLAN-ROTATE",
            ]
        )
        strict = run_cmd(
            [
                TOOL,
                "rotate-session",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--successor-agent-id",
                "coding-2",
                "--reason",
                "attention-drift",
            ],
            check=False,
        )
        self.assertEqual(strict.returncode, 2)
        self.assertIn("Strict rotation requires", strict.stderr)

        emergency = run_cmd(
            [
                TOOL,
                "rotate-session",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--successor-agent-id",
                "coding-2",
                "--reason",
                "attention-drift",
                "--emergency-without-predecessor-state",
            ]
        )
        self.assertIn("Rotated session coding-1 -> coding-2", emergency.stdout)

    def test_validate_predecessor_state_rejects_missing_required_fields(self):
        invalid = self.tmp / "bad-predecessor.md"
        invalid.write_text("# Predecessor State Packet\n\n## Objective\n\n- \n", encoding="utf-8")
        result = run_cmd(
            [TOOL, "validate-predecessor-state", "--packet", invalid],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing heading", result.stderr)

    def test_rotate_session_freezes_predecessor_and_launches_successor(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        strategy_packet = self.tmp / "strategy-packet.md"
        write_valid_strategy_packet(strategy_packet, "PLAN-ROTATE")
        run_cmd(
            [
                TOOL,
                "accept-strategy",
                "--state-dir",
                self.state_dir,
                "--packet",
                strategy_packet,
                "--plan-id",
                "PLAN-ROTATE",
                "--summary",
                "Rotate overloaded coding session",
            ]
        )
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-ROTATE",
                "--objective",
                "Finish parser repair",
                "--scope",
                "src/parser",
                "--plan-id",
                "PLAN-ROTATE",
                "--token-budget",
                "8000",
                "--max-heartbeats",
                "4",
            ]
        )
        predecessor_context = self.tmp / "predecessor-context.md"
        predecessor_context.write_text("# Predecessor Context\n", encoding="utf-8")
        run_cmd(
            [
                TOOL,
                "session-create",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--context-packet",
                predecessor_context,
                "--provider",
                "file",
            ]
        )
        for index in range(3):
            run_cmd(
                [
                    TOOL,
                    "heartbeat",
                    "--state-dir",
                    self.state_dir,
                    "--agent-id",
                    "coding-1",
                    "--state",
                    "active",
                    "--current",
                    "src/parser/tokenizer.py",
                    "--last-action",
                    "patched parser state tracking" if index == 2 else f"attempt {index}",
                    "--next-action",
                    "continue patching parser",
                    "--scope-status",
                    "yes",
                    "--confidence",
                    "medium",
                    "--files-changed",
                    "src/parser/tokenizer.py",
                    "--commands",
                    "python -m unittest tests.test_parser",
                    "--risk",
                    "attention drift after long session",
                ]
            )

        predecessor_state = self.tmp / "predecessor-state-packet.md"
        write_predecessor_state_packet(predecessor_state)

        result = run_cmd(
            [
                TOOL,
                "rotate-session",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--successor-agent-id",
                "coding-2",
                "--reason",
                "attention-drift",
                "--provider",
                "file",
                "--predecessor-state-packet",
                predecessor_state,
            ]
        )
        self.assertIn("Rotated session coding-1 -> coding-2", result.stdout)

        save_request = (
            self.state_dir
            / "packets"
            / "session-rotation"
            / "coding-1-save-state-request.md"
        )
        successor_context = (
            self.state_dir
            / "packets"
            / "session-rotation"
            / "coding-1-to-coding-2-context.md"
        )
        self.assertTrue(save_request.exists())
        self.assertTrue(successor_context.exists())
        self.assertIn("Stop current implementation", save_request.read_text(encoding="utf-8"))
        context_text = successor_context.read_text(encoding="utf-8")
        self.assertIn("Predecessor agent id: coding-1", context_text)
        self.assertIn("Successor agent id: coding-2", context_text)
        self.assertIn("Inheritance reason: attention-drift", context_text)
        self.assertIn("Last concrete progress: patched parser state tracking", context_text)
        self.assertIn("Forbidden repeats: continue patching parser", context_text)

        agents = json.loads((self.state_dir / "state" / "agents.json").read_text(encoding="utf-8"))
        self.assertEqual(agents["coding-1"]["status"], "stopping")
        self.assertEqual(agents["coding-1"]["stop_reason"], "rotated: attention-drift")
        self.assertEqual(agents["coding-2"]["role"], "Coding")
        self.assertEqual(agents["coding-2"]["objective"], "Finish parser repair")
        self.assertEqual(agents["coding-2"]["plan_id"], "PLAN-ROTATE")

        events = [
            json.loads(line)
            for line in (self.state_dir / "state" / "session-control.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        self.assertIn("session-sent", [event["event"] for event in events])
        self.assertIn("session-archived", [event["event"] for event in events])
        successor_event = events[-1]
        self.assertEqual(successor_event["event"], "session-created")
        self.assertEqual(successor_event["agent_id"], "coding-2")
        self.assertEqual(successor_event["predecessor_agent_id"], "coding-1")
        self.assertEqual(successor_event["inheritance_reason"], "attention-drift")
        self.assertEqual(
            Path(successor_event["context_packet"]).read_text(encoding="utf-8"),
            successor_context.read_text(encoding="utf-8"),
        )

    def test_supervise_uses_successor_handoff_for_attention_drift_loop(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-LOOP")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-1",
                "--role",
                "Coding",
                "--task-id",
                "TASK-LOOP",
                "--objective",
                "Finish bounded work",
                "--scope",
                "src/module",
                "--plan-id",
                "PLAN-LOOP",
            ]
        )
        for index in range(3):
            run_cmd(
                [
                    TOOL,
                    "heartbeat",
                    "--state-dir",
                    self.state_dir,
                    "--agent-id",
                    "coding-1",
                    "--state",
                    "active",
                    "--current",
                    "src/module/file.py",
                    "--last-action",
                    f"attempt {index}",
                    "--next-action",
                    "continue patching",
                    "--scope-status",
                    "yes",
                    "--confidence",
                    "medium",
                    "--risk",
                    "attention drift",
                ]
            )

        run_cmd([TOOL, "supervise", "--state-dir", self.state_dir, "--max-cycles", "1"])
        runtime = json.loads((self.state_dir / "state" / "runtime.json").read_text(encoding="utf-8"))
        self.assertIn("coding-1:spawn-successor", " ".join(runtime["active_interventions"]))
        self.assertTrue(
            (self.state_dir / "packets" / "remediation" / "coding-1-spawn-successor.md").exists()
        )

    def test_incident_and_alert_templates_are_created(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        incident = (self.state_dir / "incident-log.md").read_text(encoding="utf-8")
        alert = (self.state_dir / "alert-queue.md").read_text(encoding="utf-8")
        self.assertIn("## Open Incidents", incident)
        self.assertIn("## Pending Alerts", alert)
        self.assertTrue((self.state_dir / "state" / "incidents.jsonl").exists())
        self.assertTrue((self.state_dir / "state" / "alerts.jsonl").exists())

    def test_record_incident_appends_severity_and_state(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        result = run_cmd(
            [
                TOOL,
                "record-incident",
                "--state-dir",
                self.state_dir,
                "--severity",
                "warning",
                "--summary",
                "stale provider session",
                "--source",
                "session-control",
            ]
        )
        self.assertIn("Recorded incident", result.stdout)
        incident = json.loads(
            (self.state_dir / "state" / "incidents.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[-1]
        )
        self.assertEqual(incident["severity"], "warning")
        self.assertEqual(incident["state"], "open")

    def test_alert_queue_blocks_silent_critical_failures(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "record-incident",
                "--state-dir",
                self.state_dir,
                "--severity",
                "critical",
                "--summary",
                "critical safety breach",
                "--source",
                "supervisor",
            ]
        )
        status = run_cmd([TOOL, "alert-status", "--state-dir", self.state_dir], check=False)
        self.assertEqual(status.returncode, 1)
        self.assertIn("critical safety breach", status.stdout)

    def test_observability_summary_reports_open_alerts(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "record-incident",
                "--state-dir",
                self.state_dir,
                "--severity",
                "critical",
                "--summary",
                "state corruption",
                "--source",
                "state-io",
            ]
        )
        summary = run_cmd([TOOL, "telemetry-summary", "--state-dir", self.state_dir], check=False)
        self.assertEqual(summary.returncode, 1)
        self.assertIn("Open alerts: 1", summary.stdout)
        self.assertIn("Runtime state", summary.stdout)

    def test_acknowledge_alert_preserves_audit_history(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "record-incident",
                "--state-dir",
                self.state_dir,
                "--severity",
                "critical",
                "--summary",
                "repeated remediation failure",
                "--source",
                "supervisor",
            ]
        )
        alert = json.loads(
            (self.state_dir / "state" / "alerts.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[-1]
        )
        ack = run_cmd(
            [
                TOOL,
                "acknowledge-alert",
                "--state-dir",
                self.state_dir,
                "--alert-id",
                alert["alert_id"],
                "--note",
                "operator reviewed",
            ]
        )
        self.assertIn("Acknowledged alert", ack.stdout)
        events = [
            json.loads(line)
            for line in (self.state_dir / "state" / "alerts.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(events[-1]["event"], "alert-acknowledged")
        self.assertEqual(events[0]["event"], "alert-opened")

    def test_state_schema_template_is_created(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        schema = (self.state_dir / "state-schema.md").read_text(encoding="utf-8")
        self.assertIn("## Current Schema", schema)
        self.assertIn("## Corruption Quarantine", schema)

    def test_schema_version_is_initialized(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        schema = json.loads(
            (self.state_dir / "state" / "schema-version.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["schema_version"], "1.5")
        self.assertIn("migration_history", schema)

    def test_migration_runs_in_order(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        first = run_cmd([TOOL, "migrate-state", "--state-dir", self.state_dir])
        self.assertIn("Applied migrations", first.stdout)
        schema = json.loads(
            (self.state_dir / "state" / "schema-version.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            [entry["migration_id"] for entry in schema["migration_history"]],
            [
                "0001-base-state",
                "0002-runtime-session-observability",
                "0003-learning-layer",
                "0004-governance-optimization",
                "0005-guard-synchronization",
                "0006-round-log-evidence",
                "0007-repair-log-control",
            ],
        )
        second = run_cmd([TOOL, "migrate-state", "--state-dir", self.state_dir])
        self.assertIn("No migrations pending", second.stdout)

    def test_corrupt_json_is_quarantined_before_recovery(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        budget_path = self.state_dir / "state" / "budget.json"
        budget_path.write_text("{ broken json", encoding="utf-8")

        result = run_cmd(
            [TOOL, "recover-state", "--state-dir", self.state_dir, "--from-logs"]
        )
        self.assertIn("Recovered state", result.stdout)
        self.assertTrue(json.loads(budget_path.read_text(encoding="utf-8")))
        quarantine = list((self.state_dir / "state" / "quarantine").glob("budget.json.*.corrupt"))
        self.assertTrue(quarantine)

    def test_recovery_replays_append_only_logs(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "record-usage",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "strategy-1",
                "--tokens-used",
                "75",
                "--source",
                "measured",
                "--confidence",
                "high",
            ]
        )
        (self.state_dir / "state" / "budget.json").unlink()

        run_cmd([TOOL, "recover-state", "--state-dir", self.state_dir, "--from-logs"])
        budget = json.loads(
            (self.state_dir / "state" / "budget.json").read_text(encoding="utf-8")
        )
        self.assertEqual(budget["project_used"], 75)
        self.assertEqual(budget["agents"]["strategy-1"]["tokens_used"], 75)

    def test_stale_lock_recovery_is_bounded(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        lock_path = self.state_dir / "state" / "budget.json.lock"
        lock_path.write_text("stale", encoding="utf-8")
        outside_lock = self.tmp / "outside.lock"
        outside_lock.write_text("do not touch", encoding="utf-8")

        result = run_cmd(
            [TOOL, "recover-locks", "--state-dir", self.state_dir, "--stale-seconds", "0"]
        )
        self.assertIn("Removed stale locks: 1", result.stdout)
        self.assertFalse(lock_path.exists())
        self.assertTrue(outside_lock.exists())

    def test_recover_locks_preserves_live_owner_and_removes_dead_owner(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        live_lock = self.state_dir / "state" / "budget.json.lock"
        live_lock.write_text(f"pid={os.getpid()} time=0\n", encoding="utf-8")
        dead_lock = self.state_dir / "state" / "token-usage.jsonl.lock"
        dead_lock.write_text("pid=99999999 time=0\n", encoding="utf-8")

        result = run_cmd(
            [
                TOOL,
                "recover-locks",
                "--state-dir",
                self.state_dir,
                "--stale-seconds",
                "0",
            ]
        )

        self.assertIn("Removed stale locks: 1", result.stdout)
        self.assertTrue(live_lock.exists())
        self.assertFalse(dead_lock.exists())

    def test_append_jsonl_locked_recovers_stale_dead_owner_lock(self):
        target = self.tmp / "state" / "events.jsonl"
        lock_path = target.with_suffix(target.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("pid=99999999 time=0\n", encoding="utf-8")

        append_jsonl_locked(
            target,
            {"event": "after-stale-lock"},
            timeout_seconds=0.1,
            stale_seconds=0,
        )

        events = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(events[-1])["event"], "after-stale-lock")
        self.assertFalse(lock_path.exists())

    def test_enforce_master_boundary_allows_state_pack_changes_and_blocks_source(self):
        subprocess.run(["git", "init"], cwd=self.tmp, check=True, capture_output=True, text=True)
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        clean = run_cmd(
            [
                TOOL,
                "enforce-master-boundary",
                "--project-root",
                self.tmp,
                "--state-dir",
                self.state_dir,
            ]
        )
        self.assertIn("Master boundary clean", clean.stdout)

        source = self.tmp / "src" / "app.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("print('production change')\n", encoding="utf-8")
        blocked = run_cmd(
            [
                TOOL,
                "enforce-master-boundary",
                "--project-root",
                self.tmp,
                "--state-dir",
                self.state_dir,
            ],
            check=False,
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("src/app.py", blocked.stdout)

    def test_enforce_master_boundary_fails_closed_outside_git(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        result = run_cmd(
            [
                TOOL,
                "enforce-master-boundary",
                "--project-root",
                self.tmp,
                "--state-dir",
                self.state_dir,
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot enforce boundary", result.stderr)

    def write_work_order(
        self,
        path: Path,
        write_set: str,
        artifact_namespace: str,
        worktree_id: str = "wt-default",
        merge_owner: str = "Master Agent",
        conflict_protocol: str = "stop and return to Master",
        token_budget: str = "4000",
        max_heartbeats: str = "3",
    ) -> None:
        path.write_text(
            "\n".join(
                [
                    "# Work Order",
                    "",
                    "## Objective",
                    "",
                    "- Task id: TASK",
                    "- Coding Agent objective: bounded task",
                    "",
                    "## Root Authorization",
                    "",
                    "- Source kind: current-user-request",
                    "- Source ref: test",
                    "- Grant id: grant-test",
                    "- Approved owners: Coding",
                    "- Approved file scopes: " + write_set,
                    "- Approved material behavior domains: none",
                    "- Forbidden behavior domains: pipeline-order",
                    "",
                    "## Allowed Scope",
                    "",
                    "- Files/modules/artifacts allowed: " + write_set,
                    "",
                    "## Material Behavior Domains",
                    "",
                    "- Declared material behavior domains: none",
                    "- No material behavior change: yes",
                    "",
                    "## Parallel Safety",
                    "",
                    "- Exclusive Write Set: " + write_set,
                    "- Artifact Namespace: " + artifact_namespace,
                    "- Worktree Mode: codex-app",
                    "- Worktree Id: " + worktree_id,
                    "- Base Branch: main",
                    "- Local Mutation Policy: do not mutate local checkout",
                    "- Remote Mutation Policy: do not push or create PR without release gate",
                    "- Merge Owner: " + merge_owner,
                    "- Conflict Protocol: " + conflict_protocol,
                    "",
                    "## Heuristic Admission",
                    "",
                    "- Heuristic used: no",
                    "",
                    "## Representative Workflow",
                    "",
                    "- Claim scope: diagnostic",
                    "- Workspace: test workspace",
                    "- Bootstrap path: test bootstrap",
                    "- Mode: test mode",
                    "- Provider or model path: test provider",
                    "- Key settings: defaults",
                    "- Representative parity: yes",
                    "- Diagnostic-only if mismatch: no",
                    "",
                    "## Acceptance Gates",
                    "",
                    "- Required maturity gates: diagnostic",
                    "- Current maturity: diagnostic",
                    "- Lower gates satisfied: yes",
                    "- Evidence artifact: test evidence",
                    "",
                    "## Guard Mode",
                    "",
                    "- Guard activation required: no",
                    "- Activation source: none",
                    "- Explicit autonomous loop requested: no",
                    "- Guard state mutation allowed: no",
                    "- Missing activation status: loop_guard_not_required",
                    "- Required gates source: current-user-request",
                    "- Broad extra revalidation allowed: no",
                    "- Execution-log lineage independent: yes",
                    "",
                    "## Token Budget",
                    "",
                    "- Token budget: " + token_budget,
                    "- Maximum heartbeats: " + max_heartbeats,
                    "",
                    "## Forbidden Changes",
                    "",
                    "- unrelated files",
                    "",
                    "## Required Validation",
                    "",
                    "- python -m unittest",
                    "",
                    "## Task Record",
                    "",
                    "- Task record required: yes",
                    "- Record path or reason: docs/master-agent/task-record.md",
                    "",
                    "## Receipt Requirements",
                    "",
                    "- return receipt",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def write_guard_obligation(
        self,
        path: Path,
        loop_type: str = "implementation",
        validation_closeout: str = "no",
        budget_reset: str = "no",
        shell_string_allowed: str = "no",
        assertion_policy: str = "preserve",
        guard_required: str = "yes",
        activation_source: str = "current-user-request",
        explicit_loop: str = "yes",
        guard_state_mutation: str = "yes",
        missing_activation_status: str = "loop_guard_not_required",
        same_manifest_status: str = "already_armed",
        active_loop_status: str = "active_guard_exists",
        required_gates_source: str = "current-user-request",
        broad_extra_revalidation: str = "no",
        manifest_correction_status: str = "manifest_correction_required",
        infrastructure_retry_status: str = "infrastructure_retry_ready",
        max_infrastructure_retries: str = "1",
    ) -> None:
        path.write_text(
            "\n".join(
                [
                    "# Guard Obligation",
                    "",
                    "## Root Authorization",
                    "",
                    "- Source kind: current-user-request",
                    "- Source ref: test",
                    "- Grant id: grant-test",
                    "- Objective: verify guarded obligation semantics",
                    "- Approved production owners: Coding",
                    "- Approved production file scopes: src/module",
                    "- Approved material behavior domains: none",
                    "- Explicit exclusions: unrelated owners",
                    "",
                    "## Observation And Mutation",
                    "",
                    "- Observation outside owner allowed: yes",
                    "- Production mutation requires root grant: yes",
                    "- External mutation domain status: external_mutation_domain_identified",
                    "- Authority violation status: authority_required",
                    "",
                    "## Guard Activation",
                    "",
                    f"- Guard activation required: {guard_required}",
                    f"- Activation source: {activation_source}",
                    f"- Explicit autonomous loop requested: {explicit_loop}",
                    f"- Guard state mutation allowed: {guard_state_mutation}",
                    "- Unrelated bounded requests passivate predecessor: yes",
                    f"- Missing activation status: {missing_activation_status}",
                    f"- Same manifest status: {same_manifest_status}",
                    f"- Distinct active-loop conflict status: {active_loop_status}",
                    "",
                    "## Obligation Contract",
                    "",
                    "- Schema version: 6",
                    "- Obligation id: OBLIGATION-TEST",
                    "- Original target error: first failing boundary unresolved",
                    "- Acceptance metric: diagnostic gate passed",
                    "- Completion maturity: diagnostic",
                    "- Required gate ids: gate-diagnostic",
                    "- Contract docs: docs/plan.md",
                    f"- Required gates source: {required_gates_source}",
                    f"- Broad extra revalidation allowed: {broad_extra_revalidation}",
                    "",
                    "## Loop Budget",
                    "",
                    "- Maximum implementation attempts: 2",
                    "- Maximum reassessments: 1",
                    "- Maximum recovery transitions: 1",
                    f"- Budgets reset by reassessment: {budget_reset}",
                    "",
                    "## Loop Type And Progress",
                    "",
                    f"- Loop type: {loop_type}",
                    "- Git-visible progress scope: src/module",
                    "- Ignored paths are progress: no",
                    f"- Validation-only closeout allowed: {validation_closeout}",
                    "",
                    "## Structured Validation",
                    "",
                    "- Validation uses argv: yes",
                    "- Expected write roots declared: yes",
                    "- Native receipts update gates: yes",
                    f"- Shell string allowed: {shell_string_allowed}",
                    "",
                    "## Validation Support",
                    "",
                    "- Validation support roots: tests/",
                    f"- Assertion policy: {assertion_policy}",
                    "- Exact support files: tests/test_contract.py",
                    "- Production frozen during support: yes",
                    "",
                    "## Visual Gate Boundary",
                    "",
                    "- Visual review external: yes",
                    "- Receipt requires contract id: yes",
                    "- Receipt requires candidate fingerprint: yes",
                    "- Receipt requires coverage: yes",
                    "- Evidence index opaque: yes",
                    "",
                    "## Status Semantics",
                    "",
                    "- Authorization invalid status: authorization_invalid",
                    f"- Manifest correction status: {manifest_correction_status}",
                    "- In-root transition status: in_root_transition_required",
                    "- External mutation domain status: external_mutation_domain_identified",
                    f"- Infrastructure retry status: {infrastructure_retry_status}",
                    "- Authority required status: authority_required",
                    "",
                    "## Manifest Correction Policy",
                    "",
                    f"- Correctable defect status: {manifest_correction_status}",
                    "- Authorization reference defect status: authorization_invalid",
                    "- Active state changed by correction refusal: no",
                    "- Correction may widen contract: no",
                    "- Same refusal stop required: yes",
                    "",
                    "## Infrastructure Retry",
                    "",
                    f"- Infrastructure retry status: {infrastructure_retry_status}",
                    f"- Maximum infrastructure retries: {max_infrastructure_retries}",
                    "- Production frozen during retry: yes",
                    "- Requires unchanged manifest commands gates authority: yes",
                    "- Requires unchanged production fingerprints: yes",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def test_assess_parallelism_allows_disjoint_work_orders(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        one = self.tmp / "one.md"
        two = self.tmp / "two.md"
        self.write_work_order(one, "src/parser", "artifacts/parser", worktree_id="wt-parser")
        self.write_work_order(two, "src/render", "artifacts/render", worktree_id="wt-render")

        result = run_cmd(
            [
                TOOL,
                "assess-parallelism",
                "--state-dir",
                self.state_dir,
                "--work-order",
                one,
                "--work-order",
                two,
            ]
        )
        self.assertIn("Verdict: allow", result.stdout)

    def test_assess_parallelism_blocks_overlap_and_missing_fields(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        one = self.tmp / "one.md"
        two = self.tmp / "two.md"
        missing = self.tmp / "missing.md"
        self.write_work_order(one, "src/parser", "artifacts/parser", worktree_id="wt-parser")
        self.write_work_order(two, "src/parser/tokenizer.py", "artifacts/parser", worktree_id="wt-tokenizer")
        missing.write_text("# Work Order\n\n## Parallel Safety\n\n", encoding="utf-8")

        serial = run_cmd(
            [
                TOOL,
                "assess-parallelism",
                "--state-dir",
                self.state_dir,
                "--work-order",
                one,
                "--work-order",
                two,
            ],
            check=False,
        )
        self.assertEqual(serial.returncode, 1)
        self.assertIn("serial-required", serial.stdout)

        invalid = run_cmd(
            [
                TOOL,
                "assess-parallelism",
                "--state-dir",
                self.state_dir,
                "--work-order",
                missing,
            ],
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("invalid-work-order", invalid.stdout)

    def test_assess_parallelism_blocks_broad_wildcards(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        order = self.tmp / "broad.md"
        self.write_work_order(order, "**", "artifacts/all")
        result = run_cmd(
            [
                TOOL,
                "assess-parallelism",
                "--state-dir",
                self.state_dir,
                "--work-order",
                order,
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("broad parallel scope", result.stdout)

    def test_assess_parallelism_blocks_shared_worktree_id(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        one = self.tmp / "one.md"
        two = self.tmp / "two.md"
        self.write_work_order(one, "src/parser", "artifacts/parser", worktree_id="wt-shared")
        self.write_work_order(two, "src/render", "artifacts/render", worktree_id="wt-shared")

        result = run_cmd(
            [
                TOOL,
                "assess-parallelism",
                "--state-dir",
                self.state_dir,
                "--work-order",
                one,
                "--work-order",
                two,
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("shared Worktree Id", result.stdout)

    def test_governance_lint_accepts_valid_work_order(self):
        order = self.tmp / "governed.md"
        self.write_work_order(order, "src/parser", "artifacts/parser")

        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                order,
                "--packet-type",
                "work-order",
            ]
        )
        self.assertIn("Governance packet is valid", result.stdout)

    def test_governance_lint_accepts_valid_guard_obligation(self):
        obligation = self.tmp / "guard-obligation.md"
        self.write_guard_obligation(obligation)

        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                obligation,
                "--packet-type",
                "guard-obligation",
            ]
        )
        self.assertIn("Governance packet is valid", result.stdout)

    def test_governance_lint_accepts_validation_only_guard_obligation(self):
        obligation = self.tmp / "guard-validation-obligation.md"
        self.write_guard_obligation(
            obligation,
            loop_type="validation",
            validation_closeout="yes",
        )

        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                obligation,
                "--packet-type",
                "guard-obligation",
            ]
        )
        self.assertIn("Governance packet is valid", result.stdout)

    def test_governance_lint_rejects_invalid_guard_obligation_semantics(self):
        budget_reset = self.tmp / "budget-reset.md"
        self.write_guard_obligation(budget_reset, budget_reset="yes")
        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                budget_reset,
                "--packet-type",
                "guard-obligation",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Budgets reset by reassessment must be no", result.stderr)

        shell_string = self.tmp / "shell-string.md"
        self.write_guard_obligation(shell_string, shell_string_allowed="yes")
        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                shell_string,
                "--packet-type",
                "guard-obligation",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Shell string allowed must be no", result.stderr)

        validation_loop = self.tmp / "validation-loop.md"
        self.write_guard_obligation(validation_loop, loop_type="validation")
        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                validation_loop,
                "--packet-type",
                "guard-obligation",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("validation loop type requires validation-only closeout allowed", result.stderr)

        missing_activation = self.tmp / "missing-activation.md"
        self.write_guard_obligation(missing_activation, explicit_loop="no")
        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                missing_activation,
                "--packet-type",
                "guard-obligation",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("guard-required obligation needs explicit autonomous loop requested", result.stderr)

        wrong_status = self.tmp / "wrong-guard-status.md"
        self.write_guard_obligation(wrong_status, missing_activation_status="continue")
        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                wrong_status,
                "--packet-type",
                "guard-obligation",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Missing activation status must be loop_guard_not_required", result.stderr)

        broad_gate = self.tmp / "broad-gate.md"
        self.write_guard_obligation(broad_gate, broad_extra_revalidation="yes")
        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                broad_gate,
                "--packet-type",
                "guard-obligation",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Broad extra revalidation allowed must be no", result.stderr)

        retry_budget = self.tmp / "retry-budget.md"
        self.write_guard_obligation(retry_budget, max_infrastructure_retries="2")
        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                retry_budget,
                "--packet-type",
                "guard-obligation",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Maximum infrastructure retries must be 1", result.stderr)

    def test_governance_lint_rejects_work_order_guard_state_without_activation(self):
        order = self.tmp / "work-order-guard-state.md"
        self.write_work_order(order, "src/parser", "artifacts/parser")
        text = order.read_text(encoding="utf-8").replace(
            "- Guard state mutation allowed: no",
            "- Guard state mutation allowed: yes",
        )
        order.write_text(text, encoding="utf-8")
        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                order,
                "--packet-type",
                "work-order",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("guard-not-required work cannot mutate guard state", result.stderr)

    def test_governance_lint_enforces_required_round_log_receipt_evidence(self):
        receipt = self.tmp / "coding-receipt.md"
        receipt.write_text(
            "\n".join(
                [
                    "# Coding Receipt",
                    "",
                    "## Authority And Behavior",
                    "",
                    "- Grant id: grant-test",
                    "- Observed owner: Coding",
                    "- Observed files inside envelope: yes",
                    "- Observed material behavior domains: none",
                    "- No material behavior change: yes",
                    "- Authority status: inside-envelope",
                    "",
                    "## Acceptance Gates",
                    "",
                    "- Current maturity: diagnostic",
                    "- Lower gates satisfied: yes",
                    "- Evidence artifact: test evidence",
                    "",
                    "## Round Log Evidence",
                    "",
                    "- Round log required: yes",
                    "- Snapshot id:",
                    "- Manifest path:",
                    "- Changed paths match work order: no",
                    "",
                    "## Representative Workflow",
                    "",
                    "- Claim scope: diagnostic",
                    "- Representative parity: yes",
                    "- Diagnostic-only if mismatch: no",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        missing = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                receipt,
                "--packet-type",
                "coding-receipt",
            ],
            check=False,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("Snapshot id", missing.stderr)
        self.assertIn("must match the work order", missing.stderr)

        receipt.write_text(
            receipt.read_text(encoding="utf-8")
            .replace("- Snapshot id:", "- Snapshot id: 0001_20260730T000000")
            .replace("- Manifest path:", "- Manifest path: .codex-round-log/0001_20260730T000000/manifest.json")
            .replace("- Changed paths match work order: no", "- Changed paths match work order: yes"),
            encoding="utf-8",
        )
        valid = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                receipt,
                "--packet-type",
                "coding-receipt",
            ]
        )
        self.assertIn("Governance packet is valid", valid.stdout)

    def test_governance_lint_rejects_unadmitted_heuristic(self):
        order = self.tmp / "bad-heuristic.md"
        self.write_work_order(order, "src/parser", "artifacts/parser")
        order.write_text(
            order.read_text(encoding="utf-8").replace(
                "- Heuristic used: no",
                "- Heuristic used: yes",
            ),
            encoding="utf-8",
        )

        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                order,
                "--packet-type",
                "work-order",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Representative evidence", result.stderr)
        self.assertIn("Target-independent invariant", result.stderr)

    def test_governance_lint_requires_nonrepresentative_claims_to_be_diagnostic(self):
        order = self.tmp / "bad-representative.md"
        self.write_work_order(order, "src/parser", "artifacts/parser")
        order.write_text(
            order.read_text(encoding="utf-8").replace(
                "- Representative parity: yes", "- Representative parity: no"
            ),
            encoding="utf-8",
        )

        result = run_cmd(
            [
                TOOL,
                "governance-lint",
                "--packet",
                order,
                "--packet-type",
                "work-order",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("must be marked diagnostic-only", result.stderr)

    def test_record_authority_required_marks_agent_and_event(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-AUTH")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-auth",
                "--role",
                "Coding",
                "--task-id",
                "TASK-AUTH",
                "--objective",
                "Implement bounded work",
                "--scope",
                "src/parser",
                "--plan-id",
                "PLAN-AUTH",
            ]
        )

        result = run_cmd(
            [
                TOOL,
                "record-authority-required",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-auth",
                "--reason",
                "requested change crosses owner boundary",
                "--evidence",
                "work order approved src/parser only",
                "--required-user-decision",
                "approve renderer owner or narrow task",
            ]
        )
        self.assertIn("authority_required", result.stdout)
        agents = json.loads((self.state_dir / "state" / "agents.json").read_text(encoding="utf-8"))
        self.assertEqual(agents["coding-auth"]["status"], "authority_required")
        events = [
            json.loads(line)
            for line in (self.state_dir / "state" / "governance-events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(events[-1]["event_type"], "authority-required")

    def test_record_governance_status_marks_recoverable_status_and_event(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-GOV")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-gov",
                "--role",
                "Coding",
                "--task-id",
                "TASK-GOV",
                "--objective",
                "Implement bounded work",
                "--scope",
                "src/parser",
                "--plan-id",
                "PLAN-GOV",
            ]
        )

        result = run_cmd(
            [
                TOOL,
                "record-governance-status",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-gov",
                "--status",
                "external_mutation_domain_identified",
                "--reason",
                "diagnosis proved a renderer owner is required",
                "--evidence",
                "packets/obstacle-recovery-packet.md",
                "--next-action",
                "ask only if the user chooses renderer implementation",
            ]
        )
        self.assertIn("Marked coding-gov external_mutation_domain_identified", result.stdout)
        agents = json.loads((self.state_dir / "state" / "agents.json").read_text(encoding="utf-8"))
        self.assertEqual(agents["coding-gov"]["status"], "external_mutation_domain_identified")
        events = [
            json.loads(line)
            for line in (self.state_dir / "state" / "governance-events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(events[-1]["event_type"], "governance-status")
        self.assertEqual(events[-1]["status"], "external_mutation_domain_identified")

    def test_record_governance_status_rejects_authority_required(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-GOV-REJECT")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-gov-reject",
                "--role",
                "Coding",
                "--task-id",
                "TASK-GOV-REJECT",
                "--objective",
                "Implement bounded work",
                "--scope",
                "src/parser",
                "--plan-id",
                "PLAN-GOV-REJECT",
            ]
        )

        result = run_cmd(
            [
                TOOL,
                "record-governance-status",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-gov-reject",
                "--status",
                "authority_required",
                "--reason",
                "observed out-of-root production mutation",
                "--evidence",
                "git diff",
                "--next-action",
                "use record-authority-required",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)

    def test_acceptance_gate_requires_lower_gate_order(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        too_high = run_cmd(
            [
                TOOL,
                "record-acceptance-gate",
                "--state-dir",
                self.state_dir,
                "--scope-id",
                "scope-1",
                "--maturity",
                "live_seam_green",
                "--status",
                "passed",
                "--evidence",
                "live seam passed",
            ],
            check=False,
        )
        self.assertEqual(too_high.returncode, 1)
        self.assertIn("diagnostic", too_high.stderr)
        self.assertIn("focused_green", too_high.stderr)

        for maturity in ["diagnostic", "focused_green", "live_seam_green"]:
            result = run_cmd(
                [
                    TOOL,
                    "record-acceptance-gate",
                    "--state-dir",
                    self.state_dir,
                    "--scope-id",
                    "scope-1",
                    "--maturity",
                    maturity,
                    "--status",
                    "passed",
                    "--evidence",
                    f"{maturity} evidence",
                ]
            )
            self.assertIn(maturity, result.stdout)
        gates = [
            json.loads(line)
            for line in (self.state_dir / "state" / "acceptance-gates.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual([gate["maturity"] for gate in gates], [
            "diagnostic",
            "focused_green",
            "live_seam_green",
        ])

    def test_validate_worktreeinclude_allows_ignored_file_and_blocks_tracked_file(self):
        subprocess.run(["git", "init"], cwd=self.tmp, check=True, capture_output=True, text=True)
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        (self.tmp / ".gitignore").write_text(".env\n", encoding="utf-8")
        (self.tmp / ".env").write_text("LOCAL_ONLY=1\n", encoding="utf-8")
        (self.tmp / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".gitignore", "tracked.txt"],
            cwd=self.tmp,
            check=True,
            capture_output=True,
            text=True,
        )

        (self.tmp / ".worktreeinclude").write_text(".env\n", encoding="utf-8")
        allowed = run_cmd(
            [
                TOOL,
                "validate-worktreeinclude",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
            ]
        )
        self.assertIn(".worktreeinclude validation passed", allowed.stdout)

        (self.tmp / ".worktreeinclude").write_text("tracked.txt\n", encoding="utf-8")
        blocked = run_cmd(
            [
                TOOL,
                "validate-worktreeinclude",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
            ],
            check=False,
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("matches tracked files", blocked.stdout)

    def test_validate_worktreeinclude_blocks_broad_entries(self):
        subprocess.run(["git", "init"], cwd=self.tmp, check=True, capture_output=True, text=True)
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        (self.tmp / ".worktreeinclude").write_text("**\n", encoding="utf-8")

        blocked = run_cmd(
            [
                TOOL,
                "validate-worktreeinclude",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
            ],
            check=False,
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("too broad or unsafe", blocked.stdout)

    def test_round_log_status_reports_missing_and_available_snapshots(self):
        subprocess.run(["git", "init"], cwd=self.tmp, check=True, capture_output=True, text=True)
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        missing = run_cmd(
            [
                TOOL,
                "round-log-status",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--require-active",
            ],
            check=False,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("Round log status: missing", missing.stdout)

        write_round_log_snapshot(self.tmp, "0002_20260730T000000")
        available = run_cmd(
            [
                TOOL,
                "round-log-status",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--require-active",
            ]
        )
        self.assertIn("Round log status: available", available.stdout)
        self.assertIn("0002_20260730T000000", available.stdout)
        control = (self.state_dir / "round-log-control.md").read_text(encoding="utf-8")
        self.assertIn("0002_20260730T000000", control)

    def test_record_and_require_round_log_evidence_binds_agent_snapshot(self):
        subprocess.run(["git", "init"], cwd=self.tmp, check=True, capture_output=True, text=True)
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        accept_valid_strategy(self.state_dir, self.tmp, "PLAN-ROUND")
        run_cmd(
            [
                TOOL,
                "register-agent",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-round",
                "--role",
                "Coding",
                "--task-id",
                "TASK-ROUND",
                "--objective",
                "Implement bounded work",
                "--scope",
                "src",
                "--plan-id",
                "PLAN-ROUND",
            ]
        )
        write_round_log_snapshot(
            self.tmp,
            "0003_20260730T000000",
            copied_paths=["src/module.py", "tests/test_module.py"],
        )

        missing_path = run_cmd(
            [
                TOOL,
                "record-round-log-evidence",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--agent-id",
                "coding-round",
                "--snapshot-id",
                "0003_20260730T000000",
                "--expected-path",
                "src/missing.py",
            ],
            check=False,
        )
        self.assertEqual(missing_path.returncode, 1)
        self.assertIn("does not contain expected paths", missing_path.stderr)

        recorded = run_cmd(
            [
                TOOL,
                "record-round-log-evidence",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--agent-id",
                "coding-round",
                "--snapshot-id",
                "0003_20260730T000000",
                "--plan-id",
                "PLAN-ROUND",
                "--worktree-id",
                "wt-round",
                "--receipt",
                "packets/coding-receipt.md",
                "--expected-path",
                "src/module.py",
            ]
        )
        self.assertIn("Recorded round-log evidence", recorded.stdout)

        required = run_cmd(
            [
                TOOL,
                "require-round-log-evidence",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-round",
                "--project-root",
                self.tmp,
                "--plan-id",
                "PLAN-ROUND",
                "--worktree-id",
                "wt-round",
            ]
        )
        self.assertIn("Round-log evidence present", required.stdout)
        agents = json.loads((self.state_dir / "state" / "agents.json").read_text(encoding="utf-8"))
        self.assertEqual(agents["coding-round"]["latest_round_snapshot_id"], "0003_20260730T000000")
        events = [
            json.loads(line)
            for line in (self.state_dir / "state" / "round-log-events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(events[-1]["event"], "round-log-evidence")

    def test_require_round_log_evidence_rejects_missing_or_stale_evidence(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        missing = run_cmd(
            [
                TOOL,
                "require-round-log-evidence",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "missing-agent",
            ],
            check=False,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("No round-log evidence", missing.stderr)

        append_jsonl_locked(
            self.state_dir / "state" / "round-log-events.jsonl",
            {
                "at": "2026-07-28T00:00:00+00:00",
                "event": "round-log-evidence",
                "agent_id": "coding-stale",
                "snapshot_id": "0001_old",
            },
        )
        stale = run_cmd(
            [
                TOOL,
                "require-round-log-evidence",
                "--state-dir",
                self.state_dir,
                "--agent-id",
                "coding-stale",
                "--max-age-minutes",
                "1",
                "--at",
                "2026-07-30T00:00:00+00:00",
            ],
            check=False,
        )
        self.assertEqual(stale.returncode, 1)
        self.assertIn("is stale", stale.stderr)

    def test_round_log_export_uses_explicit_command_and_records_event(self):
        subprocess.run(["git", "init"], cwd=self.tmp, check=True, capture_output=True, text=True)
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        write_round_log_snapshot(self.tmp, "0004_20260730T000000")
        fake_command = self.tmp / "fake_round_logger.py"
        fake_command.write_text(
            "\n".join(
                [
                    "import sys",
                    "from pathlib import Path",
                    "args = sys.argv[1:]",
                    "output = Path(args[args.index('--output') + 1]) if '--output' in args else Path('unused')",
                    "output.mkdir(parents=True, exist_ok=True)",
                    "(output / 'export-info.json').write_text('{\"ok\": true}\\n', encoding='utf-8')",
                    "print('Exported readable snapshot')",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        output = self.tmp / "round-export"

        result = run_cmd(
            [
                TOOL,
                "round-log-export",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--snapshot-id",
                "0004_20260730T000000",
                "--round-log-command",
                f"{PYTHON} {fake_command}",
                "--output",
                output,
            ]
        )
        self.assertIn("Round-log export recorded", result.stdout)
        self.assertTrue((output / "export-info.json").exists())
        events = [
            json.loads(line)
            for line in (self.state_dir / "state" / "round-log-events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(events[-1]["event"], "round-log-export")

    def test_repair_log_init_status_and_task_current_row_gate(self):
        subprocess.run(["git", "init"], cwd=self.tmp, check=True, capture_output=True, text=True)
        run_cmd([TOOL, "init", "--project-root", self.tmp])

        missing = run_cmd(
            [
                TOOL,
                "repair-log-status",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--require-initialized",
            ],
            check=False,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("Repair log status: missing", missing.stdout)

        initialized = run_cmd(
            [TOOL, "repair-log-init", "--state-dir", self.state_dir, "--project-root", self.tmp]
        )
        self.assertIn("Repair log initialized", initialized.stdout)
        self.assertTrue((self.tmp / "docs" / "repair-execution-log" / "task-records" / "plan-index.md").exists())

        no_row = run_cmd(
            [
                TOOL,
                "require-current-repair-row",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--workstream",
                "renderer",
            ],
            check=False,
        )
        self.assertEqual(no_row.returncode, 1)
        self.assertIn("No current repair-log row", no_row.stderr)

        recorded = run_cmd(
            [
                TOOL,
                "record-task",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--title",
                "Renderer handoff",
                "--workstream",
                "renderer",
                "--objective",
                "Record bounded renderer handoff",
                "--status",
                "active",
                "--outcome",
                "inconclusive",
                "--reason",
                "review requires a follow-up",
                "--next-step",
                "issue one bounded review work order",
                "--escalation-trigger",
                "new graph owner required",
                "--validation",
                "review-verdict.md",
            ]
        )
        self.assertIn("Task record created", recorded.stdout)

        allowed = run_cmd(
            [
                TOOL,
                "require-current-repair-row",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--workstream",
                "renderer",
            ]
        )
        self.assertIn("Current repair-log row allows work", allowed.stdout)
        control = (self.state_dir / "repair-log-control.md").read_text(encoding="utf-8")
        self.assertIn("issue one bounded review work order", control)
        events = [
            json.loads(line)
            for line in (self.state_dir / "state" / "repair-log-events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(events[-1]["event"], "repair-log-current-row-required")

    def test_repair_log_current_row_blocks_paused_or_complete_status(self):
        subprocess.run(["git", "init"], cwd=self.tmp, check=True, capture_output=True, text=True)
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd([TOOL, "repair-log-init", "--state-dir", self.state_dir, "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "record-task",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--title",
                "Paused architecture work",
                "--workstream",
                "architecture",
                "--objective",
                "Record pause",
                "--status",
                "paused",
                "--outcome",
                "paused",
                "--reason",
                "needs user decision",
                "--next-step",
                "wait for explicit decision",
                "--escalation-trigger",
                "implementation requested before decision",
            ]
        )

        blocked = run_cmd(
            [
                TOOL,
                "require-current-repair-row",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--workstream",
                "architecture",
            ],
            check=False,
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("status blocks work: paused", blocked.stderr)

    def test_repair_cycle_records_attempt_and_blocks_reassessment(self):
        subprocess.run(["git", "init"], cwd=self.tmp, check=True, capture_output=True, text=True)
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd([TOOL, "repair-log-init", "--state-dir", self.state_dir, "--project-root", self.tmp])
        opened = run_cmd(
            [
                TOOL,
                "open-repair-cycle",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--cycle-id",
                "renderer-visible-text",
                "--repair-area",
                "Renderer Visible Text",
                "--objective",
                "Close repeated visible text failure",
                "--target-error",
                "rendered text incomplete",
                "--first-failing-boundary",
                "renderer handoff",
                "--acceptance-metric",
                "representative runtime gate passes",
                "--next-step",
                "make first bounded owner fix",
                "--attempt-budget",
                "2",
            ]
        )
        self.assertIn("Repair cycle opened", opened.stdout)

        allowed = run_cmd(
            [
                TOOL,
                "require-current-repair-row",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--cycle-id",
                "renderer-visible-text",
            ]
        )
        self.assertIn("status=active", allowed.stdout)

        recorded = run_cmd(
            [
                TOOL,
                "record-repair-attempt",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--cycle-id",
                "renderer-visible-text",
                "--attempt-id",
                "attempt-001",
                "--hypothesis",
                "the handoff omits source-bearing child text",
                "--intended-boundary",
                "renderer handoff",
                "--files-touched",
                "app/render/renderer.py",
                "--validation",
                "focused tests failed",
                "--metric-status",
                "unchanged",
                "--decision",
                "reassess",
                "--next-step",
                "write reassessment before another patch",
                "--escalation-trigger",
                "same failure recurs",
            ]
        )
        self.assertIn("Repair attempt recorded", recorded.stdout)

        blocked = run_cmd(
            [
                TOOL,
                "require-current-repair-row",
                "--state-dir",
                self.state_dir,
                "--project-root",
                self.tmp,
                "--cycle-id",
                "renderer-visible-text",
            ],
            check=False,
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("status blocks work: reassess", blocked.stderr)

    def test_release_validator_runs_fake_quick_validate_under_test_hook(self):
        fake_quick = self.tmp / "quick_validate.py"
        fake_quick.write_text("import sys\nprint('Skill is valid!')\n", encoding="utf-8")
        env = os.environ.copy()
        env["MASTER_AGENT_RELEASE_VALIDATE_SKIP_CORE"] = "1"

        result = run_cmd(
            [
                ROOT / "scripts" / "release_validate.py",
                "--quick-validate",
                fake_quick,
            ],
            env=env,
        )
        self.assertIn("PASS: root skill quick_validate", result.stdout)
        self.assertIn("PASS: personal path scan", result.stdout)
        self.assertIn("PASS: secret scan", result.stdout)

    def test_release_validator_plugin_eval_is_optional_unless_required(self):
        env = os.environ.copy()
        env["MASTER_AGENT_RELEASE_VALIDATE_SKIP_CORE"] = "1"
        missing_command = "definitely-missing-plugin-eval-command evaluate-skill {skill}"

        optional = run_cmd(
            [
                ROOT / "scripts" / "release_validate.py",
                "--plugin-eval-command",
                missing_command,
            ],
            env=env,
        )
        self.assertIn("PASS: plugin-eval", optional.stdout)
        self.assertIn("skipped", optional.stdout)

        required = run_cmd(
            [
                ROOT / "scripts" / "release_validate.py",
                "--plugin-eval-command",
                missing_command,
                "--require-plugin-eval",
            ],
            env=env,
            check=False,
        )
        self.assertEqual(required.returncode, 1)
        self.assertIn("FAIL: plugin-eval", required.stdout)

    def test_soak_validator_runs_quick_profile(self):
        result = run_cmd([ROOT / "scripts" / "soak_validate.py", "--quick"])
        self.assertIn("Soak validation passed", result.stdout)

    def test_operating_system_hardening_docs_and_examples_exist(self):
        provider_reference = ROOT / "references" / "provider-command-adapter.md"
        workflow = ROOT / ".github" / "workflows" / "release-validate.yml"
        pre_commit = ROOT / "assets" / "examples" / "pre-commit-master-boundary.ps1"
        worktree_control = ROOT / "assets" / "templates" / "worktree-control.md"

        self.assertIn("Provider-Command Adapter Contract", provider_reference.read_text(encoding="utf-8"))
        self.assertIn("release-validate", workflow.read_text(encoding="utf-8"))
        self.assertIn("enforce-master-boundary", pre_commit.read_text(encoding="utf-8"))
        self.assertIn("Worktree Control", worktree_control.read_text(encoding="utf-8"))

    def test_generated_scripts_do_not_use_non_atomic_write_text(self):
        for relative_path in [
            "scripts/master_agent_tool.py",
            "scripts/bootstrap_project_state.py",
        ]:
            source = (ROOT / relative_path).read_text(encoding="utf-8").splitlines()
            offenders = [
                f"{line_no}: {line.strip()}"
                for line_no, line in enumerate(source, start=1)
                if ".write_text(" in line
            ]
            self.assertEqual([], offenders, relative_path)

    def test_custom_role_skill_can_be_scaffolded(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "define-role",
                "--state-dir",
                self.state_dir,
                "--role",
                "Domain Research",
                "--purpose",
                "Collect bounded project evidence before strategy decisions",
                "--allowed-work",
                "Read authority docs, inspect artifacts, and return evidence packets",
                "--forbidden-work",
                "Production implementation or final product decisions",
                "--return-packet",
                "role-receipt.md",
                "--scope",
                "docs/research",
                "--token-budget",
                "6000",
                "--max-heartbeats",
                "3",
                "--approval",
                "accepted role-proposal.md",
                "--activate",
            ]
        )

        skills_dir = self.tmp / "skills"
        scaffold = run_cmd(
            [
                TOOL,
                "scaffold-role-skill",
                "--state-dir",
                self.state_dir,
                "--role",
                "Domain Research",
                "--skills-dir",
                skills_dir,
            ]
        )
        self.assertIn("Scaffolded role skill", scaffold.stdout)

        skill_dir = skills_dir / "master-domain-research-agent"
        skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        metadata = (skill_dir / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("name: master-domain-research-agent", skill_text)
        self.assertIn("Domain Research Agent", skill_text)
        self.assertIn("Use $master-domain-research-agent", metadata)

        roles = json.loads(
            (self.state_dir / "state" / "roles.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            roles["Domain Research"]["role_skill"],
            "master-domain-research-agent",
        )

    def test_scaffold_role_skill_escapes_yaml_text(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "define-role",
                "--state-dir",
                self.state_dir,
                "--role",
                "Quoted Research",
                "--purpose",
                'Collect evidence: "source packets" before decisions',
                "--allowed-work",
                'Read "authority" docs: summarize evidence',
                "--forbidden-work",
                "Production edits: never",
                "--return-packet",
                "quoted-research-receipt.md",
                "--scope",
                "docs/research",
                "--token-budget",
                "6000",
                "--max-heartbeats",
                "3",
                "--approval",
                "accepted role-proposal.md",
                "--activate",
            ]
        )

        skills_dir = self.tmp / "skills"
        run_cmd(
            [
                TOOL,
                "scaffold-role-skill",
                "--state-dir",
                self.state_dir,
                "--role",
                "Quoted Research",
                "--skills-dir",
                skills_dir,
            ]
        )

        skill_dir = skills_dir / "master-quoted-research-agent"
        skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        metadata = (skill_dir / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('description: "Use when a Quoted Research Agent', skill_text)
        self.assertIn('\\"source packets\\"', skill_text)
        self.assertIn('default_prompt: "Use $master-quoted-research-agent', metadata)

    def test_scaffold_role_skill_output_passes_quick_validate(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        run_cmd(
            [
                TOOL,
                "define-role",
                "--state-dir",
                self.state_dir,
                "--role",
                "Validation Research",
                "--purpose",
                "Validate generated skill metadata",
                "--allowed-work",
                "Read artifacts and return compact evidence",
                "--forbidden-work",
                "Implementation or final product decisions",
                "--return-packet",
                "validation-research-receipt.md",
                "--scope",
                "docs/research",
                "--token-budget",
                "6000",
                "--max-heartbeats",
                "3",
                "--approval",
                "accepted role-proposal.md",
                "--activate",
            ]
        )

        skills_dir = self.tmp / "skills"
        run_cmd(
            [
                TOOL,
                "scaffold-role-skill",
                "--state-dir",
                self.state_dir,
                "--role",
                "Validation Research",
                "--skills-dir",
                skills_dir,
            ]
        )

        quick_validate = (
            Path.home()
            / ".codex"
            / "skills"
            / ".system"
            / "skill-creator"
            / "scripts"
            / "quick_validate.py"
        )
        result = run_cmd(
            [quick_validate, skills_dir / "master-validation-research-agent"],
            cwd=ROOT,
        )
        self.assertIn("Skill is valid!", result.stdout)

    def test_dynamic_role_governance_is_documented_across_pack(self):
        root_skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        reference = (ROOT / "references" / "master-agent-system.md").read_text(
            encoding="utf-8"
        )
        context_packet = (ROOT / "assets" / "templates" / "context-packet.md").read_text(
            encoding="utf-8"
        )
        role_proposal = (ROOT / "assets" / "templates" / "role-proposal.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("define-role", root_skill)
        self.assertIn("activate-role", root_skill)
        self.assertIn("scaffold-role-skill", root_skill)
        self.assertIn("role-catalog.md", root_skill)
        self.assertIn("Dynamic Role Governance", reference)
        self.assertIn("state/roles.json", reference)
        self.assertIn("active role from `role-catalog.md`", context_packet)
        self.assertIn("Existing Role Fit", role_proposal)

    def test_role_skills_are_present_and_triggerable(self):
        root_metadata = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("Use $master-agent-system", root_metadata)

        expected = {
            "master-strategy-agent": "Strategy Agent",
            "master-coding-agent": "Coding Agent",
            "master-review-agent": "Review Agent",
            "master-policy-review-agent": "Policy Review Agent",
            "master-learning-distiller-agent": "Learning Distiller Agent",
        }

        for folder, role_name in expected.items():
            skill_dir = ROOT / "role-skills" / folder
            skill_file = skill_dir / "SKILL.md"
            metadata_file = skill_dir / "agents" / "openai.yaml"
            self.assertTrue(skill_file.exists(), f"missing {skill_file}")
            self.assertTrue(metadata_file.exists(), f"missing {metadata_file}")

            skill_text = skill_file.read_text(encoding="utf-8")
            metadata = metadata_file.read_text(encoding="utf-8")
            self.assertIn(f"name: {folder}", skill_text)
            self.assertIn("description: Use when", skill_text)
            self.assertIn(role_name, skill_text)
            self.assertIn(f"Use ${folder}", metadata)

    def test_role_skills_can_be_installed_to_skills_dir(self):
        skills_dir = self.tmp / "skills"
        result = run_cmd(
            [TOOL, "install-role-skills", "--skills-dir", skills_dir]
        )
        self.assertIn("installed role skills", result.stdout.lower())

        expected = [
            "master-strategy-agent",
            "master-coding-agent",
            "master-review-agent",
            "master-policy-review-agent",
            "master-learning-distiller-agent",
        ]
        for folder in expected:
            self.assertTrue((skills_dir / folder / "SKILL.md").exists())
            self.assertTrue((skills_dir / folder / "agents" / "openai.yaml").exists())

    def test_full_system_can_be_installed_to_skills_dir(self):
        skills_dir = self.tmp / "skills"
        result = run_cmd([TOOL, "install-system", "--skills-dir", skills_dir])
        self.assertIn("installed master agent system", result.stdout.lower())
        self.assertTrue((skills_dir / "master-agent-system" / "SKILL.md").exists())
        self.assertTrue(
            (skills_dir / "master-agent-system" / "scripts" / "master_agent_tool.py").exists()
        )
        self.assertFalse((skills_dir / "master-agent-system" / ".git").exists())
        self.assertFalse((skills_dir / "master-agent-system" / ".gitignore").exists())
        self.assertFalse((skills_dir / "master-agent-system" / "docs").exists())
        self.assertFalse((skills_dir / "master-agent-system" / "tests").exists())
        self.assertTrue((skills_dir / "master-strategy-agent" / "SKILL.md").exists())

    def test_work_order_template_forces_parallel_safety_fields(self):
        work_order = (ROOT / "assets" / "templates" / "work-order.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Exclusive Write Set", work_order)
        self.assertIn("Artifact Namespace", work_order)
        self.assertIn("Worktree Mode", work_order)
        self.assertIn("Worktree Id", work_order)
        self.assertIn("Local Mutation Policy", work_order)
        self.assertIn("Remote Mutation Policy", work_order)
        self.assertIn("Merge Owner", work_order)
        self.assertIn("Conflict Protocol", work_order)


if __name__ == "__main__":
    unittest.main()
