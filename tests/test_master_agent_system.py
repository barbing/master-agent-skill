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


def run_cmd(args, cwd=ROOT, check=True):
    result = subprocess.run(
        [PYTHON, *map(str, args)],
        cwd=cwd,
        text=True,
        capture_output=True,
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
        packet.write_text(
            "# Strategy Packet\n\n## Recommendation\n\n- Recommended decision: Proceed\n",
            encoding="utf-8",
        )

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

        event_log = (self.state_dir / "event-log.md").read_text(encoding="utf-8")
        self.assertIn("strategy-accepted", event_log)
        self.assertIn("PLAN-1", event_log)

    def test_register_agent_requires_current_plan_when_strategy_sync_active(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        packet = self.tmp / "strategy-packet.md"
        packet.write_text("# Strategy Packet\n", encoding="utf-8")
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

    def test_stale_strategy_plan_is_reported(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
        packet = self.tmp / "strategy-packet.md"
        packet.write_text("# Strategy Packet\n", encoding="utf-8")
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

    def test_audit_agent_detects_repeated_next_action_loop(self):
        run_cmd([TOOL, "init", "--project-root", self.tmp])
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
        packet.write_text("# Strategy Packet\n", encoding="utf-8")
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
        packet.write_text("# Strategy Packet\n", encoding="utf-8")
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
        packet.write_text("# Strategy Packet\n", encoding="utf-8")
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
        runtime["last_recoveries"] = {"coding-1:reinforce-context": 2}
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
        context = self.tmp / "successor-context.md"
        context.write_text("# Successor Context\n\nInherited state\n", encoding="utf-8")

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
        self.assertEqual(schema["schema_version"], "1.0")
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
            ["0001-base-state", "0002-runtime-session-observability"],
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
        self.assertIn("Merge Owner", work_order)
        self.assertIn("Conflict Protocol", work_order)


if __name__ == "__main__":
    unittest.main()
