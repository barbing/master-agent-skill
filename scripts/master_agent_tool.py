#!/usr/bin/env python3
"""Operate a file-backed Master Agent state pack."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from state_io import (
    append_jsonl_locked,
    atomic_write_json,
    atomic_write_text,
    lock_is_recoverable,
    unlink_with_retry,
    with_lock,
)
from validate_state_pack import REQUIRED_HEADINGS, validate_state_pack


SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = SKILL_ROOT / "assets" / "templates"
DEFAULT_STATE_DIR = Path("docs") / "master-agent"
MONITORED_STATES = {"starting", "active", "validating"}
SAFETY_AUTONOMOUS_ACTIONS = {
    "read-state",
    "validate-state",
    "update-ledger",
    "record-event",
    "create-context-packet",
    "create-work-order",
    "monitor-heartbeats",
    "monitor-budget",
    "recommend-token-strategy",
}
SAFETY_REMEDIATION_ACTIONS = {
    "reinforce-context",
    "stop-agent",
    "spawn-successor",
    "split-task",
}
SAFETY_HUMAN_ACTIONS = {
    "change-production-behavior",
    "change-default-behavior",
    "change-validation-gate",
    "increase-hard-budget",
    "approve-policy-conflict",
    "activate-unreviewed-role",
}
SAFETY_FORBIDDEN_ACTIONS = {
    "edit-production-code",
    "bypass-validation",
    "suppress-errors",
    "continue-hard-token-limit",
    "register-inactive-role",
    "overwrite-user-work",
}
SAFETY_WARNING_BUDGET_IMPACT = 5_000
SAFETY_HARD_BUDGET_IMPACT = 20_000
USAGE_SOURCES = ("measured", "estimated", "self-reported")
USAGE_CONFIDENCES = ("low", "medium", "high")
LARGE_CONTINUATION_TOKENS = 5_000
CURRENT_SCHEMA_VERSION = "1.0"
ORDERED_MIGRATIONS = ["0001-base-state", "0002-runtime-session-observability"]

DEFAULT_ROLES = {
    "Master": {
        "status": "active",
        "role_type": "system",
        "purpose": "Control plane for ledgers, routing, monitoring, acceptance, and stop/go decisions.",
        "allowed_work": "Maintain state artifacts, work orders, context packets, role governance, and event logs.",
        "forbidden_work": "Production implementation, runtime behavior changes, and unaccepted project decisions.",
        "return_packet": "master-ledger.md and event-log.md",
        "scope": "docs/master-agent",
        "role_skill": "master-agent-system",
        "token_budget": None,
        "max_heartbeats": None,
        "activation_reason": "Default system role.",
    },
    "Strategy": {
        "status": "active",
        "role_type": "default",
        "purpose": "Diagnose architecture, compare options, and draft strategy packets or work orders.",
        "allowed_work": "Reasoning, diagnosis, options analysis, recommendations, and proposed work orders.",
        "forbidden_work": "Production implementation unless separately assigned as a Coding Agent.",
        "return_packet": "strategy-packet.md",
        "scope": "project-defined",
        "role_skill": "master-strategy-agent",
        "token_budget": None,
        "max_heartbeats": None,
        "activation_reason": "Default role.",
    },
    "Coding": {
        "status": "active",
        "role_type": "default",
        "purpose": "Execute one bounded implementation work order.",
        "allowed_work": "Scoped production edits, tests, validation, and implementation receipts.",
        "forbidden_work": "Architecture, scope, default behavior, or validation changes not authorized by the work order.",
        "return_packet": "coding-receipt.md",
        "scope": "work-order-defined",
        "role_skill": "master-coding-agent",
        "token_budget": None,
        "max_heartbeats": None,
        "activation_reason": "Default role.",
    },
    "Review": {
        "status": "active",
        "role_type": "default",
        "purpose": "Independently check diffs, artifacts, logs, validation output, and readiness claims.",
        "allowed_work": "Evidence review, scope checks, validation checks, findings, and verdicts.",
        "forbidden_work": "Product direction, implementation, or broad redesign.",
        "return_packet": "review-verdict.md",
        "scope": "evidence-defined",
        "role_skill": "master-review-agent",
        "token_budget": None,
        "max_heartbeats": None,
        "activation_reason": "Default role.",
    },
    "Policy Review": {
        "status": "active",
        "role_type": "default",
        "purpose": "Check proposals against authority docs, project policy, validation gates, and ownership boundaries.",
        "allowed_work": "Authority and policy compliance checks with conditions or blockers.",
        "forbidden_work": "Implementation, final product decisions, or replacing user authority.",
        "return_packet": "policy-verdict.md",
        "scope": "authority-defined",
        "role_skill": "master-policy-review-agent",
        "token_budget": None,
        "max_heartbeats": None,
        "activation_reason": "Default role.",
    },
}


def parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "+00:00")


def process_is_alive(pid_value: object) -> bool:
    try:
        pid = int(str(pid_value).strip())
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                text=True,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return pid == os.getpid()
        return result.returncode == 0 and f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_key_value_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for token in text.replace("\n", " ").split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def default_roles() -> dict[str, dict]:
    return json.loads(json.dumps(DEFAULT_ROLES))


def default_runtime_state() -> dict:
    return {
        "supervisor_state": "idle",
        "stop_requested": False,
        "last_check_at": "",
        "last_cycle_result": "",
        "last_recoveries": {},
        "same_recovery_count": {},
        "critical_breach_count": 0,
        "next_wakeup_at": "",
        "active_interventions": [],
        "deferred_actions": [],
        "stopped_agents": [],
    }


def default_schema_version() -> dict:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "compatible_tool": "master_agent_tool.py",
        "migration_history": [],
    }


def normalize_role_name(value: str) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise SystemExit("Role name cannot be empty")
    return normalized


def slugify_role(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "custom-role"


def default_role_skill_name(role_name: str) -> str:
    slug = slugify_role(role_name)
    if slug.startswith("master-") and slug.endswith("-agent"):
        return slug
    return f"master-{slug}-agent"


def table_value(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "/").replace("\n", " ").strip()


def yaml_quoted(value: object) -> str:
    return json.dumps(" ".join(str(value).split()))


def empty_usage_breakdown(keys: tuple[str, ...]) -> dict[str, int]:
    return {key: 0 for key in keys}


def ensure_usage_breakdowns(container: dict) -> None:
    source_totals = container.setdefault("usage_by_source", {})
    for source in USAGE_SOURCES:
        source_totals.setdefault(source, 0)
    confidence_totals = container.setdefault("usage_by_confidence", {})
    for confidence in USAGE_CONFIDENCES:
        confidence_totals.setdefault(confidence, 0)


def add_usage_breakdown(
    container: dict,
    tokens_used: int,
    source: str,
    confidence: str,
) -> None:
    ensure_usage_breakdowns(container)
    container["usage_by_source"][source] = (
        int(container["usage_by_source"].get(source) or 0) + tokens_used
    )
    container["usage_by_confidence"][confidence] = (
        int(container["usage_by_confidence"].get(confidence) or 0) + tokens_used
    )


def state_dir_from_args(args: argparse.Namespace) -> Path:
    if getattr(args, "state_dir", None):
        return Path(args.state_dir).resolve()
    project_root = Path(getattr(args, "project_root", ".")).resolve()
    return (project_root / DEFAULT_STATE_DIR).resolve()


def ensure_within_project(project_root: Path, target_dir: Path) -> None:
    try:
        target_dir.relative_to(project_root)
    except ValueError as exc:
        raise SystemExit(f"Refusing to write outside project root: {target_dir}") from exc


def ensure_state_storage(state_dir: Path) -> None:
    storage_dir = state_dir / "state"
    storage_dir.mkdir(parents=True, exist_ok=True)
    agents_path = storage_dir / "agents.json"
    heartbeats_path = storage_dir / "heartbeats.jsonl"
    strategy_sync_path = storage_dir / "strategy-sync.jsonl"
    anomalies_path = storage_dir / "anomalies.jsonl"
    budget_path = storage_dir / "budget.json"
    usage_path = storage_dir / "token-usage.jsonl"
    roles_path = storage_dir / "roles.json"
    runtime_path = storage_dir / "runtime.json"
    session_control_path = storage_dir / "session-control.jsonl"
    incidents_path = storage_dir / "incidents.jsonl"
    alerts_path = storage_dir / "alerts.jsonl"
    schema_path = storage_dir / "schema-version.json"
    if not agents_path.exists():
        atomic_write_text(agents_path, "{}\n")
    if not heartbeats_path.exists():
        atomic_write_text(heartbeats_path, "")
    if not strategy_sync_path.exists():
        atomic_write_text(strategy_sync_path, "")
    if not anomalies_path.exists():
        atomic_write_text(anomalies_path, "")
    if not roles_path.exists():
        atomic_write_json(roles_path, default_roles())
    else:
        try:
            roles = json.loads(roles_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            roles = None
        if isinstance(roles, dict):
            changed = False
            for role_name, role_definition in default_roles().items():
                if role_name not in roles:
                    roles[role_name] = role_definition
                    changed = True
            if changed:
                atomic_write_json(roles_path, roles)
    if not budget_path.exists():
        save_budget(
            state_dir,
            {
                "project_budget": None,
                "project_used": 0,
                "warning_percent": 80,
                "hard_percent": 100,
                "usage_by_source": empty_usage_breakdown(USAGE_SOURCES),
                "usage_by_confidence": empty_usage_breakdown(USAGE_CONFIDENCES),
                "agents": {},
            },
        )
    if not usage_path.exists():
        atomic_write_text(usage_path, "")
    if not runtime_path.exists():
        atomic_write_json(runtime_path, default_runtime_state())
    if not session_control_path.exists():
        atomic_write_text(session_control_path, "")
    if not incidents_path.exists():
        atomic_write_text(incidents_path, "")
    if not alerts_path.exists():
        atomic_write_text(alerts_path, "")
    if not schema_path.exists():
        atomic_write_json(schema_path, default_schema_version())


def load_agents(state_dir: Path) -> dict[str, dict[str, str]]:
    ensure_state_storage(state_dir)
    agents_path = state_dir / "state" / "agents.json"
    try:
        data = json.loads(agents_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid agents state file: {agents_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid agents state file: {agents_path}: expected object")
    return data


def save_agents(state_dir: Path, agents: dict[str, dict[str, str]]) -> None:
    agents_path = state_dir / "state" / "agents.json"
    atomic_write_json(agents_path, agents)


def append_heartbeat(state_dir: Path, entry: dict[str, str]) -> None:
    heartbeats_path = state_dir / "state" / "heartbeats.jsonl"
    append_jsonl_locked(heartbeats_path, entry)


def load_heartbeats(state_dir: Path) -> list[dict]:
    ensure_state_storage(state_dir)
    heartbeats_path = state_dir / "state" / "heartbeats.jsonl"
    entries: list[dict] = []
    for line in heartbeats_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid heartbeat history: {heartbeats_path}: {exc}") from exc
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def load_agent_heartbeats(state_dir: Path, agent_id: str) -> list[dict]:
    return [entry for entry in load_heartbeats(state_dir) if entry.get("agent_id") == agent_id]


def load_budget(state_dir: Path) -> dict:
    ensure_state_storage(state_dir)
    budget_path = state_dir / "state" / "budget.json"
    try:
        data = json.loads(budget_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid budget state file: {budget_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid budget state file: {budget_path}: expected object")
    data.setdefault("project_budget", None)
    data.setdefault("project_used", 0)
    data.setdefault("warning_percent", 80)
    data.setdefault("hard_percent", 100)
    data.setdefault("agents", {})
    data.setdefault("project_measured_used", 0)
    data.setdefault("project_estimated_used", 0)
    data.setdefault("project_self_reported_used", 0)
    ensure_usage_breakdowns(data)
    for agent_budget in data.get("agents", {}).values():
        if isinstance(agent_budget, dict):
            ensure_usage_breakdowns(agent_budget)
    return data


def save_budget(state_dir: Path, budget: dict) -> None:
    budget_path = state_dir / "state" / "budget.json"
    atomic_write_json(budget_path, budget)


def append_token_usage(state_dir: Path, entry: dict) -> None:
    usage_path = state_dir / "state" / "token-usage.jsonl"
    append_jsonl_locked(usage_path, entry)


def append_anomaly(state_dir: Path, entry: dict) -> None:
    anomaly_path = state_dir / "state" / "anomalies.jsonl"
    append_jsonl_locked(anomaly_path, entry)


def load_anomalies(state_dir: Path) -> list[dict]:
    ensure_state_storage(state_dir)
    anomaly_path = state_dir / "state" / "anomalies.jsonl"
    anomalies: list[dict] = []
    for line in anomaly_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid anomaly history: {anomaly_path}: {exc}") from exc
        if isinstance(entry, dict):
            anomalies.append(entry)
    return anomalies


def load_runtime(state_dir: Path) -> dict:
    ensure_state_storage(state_dir)
    runtime_path = state_dir / "state" / "runtime.json"
    try:
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid runtime state file: {runtime_path}: {exc}") from exc
    if not isinstance(runtime, dict):
        raise SystemExit(f"Invalid runtime state file: {runtime_path}: expected object")
    defaults = default_runtime_state()
    for key, value in defaults.items():
        runtime.setdefault(key, value)
    return runtime


def save_runtime(state_dir: Path, runtime: dict) -> None:
    atomic_write_json(state_dir / "state" / "runtime.json", runtime)


def render_runtime_status(
    state_dir: Path,
    runtime: dict,
    active_agents: list[str],
    validation_result: str,
    heartbeat_result: str,
    budget_result: str,
    strategy_result: str,
) -> None:
    lines = [
        "# Runtime Status",
        "",
        "## Supervisor State",
        "",
        f"- State: {runtime.get('supervisor_state', 'idle')}",
        f"- Last cycle result: {runtime.get('last_cycle_result', '')}",
        f"- Stop requested: {'yes' if runtime.get('stop_requested') else 'no'}",
        "",
        "## Last Check",
        "",
        f"- Checked at: {runtime.get('last_check_at', '')}",
        f"- Validation result: {validation_result}",
        f"- Heartbeat result: {heartbeat_result}",
        f"- Budget result: {budget_result}",
        f"- Strategy sync result: {strategy_result}",
        "",
        "## Active Interventions",
        "",
    ]
    interventions = runtime.get("active_interventions") or []
    lines.extend([f"- {item}" for item in interventions] or ["- none"])
    lines.extend(
        [
            "",
            "## Next Wakeup",
            "",
            f"- {runtime.get('next_wakeup_at', '') or 'not scheduled'}",
            "",
            "## Handoff Summary",
            "",
            f"- Active plan: {runtime.get('active_plan_id', '') or 'none'}",
            f"- Active agents: {', '.join(active_agents) if active_agents else 'none'}",
            f"- Anomalies handled: {', '.join(runtime.get('anomalies_handled') or []) or 'none'}",
            f"- Deferred actions: {', '.join(runtime.get('deferred_actions') or []) or 'none'}",
            f"- Stopped agents: {', '.join(runtime.get('stopped_agents') or []) or 'none'}",
            f"- Human attention needed: {'yes' if runtime.get('human_attention_needed') else 'no'}",
        ]
    )
    atomic_write_text(state_dir / "runtime-status.md", "\n".join(lines) + "\n")


def render_anomaly_log(state_dir: Path) -> None:
    anomalies = load_anomalies(state_dir)
    lines = [
        "# Anomaly Log",
        "",
        "## Active Anomalies",
        "",
        "| Time | Agent Id | Type | Severity | Evidence | Recommended Action |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if anomalies:
        for entry in anomalies[-20:]:
            lines.append(
                "| {time} | {agent_id} | {type} | {severity} | {evidence} | {action} |".format(
                    time=table_value(entry.get("time")),
                    agent_id=table_value(entry.get("agent_id")),
                    type=table_value(entry.get("type")),
                    severity=table_value(entry.get("severity")),
                    evidence=table_value(entry.get("evidence")),
                    action=table_value(entry.get("recommended_action")),
                )
            )
    else:
        lines.append("|  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Append Only Anomalies",
            "",
            "Use one entry per detected loop, plan mismatch, scope drift, evidence-free success claim, reward-hacking pattern, validation anomaly, or token-risk anomaly.",
        ]
    )
    for entry in anomalies:
        lines.extend(
            [
                "",
                "### Anomaly",
                "",
                f"- Time: {entry.get('time', '')}",
                f"- Agent id: {entry.get('agent_id', '')}",
                f"- Type: {entry.get('type', '')}",
                f"- Severity: {entry.get('severity', '')}",
                f"- Evidence: {entry.get('evidence', '')}",
                f"- Recommended action: {entry.get('recommended_action', '')}",
            ]
        )
    atomic_write_text(state_dir / "anomaly-log.md", "\n".join(lines) + "\n")


def append_strategy_sync(state_dir: Path, entry: dict) -> None:
    sync_path = state_dir / "state" / "strategy-sync.jsonl"
    append_jsonl_locked(sync_path, entry)


def load_strategy_sync_history(state_dir: Path) -> list[dict]:
    ensure_state_storage(state_dir)
    sync_path = state_dir / "state" / "strategy-sync.jsonl"
    history: list[dict] = []
    for line in sync_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid strategy sync history: {sync_path}: {exc}") from exc
        if isinstance(entry, dict):
            history.append(entry)
    return history


def current_strategy_plan(state_dir: Path) -> dict | None:
    history = load_strategy_sync_history(state_dir)
    if not history:
        return None
    return history[-1]


def render_strategy_sync(state_dir: Path, entry: dict | None, status: str = "current") -> None:
    plan_id = entry.get("plan_id", "") if entry else ""
    summary = entry.get("summary", "") if entry else ""
    accepted_at = entry.get("accepted_at", "") if entry else ""
    packet = entry.get("packet", "") if entry else ""
    lines = [
        "# Strategy Sync",
        "",
        "## Current Accepted Plan",
        "",
        f"- Plan id: {plan_id}",
        f"- Summary: {summary}",
        f"- Accepted at: {accepted_at}",
        f"- Strategy packet: {packet}",
        f"- Status: {status if entry else 'none'}",
        "",
        "## Strategy Sessions",
        "",
        "| Agent Id | Question | Packet | Status |",
        "| --- | --- | --- | --- |",
        "|  |  |  |  |",
        "",
        "## Plan Version",
        "",
        f"- Current plan id: {plan_id}",
        "- Previous plan id:",
        f"- Version changed at: {accepted_at}",
        "- Requires resync: no",
        "",
        "## Active Work Orders",
        "",
        "| Work Order | Plan Id | Agent | Status |",
        "| --- | --- | --- | --- |",
        "|  |  |  |  |",
        "",
        "## Master Awareness",
        "",
        f"- Master has accepted the current plan: {'yes' if entry else 'no'}",
        "- Agents must register with current plan id: yes",
        f"- Last sync check: {format_time(parse_time(None))}",
        "",
        "## Resync Triggers",
        "",
        "- Strategy packet changes the accepted plan.",
        "- User changes project direction.",
        "- Running agent reports plan mismatch.",
        "- Plan age exceeds the stale threshold.",
        "- Authority docs conflict with accepted plan.",
        "",
    ]
    atomic_write_text(state_dir / "strategy-sync.md", "\n".join(lines) + "\n")


def append_event_log(
    state_dir: Path,
    event_type: str,
    related_packet: str,
    summary: str,
    evidence: str,
    ledger_update: str,
    next_action: str,
    at: str,
) -> None:
    event_path = state_dir / "event-log.md"
    lock_path = event_path.with_suffix(event_path.suffix + ".lock")
    with with_lock(lock_path):
        if not event_path.exists():
            atomic_write_text(event_path, "# Event Log\n\n## Append Only Events\n")
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(
                "\n### Event\n\n"
                f"- Date: {at}\n"
                f"- Event type: {event_type}\n"
                f"- Related packet: {related_packet}\n"
                f"- Summary: {summary}\n"
                f"- Evidence: {evidence}\n"
                f"- Ledger update: {ledger_update}\n"
                f"- Next action: {next_action}\n"
            )
            handle.flush()
            os.fsync(handle.fileno())


def load_roles(state_dir: Path) -> dict[str, dict]:
    ensure_state_storage(state_dir)
    roles_path = state_dir / "state" / "roles.json"
    try:
        data = json.loads(roles_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid roles state file: {roles_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid roles state file: {roles_path}: expected object")
    changed = False
    for role_name, role_definition in default_roles().items():
        if role_name not in data:
            data[role_name] = role_definition
            changed = True
    for role_name, definition in data.items():
        if not isinstance(definition, dict):
            raise SystemExit(
                f"Invalid roles state file: {roles_path}: {role_name!r} must be an object"
            )
        definition.setdefault("status", "proposed")
        definition.setdefault("role_type", "custom")
        definition.setdefault("purpose", "")
        definition.setdefault("allowed_work", "")
        definition.setdefault("forbidden_work", "")
        definition.setdefault("return_packet", "role-receipt.md")
        definition.setdefault("scope", "")
        definition.setdefault("role_skill", "")
        definition.setdefault("token_budget", None)
        definition.setdefault("max_heartbeats", None)
        definition.setdefault("activation_reason", "")
        definition.setdefault(
            "deactivation_condition",
            "Role is no longer needed or overlaps active roles.",
        )
    if changed:
        save_roles(state_dir, data)
    return data


def save_roles(state_dir: Path, roles: dict[str, dict]) -> None:
    roles_path = state_dir / "state" / "roles.json"
    atomic_write_json(roles_path, roles)
    render_role_catalog(state_dir, roles)


def render_role_catalog(state_dir: Path, roles: dict[str, dict]) -> None:
    lines = [
        "# Role Catalog",
        "",
        "## Role Governance",
        "",
        "- Register agents only with active roles from this catalog.",
        "- Prefer default roles before defining a custom role.",
        "- Define custom roles only when the project has a recurring or specialized responsibility that does not fit Strategy, Coding, Review, or Policy Review.",
        "- Keep custom roles bounded by allowed work, forbidden work, return packet, scope, positive token budget, positive heartbeat cap, activation approval, deactivation condition, and activation status.",
        "",
        "## Active Roles",
        "",
        "| Role | Type | Purpose | Return Packet | Role Skill | Token Budget | Heartbeat Cap |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    active_rows = 0
    for role_name, definition in sorted(roles.items()):
        if definition.get("status") != "active":
            continue
        active_rows += 1
        lines.append(
            "| {role} | {role_type} | {purpose} | {return_packet} | {role_skill} | {token_budget} | {max_heartbeats} |".format(
                role=table_value(role_name),
                role_type=table_value(definition.get("role_type")),
                purpose=table_value(definition.get("purpose")),
                return_packet=table_value(definition.get("return_packet")),
                role_skill=table_value(definition.get("role_skill")),
                token_budget=table_value(definition.get("token_budget")),
                max_heartbeats=table_value(definition.get("max_heartbeats")),
            )
        )
    if active_rows == 0:
        lines.append("|  |  |  |  |  |  |  |")

    lines.extend(
        [
            "",
            "## Inactive Or Proposed Roles",
            "",
            "| Role | Status | Type | Purpose | Activation Reason |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    inactive_rows = 0
    for role_name, definition in sorted(roles.items()):
        if definition.get("status") == "active":
            continue
        inactive_rows += 1
        lines.append(
            "| {role} | {status} | {role_type} | {purpose} | {activation_reason} |".format(
                role=table_value(role_name),
                status=table_value(definition.get("status")),
                role_type=table_value(definition.get("role_type")),
                purpose=table_value(definition.get("purpose")),
                activation_reason=table_value(definition.get("activation_reason")),
            )
        )
    if inactive_rows == 0:
        lines.append("|  |  |  |  |  |")

    lines.extend(
        [
            "",
            "## Role Creation Rules",
            "",
            "- A new role must explain why existing active roles are insufficient.",
            "- A new role must define allowed work, forbidden work, return packet, scope, positive token budget, positive heartbeat cap, activation approval, and stop conditions.",
            "- A custom role must define when it should be deactivated.",
            "- A new role must not become a permanent memory store or broad discussion agent.",
            "- A new role must remain subordinate to the Master Agent and project policy pack.",
            "",
            "## Role Activation Rules",
            "",
            "- Activate a custom role only after the role proposal is accepted, the Master records an approval basis, or the user explicitly approves it.",
            "- Activation commands must include `--approval` evidence for custom roles.",
            "- Deactivate roles that are stale, overlapping, too broad, or no longer needed.",
            "- Do not register a sub-agent with an undefined, proposed, or inactive role.",
            "- Optional role skills may be scaffolded for reusable roles, but project-local role catalog state remains authoritative.",
            "",
        ]
    )
    atomic_write_text(state_dir / "role-catalog.md", "\n".join(lines) + "\n")


def require_role(state_dir: Path, role_name: str) -> tuple[str, dict]:
    role_name = normalize_role_name(role_name)
    roles = load_roles(state_dir)
    if role_name not in roles:
        raise SystemExit(f"Undefined role: {role_name}")
    return role_name, roles[role_name]


def require_active_role(state_dir: Path, role_name: str) -> tuple[str, dict]:
    role_name, definition = require_role(state_dir, role_name)
    if definition.get("status") != "active":
        raise SystemExit(f"Inactive role: {role_name}")
    return role_name, definition


def render_running_agents(state_dir: Path, agents: dict[str, dict[str, str]]) -> None:
    budget = load_budget(state_dir)
    lines = [
        "# Running Agents",
        "",
        "## Active Agents",
        "",
        "| Agent Id | Role | Task Id | Objective | Scope | Last Heartbeat | Status | Tokens Used | Token Budget | Heartbeats | Heartbeat Cap |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    if agents:
        for agent_id in sorted(agents):
            agent = agents[agent_id]
            lines.append(
                "| {agent_id} | {role} | {task_id} | {objective} | {scope} | {last_heartbeat_at} | {status} | {tokens_used} | {token_budget} | {heartbeat_count} | {max_heartbeats} |".format(
                    agent_id=agent_id,
                    role=agent.get("role", ""),
                    task_id=agent.get("task_id", ""),
                    objective=agent.get("objective", ""),
                    scope=agent.get("scope", ""),
                    last_heartbeat_at=agent.get("last_heartbeat_at", ""),
                    status=agent.get("status", ""),
                    tokens_used=agent.get("tokens_used", ""),
                    token_budget=agent.get("token_budget", ""),
                    heartbeat_count=agent.get("heartbeat_count", ""),
                    max_heartbeats=agent.get("max_heartbeats", ""),
                )
            )
    else:
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |")

    lines.extend(
        [
            "",
            "## Heartbeat Expectations",
            "",
            "- Required at startup acknowledgement.",
            "- Required before risky edits.",
            "- Required after validation.",
            "- Required when blocked.",
            "- Required before changing scope.",
            "",
            "## Stale Agents",
            "",
            "| Agent Id | Last Known Status | Action Needed |",
            "| --- | --- | --- |",
            "|  |  |  |",
            "",
            "## Parallelism Decision",
            "",
            "- Current mode: single-agent | parallel",
            "- Reason:",
            "- Collision risks:",
            "- Artifact path separation:",
            "",
            "## Token Controls",
            "",
            f"- Project token budget: {budget.get('project_budget') or ''}",
            f"- Tokens used: {budget.get('project_used') or 0}",
            f"- Warning threshold: {budget.get('warning_percent') or ''}%",
            f"- Hard threshold: {budget.get('hard_percent') or ''}%",
            "- Session creation cap:",
            "- Active token strategy: token-strategy.md",
            "- Next token action:",
            "",
        ]
    )
    atomic_write_text(state_dir / "running-agents.md", "\n".join(lines) + "\n")


def command_init(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    if not project_root.exists() or not project_root.is_dir():
        print(f"Project root does not exist or is not a directory: {project_root}", file=sys.stderr)
        return 2

    target_dir = (project_root / args.state_dir).resolve()
    ensure_within_project(project_root, target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    skipped: list[Path] = []
    overwritten: list[Path] = []
    for template in sorted(TEMPLATE_DIR.glob("*.md")):
        destination = target_dir / template.name
        if destination.exists() and not args.force:
            skipped.append(destination)
            continue
        if destination.exists():
            overwritten.append(destination)
        else:
            created.append(destination)
        shutil.copyfile(template, destination)

    ensure_state_storage(target_dir)
    render_role_catalog(target_dir, load_roles(target_dir))
    print(f"Master Agent state pack: {target_dir}")
    for label, paths in (("created", created), ("overwritten", overwritten), ("skipped", skipped)):
        if paths:
            print(f"{label}:")
            for path in paths:
                print(f"  {path.name}")
    return 0


def _state_file_paths(state_dir: Path) -> list[Path]:
    return [
        state_dir / "state" / "agents.json",
        state_dir / "state" / "roles.json",
        state_dir / "state" / "heartbeats.jsonl",
        state_dir / "state" / "strategy-sync.jsonl",
        state_dir / "state" / "anomalies.jsonl",
        state_dir / "state" / "budget.json",
        state_dir / "state" / "token-usage.jsonl",
        state_dir / "state" / "runtime.json",
        state_dir / "state" / "session-control.jsonl",
        state_dir / "state" / "incidents.jsonl",
        state_dir / "state" / "alerts.jsonl",
        state_dir / "state" / "schema-version.json",
    ]


def _template_primary_heading(template_path: Path) -> str:
    required = REQUIRED_HEADINGS.get(template_path.name, [])
    if required:
        return required[0]
    for line in template_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line.strip()
    return ""


def command_upgrade_state(args: argparse.Namespace) -> int:
    if args.state_dir:
        target_dir = Path(args.state_dir).resolve()
    else:
        project_root = Path(args.project_root).resolve()
        if not project_root.exists() or not project_root.is_dir():
            print(f"Project root does not exist or is not a directory: {project_root}", file=sys.stderr)
            return 2
        target_dir = (project_root / DEFAULT_STATE_DIR).resolve()
        ensure_within_project(project_root, target_dir)

    target_dir.mkdir(parents=True, exist_ok=True)
    missing_state_files = [path for path in _state_file_paths(target_dir) if not path.exists()]
    ensure_state_storage(target_dir)

    created: list[Path] = []
    skipped: list[Path] = []
    overwritten: list[Path] = []
    conflicts: list[Path] = []

    for template in sorted(TEMPLATE_DIR.glob("*.md")):
        destination = target_dir / template.name
        primary_heading = _template_primary_heading(template)
        if destination.exists() and not args.force:
            existing_text = destination.read_text(encoding="utf-8")
            if not existing_text.strip():
                shutil.copyfile(template, destination)
                overwritten.append(destination)
            elif primary_heading and primary_heading not in existing_text:
                conflicts.append(destination)
            else:
                skipped.append(destination)
            continue
        if destination.exists():
            overwritten.append(destination)
        else:
            created.append(destination)
        shutil.copyfile(template, destination)

    roles = load_roles(target_dir)
    render_role_catalog(target_dir, roles)

    print(f"Master Agent state upgrade: {target_dir}")
    if missing_state_files:
        print("state initialized:")
        for path in missing_state_files:
            print(f"  {path.relative_to(target_dir).as_posix()}")
    for label, paths in (("created", created), ("overwritten", overwritten), ("skipped", skipped), ("conflicts", conflicts)):
        if paths:
            print(f"{label}:")
            for path in paths:
                print(f"  {path.name}")
    if conflicts:
        print("Manual merge required for conflicted files.")
        return 1
    return 0


def command_validate(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    errors = validate_state_pack(state_dir, strict=args.strict)
    if errors and errors[0].startswith("State directory does not exist:"):
        print(errors[0], file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"State pack is valid: {state_dir}")
    return 0


def command_register_agent(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    errors = validate_state_pack(state_dir)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    role_name, role_definition = require_active_role(state_dir, args.role)
    accepted_plan = current_strategy_plan(state_dir)
    if accepted_plan:
        current_plan_id = accepted_plan.get("plan_id")
        if args.plan_id != current_plan_id:
            print(
                f"Registering agent requires current plan {current_plan_id}",
                file=sys.stderr,
            )
            return 1
    timestamp = format_time(parse_time(args.at))
    token_budget = (
        args.token_budget
        if args.token_budget is not None
        else role_definition.get("token_budget")
    )
    max_heartbeats = (
        args.max_heartbeats
        if args.max_heartbeats is not None
        else role_definition.get("max_heartbeats")
    )
    agents = load_agents(state_dir)
    agents[args.agent_id] = {
        "role": role_name,
        "task_id": args.task_id,
        "objective": args.objective,
        "scope": args.scope,
        "status": args.status,
        "registered_at": timestamp,
        "last_heartbeat_at": timestamp,
        "last_action": "registered",
        "next_action": "send first heartbeat",
        "scope_status": "yes",
        "confidence": "medium",
        "risk": "",
        "plan_id": args.plan_id or "",
        "token_budget": str(token_budget or ""),
        "tokens_used": "0",
        "max_heartbeats": str(max_heartbeats or ""),
        "heartbeat_count": "0",
    }
    budget = load_budget(state_dir)
    budget["agents"].setdefault(
        args.agent_id,
        {
            "token_budget": token_budget,
            "tokens_used": 0,
            "usage_by_source": empty_usage_breakdown(USAGE_SOURCES),
            "usage_by_confidence": empty_usage_breakdown(USAGE_CONFIDENCES),
            "max_heartbeats": max_heartbeats,
            "heartbeat_count": 0,
        },
    )
    ensure_usage_breakdowns(budget["agents"][args.agent_id])
    budget["agents"][args.agent_id]["token_budget"] = token_budget
    budget["agents"][args.agent_id]["max_heartbeats"] = max_heartbeats
    save_budget(state_dir, budget)
    save_agents(state_dir, agents)
    render_running_agents(state_dir, agents)
    print(f"Registered agent {args.agent_id}")
    return 0


def command_heartbeat(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    timestamp = format_time(parse_time(args.at))
    agents = load_agents(state_dir)
    if args.agent_id not in agents:
        print(f"Unknown agent id: {args.agent_id}", file=sys.stderr)
        return 1

    agent = agents[args.agent_id]
    heartbeat_count = int(agent.get("heartbeat_count") or "0") + 1
    agent.update(
        {
            "status": args.state,
            "last_heartbeat_at": timestamp,
            "current": args.current,
            "last_action": args.last_action,
            "next_action": args.next_action,
            "files_changed": args.files_changed or "",
            "artifacts": args.artifacts or "",
            "commands": args.commands or "",
            "plan_id": args.plan_id or agent.get("plan_id", ""),
            "plan_alignment": args.plan_alignment or "unsure",
            "repeated_action_count": str(args.repeated_action_count or ""),
            "evidence_quality": args.evidence_quality or "weak",
            "self_reported_anomaly": args.self_reported_anomaly or "",
            "scope_status": args.scope_status,
            "confidence": args.confidence,
            "risk": args.risk or "",
            "heartbeat_count": str(heartbeat_count),
        }
    )
    budget = load_budget(state_dir)
    budget_agent = budget["agents"].setdefault(args.agent_id, {})
    budget_agent["heartbeat_count"] = heartbeat_count
    if "max_heartbeats" not in budget_agent:
        max_heartbeats = agent.get("max_heartbeats")
        budget_agent["max_heartbeats"] = int(max_heartbeats) if max_heartbeats else None
    save_budget(state_dir, budget)
    entry = {"agent_id": args.agent_id, **agent}
    append_heartbeat(state_dir, entry)
    save_agents(state_dir, agents)
    render_running_agents(state_dir, agents)
    print(f"Recorded heartbeat for {args.agent_id}")
    return 0


def command_set_budget(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    budget = load_budget(state_dir)
    budget["project_budget"] = args.project_budget
    budget["warning_percent"] = args.warning_percent
    budget["hard_percent"] = args.hard_percent
    save_budget(state_dir, budget)
    print(
        f"Project budget set: {args.project_budget} tokens "
        f"(warning={args.warning_percent}%, hard={args.hard_percent}%)"
    )
    return 0


def command_record_usage(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    timestamp = format_time(parse_time(args.at))
    source = args.source or "self-reported"
    confidence = args.confidence or "medium"
    source_key = source.replace("-", "_")
    source_total_key = f"{source_key}_tokens_used"
    project_source_key = f"project_{source_key}_used"
    budget_lock = state_dir / "state" / "budget.json.lock"
    with with_lock(budget_lock, timeout_seconds=30):
        budget = load_budget(state_dir)
        budget["project_used"] = int(budget.get("project_used") or 0) + args.tokens_used
        budget[project_source_key] = int(budget.get(project_source_key) or 0) + args.tokens_used
        add_usage_breakdown(budget, args.tokens_used, source, confidence)
        agent_budget = budget["agents"].setdefault(args.agent_id, {})
        ensure_usage_breakdowns(agent_budget)
        agent_budget["tokens_used"] = int(agent_budget.get("tokens_used") or 0) + args.tokens_used
        for key in [
            "measured_tokens_used",
            "estimated_tokens_used",
            "self_reported_tokens_used",
        ]:
            agent_budget.setdefault(key, 0)
        agent_budget[source_total_key] = int(agent_budget.get(source_total_key) or 0) + args.tokens_used
        add_usage_breakdown(agent_budget, args.tokens_used, source, confidence)
        agent_budget["last_usage_source"] = source
        agent_budget["last_usage_confidence"] = confidence
        if confidence == "low" or source in {"estimated", "self-reported"}:
            agent_budget["has_uncertain_usage"] = True
        if confidence == "low":
            agent_budget["has_low_confidence_usage"] = True
        save_budget(state_dir, budget)

        agents = load_agents(state_dir)
        if args.agent_id in agents:
            agents[args.agent_id]["tokens_used"] = str(agent_budget["tokens_used"])
            save_agents(state_dir, agents)
            render_running_agents(state_dir, agents)

        append_token_usage(
            state_dir,
            {
                "at": timestamp,
                "agent_id": args.agent_id,
                "tokens_used": args.tokens_used,
                "source": source,
                "confidence": confidence,
                "note": args.note or "",
            },
        )
    print(f"Recorded {args.tokens_used} tokens for {args.agent_id}")
    return 0


def budget_findings(state_dir: Path) -> tuple[int, list[str]]:
    budget = load_budget(state_dir)
    findings: list[str] = []
    exit_code = 0
    project_budget = budget.get("project_budget")
    project_used = int(budget.get("project_used") or 0)
    warning_percent = float(budget.get("warning_percent") or 80)
    hard_percent = float(budget.get("hard_percent") or 100)

    if project_budget:
        warning_at = int(project_budget * warning_percent / 100)
        hard_at = int(project_budget * hard_percent / 100)
        if project_used >= hard_at:
            findings.append(
                f"Hard limit reached: project used {project_used} / {project_budget}"
            )
            exit_code = max(exit_code, 2)
        elif project_used >= warning_at:
            findings.append(
                f"Warning: project used {project_used} / {project_budget}"
            )
            exit_code = max(exit_code, 1)

    for agent_id, agent_budget in sorted(budget.get("agents", {}).items()):
        agent_limit = agent_budget.get("token_budget")
        agent_used = int(agent_budget.get("tokens_used") or 0)
        if agent_limit and agent_used >= int(agent_limit):
            findings.append(
                f"Hard limit reached: {agent_id} used {agent_used} / {agent_limit}"
            )
            exit_code = max(exit_code, 2)
        max_heartbeats = agent_budget.get("max_heartbeats")
        heartbeat_count = int(agent_budget.get("heartbeat_count") or 0)
        if max_heartbeats and heartbeat_count > int(max_heartbeats):
            findings.append(
                f"Warning: {agent_id} heartbeat cap exceeded "
                f"({heartbeat_count} / {max_heartbeats})"
            )
            exit_code = max(exit_code, 1)

    if not findings:
        findings.append("Within budget")
    return exit_code, findings


def command_check_budget(args: argparse.Namespace) -> int:
    exit_code, findings = budget_findings(Path(args.state_dir).resolve())
    for finding in findings:
        print(finding)
    return exit_code


def command_budget_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    budget = load_budget(state_dir)
    project_budget = budget.get("project_budget")
    project_used = int(budget.get("project_used") or 0)
    print(f"Project used: {project_used} / {project_budget or 'unbounded'}")
    print(
        f"Thresholds: warning={budget.get('warning_percent')}%, "
        f"hard={budget.get('hard_percent')}%"
    )
    for agent_id, agent_budget in sorted(budget.get("agents", {}).items()):
        print(
            f"{agent_id}: tokens={agent_budget.get('tokens_used', 0)} / "
            f"{agent_budget.get('token_budget') or 'unbounded'}, "
            f"heartbeats={agent_budget.get('heartbeat_count', 0)} / "
            f"{agent_budget.get('max_heartbeats') or 'unbounded'}"
        )
    return 0


def command_safety_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    safety_path = state_dir / "safety-envelope.md"
    if not safety_path.exists():
        print(f"Missing safety envelope: {safety_path}", file=sys.stderr)
        return 1
    print(f"Safety envelope: {safety_path}")
    print("Autonomous authority:")
    for action in sorted(SAFETY_AUTONOMOUS_ACTIONS):
        print(f"- {action}")
    print("Requires human decision:")
    for action in sorted(SAFETY_HUMAN_ACTIONS):
        print(f"- {action}")
    print("Forbidden autonomous actions:")
    for action in sorted(SAFETY_FORBIDDEN_ACTIONS):
        print(f"- {action}")
    print(
        "Budget limits: "
        f"warning={SAFETY_WARNING_BUDGET_IMPACT}, "
        f"hard={SAFETY_HARD_BUDGET_IMPACT}"
    )
    return 0


def action_scope_is_state_like(scope: str) -> bool:
    normalized = scope.replace("\\", "/").strip("/")
    return (
        normalized.startswith("docs/master-agent")
        or normalized.startswith("state")
        or normalized.startswith("packets")
        or normalized in {"", "."}
    )


def assess_safety(
    state_dir: Path,
    action: str,
    role: str,
    scope: str,
    budget_impact: int,
) -> tuple[int, str, list[str]]:
    reasons: list[str] = []
    action = action.strip().lower()
    try:
        require_active_role(state_dir, role)
    except SystemExit as exc:
        return 2, "human-decision-or-forbidden", [str(exc)]

    if action in SAFETY_FORBIDDEN_ACTIONS:
        return 2, "human-decision-or-forbidden", ["forbidden action"]
    if action in SAFETY_HUMAN_ACTIONS:
        return 2, "human-decision-or-forbidden", ["requires human decision"]
    if budget_impact >= SAFETY_HARD_BUDGET_IMPACT:
        return 2, "human-decision-or-forbidden", ["budget impact exceeds hard safety limit"]

    if budget_impact >= SAFETY_WARNING_BUDGET_IMPACT:
        reasons.append("budget impact reaches warning safety limit")
    if action in SAFETY_REMEDIATION_ACTIONS:
        reasons.append("remediation action requires internal review")
    elif action not in SAFETY_AUTONOMOUS_ACTIONS:
        reasons.append("action is not explicitly autonomous")

    if not action_scope_is_state_like(scope) and action in SAFETY_AUTONOMOUS_ACTIONS:
        reasons.append("scope is outside Master Agent state")

    if reasons:
        return 1, "internal-remediation-or-policy-review", reasons
    return 0, "autonomous", ["inside safety envelope"]


def command_check_safety(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    errors = validate_state_pack(state_dir)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 2
    exit_code, status, reasons = assess_safety(
        state_dir=state_dir,
        action=args.action,
        role=args.role,
        scope=args.scope,
        budget_impact=args.budget_impact,
    )
    print(f"Safety: {status}")
    print(f"Action: {args.action}")
    print(f"Role: {args.role}")
    print(f"Scope: {args.scope}")
    print(f"Budget impact: {args.budget_impact}")
    print("Reasons:")
    for reason in reasons:
        print(f"- {reason}")
    return exit_code


def command_accept_strategy(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    errors = validate_state_pack(state_dir)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    packet = Path(args.packet).resolve()
    if not packet.exists() or not packet.is_file():
        print(f"Strategy packet does not exist: {packet}", file=sys.stderr)
        return 2
    timestamp = format_time(parse_time(args.at))
    entry = {
        "accepted_at": timestamp,
        "packet": str(packet),
        "plan_id": args.plan_id,
        "summary": args.summary,
    }
    append_strategy_sync(state_dir, entry)
    render_strategy_sync(state_dir, entry)
    append_event_log(
        state_dir=state_dir,
        event_type="strategy-accepted",
        related_packet=str(packet),
        summary=f"{args.plan_id}: {args.summary}",
        evidence=str(packet),
        ledger_update="strategy-sync.md updated",
        next_action="issue work order or register role agent with current plan id",
        at=timestamp,
    )
    print(f"Accepted strategy {args.plan_id}")
    return 0


def command_strategy_sync_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    entry = current_strategy_plan(state_dir)
    if not entry:
        print("Current plan: none")
        print("Plan status: missing")
        return 1
    accepted_at = parse_time(entry.get("accepted_at"))
    now = parse_time(args.now)
    age_hours = (now - accepted_at).total_seconds() / 3600
    is_stale = age_hours > args.stale_hours
    status = "stale" if is_stale else "current"
    render_strategy_sync(state_dir, entry, status=status)
    print(f"Current plan: {entry.get('plan_id')}")
    print(f"Summary: {entry.get('summary')}")
    print(f"Accepted at: {entry.get('accepted_at')}")
    print(f"Age hours: {age_hours:.1f}")
    print(f"Plan status: {status}")
    return 1 if is_stale else 0


def command_require_plan(args: argparse.Namespace) -> int:
    entry = current_strategy_plan(Path(args.state_dir).resolve())
    current_plan_id = entry.get("plan_id") if entry else None
    if current_plan_id == args.plan_id:
        print(f"Current plan matched: {args.plan_id}")
        return 0
    print(
        f"Current plan mismatch: expected {current_plan_id or 'none'}, got {args.plan_id}",
        file=sys.stderr,
    )
    return 1


def broad_next_action(value: str) -> bool:
    normalized = value.lower()
    return any(
        marker in normalized
        for marker in [
            "continue",
            "keep going",
            "fix all",
            "refactor",
            "explore",
            "investigate everything",
        ]
    )


def detect_agent_anomalies(state_dir: Path, agent_id: str) -> list[dict]:
    agents = load_agents(state_dir)
    if agent_id not in agents:
        raise SystemExit(f"Unknown agent id: {agent_id}")
    history = load_agent_heartbeats(state_dir, agent_id)
    if not history:
        return []

    latest = history[-1]
    findings: list[dict] = []
    timestamp = format_time(parse_time(None))

    def add(kind: str, severity: str, evidence: str, action: str) -> None:
        findings.append(
            {
                "time": timestamp,
                "agent_id": agent_id,
                "type": kind,
                "severity": severity,
                "evidence": evidence,
                "recommended_action": action,
            }
        )

    if len(history) >= 3:
        last_three = history[-3:]
        next_actions = [entry.get("next_action", "") for entry in last_three]
        if next_actions[0] and len(set(next_actions)) == 1:
            add(
                "repeated-next-action-loop",
                "high",
                f"same next action repeated 3 times: {next_actions[0]}",
                "reinforce context or spawn successor",
            )

    if latest.get("scope_status") in {"no", "unsure"}:
        add(
            "scope-drift",
            "high",
            f"scope_status={latest.get('scope_status')}",
            "stop agent or request policy review",
        )

    current_plan = current_strategy_plan(state_dir)
    current_plan_id = current_plan.get("plan_id") if current_plan else ""
    if latest.get("plan_alignment") == "no":
        add(
            "plan-mismatch",
            "high",
            "heartbeat reported plan_alignment=no",
            "resync strategy or stop agent",
        )
    elif current_plan_id and latest.get("plan_id") and latest.get("plan_id") != current_plan_id:
        add(
            "plan-mismatch",
            "high",
            f"agent plan {latest.get('plan_id')} != current plan {current_plan_id}",
            "resync strategy or stop agent",
        )

    if latest.get("status") == "complete":
        has_evidence = any(
            latest.get(key)
            for key in ["commands", "artifacts", "files_changed"]
        )
        if not has_evidence or latest.get("evidence_quality") == "missing":
            add(
                "evidence-free-success-claim",
                "high",
                "complete heartbeat lacks commands, artifacts, or changed files",
                "reject receipt and require evidence",
            )

    if latest.get("self_reported_anomaly"):
        add(
            "self-reported-anomaly",
            "medium",
            latest.get("self_reported_anomaly", ""),
            "inspect and remediate",
        )

    budget_exit, budget_messages = budget_findings(state_dir)
    if budget_exit >= 1 and broad_next_action(latest.get("next_action", "")):
        add(
            "token-risk",
            "medium",
            "; ".join(budget_messages),
            "compress and narrow before continuing",
        )

    return findings


def command_audit_agent(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    anomalies = detect_agent_anomalies(state_dir, args.agent_id)
    if not anomalies:
        print(f"No anomalies detected for {args.agent_id}")
        return 0
    for anomaly in anomalies:
        append_anomaly(state_dir, anomaly)
    render_anomaly_log(state_dir)
    print(f"Anomalies detected for {args.agent_id}:")
    for anomaly in anomalies:
        print(
            f"- {anomaly['type']} ({anomaly['severity']}): "
            f"{anomaly['evidence']}"
        )
    return 1


def repeated_next_action(history: list[dict]) -> str:
    if len(history) < 3:
        return ""
    last_three = history[-3:]
    next_actions = [entry.get("next_action", "") for entry in last_three]
    if next_actions[0] and len(set(next_actions)) == 1:
        return next_actions[0]
    return ""


def agent_token_status(state_dir: Path, agent_id: str) -> str:
    budget = load_budget(state_dir)
    agent_budget = budget.get("agents", {}).get(agent_id, {})
    return (
        f"{agent_budget.get('tokens_used', 0)} / "
        f"{agent_budget.get('token_budget') or 'unbounded'}"
    )


def write_remediation_packet(
    state_dir: Path,
    agent_id: str,
    filename: str,
    title: str,
    safety_status: str,
    action: str,
    budget_impact: int,
) -> Path:
    agents = load_agents(state_dir)
    if agent_id not in agents:
        raise SystemExit(f"Unknown agent id: {agent_id}")
    agent = agents[agent_id]
    history = load_agent_heartbeats(state_dir, agent_id)
    latest = history[-1] if history else agent
    current_plan = current_strategy_plan(state_dir)
    current_plan_id = current_plan.get("plan_id") if current_plan else latest.get("plan_id", "")
    forbidden_repeat = repeated_next_action(history)
    output_dir = state_dir / "packets" / "remediation"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename
    text = f"""# Remediation Packet

## Trigger

- Agent id: {agent_id}
- Anomaly: {title}
- Source heartbeat: {latest.get('last_heartbeat_at', '')}
- Safety result: {safety_status}

## Safety Check

- Action: {action}
- Role: {agent.get('role', '')}
- Scope: {agent.get('scope', '')}
- Budget impact: {budget_impact}
- Verdict: {safety_status}

## Context Reinforcement

- Current objective: {agent.get('objective', '')}
- Accepted plan id: {current_plan_id or ''}
- Authority: project policy pack, master ledger, role catalog, safety envelope
- Last concrete progress: {latest.get('last_action', '')}
- Scope reminder: {agent.get('scope', '')}
- Forbidden repeats: {forbidden_repeat}

## Successor Context

- Current plan id: {current_plan_id or ''}
- Accepted authority: project policy pack, master ledger, role catalog, safety envelope
- Last concrete progress: {latest.get('last_action', '')}
- Open risks: {latest.get('risk', '')}
- Blocked reason: {latest.get('risk', '')}
- Token status: {agent_token_status(state_dir, agent_id)}
- Forbidden repeats: {forbidden_repeat}

## Split Task

- Original task: {agent.get('task_id', '')}
- Proposed slices:
- Merge owner: Master Agent
- Conflict protocol: return to Master before shared writes

## Stop Action

- Stop reason: {latest.get('risk', '')}
- Required review: Review or Strategy packet before continuation
- Next safe action: update ledger or issue a narrowed packet
"""
    atomic_write_text(output_path, text)
    return output_path


def command_remediate_agent(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    agents = load_agents(state_dir)
    if args.agent_id not in agents:
        print(f"Unknown agent id: {args.agent_id}", file=sys.stderr)
        return 1
    agent = agents[args.agent_id]
    safety_code, safety_status, safety_reasons = assess_safety(
        state_dir=state_dir,
        action=args.action,
        role=agent.get("role", ""),
        scope=agent.get("scope", ""),
        budget_impact=args.budget_impact,
    )
    if safety_code == 2:
        print("Safety blocked remediation")
        for reason in safety_reasons:
            print(f"- {reason}")
        return 2

    filenames = {
        "reinforce-context": f"{args.agent_id}-context-reinforcement.md",
        "spawn-successor": f"{args.agent_id}-successor-context.md",
        "split-task": f"{args.agent_id}-split-task.md",
        "stop-agent": f"{args.agent_id}-stop-agent.md",
    }
    titles = {
        "reinforce-context": "context reinforcement",
        "spawn-successor": "successor context handoff",
        "split-task": "split task remediation",
        "stop-agent": "stop agent remediation",
    }
    output_path = write_remediation_packet(
        state_dir=state_dir,
        agent_id=args.agent_id,
        filename=filenames[args.action],
        title=titles[args.action],
        safety_status=safety_status,
        action=args.action,
        budget_impact=args.budget_impact,
    )
    if args.action == "stop-agent":
        agents[args.agent_id]["status"] = "stopping"
        save_agents(state_dir, agents)
        render_running_agents(state_dir, agents)
    append_event_log(
        state_dir=state_dir,
        event_type="drift-stop" if args.action == "stop-agent" else "remediation",
        related_packet=str(output_path),
        summary=f"{args.action} for {args.agent_id}",
        evidence=str(output_path),
        ledger_update="remediation packet created",
        next_action="review remediation packet",
        at=format_time(parse_time(args.at)),
    )
    print(f"Created remediation packet: {output_path}")
    return 0


def _parse_quiet_time(value: str) -> tuple[int, int]:
    hour_text, minute_text = value.split(":", 1)
    return int(hour_text), int(minute_text)


def in_quiet_period(now: datetime, quiet_start: str | None, quiet_end: str | None) -> bool:
    if not quiet_start or not quiet_end:
        return False
    start_hour, start_minute = _parse_quiet_time(quiet_start)
    end_hour, end_minute = _parse_quiet_time(quiet_end)
    current = now.hour * 60 + now.minute
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def remediation_action_for_anomaly(anomaly: dict) -> str:
    anomaly_type = anomaly.get("type", "")
    if anomaly_type in {"scope-drift", "plan-mismatch"}:
        return "stop-agent"
    if anomaly_type == "token-risk":
        return "split-task"
    return "reinforce-context"


def stop_agent(state_dir: Path, agent_id: str, reason: str) -> None:
    agents = load_agents(state_dir)
    if agent_id not in agents:
        return
    agents[agent_id]["status"] = "stopping"
    agents[agent_id]["stop_reason"] = reason
    save_agents(state_dir, agents)
    render_running_agents(state_dir, agents)


def runtime_wakeup_is_future(runtime: dict, now: datetime) -> bool:
    wakeup = str(runtime.get("next_wakeup_at") or "").strip()
    if not wakeup or wakeup.startswith("+"):
        return False
    try:
        return parse_time(wakeup) > now
    except ValueError:
        return False


def write_strategy_review_packet(
    state_dir: Path,
    agent_id: str,
    anomaly: dict,
    action: str,
    reason: str,
) -> Path:
    packet_dir = state_dir / "packets" / "remediation"
    packet_dir.mkdir(parents=True, exist_ok=True)
    output = packet_dir / f"{agent_id}-strategy-review.md"
    text = f"""# Strategy Review Packet

## Trigger

- Agent id: {agent_id}
- Anomaly type: {anomaly.get('type', '')}
- Remediation action: {action}
- Reason: {reason}

## Required Strategy Decision

- Decide whether to stop the task, narrow the work order, split the task, or spawn a successor.
- Reconfirm the current plan id before any new Coding assignment.
- Return a strategy-packet.md update or a new bounded work order.

## Evidence

- Severity: {anomaly.get('severity', '')}
- Evidence: {anomaly.get('evidence', '')}
- Recommended action: {anomaly.get('recommended_action', '')}
"""
    atomic_write_text(output, text)
    return output


def supervise_one_cycle(args: argparse.Namespace, runtime: dict, cycle: int) -> int:
    state_dir = Path(args.state_dir).resolve()
    now = parse_time(args.now)
    timestamp = format_time(now)
    validation_errors = validate_state_pack(state_dir)
    validation_result = "ok" if not validation_errors else f"{len(validation_errors)} errors"
    stale = find_stale_agents(state_dir, now, args.stale_minutes)
    heartbeat_result = "ok" if not stale else f"{len(stale)} stale"
    budget_exit, budget_messages = budget_findings(state_dir)
    budget_result = "; ".join(budget_messages)
    strategy = current_strategy_plan(state_dir)
    strategy_result = strategy.get("plan_id", "none") if strategy else "none"

    runtime["supervisor_state"] = "running"
    runtime["last_check_at"] = timestamp
    runtime["active_plan_id"] = strategy.get("plan_id", "") if strategy else ""
    runtime["active_interventions"] = []
    runtime["deferred_actions"] = []
    runtime["stopped_agents"] = []
    runtime["anomalies_handled"] = []
    runtime["human_attention_needed"] = False

    agents = load_agents(state_dir)
    active_agents = [
        agent_id
        for agent_id, agent in sorted(agents.items())
        if agent.get("status") in MONITORED_STATES
    ]
    future_wakeup = runtime_wakeup_is_future(runtime, now)
    quiet = in_quiet_period(now, args.quiet_start, args.quiet_end) or future_wakeup
    exit_code = 1 if validation_errors or budget_exit >= 2 else 0

    for anomaly in load_anomalies(state_dir):
        if anomaly.get("severity") == "critical" or anomaly.get("type") == "safety-breach":
            agent_id = anomaly.get("agent_id", "")
            stop_agent(state_dir, agent_id, "critical safety breach")
            append_incident(
                state_dir=state_dir,
                severity="critical",
                summary=f"critical safety breach for {agent_id}: {anomaly.get('evidence', '')}",
                source="supervisor",
                at=timestamp,
            )
            runtime["critical_breach_count"] = int(runtime.get("critical_breach_count") or 0) + 1
            runtime["stopped_agents"].append(f"{agent_id} stopped for critical safety breach")
            runtime["human_attention_needed"] = True
            exit_code = 1

    for agent_id in active_agents:
        anomalies = detect_agent_anomalies(state_dir, agent_id)
        if anomalies:
            for anomaly in anomalies:
                append_anomaly(state_dir, anomaly)
            render_anomaly_log(state_dir)
        for anomaly in anomalies:
            runtime["anomalies_handled"].append(f"{agent_id}:{anomaly.get('type')}")
            action = remediation_action_for_anomaly(anomaly)
            if quiet:
                runtime["deferred_actions"].append(f"{agent_id}:{action}")
                continue
            recovery_key = f"{agent_id}:{action}"
            last_recoveries = runtime.setdefault("last_recoveries", {})
            same_counts = runtime.setdefault("same_recovery_count", {})
            previous_count = int(
                same_counts.get(recovery_key)
                or last_recoveries.get(recovery_key)
                or 0
            )
            if previous_count >= 2:
                stop_agent(state_dir, agent_id, "repeated remediation limit")
                same_counts[recovery_key] = previous_count + 1
                last_recoveries[agent_id] = action
                packet = write_strategy_review_packet(
                    state_dir=state_dir,
                    agent_id=agent_id,
                    anomaly=anomaly,
                    action=action,
                    reason="same remediation limit",
                )
                append_incident(
                    state_dir=state_dir,
                    severity="critical",
                    summary=f"repeated remediation failure for {agent_id}: {action}",
                    source="supervisor",
                    at=timestamp,
                )
                runtime["stopped_agents"].append(
                    f"{agent_id} stopped after repeated remediation"
                )
                runtime["active_interventions"].append(
                    f"{agent_id}:strategy-review:{packet.name}"
                )
                runtime["human_attention_needed"] = True
                continue

            agent = load_agents(state_dir).get(agent_id, {})
            safety_code, safety_status, safety_reasons = assess_safety(
                state_dir=state_dir,
                action=action,
                role=agent.get("role", ""),
                scope=agent.get("scope", ""),
                budget_impact=0,
            )
            if safety_code == 2:
                runtime["active_interventions"].append(
                    f"{agent_id}:{action} blocked by safety: {'; '.join(safety_reasons)}"
                )
                runtime["human_attention_needed"] = True
                exit_code = max(exit_code, 1)
                continue
            packet_name = f"{agent_id}-{action}.md"
            packet = write_remediation_packet(
                state_dir=state_dir,
                agent_id=agent_id,
                filename=packet_name,
                title=f"supervisor {action}",
                safety_status=safety_status,
                action=action,
                budget_impact=0,
            )
            same_counts[recovery_key] = previous_count + 1
            last_recoveries[agent_id] = action
            runtime["active_interventions"].append(f"{agent_id}:{action}:{packet.name}")

    runtime["supervisor_state"] = "running" if args.run_until_stopped else "idle"
    runtime["last_cycle_result"] = "attention-needed" if exit_code else "ok"
    if future_wakeup:
        pass
    elif runtime["deferred_actions"]:
        runtime["next_wakeup_at"] = format_time(now + timedelta(seconds=args.poll_seconds))
    elif args.max_cycles == cycle:
        runtime["next_wakeup_at"] = ""
    else:
        runtime["next_wakeup_at"] = format_time(now + timedelta(seconds=args.poll_seconds))
    save_runtime(state_dir, runtime)
    render_runtime_status(
        state_dir=state_dir,
        runtime=runtime,
        active_agents=active_agents,
        validation_result=validation_result,
        heartbeat_result=heartbeat_result,
        budget_result=budget_result,
        strategy_result=strategy_result,
    )
    print(f"Supervisor cycle {cycle} complete")
    if runtime["stopped_agents"]:
        for item in runtime["stopped_agents"]:
            print(f"- {item}")
    return exit_code


def command_supervise(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    runtime = load_runtime(state_dir)
    cycles = args.max_cycles if args.max_cycles is not None else 1
    final_exit = 0
    cycle = 0
    while args.run_until_stopped or cycle < cycles:
        cycle += 1
        runtime = load_runtime(state_dir)
        if runtime.get("stop_requested"):
            print("Supervisor stop requested")
            runtime["supervisor_state"] = "idle"
            save_runtime(state_dir, runtime)
            return final_exit
        result = supervise_one_cycle(args, runtime, cycle)
        final_exit = max(final_exit, result)
        if not args.run_until_stopped or (
            args.max_cycles is not None and cycle >= args.max_cycles
        ):
            break
        time.sleep(args.poll_seconds)
    return final_exit


def command_supervisor_start(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    runtime = load_runtime(state_dir)
    timestamp = format_time(parse_time(args.now))
    lock_path = state_dir / "state" / "supervisor.lock"
    supervisor_id = f"supervisor-{uuid.uuid4().hex}"
    pid = os.getpid()
    spawn_process: subprocess.Popen[str] | None = None
    if args.spawn:
        log_dir = state_dir / "state" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "supervisor.out.log"
        stderr_path = log_dir / "supervisor.err.log"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "supervise",
            "--state-dir",
            str(state_dir),
            "--poll-seconds",
            str(args.poll_seconds),
            "--run-until-stopped",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        stdout_handle = stdout_path.open("a", encoding="utf-8")
        stderr_handle = stderr_path.open("a", encoding="utf-8")
        try:
            spawn_process = subprocess.Popen(
                command,
                cwd=str(SKILL_ROOT),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                close_fds=True,
                creationflags=creationflags,
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()
        pid = spawn_process.pid
    atomic_write_text(
        lock_path,
        f"pid={pid}\nsupervisor_id={supervisor_id}\ntime={time.time()}\nstarted_at={timestamp}\n",
    )
    runtime.update(
        {
            "supervisor_state": "running",
            "stop_requested": False,
            "pid": pid,
            "supervisor_id": supervisor_id,
            "lock_path": str(lock_path),
            "started_at": timestamp,
            "last_check_at": timestamp,
            "poll_seconds": args.poll_seconds,
            "deployment_mode": args.deployment_mode,
            "spawned": bool(args.spawn),
            "last_cycle_result": "started",
        }
    )
    save_runtime(state_dir, runtime)
    print(f"Supervisor start recorded: pid={pid} lock={lock_path}")
    return 0


def supervisor_runtime_status(runtime: dict, now: datetime, stale_seconds: float) -> str:
    if runtime.get("stop_requested"):
        return "stop-requested"
    if runtime.get("supervisor_state") == "crashed":
        return "crashed"
    lock_path = Path(runtime.get("lock_path") or "")
    if runtime.get("supervisor_state") == "idle":
        return "idle"
    if runtime.get("supervisor_state") == "running":
        if not lock_path.exists():
            return "dead"
        lock_metadata = read_key_value_file(lock_path)
        runtime_supervisor_id = str(runtime.get("supervisor_id") or "").strip()
        lock_supervisor_id = str(lock_metadata.get("supervisor_id") or "").strip()
        if runtime_supervisor_id and lock_supervisor_id and runtime_supervisor_id != lock_supervisor_id:
            return "identity-mismatch"
        if not process_is_alive(runtime.get("pid")):
            return "dead"
        last_check_at = runtime.get("last_check_at")
        if last_check_at:
            age_seconds = (now - parse_time(last_check_at)).total_seconds()
            if age_seconds > stale_seconds:
                return "stale"
        return "running"
    return "stopped"


def command_supervisor_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    runtime = load_runtime(state_dir)
    status = supervisor_runtime_status(runtime, parse_time(args.now), args.stale_seconds)
    print(f"Supervisor status: {status}")
    print(f"PID: {runtime.get('pid', '')}")
    print(f"Lock: {runtime.get('lock_path', '')}")
    print(f"Started at: {runtime.get('started_at', '')}")
    print(f"Last check: {runtime.get('last_check_at', '')}")
    return 0 if status in {"running", "idle"} else 1


def command_supervisor_stop(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    runtime = load_runtime(state_dir)
    runtime["stop_requested"] = True
    runtime["supervisor_state"] = "stop-requested"
    runtime["stop_requested_at"] = format_time(parse_time(args.now))
    save_runtime(state_dir, runtime)
    print("Supervisor stop requested")
    return 0


def command_supervisor_recover(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    runtime = load_runtime(state_dir)
    lock_path = Path(runtime.get("lock_path") or "")
    status = supervisor_runtime_status(runtime, parse_time(args.now), args.stale_seconds)
    lock_metadata = read_key_value_file(lock_path) if lock_path.exists() else {}
    live_runtime_pid = process_is_alive(runtime.get("pid"))
    live_lock_pid = process_is_alive(lock_metadata.get("pid"))
    if lock_path.exists() and (live_runtime_pid or live_lock_pid) and not args.force:
        print(
            f"Refusing to recover live supervisor pid={runtime.get('pid')}; "
            "rerun with --force only after confirming it must be overridden.",
            file=sys.stderr,
        )
        return 2
    if lock_path.exists():
        unlink_with_retry(lock_path)
    runtime["supervisor_state"] = "idle"
    runtime["stop_requested"] = False
    runtime["supervisor_id"] = ""
    runtime["recovered_at"] = format_time(parse_time(args.now))
    runtime["last_cycle_result"] = "recovered"
    runtime.pop("crash_marker", None)
    save_runtime(state_dir, runtime)
    print("Supervisor recovered")
    return 0


def append_session_event(state_dir: Path, entry: dict) -> None:
    append_jsonl_locked(state_dir / "state" / "session-control.jsonl", entry)


def load_session_events(state_dir: Path) -> list[dict]:
    ensure_state_storage(state_dir)
    events: list[dict] = []
    path = state_dir / "state" / "session-control.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid session control history: {path}: {exc}") from exc
        if isinstance(event, dict):
            events.append(event)
    return events


def latest_session_event(state_dir: Path, agent_id: str) -> dict | None:
    for event in reversed(load_session_events(state_dir)):
        if event.get("agent_id") != agent_id:
            continue
        if event.get("event") in {"session-archived", "session-stale"}:
            return None
        if event.get("provider_session_path"):
            return event
    return None


def run_session_provider_command(
    command: str,
    request: dict,
    timeout_seconds: float,
) -> tuple[dict | None, str | None]:
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        return None, f"Provider command could not be parsed: {exc}"
    if not argv:
        return None, "Provider command is empty"
    try:
        result = subprocess.run(
            argv,
            input=json.dumps(request, sort_keys=True) + "\n",
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return None, f"Provider command timed out after {timeout_seconds:g}s"
    except OSError as exc:
        return None, f"Provider command failed to start: {exc}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        return None, f"Provider command failed: {detail}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"Provider command returned invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "Provider command returned invalid JSON: expected object"
    return payload, None


def required_provider_command(args: argparse.Namespace, provider: str) -> str | None:
    command = getattr(args, "provider_command", None) or os.environ.get(
        "MASTER_AGENT_SESSION_PROVIDER"
    )
    if provider == "codex" and not command:
        print(
            "Provider command is required for live provider 'codex'. "
            "Pass --provider-command or set MASTER_AGENT_SESSION_PROVIDER.",
            file=sys.stderr,
        )
        return None
    return command


def run_live_session_operation(
    args: argparse.Namespace,
    event: dict,
    operation: str,
    **payload: object,
) -> tuple[dict | None, int]:
    provider = event.get("provider", "")
    provider_command = required_provider_command(args, str(provider))
    if not provider_command:
        return None, 2
    request = {
        "event": operation,
        "provider": provider,
        "agent_id": event.get("agent_id", ""),
        "role": event.get("role", ""),
        "provider_session_id": event.get("provider_session_id", ""),
        "provider_session_path": event.get("provider_session_path", ""),
        **payload,
    }
    provider_payload, provider_error = run_session_provider_command(
        provider_command,
        request,
        getattr(args, "provider_timeout_seconds", 60),
    )
    if provider_error:
        print(provider_error, file=sys.stderr)
        return None, 2
    return provider_payload, 0


def command_session_create(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    role_name, _definition = require_active_role(state_dir, args.role)
    context_packet = Path(args.context_packet).resolve()
    if not context_packet.exists():
        print(f"Context packet does not exist: {context_packet}", file=sys.stderr)
        return 2
    timestamp = format_time(parse_time(args.at))
    session_dir = state_dir / "state" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    provider_session_path = session_dir / f"{args.agent_id}.json"
    provider_session_id = f"{args.provider}:{args.agent_id}"
    provider_confirmed = False
    session = {
        "provider_session_id": provider_session_id,
        "agent_id": args.agent_id,
        "role": role_name,
        "status": "active" if args.provider == "file" else "pending-manual-provider",
        "context_packet": str(context_packet),
        "predecessor_agent_id": args.predecessor_agent_id or "",
        "inheritance_reason": args.reason or "",
        "messages": [
            {
                "at": timestamp,
                "sender": "master",
                "message": f"context-packet:{context_packet}",
            }
        ],
    }
    if args.provider == "file":
        atomic_write_json(provider_session_path, session)
        provider_confirmed = True
    elif args.provider == "codex":
        provider_command = required_provider_command(args, args.provider)
        if not provider_command:
            return 2
        provider_request = {
            "event": "session-create",
            "provider": args.provider,
            "agent_id": args.agent_id,
            "role": role_name,
            "context_packet": str(context_packet),
            "predecessor_agent_id": args.predecessor_agent_id or "",
            "inheritance_reason": args.reason or "",
            "requested_at": timestamp,
        }
        provider_payload, provider_error = run_session_provider_command(
            provider_command,
            provider_request,
            args.provider_timeout_seconds,
        )
        if provider_error:
            print(provider_error, file=sys.stderr)
            return 2
        provider_session_id = str(
            provider_payload.get("provider_session_id")
            or provider_payload.get("session_id")
            or provider_session_id
        )
        provider_session_path = Path(
            provider_payload.get("provider_session_path") or provider_session_path
        )
        provider_status = str(provider_payload.get("status") or "")
        if provider_status != "active":
            print(
                "Provider command did not confirm an active session.",
                file=sys.stderr,
            )
            return 2
        if not provider_session_path.exists():
            print(
                f"Provider command confirmed a session but evidence file is missing: {provider_session_path}",
                file=sys.stderr,
            )
            return 2
        session["status"] = "active"
        session["provider_session_id"] = provider_session_id
        provider_confirmed = True
    event = {
        "at": timestamp,
        "event": "session-created",
        "agent_id": args.agent_id,
        "role": role_name,
        "provider": args.provider,
        "provider_session_id": provider_session_id,
        "provider_session_path": str(provider_session_path),
        "context_packet": str(context_packet),
        "predecessor_agent_id": args.predecessor_agent_id or "",
        "inheritance_reason": args.reason or "",
        "status": session["status"],
        "provider_confirmed": provider_confirmed,
    }
    append_session_event(state_dir, event)
    print(f"Created session {provider_session_id}")
    return 0


def _load_provider_session(path: Path) -> dict:
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid provider session: {path}: {exc}") from exc
    if not isinstance(session, dict):
        raise SystemExit(f"Invalid provider session: {path}: expected object")
    session.setdefault("messages", [])
    return session


def command_session_send(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    event = latest_session_event(state_dir, args.agent_id)
    if not event:
        print(f"No session found for {args.agent_id}", file=sys.stderr)
        return 1
    provider_session_path = Path(event["provider_session_path"])
    timestamp = format_time(parse_time(args.at))
    if event.get("provider") == "codex":
        provider_payload, exit_code = run_live_session_operation(
            args,
            event,
            "session-send",
            message=args.message,
            at=timestamp,
        )
        if exit_code:
            return exit_code
        if provider_payload and provider_payload.get("provider_session_path"):
            provider_session_path = Path(provider_payload["provider_session_path"])
    else:
        if not provider_session_path.exists():
            print(f"Provider session missing for {args.agent_id}", file=sys.stderr)
            return 1
        session = _load_provider_session(provider_session_path)
        session["messages"].append({"at": timestamp, "sender": "master", "message": args.message})
        atomic_write_json(provider_session_path, session)
    append_session_event(
        state_dir,
        {
            "at": timestamp,
            "event": "session-sent",
            "agent_id": args.agent_id,
            "role": event.get("role", ""),
            "provider": event.get("provider", "file"),
            "provider_session_id": event.get("provider_session_id", ""),
            "provider_session_path": str(provider_session_path),
            "message": args.message,
            "provider_confirmed": event.get("provider") == "codex",
        },
    )
    print(f"Sent message to {args.agent_id}")
    return 0


def command_session_read(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    event = latest_session_event(state_dir, args.agent_id)
    if not event:
        print(f"No session found for {args.agent_id}", file=sys.stderr)
        return 1
    provider_session_path = Path(event["provider_session_path"])
    timestamp = format_time(parse_time(args.at))
    if event.get("provider") == "codex":
        provider_payload, exit_code = run_live_session_operation(
            args,
            event,
            "session-read",
            at=timestamp,
        )
        if exit_code:
            return exit_code
        session = provider_payload or {}
        if session.get("provider_session_path"):
            provider_session_path = Path(session["provider_session_path"])
    else:
        if not provider_session_path.exists():
            print(f"Provider session missing for {args.agent_id}", file=sys.stderr)
            return 1
        session = _load_provider_session(provider_session_path)
    print(f"Session {args.agent_id}:")
    for message in session.get("messages", []):
        print(f"- {message.get('sender')}: {message.get('message')}")
    append_session_event(
        state_dir,
        {
            "at": timestamp,
            "event": "session-read",
            "agent_id": args.agent_id,
            "role": event.get("role", ""),
            "provider": event.get("provider", "file"),
            "provider_session_id": event.get("provider_session_id", ""),
            "provider_session_path": str(provider_session_path),
            "message_count": len(session.get("messages", [])),
            "provider_confirmed": event.get("provider") == "codex",
        },
    )
    return 0


def command_session_archive(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    event = latest_session_event(state_dir, args.agent_id)
    if not event:
        print(f"No session found for {args.agent_id}", file=sys.stderr)
        return 1
    provider_session_path = Path(event["provider_session_path"])
    provider_confirmed = False
    if event.get("provider") == "codex":
        provider_payload, exit_code = run_live_session_operation(
            args,
            event,
            "session-archive",
            at=format_time(parse_time(args.at)),
        )
        if exit_code:
            return exit_code
        provider_confirmed = True
        if provider_payload and provider_payload.get("provider_session_path"):
            provider_session_path = Path(provider_payload["provider_session_path"])
    elif provider_session_path.exists():
        session = _load_provider_session(provider_session_path)
        session["status"] = "archived"
        atomic_write_json(provider_session_path, session)
    append_session_event(
        state_dir,
        {
            "at": format_time(parse_time(args.at)),
            "event": "session-archived",
            "agent_id": args.agent_id,
            "role": event.get("role", ""),
            "provider": event.get("provider", "file"),
            "provider_session_id": event.get("provider_session_id", ""),
            "provider_session_path": str(provider_session_path),
            "status": "archived",
            "provider_confirmed": provider_confirmed,
        },
    )
    print(f"Archived session {args.agent_id}")
    return 0


def command_session_reconcile(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    timestamp = format_time(parse_time(args.at))
    latest_by_agent: dict[str, dict] = {}
    for event in load_session_events(state_dir):
        if event.get("event") == "session-created":
            latest_by_agent[event["agent_id"]] = event
        elif event.get("event") in {"session-archived", "session-stale"}:
            latest_by_agent.pop(event.get("agent_id", ""), None)
    stale: list[str] = []
    for agent_id, event in sorted(latest_by_agent.items()):
        provider_path = Path(event.get("provider_session_path", ""))
        provider_status = "active"
        if event.get("provider") == "codex":
            provider_payload, exit_code = run_live_session_operation(
                args,
                event,
                "session-reconcile",
                at=timestamp,
            )
            if exit_code:
                return exit_code
            if provider_payload:
                provider_status = str(provider_payload.get("status") or "")
                if provider_payload.get("provider_session_path"):
                    provider_path = Path(provider_payload["provider_session_path"])
        if provider_status in {"stale", "missing", "dead"} or not provider_path.exists():
            stale.append(agent_id)
            append_session_event(
                state_dir,
                {
                    "at": timestamp,
                    "event": "session-stale",
                    "agent_id": agent_id,
                    "provider": event.get("provider", "file"),
                    "provider_session_path": str(provider_path),
                    "status": "stale",
                },
            )
    if stale:
        print("stale sessions:")
        for agent_id in stale:
            print(f"- {agent_id}")
        return 1
    print("No stale sessions")
    return 0


def load_jsonl_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def append_incident(
    state_dir: Path,
    severity: str,
    summary: str,
    source: str,
    at: str,
) -> str:
    incident_id = f"incident-{time.time_ns()}"
    incident = {
        "at": at,
        "incident_id": incident_id,
        "severity": severity,
        "summary": summary,
        "source": source,
        "state": "open",
    }
    append_jsonl_locked(state_dir / "state" / "incidents.jsonl", incident)
    if severity == "critical":
        alert = {
            "at": at,
            "event": "alert-opened",
            "alert_id": f"alert-{time.time_ns()}",
            "incident_id": incident_id,
            "severity": severity,
            "summary": summary,
            "source": source,
            "state": "open",
        }
        append_jsonl_locked(state_dir / "state" / "alerts.jsonl", alert)
    render_incident_log(state_dir)
    render_alert_queue(state_dir)
    return incident_id


def open_alerts(state_dir: Path) -> list[dict]:
    events = load_jsonl_entries(state_dir / "state" / "alerts.jsonl")
    acknowledged = {
        event.get("alert_id")
        for event in events
        if event.get("event") == "alert-acknowledged"
    }
    return [
        event
        for event in events
        if event.get("event") == "alert-opened"
        and event.get("alert_id") not in acknowledged
    ]


def render_alert_queue(state_dir: Path) -> None:
    alerts = open_alerts(state_dir)
    lines = [
        "# Alert Queue",
        "",
        "## Pending Alerts",
        "",
    ]
    lines.extend(
        [
            f"- {alert.get('alert_id')}: {alert.get('severity')} {alert.get('summary')}"
            for alert in alerts
        ]
        or ["- none"]
    )
    lines.extend(
        [
            "",
            "## Severity",
            "",
            "- critical",
            "- warning",
            "- info",
            "",
            "## Due Time",
            "",
            "- immediate for critical",
            "",
            "## Suppression",
            "",
            "- append acknowledgement instead of deleting alert history",
            "",
            "## Acknowledgement",
            "",
            "- use acknowledge-alert",
            "",
            "## Escalation",
            "",
            "- critical alerts require operator handoff",
        ]
    )
    atomic_write_text(state_dir / "alert-queue.md", "\n".join(lines) + "\n")


def render_incident_log(state_dir: Path) -> None:
    incidents = load_jsonl_entries(state_dir / "state" / "incidents.jsonl")
    open_incidents = [entry for entry in incidents if entry.get("state") == "open"]
    lines = [
        "# Incident Log",
        "",
        "## Incident Summary",
        "",
        f"- Open incident count: {len(open_incidents)}",
        f"- Last critical incident: {next((entry.get('summary') for entry in reversed(incidents) if entry.get('severity') == 'critical'), '')}",
        "",
        "## Severity Levels",
        "",
        "- critical: safety breach, corruption, repeated remediation failure, or unrecoverable provider loss",
        "- warning: budget pressure, stale session, or deferred remediation",
        "- info: notable but handled event",
        "",
        "## Open Incidents",
        "",
    ]
    lines.extend(
        [
            f"- {entry.get('incident_id')}: {entry.get('severity')} {entry.get('summary')}"
            for entry in open_incidents
        ]
        or ["- none"]
    )
    lines.extend(
        [
            "",
            "## Resolved Incidents",
            "",
            "- none",
            "",
            "## Root Cause",
            "",
            "- recorded per incident",
            "",
            "## Remediation",
            "",
            "- recorded per incident",
            "",
            "## Operator Handoff",
            "",
            "- critical incidents open alerts",
        ]
    )
    atomic_write_text(state_dir / "incident-log.md", "\n".join(lines) + "\n")


def command_record_incident(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    timestamp = format_time(parse_time(args.at))
    incident_id = append_incident(
        state_dir=state_dir,
        severity=args.severity,
        summary=args.summary,
        source=args.source,
        at=timestamp,
    )
    print(f"Recorded incident {incident_id}")
    return 0


def command_alert_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    alerts = open_alerts(state_dir)
    if not alerts:
        print("No open alerts")
        return 0
    print(f"Open alerts: {len(alerts)}")
    for alert in alerts:
        print(f"- {alert.get('alert_id')}: {alert.get('severity')} {alert.get('summary')}")
    return 1 if any(alert.get("severity") == "critical" for alert in alerts) else 0


def command_acknowledge_alert(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    event = {
        "at": format_time(parse_time(args.at)),
        "event": "alert-acknowledged",
        "alert_id": args.alert_id,
        "note": args.note,
    }
    append_jsonl_locked(state_dir / "state" / "alerts.jsonl", event)
    render_alert_queue(state_dir)
    print(f"Acknowledged alert {args.alert_id}")
    return 0


def command_telemetry_summary(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    alerts = open_alerts(state_dir)
    anomalies = load_anomalies(state_dir)
    runtime = load_runtime(state_dir)
    agents = load_agents(state_dir)
    strategy = current_strategy_plan(state_dir)
    budget = load_budget(state_dir)
    print(f"Active plan: {strategy.get('plan_id') if strategy else 'none'}")
    print(f"Active agents: {', '.join(sorted(agents)) if agents else 'none'}")
    print(f"Project tokens: {budget.get('project_used', 0)} / {budget.get('project_budget') or 'unbounded'}")
    print(f"Open anomalies: {len(anomalies)}")
    print(f"Open alerts: {len(alerts)}")
    print(f"Runtime state: {runtime.get('supervisor_state')}")
    print(f"Last supervisor check: {runtime.get('last_check_at', '')}")
    return 1 if alerts else 0


def load_schema_version(state_dir: Path) -> dict:
    ensure_state_storage(state_dir)
    path = state_dir / "state" / "schema-version.json"
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid schema version file: {path}: {exc}") from exc
    if not isinstance(schema, dict):
        raise SystemExit(f"Invalid schema version file: {path}: expected object")
    schema.setdefault("schema_version", CURRENT_SCHEMA_VERSION)
    schema.setdefault("compatible_tool", "master_agent_tool.py")
    schema.setdefault("migration_history", [])
    return schema


def command_schema_status(args: argparse.Namespace) -> int:
    schema = load_schema_version(Path(args.state_dir).resolve())
    print(f"Schema version: {schema.get('schema_version')}")
    print("Migrations:")
    for entry in schema.get("migration_history", []):
        print(f"- {entry.get('migration_id')}")
    return 0


def command_migrate_state(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    schema = load_schema_version(state_dir)
    existing = {
        entry.get("migration_id")
        for entry in schema.get("migration_history", [])
        if isinstance(entry, dict)
    }
    applied: list[str] = []
    for migration_id in ORDERED_MIGRATIONS:
        if migration_id in existing:
            continue
        schema["migration_history"].append(
            {
                "migration_id": migration_id,
                "applied_at": format_time(parse_time(args.at)),
            }
        )
        applied.append(migration_id)
    schema["schema_version"] = CURRENT_SCHEMA_VERSION
    atomic_write_json(state_dir / "state" / "schema-version.json", schema)
    if applied:
        print("Applied migrations:")
        for migration_id in applied:
            print(f"- {migration_id}")
    else:
        print("No migrations pending")
    return 0


def default_budget_state() -> dict:
    return {
        "project_budget": None,
        "project_used": 0,
        "warning_percent": 80,
        "hard_percent": 100,
        "usage_by_source": empty_usage_breakdown(USAGE_SOURCES),
        "usage_by_confidence": empty_usage_breakdown(USAGE_CONFIDENCES),
        "agents": {},
    }


def quarantine_corrupt_json(path: Path, state_dir: Path) -> bool:
    if not path.exists():
        return False
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return False
    except json.JSONDecodeError:
        quarantine_dir = state_dir / "state" / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = quarantine_dir / f"{path.name}.{time.time_ns()}.corrupt"
        shutil.copyfile(path, target)
        return True


def replay_budget_from_usage(state_dir: Path) -> dict:
    budget = default_budget_state()
    usage_path = state_dir / "state" / "token-usage.jsonl"
    for entry in load_jsonl_entries(usage_path):
        tokens = int(entry.get("tokens_used") or 0)
        source = entry.get("source") if entry.get("source") in USAGE_SOURCES else "self-reported"
        confidence = entry.get("confidence") if entry.get("confidence") in USAGE_CONFIDENCES else "medium"
        agent_id = entry.get("agent_id") or "unknown"
        budget["project_used"] += tokens
        add_usage_breakdown(budget, tokens, source, confidence)
        agent_budget = budget["agents"].setdefault(
            agent_id,
            {
                "tokens_used": 0,
                "usage_by_source": empty_usage_breakdown(USAGE_SOURCES),
                "usage_by_confidence": empty_usage_breakdown(USAGE_CONFIDENCES),
            },
        )
        agent_budget["tokens_used"] = int(agent_budget.get("tokens_used") or 0) + tokens
        add_usage_breakdown(agent_budget, tokens, source, confidence)
    return budget


def command_recover_state(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    budget_path = state_dir / "state" / "budget.json"
    budget_missing_before = not budget_path.exists()
    ensure_state_storage(state_dir)
    recovered: list[str] = []
    corrupt_budget = quarantine_corrupt_json(budget_path, state_dir)
    if args.from_logs and (corrupt_budget or budget_missing_before):
        atomic_write_json(budget_path, replay_budget_from_usage(state_dir))
        recovered.append("budget.json")
    for path, default_value in [
        (state_dir / "state" / "runtime.json", default_runtime_state()),
        (state_dir / "state" / "schema-version.json", default_schema_version()),
        (state_dir / "state" / "agents.json", {}),
        (state_dir / "state" / "roles.json", default_roles()),
    ]:
        was_corrupt = quarantine_corrupt_json(path, state_dir)
        if was_corrupt or not path.exists():
            atomic_write_json(path, default_value)
            recovered.append(path.name)
    print(f"Recovered state: {', '.join(recovered) if recovered else 'no changes'}")
    return 0


def command_recover_locks(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    state_root = state_dir / "state"
    removed = 0
    for lock_path in state_root.rglob("*.lock"):
        try:
            lock_path.relative_to(state_root)
        except ValueError:
            continue
        if lock_is_recoverable(lock_path, args.stale_seconds):
            unlink_with_retry(lock_path)
            removed += 1
    print(f"Removed stale locks: {removed}")
    return 0


def command_list_roles(args: argparse.Namespace) -> int:
    roles = load_roles(Path(args.state_dir).resolve())
    for role_name, definition in sorted(roles.items()):
        status = definition.get("status", "proposed")
        if args.active_only and status != "active":
            continue
        print(
            f"{role_name}: status={status} type={definition.get('role_type', 'custom')} "
            f"return={definition.get('return_packet', '')} "
            f"skill={definition.get('role_skill', '') or 'none'}"
        )
    return 0


def custom_role_activation_errors(definition: dict, require_approval: bool) -> list[str]:
    errors: list[str] = []
    if not str(definition.get("scope") or "").strip():
        errors.append("active custom role requires scope")
    if require_approval and not str(definition.get("activation_approval") or "").strip():
        errors.append("active custom role requires activation approval")
    for field_name, label in [
        ("token_budget", "token budget"),
        ("max_heartbeats", "heartbeat cap"),
    ]:
        value = definition.get(field_name)
        try:
            if int(value) <= 0:
                errors.append(f"active custom role requires positive {label}")
        except (TypeError, ValueError):
            errors.append(f"active custom role requires positive {label}")
    if not str(definition.get("deactivation_condition") or "").strip():
        errors.append("active custom role requires deactivation condition")
    return errors


def command_define_role(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    validate_errors = validate_state_pack(state_dir)
    if validate_errors:
        for error in validate_errors:
            print(error, file=sys.stderr)
        return 1

    role_name = normalize_role_name(args.role)
    roles = load_roles(state_dir)
    existing = roles.get(role_name, {})
    role_type = existing.get("role_type", "custom")
    if role_type in {"system", "default"} and not args.force:
        print(
            f"Refusing to redefine default role without --force: {role_name}",
            file=sys.stderr,
        )
        return 1

    timestamp = format_time(parse_time(args.at))
    status = "active" if args.activate else args.status
    activation_approval = args.approval or existing.get("activation_approval", "")
    role_definition = {
        **existing,
        "status": status,
        "role_type": role_type if role_type in {"system", "default"} else "custom",
        "purpose": args.purpose,
        "allowed_work": args.allowed_work,
        "forbidden_work": args.forbidden_work,
        "return_packet": args.return_packet,
        "scope": args.scope or "",
        "role_skill": args.role_skill or existing.get("role_skill", ""),
        "token_budget": args.token_budget,
        "max_heartbeats": args.max_heartbeats,
        "activation_reason": args.reason or ("defined active" if args.activate else "defined"),
        "activation_approval": activation_approval,
        "deactivation_condition": args.deactivation_condition
        or existing.get("deactivation_condition")
        or "Role is no longer needed or overlaps active roles.",
        "created_at": existing.get("created_at", timestamp),
        "updated_at": timestamp,
    }
    if role_definition["status"] == "active" and role_definition["role_type"] == "custom":
        errors = custom_role_activation_errors(role_definition, require_approval=True)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
    roles[role_name] = role_definition
    save_roles(state_dir, roles)
    print(f"Defined role {role_name} ({status})")
    return 0


def command_activate_role(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    roles = load_roles(state_dir)
    role_name = normalize_role_name(args.role)
    if role_name not in roles:
        raise SystemExit(f"Undefined role: {role_name}")
    definition = roles[role_name]
    definition["status"] = "active"
    definition["activation_reason"] = args.reason or "activated"
    if args.approval:
        definition["activation_approval"] = args.approval
    definition["updated_at"] = format_time(parse_time(args.at))
    if definition.get("role_type") == "custom":
        errors = custom_role_activation_errors(definition, require_approval=True)
        if errors:
            definition["status"] = "proposed"
            for error in errors:
                print(error, file=sys.stderr)
            return 1
    roles[role_name] = definition
    save_roles(state_dir, roles)
    print(f"Activated role {role_name}")
    return 0


def command_deactivate_role(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    role_name, definition = require_role(state_dir, args.role)
    if role_name == "Master" and not args.force:
        print("Refusing to deactivate Master role without --force", file=sys.stderr)
        return 1
    definition["status"] = "inactive"
    definition["activation_reason"] = args.reason or "deactivated"
    definition["updated_at"] = format_time(parse_time(args.at))
    roles = load_roles(state_dir)
    roles[role_name] = definition
    save_roles(state_dir, roles)
    print(f"Deactivated role {role_name}")
    return 0


def command_scaffold_role_skill(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    role_name, definition = require_role(state_dir, args.role)
    skill_name = args.skill_name or definition.get("role_skill") or default_role_skill_name(role_name)
    skill_name = slugify_role(skill_name)
    destination_root = Path(args.skills_dir).resolve()
    destination = destination_root / skill_name
    if destination.exists() and not args.force:
        print(f"Role skill already exists: {destination}", file=sys.stderr)
        return 1

    if destination.exists():
        shutil.rmtree(destination)
    (destination / "agents").mkdir(parents=True, exist_ok=True)

    role_title = f"{role_name} Agent"
    return_packet = definition.get("return_packet") or "role-receipt.md"
    description = (
        f"Use when a {role_title} receives a Master Agent context packet for "
        f"{definition.get('purpose', 'a project-defined role')}."
    )
    skill_text = f"""---
name: {skill_name}
description: {yaml_quoted(description)}
---

# {role_title}

## Overview

Act as a short-lived {role_title} inside a Master Agent system. Complete only the assigned context packet and return `{return_packet}`.

## Required Inputs

- Context packet.
- Project policy pack.
- Master ledger excerpt.
- Role catalog entry.
- Required return packet format.

## Rules

- Stay inside the assigned scope.
- Allowed work: {definition.get('allowed_work', '')}
- Forbidden work: {definition.get('forbidden_work', '')}
- Do not treat this role as project memory or product authority.
- Report token usage and heartbeat status as required by the context packet.
- Stop when role authority, scope, or validation is ambiguous.

## Output

Return `{return_packet}` with:

- Task id and role.
- Evidence reviewed or work completed.
- Validation or inspection performed.
- Token usage and budget status.
- Risks, blockers, and recommended next action.
"""
    metadata = f"""interface:
  display_name: {yaml_quoted(role_title)}
  short_description: {yaml_quoted("Project-defined Master Agent role")}
  default_prompt: {yaml_quoted(f"Use ${skill_name} to complete the assigned {role_name} role packet and return {return_packet}.")}
"""
    atomic_write_text(destination / "SKILL.md", skill_text)
    atomic_write_text(destination / "agents" / "openai.yaml", metadata)

    roles = load_roles(state_dir)
    roles[role_name]["role_skill"] = skill_name
    roles[role_name]["skill_path"] = str(destination)
    roles[role_name]["updated_at"] = format_time(parse_time(args.at))
    save_roles(state_dir, roles)
    print(f"Scaffolded role skill: {destination}")
    return 0


def remaining_budget(budget: dict, agent_id: str) -> tuple[int | None, int | None]:
    project_limit = budget.get("project_budget")
    project_remaining = None
    if project_limit:
        project_remaining = int(project_limit) - int(budget.get("project_used") or 0)

    agent_remaining = None
    agent_budget = budget.get("agents", {}).get(agent_id, {})
    agent_limit = agent_budget.get("token_budget")
    if agent_limit:
        agent_remaining = int(agent_limit) - int(agent_budget.get("tokens_used") or 0)
    return project_remaining, agent_remaining


def command_recommend_token_strategy(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    budget = load_budget(state_dir)
    agents = load_agents(state_dir)
    agent = agents.get(args.agent_id, {})
    project_remaining, agent_remaining = remaining_budget(budget, args.agent_id)
    expected = args.expected_tokens
    warning_percent = float(budget.get("warning_percent") or 80)
    project_limit = budget.get("project_budget")
    project_used = int(budget.get("project_used") or 0)
    agent_budget = budget.get("agents", {}).get(args.agent_id, {})
    agent_limit = agent_budget.get("token_budget")
    agent_used = int(agent_budget.get("tokens_used") or 0)
    has_low_confidence_usage = bool(agent_budget.get("has_low_confidence_usage"))
    has_uncertain_usage = bool(agent_budget.get("has_uncertain_usage"))
    usage_unknown = agent_used == 0 and not agent_budget.get("last_usage_source")

    action = "continue"
    exit_code = 0
    reasons: list[str] = []

    if (project_remaining is not None and expected > project_remaining) or (
        agent_remaining is not None and expected > agent_remaining
    ):
        action = "stop-or-request-budget"
        exit_code = 2
        reasons.append("expected tokens exceed remaining hard budget")
    elif project_limit and (
        project_used >= int(project_limit * warning_percent / 100)
        or project_used + expected >= int(project_limit * warning_percent / 100)
    ):
        action = "compress-and-narrow"
        exit_code = 1
        reasons.append("project budget is at or projected to reach warning threshold")
    elif agent_limit and (
        agent_used >= int(agent_limit * warning_percent / 100)
        or agent_used + expected >= int(agent_limit * warning_percent / 100)
    ):
        action = "compress-and-narrow"
        exit_code = 1
        reasons.append("agent budget is at or projected to reach warning threshold")
    elif usage_unknown and expected >= LARGE_CONTINUATION_TOKENS:
        action = "compress-and-narrow"
        exit_code = 1
        reasons.append("token usage is unknown; require a usage report before large continuation")
    elif has_low_confidence_usage and expected >= 1000:
        action = "compress-and-narrow"
        exit_code = 1
        reasons.append("low-confidence token usage requires compression before large continuation")
    elif has_uncertain_usage and expected >= 3000:
        action = "compress-and-narrow"
        exit_code = 1
        reasons.append("estimated or self-reported token usage requires a narrower continuation")

    context_tiers = {
        "low": "authority docs + current packet only",
        "medium": "authority docs + current packet + directly cited evidence",
        "high": "authority docs + current packet + directly cited evidence + one compact prior summary",
    }
    tier = context_tiers[args.task_complexity]

    print(f"Action: {action}")
    print(f"Agent: {args.agent_id}")
    print(f"Role: {agent.get('role', 'unknown')}")
    print(f"Expected tokens: {expected}")
    print(f"Project remaining: {project_remaining if project_remaining is not None else 'unbounded'}")
    print(f"Agent remaining: {agent_remaining if agent_remaining is not None else 'unbounded'}")
    print(f"Usage confidence: {agent_budget.get('last_usage_confidence', 'unknown')}")
    print(f"Usage source: {agent_budget.get('last_usage_source', 'unknown')}")
    print(f"Context tiers: {tier}")
    print("Master constraints:")
    print("- cap sub-agent count before spawning")
    print("- pass file paths and accepted packets, not raw chat")
    print("- require token usage in each heartbeat and receipt")
    print("Sub-agent autonomous strategy:")
    print("- summarize tool output before carrying it forward")
    print("- cite artifacts instead of pasting long evidence")
    print("- request compression or budget review before continuing large loops")
    if reasons:
        print("Reasons:")
        for reason in reasons:
            print(f"- {reason}")
    return exit_code


def command_check_heartbeats(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    now = parse_time(args.now)
    stale = find_stale_agents(state_dir, now, args.stale_minutes)
    return print_stale_result(stale)


def find_stale_agents(
    state_dir: Path, now: datetime, stale_minutes: float
) -> list[tuple[str, dict[str, str], float]]:
    agents = load_agents(state_dir)
    stale: list[tuple[str, dict[str, str], float]] = []
    for agent_id, agent in sorted(agents.items()):
        if agent.get("status") not in MONITORED_STATES:
            continue
        heartbeat_at = parse_time(agent.get("last_heartbeat_at"))
        age_minutes = (now - heartbeat_at).total_seconds() / 60
        if age_minutes > stale_minutes:
            stale.append((agent_id, agent, age_minutes))
    return stale


def print_stale_result(stale: list[tuple[str, dict[str, str], float]]) -> int:
    if not stale:
        print("No stale agents")
        return 0

    print("Stale agents:")
    for agent_id, agent, age_minutes in stale:
        print(
            f"- {agent_id}: stale for {age_minutes:.1f} minutes "
            f"(status={agent.get('status')}, task={agent.get('task_id')})"
        )
    return 1


def command_watch_heartbeats(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    checks = 0
    while True:
        checks += 1
        now = parse_time(args.now)
        print(f"Heartbeat watch check {checks}: {format_time(now)}")
        stale = find_stale_agents(state_dir, now, args.stale_minutes)
        result = print_stale_result(stale)
        if result != 0:
            return result
        if args.max_checks and checks >= args.max_checks:
            return 0
        time.sleep(args.poll_seconds)


def command_status(args: argparse.Namespace) -> int:
    agents = load_agents(Path(args.state_dir).resolve())
    if not agents:
        print("No registered agents")
        return 0
    for agent_id, agent in sorted(agents.items()):
        print(
            f"{agent_id}: role={agent.get('role')} status={agent.get('status')} "
            f"task={agent.get('task_id')} last_heartbeat={agent.get('last_heartbeat_at')}"
        )
    return 0


def command_new_packet(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    template_name = args.template
    if not template_name.endswith(".md"):
        template_name = f"{template_name}.md"
    template_path = TEMPLATE_DIR / template_name
    if not template_path.exists():
        print(f"Unknown template: {args.template}", file=sys.stderr)
        return 1

    output_dir = state_dir / "packets"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output).resolve() if args.output else output_dir / template_name
    try:
        output_path.relative_to(state_dir)
    except ValueError:
        print(f"Refusing to write packet outside state directory: {output_path}", file=sys.stderr)
        return 2
    if output_path.exists() and not args.force:
        print(f"Packet already exists: {output_path}", file=sys.stderr)
        return 1
    shutil.copyfile(template_path, output_path)
    print(f"Created packet: {output_path}")
    return 0


def command_install_role_skills(args: argparse.Namespace) -> int:
    source_dir = SKILL_ROOT / "role-skills"
    destination_root = Path(args.skills_dir).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    skipped: list[str] = []
    overwritten: list[str] = []

    for source in sorted(path for path in source_dir.iterdir() if path.is_dir()):
        destination = destination_root / source.name
        if destination.exists() and not args.force:
            skipped.append(source.name)
            continue
        if destination.exists():
            shutil.rmtree(destination)
            overwritten.append(source.name)
        else:
            installed.append(source.name)
        shutil.copytree(source, destination)

    print(f"Installed role skills to: {destination_root}")
    if installed:
        print("installed:")
        for name in installed:
            print(f"  {name}")
    if overwritten:
        print("overwritten:")
        for name in overwritten:
            print(f"  {name}")
    if skipped:
        print("skipped:")
        for name in skipped:
            print(f"  {name}")
    return 0


def _copy_skill_directory(source: Path, destination: Path, force: bool) -> str:
    if destination.exists() and not force:
        return "skipped"
    if destination.exists():
        shutil.rmtree(destination)
        result = "overwritten"
    else:
        result = "installed"
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            ".gitignore",
            "__pycache__",
            "*.pyc",
            "docs",
            "tests",
        ),
    )
    return result


def command_install_system(args: argparse.Namespace) -> int:
    destination_root = Path(args.skills_dir).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / "master-agent-system"
    try:
        destination_root.relative_to(SKILL_ROOT)
    except ValueError:
        pass
    else:
        print(
            f"Refusing to install into a directory inside the source skill pack: {destination_root}",
            file=sys.stderr,
        )
        return 2

    result = _copy_skill_directory(SKILL_ROOT, destination, args.force)
    role_args = argparse.Namespace(skills_dir=str(destination_root), force=args.force)
    command_install_role_skills(role_args)
    print(f"Installed Master Agent System: {destination} ({result})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate a Master Agent state pack.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Bootstrap templates and state files.")
    init.add_argument("--project-root", default=".")
    init.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=command_init)

    upgrade = subparsers.add_parser("upgrade-state", help="Non-destructively upgrade an existing state pack.")
    upgrade.add_argument("--project-root", default=".")
    upgrade.add_argument("--state-dir")
    upgrade.add_argument("--force", action="store_true")
    upgrade.set_defaults(func=command_upgrade_state)

    validate = subparsers.add_parser("validate", help="Validate state pack files.")
    validate.add_argument("--state-dir", required=True)
    validate.add_argument("--strict", action="store_true")
    validate.set_defaults(func=command_validate)

    register = subparsers.add_parser("register-agent", help="Register a running sub-agent.")
    register.add_argument("--state-dir", required=True)
    register.add_argument("--agent-id", required=True)
    register.add_argument("--role", required=True, help="Active role name from role-catalog.md.")
    register.add_argument("--task-id", required=True)
    register.add_argument("--objective", required=True)
    register.add_argument("--scope", required=True)
    register.add_argument("--status", default="active", choices=["starting", "active", "validating", "blocked", "complete", "stopping"])
    register.add_argument("--token-budget", type=int)
    register.add_argument("--max-heartbeats", type=int)
    register.add_argument("--plan-id")
    register.add_argument("--at")
    register.set_defaults(func=command_register_agent)

    heartbeat = subparsers.add_parser("heartbeat", help="Record a sub-agent heartbeat.")
    heartbeat.add_argument("--state-dir", required=True)
    heartbeat.add_argument("--agent-id", required=True)
    heartbeat.add_argument("--state", required=True, choices=["starting", "active", "validating", "blocked", "complete", "stopping"])
    heartbeat.add_argument("--current", required=True)
    heartbeat.add_argument("--last-action", required=True)
    heartbeat.add_argument("--next-action", required=True)
    heartbeat.add_argument("--scope-status", required=True, choices=["yes", "no", "unsure"])
    heartbeat.add_argument("--confidence", required=True, choices=["low", "medium", "high"])
    heartbeat.add_argument("--files-changed")
    heartbeat.add_argument("--artifacts")
    heartbeat.add_argument("--commands")
    heartbeat.add_argument("--plan-id")
    heartbeat.add_argument("--plan-alignment", choices=["yes", "no", "unsure"])
    heartbeat.add_argument("--repeated-action-count", type=int)
    heartbeat.add_argument("--evidence-quality", choices=["concrete", "weak", "missing"])
    heartbeat.add_argument("--self-reported-anomaly")
    heartbeat.add_argument("--risk")
    heartbeat.add_argument("--at")
    heartbeat.set_defaults(func=command_heartbeat)

    set_budget = subparsers.add_parser("set-budget", help="Set project-level token budget thresholds.")
    set_budget.add_argument("--state-dir", required=True)
    set_budget.add_argument("--project-budget", type=int, required=True)
    set_budget.add_argument("--warning-percent", type=float, default=80)
    set_budget.add_argument("--hard-percent", type=float, default=100)
    set_budget.set_defaults(func=command_set_budget)

    usage = subparsers.add_parser("record-usage", help="Record token usage for an agent.")
    usage.add_argument("--state-dir", required=True)
    usage.add_argument("--agent-id", required=True)
    usage.add_argument("--tokens-used", type=int, required=True)
    usage.add_argument("--source", choices=["measured", "estimated", "self-reported"], default="self-reported")
    usage.add_argument("--confidence", choices=["low", "medium", "high"], default="medium")
    usage.add_argument("--note")
    usage.add_argument("--at")
    usage.set_defaults(func=command_record_usage)

    check_budget = subparsers.add_parser("check-budget", help="Fail when token or heartbeat budgets exceed thresholds.")
    check_budget.add_argument("--state-dir", required=True)
    check_budget.set_defaults(func=command_check_budget)

    budget_status = subparsers.add_parser("budget-status", help="Print token and heartbeat budget status.")
    budget_status.add_argument("--state-dir", required=True)
    budget_status.set_defaults(func=command_budget_status)

    safety_status = subparsers.add_parser("safety-status", help="Print the active safety envelope summary.")
    safety_status.add_argument("--state-dir", required=True)
    safety_status.set_defaults(func=command_safety_status)

    check_safety = subparsers.add_parser("check-safety", help="Check whether a Master action is inside the safety envelope.")
    check_safety.add_argument("--state-dir", required=True)
    check_safety.add_argument("--action", required=True)
    check_safety.add_argument("--role", required=True)
    check_safety.add_argument("--scope", required=True)
    check_safety.add_argument("--budget-impact", type=int, required=True)
    check_safety.set_defaults(func=command_check_safety)

    accept_strategy = subparsers.add_parser("accept-strategy", help="Accept a strategy packet as the current plan.")
    accept_strategy.add_argument("--state-dir", required=True)
    accept_strategy.add_argument("--packet", required=True)
    accept_strategy.add_argument("--plan-id", required=True)
    accept_strategy.add_argument("--summary", required=True)
    accept_strategy.add_argument("--at")
    accept_strategy.set_defaults(func=command_accept_strategy)

    strategy_sync = subparsers.add_parser("strategy-sync-status", help="Print current accepted strategy plan status.")
    strategy_sync.add_argument("--state-dir", required=True)
    strategy_sync.add_argument("--stale-hours", type=float, default=24)
    strategy_sync.add_argument("--now")
    strategy_sync.set_defaults(func=command_strategy_sync_status)

    require_plan = subparsers.add_parser("require-plan", help="Fail unless the supplied plan id is current.")
    require_plan.add_argument("--state-dir", required=True)
    require_plan.add_argument("--plan-id", required=True)
    require_plan.set_defaults(func=command_require_plan)

    audit_agent = subparsers.add_parser("audit-agent", help="Detect loop, drift, reward-hacking, and token anomalies for an agent.")
    audit_agent.add_argument("--state-dir", required=True)
    audit_agent.add_argument("--agent-id", required=True)
    audit_agent.set_defaults(func=command_audit_agent)

    remediate = subparsers.add_parser("remediate-agent", help="Create a safety-checked remediation packet for an agent.")
    remediate.add_argument("--state-dir", required=True)
    remediate.add_argument("--agent-id", required=True)
    remediate.add_argument(
        "--action",
        required=True,
        choices=["reinforce-context", "spawn-successor", "split-task", "stop-agent"],
    )
    remediate.add_argument("--budget-impact", type=int, default=0)
    remediate.add_argument("--at")
    remediate.set_defaults(func=command_remediate_agent)

    supervise = subparsers.add_parser("supervise", help="Run the 24/7 runtime supervisor loop.")
    supervise.add_argument("--state-dir", required=True)
    supervise.add_argument("--poll-seconds", type=float, default=60)
    supervise.add_argument("--max-cycles", type=int)
    supervise.add_argument("--run-until-stopped", action="store_true")
    supervise.add_argument("--stale-minutes", type=float, default=30)
    supervise.add_argument("--quiet-start")
    supervise.add_argument("--quiet-end")
    supervise.add_argument("--now")
    supervise.set_defaults(func=command_supervise)

    supervisor_start = subparsers.add_parser("supervisor-start", help="Record supervisor process identity and lock state.")
    supervisor_start.add_argument("--state-dir", required=True)
    supervisor_start.add_argument("--poll-seconds", type=int, default=60)
    supervisor_start.add_argument("--deployment-mode", default="foreground", choices=["foreground", "scheduled", "service-wrapper"])
    supervisor_start.add_argument("--spawn", action="store_true", help="Start a background supervise loop and record its PID.")
    supervisor_start.add_argument("--now")
    supervisor_start.set_defaults(func=command_supervisor_start)

    supervisor_status = subparsers.add_parser("supervisor-status", help="Report supervisor lifecycle status.")
    supervisor_status.add_argument("--state-dir", required=True)
    supervisor_status.add_argument("--stale-seconds", type=float, default=600)
    supervisor_status.add_argument("--now")
    supervisor_status.set_defaults(func=command_supervisor_status)

    supervisor_stop = subparsers.add_parser("supervisor-stop", help="Request graceful supervisor stop.")
    supervisor_stop.add_argument("--state-dir", required=True)
    supervisor_stop.add_argument("--now")
    supervisor_stop.set_defaults(func=command_supervisor_stop)

    supervisor_recover = subparsers.add_parser("supervisor-recover", help="Recover stale or crashed supervisor state.")
    supervisor_recover.add_argument("--state-dir", required=True)
    supervisor_recover.add_argument("--stale-seconds", type=float, default=600)
    supervisor_recover.add_argument("--force", action="store_true")
    supervisor_recover.add_argument("--now")
    supervisor_recover.set_defaults(func=command_supervisor_recover)

    session_create = subparsers.add_parser("session-create", help="Create a provider-backed sub-agent session record.")
    session_create.add_argument("--state-dir", required=True)
    session_create.add_argument("--agent-id", required=True)
    session_create.add_argument("--role", required=True)
    session_create.add_argument("--context-packet", required=True)
    session_create.add_argument("--provider", choices=["file", "manual-provider", "codex"], default="file")
    session_create.add_argument("--provider-command")
    session_create.add_argument("--provider-timeout-seconds", type=float, default=60)
    session_create.add_argument("--predecessor-agent-id")
    session_create.add_argument("--reason")
    session_create.add_argument("--at")
    session_create.set_defaults(func=command_session_create)

    session_send = subparsers.add_parser("session-send", help="Send a message to a provider-backed session.")
    session_send.add_argument("--state-dir", required=True)
    session_send.add_argument("--agent-id", required=True)
    session_send.add_argument("--message", required=True)
    session_send.add_argument("--provider-command")
    session_send.add_argument("--provider-timeout-seconds", type=float, default=60)
    session_send.add_argument("--at")
    session_send.set_defaults(func=command_session_send)

    session_read = subparsers.add_parser("session-read", help="Read a provider-backed session transcript.")
    session_read.add_argument("--state-dir", required=True)
    session_read.add_argument("--agent-id", required=True)
    session_read.add_argument("--provider-command")
    session_read.add_argument("--provider-timeout-seconds", type=float, default=60)
    session_read.add_argument("--at")
    session_read.set_defaults(func=command_session_read)

    session_archive = subparsers.add_parser("session-archive", help="Archive a provider-backed session.")
    session_archive.add_argument("--state-dir", required=True)
    session_archive.add_argument("--agent-id", required=True)
    session_archive.add_argument("--provider-command")
    session_archive.add_argument("--provider-timeout-seconds", type=float, default=60)
    session_archive.add_argument("--at")
    session_archive.set_defaults(func=command_session_archive)

    session_reconcile = subparsers.add_parser("session-reconcile", help="Reconcile requested sessions with provider evidence.")
    session_reconcile.add_argument("--state-dir", required=True)
    session_reconcile.add_argument("--provider-command")
    session_reconcile.add_argument("--provider-timeout-seconds", type=float, default=60)
    session_reconcile.add_argument("--at")
    session_reconcile.set_defaults(func=command_session_reconcile)

    incident = subparsers.add_parser("record-incident", help="Record a production incident and open alerts for critical severity.")
    incident.add_argument("--state-dir", required=True)
    incident.add_argument("--severity", required=True, choices=["info", "warning", "critical"])
    incident.add_argument("--summary", required=True)
    incident.add_argument("--source", required=True)
    incident.add_argument("--at")
    incident.set_defaults(func=command_record_incident)

    alert_status = subparsers.add_parser("alert-status", help="Report open alerts.")
    alert_status.add_argument("--state-dir", required=True)
    alert_status.set_defaults(func=command_alert_status)

    ack_alert = subparsers.add_parser("acknowledge-alert", help="Append an alert acknowledgement.")
    ack_alert.add_argument("--state-dir", required=True)
    ack_alert.add_argument("--alert-id", required=True)
    ack_alert.add_argument("--note", required=True)
    ack_alert.add_argument("--at")
    ack_alert.set_defaults(func=command_acknowledge_alert)

    telemetry = subparsers.add_parser("telemetry-summary", help="Print production telemetry summary.")
    telemetry.add_argument("--state-dir", required=True)
    telemetry.set_defaults(func=command_telemetry_summary)

    schema_status = subparsers.add_parser("schema-status", help="Print state schema version.")
    schema_status.add_argument("--state-dir", required=True)
    schema_status.set_defaults(func=command_schema_status)

    migrate_state = subparsers.add_parser("migrate-state", help="Run ordered state migrations.")
    migrate_state.add_argument("--state-dir", required=True)
    migrate_state.add_argument("--at")
    migrate_state.set_defaults(func=command_migrate_state)

    recover_state = subparsers.add_parser("recover-state", help="Recover corrupt or missing state from append-only logs.")
    recover_state.add_argument("--state-dir", required=True)
    recover_state.add_argument("--from-logs", action="store_true")
    recover_state.set_defaults(func=command_recover_state)

    recover_locks = subparsers.add_parser("recover-locks", help="Remove stale state lock files.")
    recover_locks.add_argument("--state-dir", required=True)
    recover_locks.add_argument("--stale-seconds", type=float, default=600)
    recover_locks.set_defaults(func=command_recover_locks)

    list_roles = subparsers.add_parser("list-roles", help="List defined Master Agent roles.")
    list_roles.add_argument("--state-dir", required=True)
    list_roles.add_argument("--active-only", action="store_true")
    list_roles.set_defaults(func=command_list_roles)

    define_role = subparsers.add_parser("define-role", help="Create or update a governed role definition.")
    define_role.add_argument("--state-dir", required=True)
    define_role.add_argument("--role", required=True)
    define_role.add_argument("--purpose", required=True)
    define_role.add_argument("--allowed-work", required=True)
    define_role.add_argument("--forbidden-work", required=True)
    define_role.add_argument("--return-packet", required=True)
    define_role.add_argument("--scope")
    define_role.add_argument("--role-skill")
    define_role.add_argument("--token-budget", type=int)
    define_role.add_argument("--max-heartbeats", type=int)
    define_role.add_argument("--deactivation-condition")
    define_role.add_argument("--status", choices=["proposed", "active", "inactive"], default="proposed")
    define_role.add_argument("--activate", action="store_true")
    define_role.add_argument("--reason")
    define_role.add_argument("--approval")
    define_role.add_argument("--force", action="store_true")
    define_role.add_argument("--at")
    define_role.set_defaults(func=command_define_role)

    activate_role = subparsers.add_parser("activate-role", help="Mark a governed role active.")
    activate_role.add_argument("--state-dir", required=True)
    activate_role.add_argument("--role", required=True)
    activate_role.add_argument("--reason")
    activate_role.add_argument("--approval")
    activate_role.add_argument("--at")
    activate_role.set_defaults(func=command_activate_role)

    deactivate_role = subparsers.add_parser("deactivate-role", help="Mark a governed role inactive.")
    deactivate_role.add_argument("--state-dir", required=True)
    deactivate_role.add_argument("--role", required=True)
    deactivate_role.add_argument("--reason")
    deactivate_role.add_argument("--force", action="store_true")
    deactivate_role.add_argument("--at")
    deactivate_role.set_defaults(func=command_deactivate_role)

    scaffold_role = subparsers.add_parser("scaffold-role-skill", help="Create a Codex skill stub for a governed role.")
    scaffold_role.add_argument("--state-dir", required=True)
    scaffold_role.add_argument("--role", required=True)
    scaffold_role.add_argument("--skills-dir", required=True)
    scaffold_role.add_argument("--skill-name")
    scaffold_role.add_argument("--force", action="store_true")
    scaffold_role.add_argument("--at")
    scaffold_role.set_defaults(func=command_scaffold_role_skill)

    recommend = subparsers.add_parser("recommend-token-strategy", help="Recommend token-saving constraints for a sub-agent.")
    recommend.add_argument("--state-dir", required=True)
    recommend.add_argument("--agent-id", required=True)
    recommend.add_argument("--expected-tokens", type=int, required=True)
    recommend.add_argument("--task-complexity", choices=["low", "medium", "high"], default="medium")
    recommend.set_defaults(func=command_recommend_token_strategy)

    check = subparsers.add_parser("check-heartbeats", help="Fail when monitored agents are stale.")
    check.add_argument("--state-dir", required=True)
    check.add_argument("--stale-minutes", type=float, default=30)
    check.add_argument("--now")
    check.set_defaults(func=command_check_heartbeats)

    watch = subparsers.add_parser("watch-heartbeats", help="Poll heartbeat status until stale or stopped.")
    watch.add_argument("--state-dir", required=True)
    watch.add_argument("--stale-minutes", type=float, default=30)
    watch.add_argument("--poll-seconds", type=float, default=60)
    watch.add_argument("--max-checks", type=int)
    watch.add_argument("--now")
    watch.set_defaults(func=command_watch_heartbeats)

    status = subparsers.add_parser("status", help="Print registered agent status.")
    status.add_argument("--state-dir", required=True)
    status.set_defaults(func=command_status)

    new_packet = subparsers.add_parser("new-packet", help="Copy a packet template into the state directory.")
    new_packet.add_argument("--state-dir", required=True)
    new_packet.add_argument("--template", required=True)
    new_packet.add_argument("--output")
    new_packet.add_argument("--force", action="store_true")
    new_packet.set_defaults(func=command_new_packet)

    install = subparsers.add_parser("install-role-skills", help="Copy role skills into a Codex skills directory.")
    install.add_argument(
        "--skills-dir",
        required=True,
        help="Destination skills directory, such as %%USERPROFILE%%\\.codex\\skills.",
    )
    install.add_argument("--force", action="store_true")
    install.set_defaults(func=command_install_role_skills)

    install_system = subparsers.add_parser("install-system", help="Install the root skill and role skills into a Codex skills directory.")
    install_system.add_argument(
        "--skills-dir",
        required=True,
        help="Destination skills directory, such as %%USERPROFILE%%\\.codex\\skills.",
    )
    install_system.add_argument("--force", action="store_true")
    install_system.set_defaults(func=command_install_system)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
