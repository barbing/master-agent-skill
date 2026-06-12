#!/usr/bin/env python3
"""Validate that a Master Agent state pack has the required files and headings."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REQUIRED_HEADINGS = {
    "master-ledger.md": [
        "# Master Ledger",
        "## Current State",
        "## Authority",
        "## Running Agents",
        "## Accepted Decisions",
        "## Token Budget",
        "## Next Actions",
        "## Blockers",
    ],
    "project-policy-pack.md": [
        "# Project Policy Pack",
        "## Authority Docs",
        "## Active Objective",
        "## Ownership Boundaries",
        "## Forbidden Changes",
        "## Validation Policy",
        "## Stop And Ask Conditions",
    ],
    "running-agents.md": [
        "# Running Agents",
        "## Active Agents",
        "## Heartbeat Expectations",
        "## Stale Agents",
        "## Token Controls",
    ],
    "role-catalog.md": [
        "# Role Catalog",
        "## Role Governance",
        "## Active Roles",
        "## Inactive Or Proposed Roles",
        "## Role Creation Rules",
        "## Role Activation Rules",
    ],
    "role-proposal.md": [
        "# Role Proposal",
        "## Need",
        "## Existing Role Fit",
        "## Proposed Role",
        "## Scope",
        "## Token Controls",
        "## Activation Plan",
        "## Acceptance Check",
    ],
    "safety-envelope.md": [
        "# Safety Envelope",
        "## Autonomous Authority",
        "## Requires Human Decision",
        "## Forbidden Autonomous Actions",
        "## Budget And Role Limits",
        "## Remediation Permissions",
        "## Escalation Triggers",
    ],
    "master-boundary.md": [
        "# Master Boundary",
        "## Purpose",
        "## Allowed Master Write Paths",
        "## Forbidden Master Write Paths",
        "## Enforcement Policy",
        "## Escalation",
    ],
    "strategy-sync.md": [
        "# Strategy Sync",
        "## Current Accepted Plan",
        "## Strategy Sessions",
        "## Plan Version",
        "## Active Work Orders",
        "## Master Awareness",
        "## Resync Triggers",
    ],
    "anomaly-log.md": [
        "# Anomaly Log",
        "## Active Anomalies",
        "## Append Only Anomalies",
    ],
    "remediation-packet.md": [
        "# Remediation Packet",
        "## Trigger",
        "## Safety Check",
        "## Context Reinforcement",
        "## Successor Context",
        "## Split Task",
        "## Stop Action",
    ],
    "predecessor-state-packet.md": [
        "# Predecessor State Packet",
        "## Objective",
        "## Plan Id",
        "## Completed Work",
        "## Changed Files And Artifacts",
        "## Validation Evidence",
        "## Known Failures",
        "## Risks",
        "## Next Safe Step",
        "## Forbidden Repeats",
        "## Token Usage",
        "## Open Questions",
    ],
    "event-log.md": [
        "# Event Log",
        "## Append Only Events",
    ],
    "context-packet.md": [
        "# Context Packet",
        "## Assignment",
        "## Authority",
        "## Scope",
        "## Validation",
        "## Token Budget",
        "## Stop Conditions",
    ],
    "heartbeat-packet.md": [
        "# Heartbeat Packet",
        "## Status",
        "## Progress",
        "## Scope Check",
        "## Next Action",
    ],
    "strategy-packet.md": [
        "# Strategy Packet",
        "## Question",
        "## Plan Sync",
        "## Diagnosis",
        "## Options Considered",
        "## Recommendation",
        "## Proposed Work Order",
        "## Token Impact",
    ],
    "work-order.md": [
        "# Work Order",
        "## Objective",
        "## Allowed Scope",
        "## Parallel Safety",
        "## Token Budget",
        "## Forbidden Changes",
        "## Required Validation",
        "## Receipt Requirements",
    ],
    "coding-receipt.md": [
        "# Coding Receipt",
        "## Result",
        "## Changed Files",
        "## Validation",
        "## Artifacts",
        "## Token Usage",
        "## Risks",
    ],
    "review-verdict.md": [
        "# Review Verdict",
        "## Verdict",
        "## Evidence Reviewed",
        "## Findings",
        "## Required Follow Up",
    ],
    "policy-verdict.md": [
        "# Policy Verdict",
        "## Verdict",
        "## Authority Checked",
        "## Compliance Findings",
        "## Conditions Or Blockers",
    ],
    "token-strategy.md": [
        "# Token Strategy",
        "## Optimization Objective",
        "## Master Constraints",
        "## Sub-Agent Autonomous Strategies",
        "## Context Tiers",
        "## Compression Triggers",
        "## Escalation Rules",
        "## Research Boundary",
    ],
    "runtime-supervisor.md": [
        "# Runtime Supervisor",
        "## Operating Mode",
        "## Poll Cadence",
        "## Critical Checks",
        "## Recovery Policy",
        "## Quiet Periods",
        "## Operator Handoff",
        "## Stop Conditions",
    ],
    "runtime-status.md": [
        "# Runtime Status",
        "## Supervisor State",
        "## Last Check",
        "## Active Interventions",
        "## Next Wakeup",
        "## Handoff Summary",
    ],
    "runtime-deployment.md": [
        "# Runtime Deployment",
        "## Deployment Mode",
        "## Windows Startup",
        "## Process Identity",
        "## Crash Recovery",
        "## Stop And Status",
        "## Operator Override",
        "## Production Limits",
    ],
    "session-control.md": [
        "# Session Control",
        "## Provider Boundary",
        "## Session Lifecycle",
        "## Context Injection",
        "## Status Reconciliation",
        "## Termination And Archive",
        "## Failure Handling",
        "## Audit Trail",
    ],
    "incident-log.md": [
        "# Incident Log",
        "## Incident Summary",
        "## Severity Levels",
        "## Open Incidents",
        "## Resolved Incidents",
        "## Root Cause",
        "## Remediation",
        "## Operator Handoff",
    ],
    "alert-queue.md": [
        "# Alert Queue",
        "## Pending Alerts",
        "## Severity",
        "## Due Time",
        "## Suppression",
        "## Acknowledgement",
        "## Escalation",
    ],
    "state-schema.md": [
        "# State Schema",
        "## Current Schema",
        "## Migration Order",
        "## Compatibility Policy",
        "## Recovery Policy",
        "## Corruption Quarantine",
        "## Replay Sources",
        "## Stale Lock Handling",
    ],
}

STRICT_FIELD_REQUIREMENTS = {
    "master-ledger.md": [
        "Project",
        "Current objective",
        "Project policy pack",
        "Authority docs",
        "Active plan",
    ],
    "project-policy-pack.md": [
        "Objective",
        "Acceptance criteria",
    ],
}

STRICT_SECTION_REQUIREMENTS = {
    "project-policy-pack.md": {
        "## Authority Docs": "at least one authority document",
    }
}

STRICT_STATE_FILES = [
    Path("state") / "agents.json",
    Path("state") / "roles.json",
    Path("state") / "heartbeats.jsonl",
    Path("state") / "strategy-sync.jsonl",
    Path("state") / "anomalies.jsonl",
    Path("state") / "budget.json",
    Path("state") / "token-usage.jsonl",
    Path("state") / "runtime.json",
    Path("state") / "session-control.jsonl",
    Path("state") / "incidents.jsonl",
    Path("state") / "alerts.jsonl",
    Path("state") / "schema-version.json",
]

ACTIVE_ROLE_REQUIRED_FIELDS = [
    "purpose",
    "allowed_work",
    "forbidden_work",
    "return_packet",
]

ACTIVE_CUSTOM_ROLE_REQUIRED_FIELDS = [
    *ACTIVE_ROLE_REQUIRED_FIELDS,
    "scope",
    "token_budget",
    "max_heartbeats",
    "activation_approval",
    "deactivation_condition",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a project-local Master Agent state pack."
    )
    parser.add_argument("state_dir", help="Path to the Master Agent state directory.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also reject unfilled required fields in active ledger and policy files.",
    )
    return parser.parse_args()


def _field_is_filled(text: str, field_name: str) -> bool:
    prefix = f"- {field_name}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return bool(stripped[len(prefix) :].strip())
    return False


def _section_is_filled(text: str, heading: str) -> bool:
    lines = text.splitlines()
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_section:
                return False
            in_section = stripped == heading
            continue
        if in_section and stripped.startswith("- ") and stripped != "-":
            return True
    return False


def _read_json_object(path: Path, errors: list[str], label: str) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - validator should report parse failures.
        errors.append(f"{label}: invalid JSON ({exc})")
        return None
    if not isinstance(data, dict):
        errors.append(f"{label}: expected JSON object")
        return None
    return data


def _read_jsonl(path: Path, errors: list[str], label: str) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{label}:{index}: invalid JSON ({exc})")
            continue
        if isinstance(entry, dict):
            rows.append(entry)
        else:
            errors.append(f"{label}:{index}: expected JSON object")
    return rows


def _current_plan_id(state_dir: Path, errors: list[str]) -> str:
    history = _read_jsonl(
        state_dir / "state" / "strategy-sync.jsonl",
        errors,
        "state/strategy-sync.jsonl",
    )
    if not history:
        return ""
    return str(history[-1].get("plan_id") or "")


def _validate_active_role_contracts(state_dir: Path, errors: list[str]) -> None:
    roles = _read_json_object(state_dir / "state" / "roles.json", errors, "state/roles.json")
    if not roles:
        return
    for role_name, definition in sorted(roles.items()):
        if not isinstance(definition, dict):
            continue
        if definition.get("status") != "active":
            continue
        required_fields = (
            ACTIVE_CUSTOM_ROLE_REQUIRED_FIELDS
            if definition.get("role_type") == "custom"
            else ACTIVE_ROLE_REQUIRED_FIELDS
        )
        for field_name in required_fields:
            if not str(definition.get(field_name) or "").strip():
                errors.append(
                    f"state/roles.json: active role {role_name!r} missing required contract field {field_name!r}"
                )
        for field_name in ["token_budget", "max_heartbeats"]:
            value = definition.get(field_name)
            if value in {None, ""}:
                continue
            try:
                integer_value = int(value)
            except (TypeError, ValueError):
                errors.append(
                    f"state/roles.json: role {role_name!r} {field_name} must be an integer"
                )
                continue
            if definition.get("role_type") == "custom" and integer_value <= 0:
                errors.append(
                    f"state/roles.json: role {role_name!r} {field_name} must be positive"
                )
            elif integer_value < 0:
                errors.append(
                    f"state/roles.json: role {role_name!r} {field_name} must be non-negative"
                )


def _active_roles(state_dir: Path, errors: list[str]) -> set[str]:
    roles = _read_json_object(state_dir / "state" / "roles.json", errors, "state/roles.json")
    if not roles:
        return set()
    return {
        role_name
        for role_name, definition in roles.items()
        if isinstance(definition, dict) and definition.get("status") == "active"
    }


def _validate_registered_agent_roles(state_dir: Path, errors: list[str]) -> None:
    agents_path = state_dir / "state" / "agents.json"
    roles_path = state_dir / "state" / "roles.json"
    agents = _read_json_object(agents_path, errors, "state/agents.json")
    if not agents:
        return
    roles = _read_json_object(roles_path, errors, "state/roles.json")
    if roles is None:
        errors.append("state/roles.json: missing role registry for registered agents")
        return
    current_plan_id = _current_plan_id(state_dir, errors)
    for agent_id, agent in sorted(agents.items()):
        if not isinstance(agent, dict):
            errors.append(f"state/agents.json: {agent_id} must be an object")
            continue
        role = agent.get("role")
        if not role:
            errors.append(f"state/agents.json: {agent_id} missing role")
            continue
        role_definition = roles.get(role)
        if role_definition is None:
            errors.append(
                f"state/agents.json: {agent_id} uses undefined role {role!r}"
            )
            continue
        if not isinstance(role_definition, dict):
            errors.append(f"state/roles.json: role {role!r} must be an object")
            continue
        if role_definition.get("status") != "active":
            errors.append(
                f"state/agents.json: {agent_id} uses inactive role {role!r}"
            )
        agent_plan_id = str(agent.get("plan_id") or "")
        agent_status = str(agent.get("status") or "")
        if (
            current_plan_id
            and agent_status in {"starting", "active", "validating"}
            and agent_plan_id
            and agent_plan_id != current_plan_id
        ):
            errors.append(
                f"state/agents.json: {agent_id} plan {agent_plan_id!r} does not match current strategy plan {current_plan_id!r}"
            )


def _validate_work_order_roles(state_dir: Path, errors: list[str]) -> None:
    active_roles = _active_roles(state_dir, errors)
    candidates = [state_dir / "work-order.md"]
    packets_dir = state_dir / "packets"
    if packets_dir.exists():
        candidates.extend(sorted(packets_dir.rglob("*.md")))
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if "# Work Order" not in text:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith("- assigned role:"):
                continue
            role = stripped.split(":", 1)[1].strip()
            if role and role not in active_roles:
                rel = path.relative_to(state_dir).as_posix()
                errors.append(f"{rel}: work order uses undefined role {role!r}")


def _validate_budget_semantics(state_dir: Path, errors: list[str]) -> None:
    budget = _read_json_object(state_dir / "state" / "budget.json", errors, "state/budget.json")
    if not budget:
        return
    warning = budget.get("warning_percent")
    hard = budget.get("hard_percent")
    try:
        if warning is not None and hard is not None and float(hard) < float(warning):
            errors.append("state/budget.json: hard threshold must be >= warning threshold")
    except (TypeError, ValueError):
        errors.append("state/budget.json: warning and hard thresholds must be numeric")
    for field_name in ["project_budget", "project_used"]:
        value = budget.get(field_name)
        if value is not None:
            try:
                if int(value) < 0:
                    errors.append(f"state/budget.json: {field_name} must be non-negative")
            except (TypeError, ValueError):
                errors.append(f"state/budget.json: {field_name} must be an integer")
    for agent_id, agent_budget in budget.get("agents", {}).items():
        if not isinstance(agent_budget, dict):
            continue
        for field_name in ["token_budget", "tokens_used"]:
            value = agent_budget.get(field_name)
            if value is not None:
                try:
                    if int(value) < 0:
                        errors.append(
                            f"state/budget.json: {agent_id}.{field_name} must be non-negative"
                        )
                except (TypeError, ValueError):
                    errors.append(
                        f"state/budget.json: {agent_id}.{field_name} must be an integer"
                    )


def validate_state_pack(state_dir: Path, strict: bool = False) -> list[str]:
    state_dir = state_dir.resolve()
    errors: list[str] = []
    if not state_dir.exists() or not state_dir.is_dir():
        return [f"State directory does not exist: {state_dir}"]

    for filename, headings in REQUIRED_HEADINGS.items():
        path = state_dir / filename
        if not path.exists():
            errors.append(f"missing file: {filename}")
            continue
        text = path.read_text(encoding="utf-8")
        for heading in headings:
            if heading not in text:
                errors.append(f"{filename}: missing heading {heading!r}")

    _validate_registered_agent_roles(state_dir, errors)
    _validate_active_role_contracts(state_dir, errors)
    _validate_work_order_roles(state_dir, errors)
    _validate_budget_semantics(state_dir, errors)

    if strict:
        for relative_path in STRICT_STATE_FILES:
            path = state_dir / relative_path
            if not path.exists():
                errors.append(f"missing state file: {relative_path.as_posix()}")
                continue
            if relative_path.name in {"agents.json", "budget.json", "roles.json"}:
                _read_json_object(path, errors, relative_path.as_posix())
        for filename, field_names in STRICT_FIELD_REQUIREMENTS.items():
            path = state_dir / filename
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for field_name in field_names:
                if not _field_is_filled(text, field_name):
                    errors.append(
                        f"{filename}: unfilled required field {field_name!r}"
                    )
        for filename, sections in STRICT_SECTION_REQUIREMENTS.items():
            path = state_dir / filename
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for heading, description in sections.items():
                if not _section_is_filled(text, heading):
                    errors.append(
                        f"{filename}: unfilled required section {heading!r} ({description})"
                    )

    return errors


def main() -> int:
    args = parse_args()
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


if __name__ == "__main__":
    raise SystemExit(main())
