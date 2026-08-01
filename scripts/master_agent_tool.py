#!/usr/bin/env python3
"""Operate a file-backed Master Agent state pack."""

from __future__ import annotations

import argparse
import fnmatch
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
AGENT_STATES = [
    "starting",
    "active",
    "validating",
    "blocked",
    "authorization_invalid",
    "evidence_required",
    "attempt_recording_required",
    "validation_support_required",
    "in_root_transition_required",
    "external_mutation_domain_identified",
    "authority_required",
    "implementation_frozen_evidence_pending",
    "implementation_budget_exhausted",
    "complete",
    "stopping",
]
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
    "record-learning-correction",
    "start-learning-cycle",
    "learning-cycle-start",
    "lint-learning-proposal",
    "learning-proposal-lint",
    "accept-learning-proposal",
    "record-learning-effectiveness",
    "lint-governance-packet",
    "governance-lint",
    "record-governance-status",
    "record-authority-required",
    "record-acceptance-gate",
    "round-log-status",
    "record-round-log-evidence",
    "require-round-log-evidence",
    "round-log-export",
    "repair-log-status",
    "repair-log-init",
    "record-task",
    "open-repair-cycle",
    "record-repair-attempt",
    "require-current-repair-row",
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
    "round-log-restore",
}
SAFETY_WARNING_BUDGET_IMPACT = 5_000
SAFETY_HARD_BUDGET_IMPACT = 20_000
USAGE_SOURCES = ("measured", "estimated", "self-reported")
USAGE_CONFIDENCES = ("low", "medium", "high")
LARGE_CONTINUATION_TOKENS = 5_000
CURRENT_SCHEMA_VERSION = "1.5"
ORDERED_MIGRATIONS = [
    "0001-base-state",
    "0002-runtime-session-observability",
    "0003-learning-layer",
    "0004-governance-optimization",
    "0005-guard-synchronization",
    "0006-round-log-evidence",
    "0007-repair-log-control",
]
DEFAULT_CODEX_APP_READ_MAX_MINUTES = 60.0
DEFAULT_WORKTREE_EVIDENCE_MAX_MINUTES = 60.0
DEFAULT_ROUND_LOG_EVIDENCE_MAX_MINUTES = 1440.0
DEFAULT_REPAIR_LOG_ALLOWED_STATUSES = {"active", "continue", "in-progress", "ready"}
DEFAULT_REPAIR_LOG_BLOCKED_STATUSES = {
    "accepted",
    "blocked",
    "complete",
    "no-further-action",
    "not-ready",
    "paused",
    "repair-cycle-needed",
    "rollback-only",
    "superseded",
}

ROOT_AUTHORITY_SOURCE_KINDS = {
    "current-user-request",
    "current-goal",
    "user-approved-plan",
}

AUTHORITY_REQUIRED_STATUS = "authority_required"

AUTHORIZATION_INVALID_STATUS = "authorization_invalid"
IN_ROOT_TRANSITION_STATUS = "in_root_transition_required"
EXTERNAL_MUTATION_DOMAIN_IDENTIFIED_STATUS = "external_mutation_domain_identified"

GOVERNANCE_STATUS_VALUES = {
    "continue",
    "reassessment_required",
    "blocked",
    "evidence_required",
    "closeout_pending",
    "attempt_recording_required",
    "validation_support_required",
    "implementation_frozen_evidence_pending",
    "implementation_budget_exhausted",
    "scope_expansion_requires_explicit_domain",
    "authorization_invalid",
    "in_root_transition_required",
    "external_mutation_domain_identified",
    "blocked_scope_or_contract",
    "concurrent_scope_conflict",
    "invalid_implementation_progress_scope",
    AUTHORITY_REQUIRED_STATUS,
}

AUTHORITY_STATUS_VALUES = {
    "inside-envelope",
    "needs-user-decision",
    *GOVERNANCE_STATUS_VALUES,
}

LOOP_TYPES = {
    "implementation",
    "validation",
}

ASSERTION_POLICIES = {
    "preserve",
    "strengthen",
}

MATERIAL_BEHAVIOR_DOMAINS = {
    "none",
    "owner-internal-behavior",
    "pipeline-order",
    "batching-or-barrier-placement",
    "persistence-or-checkpoint-timing",
    "gui-event-timing",
    "cancellation-or-failure-semantics",
    "default-or-fallback-behavior",
}

ACCEPTANCE_MATURITY_ORDER = [
    "diagnostic",
    "focused_green",
    "live_seam_green",
    "representative_runtime_green",
    "visual_accepted",
    "production_accepted",
]
ACCEPTANCE_GATE_STATUSES = {
    "pending",
    "passed",
    "failed",
    "inconclusive",
}

PREDECESSOR_STATE_HEADINGS = [
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
]

PREDECESSOR_STATE_REQUIRED_SECTIONS = {
    "## Objective",
    "## Plan Id",
    "## Completed Work",
    "## Changed Files And Artifacts",
    "## Validation Evidence",
    "## Next Safe Step",
    "## Token Usage",
}

STRATEGY_PACKET_HEADINGS = [
    "# Strategy Packet",
    "## Question",
    "## Authority",
    "## Plan Sync",
    "## Diagnosis",
    "## Options Considered",
    "## Recommendation",
    "## Proposed Work Order",
    "## Open Risks",
    "## Token Impact",
]

STRATEGY_PACKET_REQUIRED_FIELDS = {
    "## Question": [
        "Strategy session id",
        "Question being answered",
        "User decision requested",
    ],
    "## Authority": [
        "Authority docs consulted",
        "Master ledger state used",
        "Policy pack sections used",
    ],
    "## Plan Sync": [
        "Proposed plan id",
        "Current accepted plan id",
        "Plan version change",
        "Master ledger update required",
        "Resync trigger",
    ],
    "## Diagnosis": [
        "Current diagnosis",
        "Current code path or process path",
        "Intended code path or process path",
        "First failing boundary",
    ],
    "## Recommendation": [
        "Recommended decision",
        "Reason",
        "Rejected alternatives",
        "Confidence",
    ],
    "## Proposed Work Order": [
        "Proposed objective",
        "Root authorization source",
        "Root authorization grant id",
        "Allowed scope",
        "Approved material behavior domains",
        "Declared material behavior domains",
        "Worktree mode",
        "Worktree id",
        "Base branch",
        "Local mutation policy",
        "Remote mutation policy",
        "Forbidden changes",
        "Acceptance maturity required",
        "Representative workflow required",
        "Heuristic admission required",
        "Task record required",
        "Validation required",
        "Expected artifacts",
        "Stop conditions",
    ],
    "## Token Impact": [
        "Estimated next-session token cost",
        "Recommended sub-agent count",
        "Recommended heartbeat cap",
        "Recommended context tier",
        "Recommended Master constraints",
        "Recommended sub-agent autonomous strategy",
        "Compression or narrowing trigger",
        "Token risks",
    ],
}

GOVERNANCE_PACKET_REQUIRED_FIELDS = {
    "context-packet": {
        "## Root Authorization": [
            "Source kind",
            "Source ref",
            "Grant id",
            "Objective",
            "Approved owners",
            "Approved file scopes",
            "Approved material behavior domains",
            "Explicit exclusions",
        ],
        "## Material Behavior Domains": [
            "Declared material behavior domains",
            "No material behavior change",
        ],
        "## Acceptance Gates": [
            "Required maturity gates",
            "Current maturity",
            "Lower gates satisfied",
            "Evidence artifact",
        ],
    },
    "work-order": {
        "## Root Authorization": [
            "Source kind",
            "Source ref",
            "Grant id",
            "Approved owners",
            "Approved file scopes",
            "Approved material behavior domains",
            "Forbidden behavior domains",
        ],
        "## Material Behavior Domains": [
            "Declared material behavior domains",
            "No material behavior change",
        ],
        "## Heuristic Admission": [
            "Heuristic used",
        ],
        "## Representative Workflow": [
            "Claim scope",
            "Workspace",
            "Bootstrap path",
            "Mode",
            "Provider or model path",
            "Key settings",
            "Representative parity",
            "Diagnostic-only if mismatch",
        ],
        "## Acceptance Gates": [
            "Required maturity gates",
            "Current maturity",
            "Lower gates satisfied",
            "Evidence artifact",
        ],
        "## Task Record": [
            "Task record required",
            "Record path or reason",
        ],
    },
    "coding-receipt": {
        "## Authority And Behavior": [
            "Grant id",
            "Observed owner",
            "Observed files inside envelope",
            "Observed material behavior domains",
            "No material behavior change",
            "Authority status",
        ],
        "## Acceptance Gates": [
            "Current maturity",
            "Lower gates satisfied",
            "Evidence artifact",
        ],
        "## Round Log Evidence": [
            "Round log required",
            "Changed paths match work order",
        ],
        "## Representative Workflow": [
            "Claim scope",
            "Representative parity",
            "Diagnostic-only if mismatch",
        ],
    },
    "review-verdict": {
        "## Acceptance Gate Review": [
            "Claimed maturity",
            "Highest supported maturity",
            "Lower gates satisfied",
            "Representative workflow parity checked",
            "Evidence artifact",
        ],
    },
    "policy-verdict": {
        "## Governance Review": [
            "Root authorization checked",
            "Material behavior domains checked",
            "Heuristic admission checked",
            "Representative workflow checked",
            "Authority status",
        ],
    },
    "obstacle-recovery-packet": {
        "## Obstacle Recovery": [
            "Status requested",
            "First failing boundary",
            "Safe diagnostics attempted",
            "In-scope alternatives attempted",
            "Remaining safe in-scope actions",
            "External or authority condition",
            "Smallest unblocking action",
        ],
    },
    "acceptance-gate": {
        "## Gate State": [
            "Scope id",
            "Maturity",
            "Status",
            "Evidence artifact",
            "Lower gates satisfied",
        ],
    },
    "guard-obligation": {
        "## Root Authorization": [
            "Source kind",
            "Source ref",
            "Grant id",
            "Objective",
            "Approved production owners",
            "Approved production file scopes",
            "Approved material behavior domains",
            "Explicit exclusions",
        ],
        "## Observation And Mutation": [
            "Observation outside owner allowed",
            "Production mutation requires root grant",
            "External mutation domain status",
            "Authority violation status",
        ],
        "## Obligation Contract": [
            "Schema version",
            "Obligation id",
            "Original target error",
            "Acceptance metric",
            "Completion maturity",
            "Required gate ids",
            "Contract docs",
        ],
        "## Loop Budget": [
            "Maximum implementation attempts",
            "Maximum reassessments",
            "Maximum recovery transitions",
            "Budgets reset by reassessment",
        ],
        "## Loop Type And Progress": [
            "Loop type",
            "Git-visible progress scope",
            "Ignored paths are progress",
            "Validation-only closeout allowed",
        ],
        "## Structured Validation": [
            "Validation uses argv",
            "Expected write roots declared",
            "Native receipts update gates",
            "Shell string allowed",
        ],
        "## Validation Support": [
            "Validation support roots",
            "Assertion policy",
            "Exact support files",
            "Production frozen during support",
        ],
        "## Visual Gate Boundary": [
            "Visual review external",
            "Receipt requires contract id",
            "Receipt requires candidate fingerprint",
            "Receipt requires coverage",
            "Evidence index opaque",
        ],
        "## Status Semantics": [
            "Authorization invalid status",
            "In-root transition status",
            "External mutation domain status",
            "Authority required status",
        ],
    },
}

LEARNING_PROPOSAL_HEADINGS = [
    "# Learning Proposal",
    "## Trigger",
    "## Distilled Lesson",
    "## Target",
    "## Safety Review",
    "## Validation",
    "## Decision",
]

LEARNING_PROPOSAL_REQUIRED_FIELDS = {
    "## Trigger": [
        "Proposal id",
        "Source corrections",
        "Failure mode",
        "Evidence",
    ],
    "## Distilled Lesson": [
        "Lesson",
        "Applies when",
        "Does not apply when",
        "Evidence trigger",
        "Escape condition",
        "Counterexample checked",
    ],
    "## Target": [
        "Target type",
        "Target path",
        "Change summary",
        "Implementation owner",
        "Requires production code change",
    ],
    "## Safety Review": [
        "Anti-narrowing risk",
        "Privacy or secret risk",
        "Licensing risk",
        "Policy review required",
    ],
    "## Validation": [
        "Required validation",
        "Success metric",
        "Recurrence check",
    ],
    "## Decision": [
        "Proposed decision",
        "Confidence",
        "Open questions",
    ],
}

LEARNING_TARGET_TYPES = {
    "project-policy-pack",
    "agents-md",
    "skill",
    "plugin-validator",
    "template",
    "memory-note",
    "skip",
}

LEARNING_DECISIONS = {
    "create",
    "extend",
    "validator",
    "skip",
    "needs-more-evidence",
}

LEARNING_EFFECTIVENESS_STATUSES = {
    "not-yet-measured",
    "recurrence-prevented",
    "recurrence-detected",
    "needs-more-evidence",
}

UNFILLED_PACKET_VALUES = {
    "",
    "-",
    "TODO",
    "TBD",
    "yes | no",
    "yes | no | not-required",
    "low | medium | high",
    "create | extend | validator | skip | needs-more-evidence",
    "project-policy-pack | agents-md | skill | plugin-validator | template | memory-note | skip",
    "not-yet-measured | recurrence-prevented | recurrence-detected | needs-more-evidence",
    "current-user-request | current-goal | user-approved-plan",
    "diagnostic | focused_green | live_seam_green | representative_runtime_green | visual_accepted | production_accepted",
    "pending | passed | failed | inconclusive",
    "owner-internal-behavior | pipeline-order | batching-or-barrier-placement | persistence-or-checkpoint-timing | gui-event-timing | cancellation-or-failure-semantics | default-or-fallback-behavior",
    "implementation | validation",
    "preserve | strengthen",
    "authorization_invalid | in_root_transition_required | external_mutation_domain_identified | authority_required",
}

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
    "Learning Distiller": {
        "status": "active",
        "role_type": "default",
        "purpose": "Distill corrections, incidents, failed reviews, and repeated agent mistakes into governed learning proposals.",
        "allowed_work": "Mine accepted state, correction records, incidents, anomalies, and review verdicts; produce learning cycles and learning proposals.",
        "forbidden_work": "Production implementation, unreviewed self-modification, broad memory-store behavior, or applying learning updates without Master acceptance.",
        "return_packet": "learning-proposal.md",
        "scope": "docs/master-agent and approved behavior assets",
        "role_skill": "master-learning-distiller-agent",
        "token_budget": None,
        "max_heartbeats": None,
        "activation_reason": "Default learning-layer role.",
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


def parse_markdown_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("#"):
            current = line.strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {heading: "\n".join(lines).strip() for heading, lines in sections.items()}


def section_has_content(value: str) -> bool:
    stripped = "\n".join(
        line.strip()
        for line in value.splitlines()
        if line.strip() and line.strip() not in {"-", "- ", "TODO", "TBD"}
    ).strip()
    return bool(stripped)


def validate_predecessor_state_packet(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"predecessor state packet does not exist: {path}"]
    text = path.read_text(encoding="utf-8")
    for heading in PREDECESSOR_STATE_HEADINGS:
        if heading not in text:
            errors.append(f"missing heading: {heading}")
    sections = parse_markdown_sections(text)
    for heading in sorted(PREDECESSOR_STATE_REQUIRED_SECTIONS):
        if heading in sections and not section_has_content(sections[heading]):
            errors.append(f"empty required section: {heading}")
    return errors


def markdown_bullet_field(section_text: str, field_name: str) -> str | None:
    pattern = re.compile(rf"^[ \t]*[-*][ \t]+{re.escape(field_name)}:[ \t]*(.*)$", re.MULTILINE)
    match = pattern.search(section_text)
    if not match:
        return None
    return " ".join(match.group(1).split()).strip()


def field_is_filled(value: str | None) -> bool:
    if value is None:
        return False
    normalized = " ".join(value.split()).strip()
    return normalized not in UNFILLED_PACKET_VALUES


def validate_strategy_packet(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"strategy packet does not exist: {path}"]
    text = path.read_text(encoding="utf-8")
    for heading in STRATEGY_PACKET_HEADINGS:
        if heading not in text:
            errors.append(f"missing heading: {heading}")
    sections = parse_markdown_sections(text)
    for heading, field_names in STRATEGY_PACKET_REQUIRED_FIELDS.items():
        section_text = sections.get(heading, "")
        if not section_has_content(section_text):
            errors.append(f"empty required section: {heading}")
            continue
        for field_name in field_names:
            value = markdown_bullet_field(section_text, field_name)
            if not field_is_filled(value):
                errors.append(f"unfilled field: {heading} / {field_name}")
    return errors


def validate_learning_proposal(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"learning proposal does not exist: {path}"]
    text = path.read_text(encoding="utf-8")
    for heading in LEARNING_PROPOSAL_HEADINGS:
        if heading not in text:
            errors.append(f"missing heading: {heading}")
    sections = parse_markdown_sections(text)
    for heading, field_names in LEARNING_PROPOSAL_REQUIRED_FIELDS.items():
        section_text = sections.get(heading, "")
        if not section_has_content(section_text):
            errors.append(f"empty required section: {heading}")
            continue
        for field_name in field_names:
            value = markdown_bullet_field(section_text, field_name)
            if not field_is_filled(value):
                errors.append(f"unfilled field: {heading} / {field_name}")

    target_type = markdown_bullet_field(sections.get("## Target", ""), "Target type")
    if target_type and target_type not in LEARNING_TARGET_TYPES:
        errors.append(
            "invalid field: ## Target / Target type "
            f"must be one of {', '.join(sorted(LEARNING_TARGET_TYPES))}"
        )
    production_code = (
        markdown_bullet_field(
            sections.get("## Target", ""), "Requires production code change"
        )
        or ""
    ).lower()
    if production_code not in {"yes", "no"}:
        errors.append("invalid field: ## Target / Requires production code change must be yes or no")
    elif production_code == "yes":
        errors.append("learning proposal cannot require production code change; create a normal work order instead")
    proposed_decision = markdown_bullet_field(
        sections.get("## Decision", ""), "Proposed decision"
    )
    if proposed_decision and proposed_decision not in LEARNING_DECISIONS:
        errors.append(
            "invalid field: ## Decision / Proposed decision "
            f"must be one of {', '.join(sorted(LEARNING_DECISIONS))}"
        )
    confidence = markdown_bullet_field(sections.get("## Decision", ""), "Confidence")
    if confidence and confidence not in {"low", "medium", "high"}:
        errors.append("invalid field: ## Decision / Confidence must be low, medium, or high")
    return errors


def split_list_field(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = value.strip()
    if normalized.lower() in {"none", "n/a"}:
        return ["none"]
    parts = re.split(r"[,;]", normalized)
    return [part.strip() for part in parts if part.strip()]


def field_value(
    sections: dict[str, str],
    heading: str,
    field_name: str,
) -> str:
    return markdown_bullet_field(sections.get(heading, ""), field_name) or ""


def validate_yes_no(
    errors: list[str],
    value: str,
    label: str,
) -> str:
    normalized = value.lower()
    if normalized not in {"yes", "no"}:
        errors.append(f"invalid field: {label} must be yes or no")
    return normalized


def validate_material_domains(
    errors: list[str],
    value: str,
    label: str,
) -> list[str]:
    domains = split_list_field(value)
    unknown = [domain for domain in domains if domain not in MATERIAL_BEHAVIOR_DOMAINS]
    if unknown:
        errors.append(
            f"invalid field: {label} contains unknown material behavior domains: "
            + ", ".join(unknown)
        )
    if "none" in domains and len(domains) > 1:
        errors.append(f"invalid field: {label} cannot combine none with other domains")
    return domains


def validate_maturity(
    errors: list[str],
    value: str,
    label: str,
) -> str:
    if value not in ACCEPTANCE_MATURITY_ORDER:
        errors.append(
            f"invalid field: {label} must be one of "
            + ", ".join(ACCEPTANCE_MATURITY_ORDER)
        )
    return value


def validate_governance_packet(path: Path, packet_type: str) -> list[str]:
    errors: list[str] = []
    if packet_type not in GOVERNANCE_PACKET_REQUIRED_FIELDS:
        return [f"unknown governance packet type: {packet_type}"]
    if not path.exists():
        return [f"governance packet does not exist: {path}"]
    text = path.read_text(encoding="utf-8")
    sections = parse_markdown_sections(text)
    requirements = GOVERNANCE_PACKET_REQUIRED_FIELDS[packet_type]
    for heading, field_names in requirements.items():
        section_text = sections.get(heading, "")
        if not section_text:
            errors.append(f"missing heading: {heading}")
            continue
        if not section_has_content(section_text):
            errors.append(f"empty required section: {heading}")
            continue
        for field_name in field_names:
            value = markdown_bullet_field(section_text, field_name)
            if not field_is_filled(value):
                errors.append(f"unfilled field: {heading} / {field_name}")

    for heading in ["## Root Authorization"]:
        if heading in sections:
            source_kind = field_value(sections, heading, "Source kind")
            if source_kind and source_kind not in ROOT_AUTHORITY_SOURCE_KINDS:
                errors.append(
                    "invalid field: ## Root Authorization / Source kind must be one of "
                    + ", ".join(sorted(ROOT_AUTHORITY_SOURCE_KINDS))
                )
            for label, field_name in [
                ("Approved material behavior domains", "Approved material behavior domains"),
                ("Forbidden behavior domains", "Forbidden behavior domains"),
            ]:
                value = field_value(sections, heading, field_name)
                if value:
                    validate_material_domains(errors, value, f"{heading} / {label}")

    material_heading = "## Material Behavior Domains"
    if material_heading in sections:
        domains = validate_material_domains(
            errors,
            field_value(sections, material_heading, "Declared material behavior domains"),
            f"{material_heading} / Declared material behavior domains",
        )
        no_material = validate_yes_no(
            errors,
            field_value(sections, material_heading, "No material behavior change"),
            f"{material_heading} / No material behavior change",
        )
        if no_material == "yes" and domains != ["none"]:
            errors.append(
                "invalid field: ## Material Behavior Domains / No material behavior change "
                "requires declared domains to be none"
            )
        if no_material == "no" and domains == ["none"]:
            errors.append(
                "invalid field: ## Material Behavior Domains / No material behavior change "
                "is no but declared domains are none"
            )

    authority_behavior_heading = "## Authority And Behavior"
    if authority_behavior_heading in sections:
        domains = validate_material_domains(
            errors,
            field_value(sections, authority_behavior_heading, "Observed material behavior domains"),
            f"{authority_behavior_heading} / Observed material behavior domains",
        )
        no_material = validate_yes_no(
            errors,
            field_value(sections, authority_behavior_heading, "No material behavior change"),
            f"{authority_behavior_heading} / No material behavior change",
        )
        if no_material == "yes" and domains != ["none"]:
            errors.append(
                "invalid field: ## Authority And Behavior / No material behavior change "
                "requires observed domains to be none"
            )
        authority_status = field_value(sections, authority_behavior_heading, "Authority status")
        if authority_status and authority_status not in AUTHORITY_STATUS_VALUES:
            errors.append(
                "invalid field: ## Authority And Behavior / Authority status "
                "must be a recognized governance status"
            )

    heuristic_heading = "## Heuristic Admission"
    if heuristic_heading in sections:
        heuristic_used = validate_yes_no(
            errors,
            field_value(sections, heuristic_heading, "Heuristic used"),
            f"{heuristic_heading} / Heuristic used",
        )
        if heuristic_used == "yes":
            for field_name in [
                "Authorized by",
                "Target-independent invariant",
                "Owning boundary",
                "Representative evidence",
                "Non-regression coverage",
                "Failure or escape behavior",
            ]:
                value = markdown_bullet_field(sections[heuristic_heading], field_name)
                if not field_is_filled(value):
                    errors.append(f"unfilled field: {heuristic_heading} / {field_name}")

    representative_heading = "## Representative Workflow"
    if representative_heading in sections:
        parity = validate_yes_no(
            errors,
            field_value(sections, representative_heading, "Representative parity"),
            f"{representative_heading} / Representative parity",
        )
        diagnostic = validate_yes_no(
            errors,
            field_value(sections, representative_heading, "Diagnostic-only if mismatch"),
            f"{representative_heading} / Diagnostic-only if mismatch",
        )
        if parity == "no" and diagnostic != "yes":
            errors.append(
                "invalid field: ## Representative Workflow / non-representative evidence "
                "must be marked diagnostic-only"
            )

    for heading, maturity_field in [
        ("## Acceptance Gates", "Current maturity"),
        ("## Acceptance Gate Review", "Highest supported maturity"),
        ("## Gate State", "Maturity"),
    ]:
        if heading in sections:
            maturity = validate_maturity(
                errors,
                field_value(sections, heading, maturity_field),
                f"{heading} / {maturity_field}",
            )
            lower = field_value(sections, heading, "Lower gates satisfied")
            if lower:
                lower_value = validate_yes_no(
                    errors,
                    lower,
                    f"{heading} / Lower gates satisfied",
                )
                if maturity in ACCEPTANCE_MATURITY_ORDER[1:] and lower_value != "yes":
                    errors.append(
                        f"invalid field: {heading} / higher maturity requires lower gates satisfied"
                    )
            status = field_value(sections, heading, "Status")
            if status and status not in ACCEPTANCE_GATE_STATUSES:
                errors.append(
                    f"invalid field: {heading} / Status must be one of "
                    + ", ".join(sorted(ACCEPTANCE_GATE_STATUSES))
                )

    policy_heading = "## Governance Review"
    if policy_heading in sections:
        authority_status = field_value(sections, policy_heading, "Authority status")
        if authority_status and authority_status not in AUTHORITY_STATUS_VALUES:
            errors.append(
                "invalid field: ## Governance Review / Authority status must be a recognized governance status"
            )

    round_log_heading = "## Round Log Evidence"
    if round_log_heading in sections:
        required = validate_yes_no(
            errors,
            field_value(sections, round_log_heading, "Round log required"),
            f"{round_log_heading} / Round log required",
        )
        match_status = field_value(sections, round_log_heading, "Changed paths match work order")
        if match_status and match_status not in {"yes", "no", "not-required"}:
            errors.append(
                "invalid field: ## Round Log Evidence / Changed paths match work order "
                "must be yes, no, or not-required"
            )
        if required == "yes":
            for field_name in ["Snapshot id", "Manifest path"]:
                value = markdown_bullet_field(sections[round_log_heading], field_name)
                if not field_is_filled(value):
                    errors.append(f"unfilled field: {round_log_heading} / {field_name}")
            if match_status != "yes":
                errors.append(
                    "invalid field: ## Round Log Evidence / required round-log evidence "
                    "must match the work order"
                )

    obstacle_heading = "## Obstacle Recovery"
    if obstacle_heading in sections:
        status_requested = field_value(sections, obstacle_heading, "Status requested")
        if status_requested and status_requested not in GOVERNANCE_STATUS_VALUES:
            errors.append(
                "invalid field: ## Obstacle Recovery / Status requested must be "
                "a recognized governance status"
            )
        if status_requested in {
            "blocked",
            "reassessment_required",
            IN_ROOT_TRANSITION_STATUS,
            EXTERNAL_MUTATION_DOMAIN_IDENTIFIED_STATUS,
            AUTHORITY_REQUIRED_STATUS,
            "implementation_budget_exhausted",
        }:
            for field_name in [
                "Safe diagnostics attempted",
                "In-scope alternatives attempted",
                "Remaining safe in-scope actions",
                "External or authority condition",
                "Smallest unblocking action",
            ]:
                value = markdown_bullet_field(sections[obstacle_heading], field_name)
                if not field_is_filled(value):
                    errors.append(f"unfilled field: {obstacle_heading} / {field_name}")

    observation_heading = "## Observation And Mutation"
    if observation_heading in sections:
        for field_name in [
            "Observation outside owner allowed",
            "Production mutation requires root grant",
        ]:
            validate_yes_no(
                errors,
                field_value(sections, observation_heading, field_name),
                f"{observation_heading} / {field_name}",
            )
        for field_name in ["External mutation domain status", "Authority violation status"]:
            status = field_value(sections, observation_heading, field_name)
            if status and status not in GOVERNANCE_STATUS_VALUES:
                errors.append(
                    f"invalid field: {observation_heading} / {field_name} "
                    "must be a recognized governance status"
                )

    obligation_heading = "## Obligation Contract"
    if obligation_heading in sections:
        schema_version = field_value(sections, obligation_heading, "Schema version")
        if schema_version and schema_version != "6":
            errors.append("invalid field: ## Obligation Contract / Schema version must be 6")
        completion = field_value(sections, obligation_heading, "Completion maturity")
        if completion:
            validate_maturity(
                errors,
                completion,
                f"{obligation_heading} / Completion maturity",
            )

    budget_heading = "## Loop Budget"
    if budget_heading in sections:
        reset = validate_yes_no(
            errors,
            field_value(sections, budget_heading, "Budgets reset by reassessment"),
            f"{budget_heading} / Budgets reset by reassessment",
        )
        if reset == "yes":
            errors.append(
                "invalid field: ## Loop Budget / Budgets reset by reassessment must be no"
            )

    progress_heading = "## Loop Type And Progress"
    if progress_heading in sections:
        loop_type = field_value(sections, progress_heading, "Loop type")
        if loop_type and loop_type not in LOOP_TYPES:
            errors.append(
                "invalid field: ## Loop Type And Progress / Loop type must be implementation or validation"
            )
        ignored_progress = validate_yes_no(
            errors,
            field_value(sections, progress_heading, "Ignored paths are progress"),
            f"{progress_heading} / Ignored paths are progress",
        )
        if ignored_progress == "yes":
            errors.append(
                "invalid field: ## Loop Type And Progress / Ignored paths are progress must be no"
            )
        validation_closeout = validate_yes_no(
            errors,
            field_value(sections, progress_heading, "Validation-only closeout allowed"),
            f"{progress_heading} / Validation-only closeout allowed",
        )
        if loop_type == "validation" and validation_closeout != "yes":
            errors.append(
                "invalid field: ## Loop Type And Progress / validation loop type requires validation-only closeout allowed"
            )

    validation_heading = "## Structured Validation"
    if validation_heading in sections:
        for field_name in [
            "Validation uses argv",
            "Expected write roots declared",
            "Native receipts update gates",
        ]:
            value = validate_yes_no(
                errors,
                field_value(sections, validation_heading, field_name),
                f"{validation_heading} / {field_name}",
            )
            if value == "no":
                errors.append(f"invalid field: {validation_heading} / {field_name} must be yes")
        shell_allowed = validate_yes_no(
            errors,
            field_value(sections, validation_heading, "Shell string allowed"),
            f"{validation_heading} / Shell string allowed",
        )
        if shell_allowed == "yes":
            errors.append("invalid field: ## Structured Validation / Shell string allowed must be no")

    support_heading = "## Validation Support"
    if support_heading in sections:
        assertion_policy = field_value(sections, support_heading, "Assertion policy")
        if assertion_policy and assertion_policy not in ASSERTION_POLICIES:
            errors.append(
                "invalid field: ## Validation Support / Assertion policy must be preserve or strengthen"
            )
        frozen = validate_yes_no(
            errors,
            field_value(sections, support_heading, "Production frozen during support"),
            f"{support_heading} / Production frozen during support",
        )
        if frozen == "no":
            errors.append(
                "invalid field: ## Validation Support / Production frozen during support must be yes"
            )

    visual_heading = "## Visual Gate Boundary"
    if visual_heading in sections:
        for field_name in [
            "Visual review external",
            "Receipt requires contract id",
            "Receipt requires candidate fingerprint",
            "Receipt requires coverage",
            "Evidence index opaque",
        ]:
            value = validate_yes_no(
                errors,
                field_value(sections, visual_heading, field_name),
                f"{visual_heading} / {field_name}",
            )
            if value == "no":
                errors.append(f"invalid field: {visual_heading} / {field_name} must be yes")

    status_heading = "## Status Semantics"
    if status_heading in sections:
        expected_statuses = {
            "Authorization invalid status": AUTHORIZATION_INVALID_STATUS,
            "In-root transition status": IN_ROOT_TRANSITION_STATUS,
            "External mutation domain status": EXTERNAL_MUTATION_DOMAIN_IDENTIFIED_STATUS,
            "Authority required status": AUTHORITY_REQUIRED_STATUS,
        }
        for field_name, expected in expected_statuses.items():
            actual = field_value(sections, status_heading, field_name)
            if actual and actual != expected:
                errors.append(
                    f"invalid field: {status_heading} / {field_name} must be {expected}"
                )
    return errors


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
    worktrees_path = storage_dir / "worktrees.jsonl"
    incidents_path = storage_dir / "incidents.jsonl"
    alerts_path = storage_dir / "alerts.jsonl"
    learning_corrections_path = storage_dir / "learning-corrections.jsonl"
    learning_cycles_path = storage_dir / "learning-cycles.jsonl"
    learning_updates_path = storage_dir / "learning-updates.jsonl"
    learning_effectiveness_path = storage_dir / "learning-effectiveness.jsonl"
    governance_events_path = storage_dir / "governance-events.jsonl"
    acceptance_gates_path = storage_dir / "acceptance-gates.jsonl"
    round_log_events_path = storage_dir / "round-log-events.jsonl"
    repair_log_events_path = storage_dir / "repair-log-events.jsonl"
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
    if not worktrees_path.exists():
        atomic_write_text(worktrees_path, "")
    if not incidents_path.exists():
        atomic_write_text(incidents_path, "")
    if not alerts_path.exists():
        atomic_write_text(alerts_path, "")
    if not learning_corrections_path.exists():
        atomic_write_text(learning_corrections_path, "")
    if not learning_cycles_path.exists():
        atomic_write_text(learning_cycles_path, "")
    if not learning_updates_path.exists():
        atomic_write_text(learning_updates_path, "")
    if not learning_effectiveness_path.exists():
        atomic_write_text(learning_effectiveness_path, "")
    if not governance_events_path.exists():
        atomic_write_text(governance_events_path, "")
    if not acceptance_gates_path.exists():
        atomic_write_text(acceptance_gates_path, "")
    if not round_log_events_path.exists():
        atomic_write_text(round_log_events_path, "")
    if not repair_log_events_path.exists():
        atomic_write_text(repair_log_events_path, "")
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


def role_requires_validated_strategy(role_name: str) -> bool:
    return role_name in {"Coding", "Review", "Policy Review"}


def strategy_packet_validation_errors_for_entry(entry: dict | None) -> list[str]:
    if not entry:
        return ["no current accepted strategy plan"]
    if not entry.get("strategy_packet_validated"):
        return ["current strategy packet has not been validated"]
    packet = Path(str(entry.get("packet") or "")).resolve()
    errors = validate_strategy_packet(packet)
    if errors:
        return [f"accepted strategy packet is no longer valid: {error}" for error in errors]
    return []


def require_validated_strategy_for_work(
    state_dir: Path,
    role_name: str,
    plan_id: str | None,
) -> list[str]:
    if not role_requires_validated_strategy(role_name):
        return []
    entry = current_strategy_plan(state_dir)
    if not entry:
        return [f"{role_name} work requires a current validated strategy plan"]
    current_plan_id = entry.get("plan_id")
    if plan_id != current_plan_id:
        return [f"{role_name} work requires current plan {current_plan_id}"]
    return strategy_packet_validation_errors_for_entry(entry)


def render_strategy_sync(state_dir: Path, entry: dict | None, status: str = "current") -> None:
    plan_id = entry.get("plan_id", "") if entry else ""
    summary = entry.get("summary", "") if entry else ""
    accepted_at = entry.get("accepted_at", "") if entry else ""
    packet = entry.get("packet", "") if entry else ""
    validated = "yes" if entry and entry.get("strategy_packet_validated") else "no"
    lines = [
        "# Strategy Sync",
        "",
        "## Current Accepted Plan",
        "",
        f"- Plan id: {plan_id}",
        f"- Summary: {summary}",
        f"- Accepted at: {accepted_at}",
        f"- Strategy packet: {packet}",
        f"- Strategy packet validated: {validated}",
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
        state_dir / "state" / "worktrees.jsonl",
        state_dir / "state" / "incidents.jsonl",
        state_dir / "state" / "alerts.jsonl",
        state_dir / "state" / "learning-corrections.jsonl",
        state_dir / "state" / "learning-cycles.jsonl",
        state_dir / "state" / "learning-updates.jsonl",
        state_dir / "state" / "learning-effectiveness.jsonl",
        state_dir / "state" / "governance-events.jsonl",
        state_dir / "state" / "acceptance-gates.jsonl",
        state_dir / "state" / "round-log-events.jsonl",
        state_dir / "state" / "repair-log-events.jsonl",
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
    strategy_errors = require_validated_strategy_for_work(
        state_dir,
        role_name,
        args.plan_id,
    )
    if strategy_errors:
        for error in strategy_errors:
            print(error, file=sys.stderr)
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
    packet_errors = validate_strategy_packet(packet)
    if packet_errors:
        for error in packet_errors:
            print(error, file=sys.stderr)
        return 1
    entry = {
        "accepted_at": timestamp,
        "packet": str(packet),
        "plan_id": args.plan_id,
        "summary": args.summary,
        "strategy_packet_validated": True,
        "strategy_packet_validated_at": timestamp,
        "strategy_packet_validation": "strategy-packet-lint",
    }
    append_strategy_sync(state_dir, entry)
    render_strategy_sync(state_dir, entry)
    append_event_log(
        state_dir=state_dir,
        event_type="strategy-accepted",
        related_packet=str(packet),
        summary=f"{args.plan_id}: {args.summary}",
        evidence=f"{packet}; strategy-packet-lint passed",
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


def command_strategy_packet_lint(args: argparse.Namespace) -> int:
    packet = Path(args.packet).resolve()
    errors = validate_strategy_packet(packet)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"Strategy packet is valid: {packet}")
    return 0


def command_require_strategy_packet_before_work(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    entry = current_strategy_plan(state_dir)
    if not entry:
        print("No current accepted strategy plan.", file=sys.stderr)
        return 1
    current_plan_id = entry.get("plan_id")
    if current_plan_id != args.plan_id:
        print(
            f"Current plan mismatch: expected {current_plan_id or 'none'}, got {args.plan_id}",
            file=sys.stderr,
        )
        return 1

    accepted_packet = Path(str(entry.get("packet") or "")).resolve()
    if not accepted_packet.exists():
        print(
            f"Accepted strategy packet is missing: {accepted_packet}",
            file=sys.stderr,
        )
        return 1

    supplied_packet = Path(args.packet).resolve() if args.packet else accepted_packet
    if supplied_packet != accepted_packet:
        print(
            f"Strategy packet is not the current accepted packet: {supplied_packet}",
            file=sys.stderr,
        )
        print(f"Current accepted packet: {accepted_packet}", file=sys.stderr)
        return 1

    errors = strategy_packet_validation_errors_for_entry(entry)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(f"Strategy pre-work gate passed: {current_plan_id}")
    print(f"Packet: {accepted_packet}")
    return 0


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

    repeated_loop_detected = False
    if len(history) >= 3:
        last_three = history[-3:]
        next_actions = [entry.get("next_action", "") for entry in last_three]
        if next_actions[0] and len(set(next_actions)) == 1:
            repeated_loop_detected = True
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
        anomaly_text = latest.get("self_reported_anomaly", "")
        normalized_anomaly = anomaly_text.lower()
        if any(
            marker in normalized_anomaly
            for marker in ["attention", "context overload", "context bloat", "lost focus"]
        ):
            if not repeated_loop_detected:
                add(
                    "attention-drift",
                    "high",
                    anomaly_text,
                    "spawn successor with compact inherited context",
                )
        else:
            add(
                "self-reported-anomaly",
                "medium",
                anomaly_text,
                "inspect and remediate",
            )

    if latest.get("risk"):
        risk_text = latest.get("risk", "")
        normalized_risk = risk_text.lower()
        if any(
            marker in normalized_risk
            for marker in ["attention drift", "context overload", "context bloat", "lost focus"]
        ):
            if not repeated_loop_detected:
                add(
                    "attention-drift",
                    "high",
                    risk_text,
                    "spawn successor with compact inherited context",
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


def optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


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


def write_session_rotation_packets(
    state_dir: Path,
    agent_id: str,
    successor_agent_id: str,
    reason: str,
    predecessor_state_packet: Path | None,
) -> tuple[Path, Path]:
    agents = load_agents(state_dir)
    if agent_id not in agents:
        raise SystemExit(f"Unknown agent id: {agent_id}")
    predecessor = agents[agent_id]
    history = load_agent_heartbeats(state_dir, agent_id)
    latest = history[-1] if history else predecessor
    current_plan = current_strategy_plan(state_dir)
    current_plan_id = current_plan.get("plan_id") if current_plan else latest.get("plan_id", "")
    forbidden_repeat = repeated_next_action(history)
    output_dir = state_dir / "packets" / "session-rotation"
    output_dir.mkdir(parents=True, exist_ok=True)

    save_request = output_dir / f"{agent_id}-save-state-request.md"
    save_text = f"""# Agent State Save Request

## Instruction

Stop current implementation and return a compact predecessor state packet before doing any more task work.

## Required State

- Agent id: {agent_id}
- Task id: {predecessor.get('task_id', '')}
- Objective: {predecessor.get('objective', '')}
- Scope: {predecessor.get('scope', '')}
- Accepted plan id: {current_plan_id or ''}
- Current artifact/file: {latest.get('current', '')}
- Last concrete progress: {latest.get('last_action', '')}
- Next intended action: {latest.get('next_action', '')}
- Files changed: {latest.get('files_changed', '')}
- Artifacts: {latest.get('artifacts', '')}
- Commands: {latest.get('commands', '')}
- Open risks: {latest.get('risk', '')}
- Token status: {agent_token_status(state_dir, agent_id)}
- Forbidden repeats: {forbidden_repeat}

## Output Contract

Return only a compact state packet with completed work, changed files, validation, unresolved blockers, risks, and the next atomic safe step. Do not continue implementation in this session.
"""
    atomic_write_text(save_request, save_text)

    predecessor_state_source = "latest heartbeat"
    predecessor_state_text = ""
    if predecessor_state_packet:
        predecessor_state_source = str(predecessor_state_packet)
        predecessor_state_text = predecessor_state_packet.read_text(encoding="utf-8")

    successor_context = output_dir / f"{agent_id}-to-{successor_agent_id}-context.md"
    context_text = f"""# Successor Context Packet

## Rotation Metadata

- Predecessor agent id: {agent_id}
- Successor agent id: {successor_agent_id}
- Inheritance reason: {reason}
- State save request: {save_request}
- Predecessor state source: {predecessor_state_source}

## Assignment

- Role: {predecessor.get('role', '')}
- Task id: {predecessor.get('task_id', '')}
- Objective: {predecessor.get('objective', '')}
- Scope: {predecessor.get('scope', '')}
- Accepted plan id: {current_plan_id or ''}

## Latest Predecessor State

- Current artifact/file: {latest.get('current', '')}
- Last concrete progress: {latest.get('last_action', '')}
- Next intended action: {latest.get('next_action', '')}
- Files changed: {latest.get('files_changed', '')}
- Artifacts: {latest.get('artifacts', '')}
- Commands: {latest.get('commands', '')}
- Open risks: {latest.get('risk', '')}
- Token status: {agent_token_status(state_dir, agent_id)}
- Forbidden repeats: {forbidden_repeat}

## Predecessor State Packet

{predecessor_state_text or '- No final predecessor state packet was supplied; use the latest structured heartbeat and state save request as the continuity baseline.'}

## Successor Instructions

- Treat this packet as the authority for inherited context.
- Do not read or rely on raw predecessor chat history unless the Master explicitly authorizes it.
- Start from the next atomic safe step, not from the predecessor's repeated loop.
- Preserve the accepted plan, scope, validation requirements, and stop conditions.
- Report the first heartbeat before making risky edits.
"""
    atomic_write_text(successor_context, context_text)
    return save_request, successor_context


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
    if anomaly_type in {"attention-drift", "repeated-next-action-loop"}:
        return "spawn-successor"
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


def session_event_is_terminal(event: dict) -> bool:
    return event.get("event") in {"session-archived", "session-stale"}


def event_session_ref(event: dict) -> str:
    return str(
        event.get("provider_session_ref")
        or event.get("provider_session_path")
        or event.get("provider_session_id")
        or ""
    )


def latest_session_event(state_dir: Path, agent_id: str) -> dict | None:
    for event in reversed(load_session_events(state_dir)):
        if event.get("agent_id") != agent_id:
            continue
        if session_event_is_terminal(event):
            return None
        if event.get("event") == "session-created" and event_session_ref(event):
            return event
    return None


def latest_session_event_of_type(
    state_dir: Path,
    agent_id: str,
    event_types: set[str],
) -> dict | None:
    for event in reversed(load_session_events(state_dir)):
        if event.get("agent_id") == agent_id and event.get("event") in event_types:
            return event
    return None


def latest_confirmed_read_event(state_dir: Path, agent_id: str) -> dict | None:
    for event in reversed(load_session_events(state_dir)):
        if (
            event.get("agent_id") == agent_id
            and event.get("event") == "session-read"
            and event.get("provider") == "codex-app"
            and event.get("provider_confirmed")
        ):
            return event
    return None


def codex_app_ref(thread_id: str) -> str:
    return f"codex-app:{thread_id}"


def append_worktree_event(state_dir: Path, entry: dict) -> None:
    append_jsonl_locked(state_dir / "state" / "worktrees.jsonl", entry)


def load_worktree_events(state_dir: Path) -> list[dict]:
    ensure_state_storage(state_dir)
    events: list[dict] = []
    path = state_dir / "state" / "worktrees.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid worktree history: {path}: {exc}") from exc
        if isinstance(event, dict):
            events.append(event)
    return events


def worktree_event_is_terminal(event: dict) -> bool:
    return event.get("event") in {"worktree-closed", "worktree-stale"}


def latest_worktree_event(state_dir: Path, worktree_id: str) -> dict | None:
    for event in reversed(load_worktree_events(state_dir)):
        if event.get("worktree_id") != worktree_id:
            continue
        if worktree_event_is_terminal(event):
            return None
        if event.get("event") in {
            "worktree-planned",
            "worktree-created",
            "worktree-session-bound",
            "worktree-handoff-requested",
        }:
            return event
    return None


def latest_created_worktree_event(state_dir: Path, worktree_id: str) -> dict | None:
    for event in reversed(load_worktree_events(state_dir)):
        if event.get("worktree_id") != worktree_id:
            continue
        if worktree_event_is_terminal(event):
            return None
        if event.get("event") == "worktree-created":
            return event
    return None


def latest_worktree_binding(state_dir: Path, worktree_id: str) -> dict | None:
    for event in reversed(load_worktree_events(state_dir)):
        if event.get("worktree_id") != worktree_id:
            continue
        if worktree_event_is_terminal(event):
            return None
        if event.get("event") == "worktree-session-bound":
            return event
    return None


def worktree_ref(provider: str, worktree_id: str, provider_worktree_ref: str = "") -> str:
    if provider_worktree_ref:
        return provider_worktree_ref
    return f"{provider}-worktree:{worktree_id}"


def require_worktree_plan_or_active(state_dir: Path, worktree_id: str) -> dict | None:
    if not worktree_id:
        return None
    event = latest_worktree_event(state_dir, worktree_id)
    if not event:
        print(f"No planned or active worktree found for {worktree_id}", file=sys.stderr)
        return None
    return event


def require_created_worktree(state_dir: Path, worktree_id: str) -> dict | None:
    event = latest_created_worktree_event(state_dir, worktree_id)
    if not event:
        print(f"No confirmed active worktree found for {worktree_id}", file=sys.stderr)
        return None
    return event


def git_repo_root(project_root: Path) -> tuple[Path | None, str | None]:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git is unavailable ({exc})"
    if result.returncode != 0:
        return None, "project root is not inside a Git repository"
    return Path(result.stdout.strip()).resolve(), None


def normalize_git_output_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def git_tracked_paths(repo_root: Path) -> tuple[list[str], str | None]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        text=True,
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        return [], f"git ls-files failed ({result.stderr.strip()})"
    return [normalize_git_output_path(line) for line in result.stdout.splitlines() if line.strip()], None


def git_path_is_ignored(repo_root: Path, relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "check-ignore", "--quiet", "--", relative_path],
        text=True,
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def parse_worktreeinclude(path: Path) -> list[str]:
    if not path.exists():
        return []
    patterns: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def normalize_worktreeinclude_pattern(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def pattern_matches_tracked(pattern: str, tracked_paths: list[str]) -> list[str]:
    normalized = normalize_worktreeinclude_pattern(pattern)
    if any(char in normalized for char in "*?["):
        return [path for path in tracked_paths if fnmatch.fnmatch(path, normalized)]
    return [path for path in tracked_paths if path == normalized or path.startswith(normalized.rstrip("/") + "/")]


def validate_worktreeinclude(project_root: Path) -> tuple[list[str], list[str]]:
    repo_root, repo_error = git_repo_root(project_root)
    if repo_error:
        return [], [f"cannot validate .worktreeinclude: {repo_error}"]
    assert repo_root is not None
    include_path = repo_root / ".worktreeinclude"
    patterns = parse_worktreeinclude(include_path)
    if not include_path.exists():
        return [], []
    tracked, tracked_error = git_tracked_paths(repo_root)
    if tracked_error:
        return patterns, [f"cannot validate .worktreeinclude: {tracked_error}"]
    errors: list[str] = []
    for pattern in patterns:
        normalized = normalize_worktreeinclude_pattern(pattern)
        if Path(pattern).is_absolute() or normalized.startswith("/") or normalized.startswith("../") or "/../" in normalized:
            errors.append(f".worktreeinclude entry escapes repository: {pattern}")
            continue
        if is_broad_parallel_scope(normalized) or normalized in {".git", ".git/", ".git/**"}:
            errors.append(f".worktreeinclude entry is too broad or unsafe: {pattern}")
            continue
        tracked_matches = pattern_matches_tracked(normalized, tracked)
        if tracked_matches:
            errors.append(
                f".worktreeinclude entry matches tracked files: {pattern} -> {', '.join(tracked_matches[:3])}"
            )
        if any(char in normalized for char in "*?["):
            continue
        local_path = repo_root / normalized
        if local_path.exists():
            if local_path.is_symlink():
                errors.append(f".worktreeinclude entry is a symlink: {pattern}")
            if not git_path_is_ignored(repo_root, normalized):
                errors.append(f".worktreeinclude entry is not ignored by Git: {pattern}")
    return patterns, errors


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


def command_worktree_plan(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    timestamp = format_time(parse_time(args.at))
    if args.project_root:
        _repo_root, repo_error = git_repo_root(Path(args.project_root).resolve())
        if repo_error:
            print(f"Worktree planning requires a Git repository: {repo_error}", file=sys.stderr)
            return 2
    event = {
        "at": timestamp,
        "event": "worktree-planned",
        "worktree_id": args.worktree_id,
        "provider": args.provider,
        "base_branch": args.base_branch,
        "purpose": args.purpose,
        "status": "planned",
        "local_mutation_policy": args.local_mutation_policy,
        "remote_mutation_policy": args.remote_mutation_policy,
        "copy_ignored_policy": args.copy_ignored_policy,
        "provider_worktree_ref": "",
        "worktree_path": "",
        "provider_confirmed": False,
    }
    append_worktree_event(state_dir, event)
    print(f"Planned worktree {args.worktree_id}")
    return 0


def command_worktree_confirm_create(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    plan = latest_worktree_event(state_dir, args.worktree_id)
    if not plan:
        print(f"No worktree plan found for {args.worktree_id}", file=sys.stderr)
        return 1
    timestamp = format_time(parse_time(args.at))
    provider = args.provider or str(plan.get("provider") or "codex-app")
    provider_ref = worktree_ref(
        provider,
        args.worktree_id,
        args.provider_worktree_ref or (codex_app_ref(args.thread_id) if args.thread_id else ""),
    )
    worktree_path = args.worktree_path or ""
    if provider == "local-git" and not worktree_path:
        print("local-git worktree confirmation requires --worktree-path", file=sys.stderr)
        return 2
    if worktree_path and not Path(worktree_path).exists():
        print(f"Confirmed worktree path does not exist: {worktree_path}", file=sys.stderr)
        return 2
    event = {
        "at": timestamp,
        "event": "worktree-created",
        "worktree_id": args.worktree_id,
        "provider": provider,
        "base_branch": args.base_branch or plan.get("base_branch", ""),
        "purpose": plan.get("purpose", ""),
        "status": "active",
        "provider_worktree_ref": provider_ref,
        "worktree_path": worktree_path,
        "thread_id": args.thread_id or "",
        "local_mutation_policy": plan.get("local_mutation_policy", ""),
        "remote_mutation_policy": plan.get("remote_mutation_policy", ""),
        "copy_ignored_policy": plan.get("copy_ignored_policy", ""),
        "provider_confirmed": True,
        "confirmation": args.note or "worktree create confirmed",
    }
    append_worktree_event(state_dir, event)
    print(f"Confirmed worktree {args.worktree_id}")
    return 0


def command_worktree_assign_session(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    worktree = require_created_worktree(state_dir, args.worktree_id)
    if not worktree:
        return 1
    session = latest_session_event(state_dir, args.agent_id)
    if not session:
        print(f"No active session found for {args.agent_id}", file=sys.stderr)
        return 1
    timestamp = format_time(parse_time(args.at))
    worktree_ref_value = str(worktree.get("provider_worktree_ref") or "")
    append_worktree_event(
        state_dir,
        {
            "at": timestamp,
            "event": "worktree-session-bound",
            "worktree_id": args.worktree_id,
            "provider": worktree.get("provider", ""),
            "base_branch": worktree.get("base_branch", ""),
            "provider_worktree_ref": worktree_ref_value,
            "worktree_path": worktree.get("worktree_path", ""),
            "agent_id": args.agent_id,
            "provider_session_id": session.get("provider_session_id", ""),
            "provider_session_ref": session.get("provider_session_ref", ""),
            "status": "active",
            "provider_confirmed": True,
            "confirmation": args.note or "session assigned to worktree",
        },
    )
    append_session_event(
        state_dir,
        {
            "at": timestamp,
            "event": "session-worktree-bound",
            "agent_id": args.agent_id,
            "role": session.get("role", ""),
            "provider": session.get("provider", ""),
            "provider_session_id": session.get("provider_session_id", ""),
            "provider_session_path": session.get("provider_session_path", ""),
            "provider_session_ref": session.get("provider_session_ref", ""),
            "worktree_id": args.worktree_id,
            "provider_worktree_ref": worktree_ref_value,
            "status": session.get("status", "active"),
            "provider_confirmed": True,
        },
    )
    print(f"Assigned session {args.agent_id} to worktree {args.worktree_id}")
    return 0


def command_worktree_reconcile(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    timestamp = format_time(parse_time(args.at))
    active: dict[str, dict] = {}
    for event in load_worktree_events(state_dir):
        worktree_id = str(event.get("worktree_id") or "")
        if not worktree_id:
            continue
        if event.get("event") == "worktree-created":
            active[worktree_id] = event
        elif event.get("event") == "worktree-session-bound" and worktree_id in active:
            active[worktree_id] = {**active[worktree_id], **event}
        elif worktree_event_is_terminal(event):
            active.pop(worktree_id, None)
    stale: list[str] = []
    for worktree_id, event in sorted(active.items()):
        provider = event.get("provider", "")
        if provider == "local-git":
            worktree_path = Path(str(event.get("worktree_path") or ""))
            if not worktree_path.exists():
                stale.append(worktree_id)
                append_worktree_event(
                    state_dir,
                    {
                        "at": timestamp,
                        "event": "worktree-stale",
                        "worktree_id": worktree_id,
                        "provider": provider,
                        "status": "stale",
                        "reason": "local-git worktree path is missing",
                    },
                )
            continue
        if provider == "codex-app":
            agent_id = str(event.get("agent_id") or "")
            if not agent_id:
                continue
            read_event = latest_confirmed_read_event(state_dir, agent_id)
            if not read_event:
                stale.append(worktree_id)
                append_worktree_event(
                    state_dir,
                    {
                        "at": timestamp,
                        "event": "worktree-stale",
                        "worktree_id": worktree_id,
                        "provider": provider,
                        "agent_id": agent_id,
                        "status": "stale",
                        "reason": "missing recent session-confirm-read evidence for bound Codex app session",
                    },
                )
                continue
            max_age = timedelta(minutes=args.codex_app_read_max_minutes)
            if parse_time(args.at) - parse_time(str(read_event.get("at") or timestamp)) > max_age:
                stale.append(worktree_id)
                append_worktree_event(
                    state_dir,
                    {
                        "at": timestamp,
                        "event": "worktree-stale",
                        "worktree_id": worktree_id,
                        "provider": provider,
                        "agent_id": agent_id,
                        "status": "stale",
                        "reason": "bound Codex app session read evidence is stale",
                    },
                )
    if stale:
        print("stale worktrees:")
        for worktree_id in stale:
            print(f"- {worktree_id}")
        return 1
    print("No stale worktrees")
    return 0


def command_worktree_close(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    worktree = require_created_worktree(state_dir, args.worktree_id)
    if not worktree:
        return 1
    timestamp = format_time(parse_time(args.at))
    append_worktree_event(
        state_dir,
        {
            "at": timestamp,
            "event": "worktree-close-requested",
            "worktree_id": args.worktree_id,
            "provider": worktree.get("provider", ""),
            "provider_worktree_ref": worktree.get("provider_worktree_ref", ""),
            "worktree_path": worktree.get("worktree_path", ""),
            "status": "pending-close-confirmation",
            "reason": args.reason,
            "provider_confirmed": False,
        },
    )
    print(f"Requested worktree close for {args.worktree_id}")
    return 0


def command_worktree_confirm_close(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    worktree = latest_created_worktree_event(state_dir, args.worktree_id)
    if not worktree:
        print(f"No active worktree found for {args.worktree_id}", file=sys.stderr)
        return 1
    timestamp = format_time(parse_time(args.at))
    append_worktree_event(
        state_dir,
        {
            "at": timestamp,
            "event": "worktree-closed",
            "worktree_id": args.worktree_id,
            "provider": worktree.get("provider", ""),
            "provider_worktree_ref": worktree.get("provider_worktree_ref", ""),
            "worktree_path": worktree.get("worktree_path", ""),
            "status": "closed",
            "provider_confirmed": True,
            "confirmation": args.note or "worktree close confirmed",
        },
    )
    print(f"Confirmed worktree closed: {args.worktree_id}")
    return 0


def command_validate_worktreeinclude(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    project_root = Path(args.project_root).resolve()
    patterns, errors = validate_worktreeinclude(project_root)
    timestamp = format_time(parse_time(args.at))
    append_worktree_event(
        state_dir,
        {
            "at": timestamp,
            "event": "worktreeinclude-validated",
            "project_root": str(project_root),
            "patterns": patterns,
            "status": "failed" if errors else "passed",
            "errors": errors,
        },
    )
    if errors:
        print(".worktreeinclude validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    if patterns:
        print(".worktreeinclude validation passed")
        for pattern in patterns:
            print(f"- {pattern}")
    else:
        print("No .worktreeinclude entries; no ignored local files are planned for copy")
    return 0


def command_session_create(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    role_name, _definition = require_active_role(state_dir, args.role)
    worktree = None
    worktree_id = getattr(args, "worktree_id", "") or ""
    if worktree_id:
        worktree = require_worktree_plan_or_active(state_dir, worktree_id)
        if not worktree:
            return 1
    if role_requires_validated_strategy(role_name):
        agents = load_agents(state_dir)
        agent = agents.get(args.agent_id)
        if not agent:
            print(
                f"Session launch for {role_name} requires registered agent {args.agent_id}",
                file=sys.stderr,
            )
            return 1
        if agent.get("role") != role_name:
            print(
                f"Session launch role mismatch for {args.agent_id}: registered {agent.get('role')}, requested {role_name}",
                file=sys.stderr,
            )
            return 1
        strategy_errors = require_validated_strategy_for_work(
            state_dir,
            role_name,
            agent.get("plan_id", ""),
        )
        if strategy_errors:
            for error in strategy_errors:
                print(error, file=sys.stderr)
            return 1
    context_packet = Path(args.context_packet).resolve()
    if not context_packet.exists():
        print(f"Context packet does not exist: {context_packet}", file=sys.stderr)
        return 2
    timestamp = format_time(parse_time(args.at))
    session_dir = state_dir / "state" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    provider_session_path = session_dir / f"{args.agent_id}.json"
    provider_session_id = f"{args.provider}:{args.agent_id}"
    provider_session_ref = str(provider_session_path)
    provider_confirmed = False
    initial_status = "active" if args.provider == "file" else "pending-manual-provider"
    if args.provider == "codex-app":
        initial_status = "pending-codex-app-confirmation"
        provider_session_id = ""
        provider_session_ref = ""
    session = {
        "provider_session_id": provider_session_id,
        "agent_id": args.agent_id,
        "role": role_name,
        "status": initial_status,
        "context_packet": str(context_packet),
        "predecessor_agent_id": args.predecessor_agent_id or "",
        "inheritance_reason": args.reason or "",
        "worktree_id": worktree_id,
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
            "worktree_id": worktree_id,
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
        provider_session_ref = str(provider_session_path)
        provider_confirmed = True
    elif args.provider == "codex-app":
        provider_session_path = Path("")
        print(
            "Codex app session create requested. Use create_thread, then run "
            "`session-confirm-create` with the returned thread id."
        )
    event = {
        "at": timestamp,
        "event": "session-create-requested" if args.provider == "codex-app" else "session-created",
        "agent_id": args.agent_id,
        "role": role_name,
        "provider": args.provider,
        "provider_session_id": provider_session_id,
        "provider_session_path": "" if args.provider == "codex-app" else str(provider_session_path),
        "provider_session_ref": provider_session_ref,
        "context_packet": str(context_packet),
        "predecessor_agent_id": args.predecessor_agent_id or "",
        "inheritance_reason": args.reason or "",
        "worktree_id": worktree_id,
        "provider_worktree_ref": (worktree or {}).get("provider_worktree_ref", ""),
        "status": session["status"],
        "provider_confirmed": provider_confirmed,
    }
    append_session_event(state_dir, event)
    if args.provider == "codex-app":
        print(f"Requested Codex app session for {args.agent_id}")
    else:
        print(f"Created session {provider_session_id}")
    return 0


def command_session_confirm_create(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    request = latest_session_event_of_type(
        state_dir,
        args.agent_id,
        {"session-create-requested"},
    )
    if not request:
        print(f"No Codex app create request found for {args.agent_id}", file=sys.stderr)
        return 1
    if request.get("provider") != "codex-app":
        print(f"Latest create request for {args.agent_id} is not codex-app", file=sys.stderr)
        return 2
    timestamp = format_time(parse_time(args.at))
    worktree = None
    worktree_id = args.worktree_id or request.get("worktree_id", "")
    if worktree_id:
        worktree = require_worktree_plan_or_active(state_dir, str(worktree_id))
        if not worktree:
            return 1
    event = {
        "at": timestamp,
        "event": "session-created",
        "agent_id": args.agent_id,
        "role": request.get("role", ""),
        "provider": "codex-app",
        "provider_session_id": args.thread_id,
        "provider_session_path": "",
        "provider_session_ref": codex_app_ref(args.thread_id),
        "context_packet": request.get("context_packet", ""),
        "predecessor_agent_id": request.get("predecessor_agent_id", ""),
        "inheritance_reason": request.get("inheritance_reason", ""),
        "worktree_id": worktree_id,
        "provider_worktree_ref": (worktree or {}).get("provider_worktree_ref", ""),
        "status": "active",
        "provider_confirmed": True,
        "confirmation": args.note or "Codex app thread created",
    }
    append_session_event(state_dir, event)
    print(f"Confirmed Codex app session {args.thread_id} for {args.agent_id}")
    return 0


def require_codex_app_session(state_dir: Path, agent_id: str) -> dict | None:
    event = latest_session_event(state_dir, agent_id)
    if not event:
        print(f"No active session found for {agent_id}", file=sys.stderr)
        return None
    if event.get("provider") != "codex-app":
        print(f"Session for {agent_id} is not a Codex app session", file=sys.stderr)
        return None
    if not event.get("provider_confirmed"):
        print(f"Codex app session for {agent_id} is not confirmed", file=sys.stderr)
        return None
    return event


def command_session_confirm_send(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    event = require_codex_app_session(state_dir, args.agent_id)
    if not event:
        return 1
    request = latest_session_event_of_type(
        state_dir,
        args.agent_id,
        {"session-send-requested"},
    )
    message = args.message or (request.get("message", "") if request else "")
    if not message:
        print("session-confirm-send requires --message when no send request exists", file=sys.stderr)
        return 2
    timestamp = format_time(parse_time(args.at))
    append_session_event(
        state_dir,
        {
            "at": timestamp,
            "event": "session-sent",
            "agent_id": args.agent_id,
            "role": event.get("role", ""),
            "provider": "codex-app",
            "provider_session_id": args.thread_id or event.get("provider_session_id", ""),
            "provider_session_path": "",
            "provider_session_ref": codex_app_ref(args.thread_id or event.get("provider_session_id", "")),
            "message": message,
            "provider_confirmed": True,
            "confirmation": args.note or "Codex app send_message_to_thread confirmed",
        },
    )
    print(f"Confirmed Codex app send for {args.agent_id}")
    return 0


def command_session_confirm_read(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    event = require_codex_app_session(state_dir, args.agent_id)
    if not event:
        return 1
    timestamp = format_time(parse_time(args.at))
    append_session_event(
        state_dir,
        {
            "at": timestamp,
            "event": "session-read",
            "agent_id": args.agent_id,
            "role": event.get("role", ""),
            "provider": "codex-app",
            "provider_session_id": args.thread_id or event.get("provider_session_id", ""),
            "provider_session_path": "",
            "provider_session_ref": codex_app_ref(args.thread_id or event.get("provider_session_id", "")),
            "message_count": args.turn_count,
            "summary": args.summary,
            "provider_confirmed": True,
            "confirmation": args.note or "Codex app read_thread confirmed",
        },
    )
    print(f"Confirmed Codex app read for {args.agent_id}")
    return 0


def command_session_confirm_archive(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    event = require_codex_app_session(state_dir, args.agent_id)
    if not event:
        return 1
    timestamp = format_time(parse_time(args.at))
    append_session_event(
        state_dir,
        {
            "at": timestamp,
            "event": "session-archived",
            "agent_id": args.agent_id,
            "role": event.get("role", ""),
            "provider": "codex-app",
            "provider_session_id": args.thread_id or event.get("provider_session_id", ""),
            "provider_session_path": "",
            "provider_session_ref": codex_app_ref(args.thread_id or event.get("provider_session_id", "")),
            "status": "archived",
            "provider_confirmed": True,
            "confirmation": args.note or "Codex app archive confirmed",
        },
    )
    print(f"Confirmed Codex app archive for {args.agent_id}")
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
    provider_session_path = Path(event.get("provider_session_path") or "")
    timestamp = format_time(parse_time(args.at))
    if event.get("provider") == "codex-app":
        append_session_event(
            state_dir,
            {
                "at": timestamp,
                "event": "session-send-requested",
                "agent_id": args.agent_id,
                "role": event.get("role", ""),
                "provider": "codex-app",
                "provider_session_id": event.get("provider_session_id", ""),
                "provider_session_path": "",
                "provider_session_ref": event.get("provider_session_ref", ""),
                "message": args.message,
                "provider_confirmed": False,
            },
        )
        print(
            "Codex app send requested. Use send_message_to_thread, then run "
            "`session-confirm-send`."
        )
        return 0
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
            "provider_session_ref": event.get("provider_session_ref", str(provider_session_path)),
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
    provider_session_path = Path(event.get("provider_session_path") or "")
    timestamp = format_time(parse_time(args.at))
    if event.get("provider") == "codex-app":
        append_session_event(
            state_dir,
            {
                "at": timestamp,
                "event": "session-read-requested",
                "agent_id": args.agent_id,
                "role": event.get("role", ""),
                "provider": "codex-app",
                "provider_session_id": event.get("provider_session_id", ""),
                "provider_session_path": "",
                "provider_session_ref": event.get("provider_session_ref", ""),
                "provider_confirmed": False,
            },
        )
        print(
            "Codex app read requested. Use read_thread, then run "
            "`session-confirm-read` with a compact summary."
        )
        return 0
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
            "provider_session_ref": event.get("provider_session_ref", str(provider_session_path)),
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
    provider_session_path = Path(event.get("provider_session_path") or "")
    provider_confirmed = False
    if event.get("provider") == "codex-app":
        append_session_event(
            state_dir,
            {
                "at": format_time(parse_time(args.at)),
                "event": "session-archive-requested",
                "agent_id": args.agent_id,
                "role": event.get("role", ""),
                "provider": "codex-app",
                "provider_session_id": event.get("provider_session_id", ""),
                "provider_session_path": "",
                "provider_session_ref": event.get("provider_session_ref", ""),
                "status": "pending-archive-confirmation",
                "provider_confirmed": False,
            },
        )
        print(
            "Codex app archive requested. Use set_thread_archived, then run "
            "`session-confirm-archive`."
        )
        return 0
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
            "provider_session_ref": event.get("provider_session_ref", str(provider_session_path)),
            "status": "archived",
            "provider_confirmed": provider_confirmed,
        },
    )
    print(f"Archived session {args.agent_id}")
    return 0


def command_validate_predecessor_state(args: argparse.Namespace) -> int:
    packet = Path(args.packet).resolve()
    errors = validate_predecessor_state_packet(packet)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"Predecessor state packet is valid: {packet}")
    return 0


def command_request_rotation(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    agents = load_agents(state_dir)
    if args.agent_id not in agents:
        print(f"Unknown agent id: {args.agent_id}", file=sys.stderr)
        return 1
    save_request, _successor_context = write_session_rotation_packets(
        state_dir=state_dir,
        agent_id=args.agent_id,
        successor_agent_id=args.successor_agent_id,
        reason=args.reason,
        predecessor_state_packet=None,
    )
    predecessor_event = latest_session_event(state_dir, args.agent_id)
    if predecessor_event:
        send_code = command_session_send(
            argparse.Namespace(
                state_dir=str(state_dir),
                agent_id=args.agent_id,
                message=(
                    "Save current state for strict session rotation, stop current task work, "
                    f"and return a predecessor-state-packet using this request: {save_request}"
                ),
                provider_command=args.provider_command,
                provider_timeout_seconds=args.provider_timeout_seconds,
                at=args.at,
            )
        )
        if send_code:
            return send_code
    append_event_log(
        state_dir=state_dir,
        event_type="rotation-request",
        related_packet=str(save_request),
        summary=f"requested strict rotation state from {args.agent_id}",
        evidence=str(save_request),
        ledger_update="rotation request issued; successor not launched yet",
        next_action="wait for validated predecessor-state-packet",
        at=format_time(parse_time(args.at)),
    )
    print(f"Rotation state requested for {args.agent_id}")
    print(f"Save-state request: {save_request}")
    return 0


def command_rotate_session(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    agents = load_agents(state_dir)
    if args.agent_id not in agents:
        print(f"Unknown agent id: {args.agent_id}", file=sys.stderr)
        return 1
    if args.successor_agent_id in agents:
        print(
            f"Successor agent already exists: {args.successor_agent_id}",
            file=sys.stderr,
        )
        return 2

    predecessor = agents[args.agent_id]
    predecessor_state_packet: Path | None = None
    if args.predecessor_state_packet:
        predecessor_state_packet = Path(args.predecessor_state_packet).resolve()
        errors = validate_predecessor_state_packet(predecessor_state_packet)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 2
    elif not args.emergency_without_predecessor_state:
        print(
            "Strict rotation requires --predecessor-state-packet. "
            "Use request-rotation first, or pass --emergency-without-predecessor-state "
            "to record degraded continuity.",
            file=sys.stderr,
        )
        return 2

    predecessor_event = latest_session_event(state_dir, args.agent_id)
    provider = args.provider or (
        str(predecessor_event.get("provider") or "file") if predecessor_event else "file"
    )
    if provider == "codex" and not required_provider_command(args, provider):
        return 2

    safety_code, safety_status, safety_reasons = assess_safety(
        state_dir=state_dir,
        action="spawn-successor",
        role=predecessor.get("role", ""),
        scope=predecessor.get("scope", ""),
        budget_impact=args.budget_impact,
    )
    if safety_code == 2:
        print("Safety blocked session rotation")
        for reason in safety_reasons:
            print(f"- {reason}")
        return 2

    save_request, successor_context = write_session_rotation_packets(
        state_dir=state_dir,
        agent_id=args.agent_id,
        successor_agent_id=args.successor_agent_id,
        reason=args.reason,
        predecessor_state_packet=predecessor_state_packet,
    )

    if predecessor_event:
        send_code = command_session_send(
            argparse.Namespace(
                state_dir=str(state_dir),
                agent_id=args.agent_id,
                message=(
                    "Save current state for session rotation, stop current implementation, "
                    f"and follow this packet: {save_request}"
                ),
                provider_command=args.provider_command,
                provider_timeout_seconds=args.provider_timeout_seconds,
                at=args.at,
            )
        )
        if send_code:
            return send_code
        archive_code = command_session_archive(
            argparse.Namespace(
                state_dir=str(state_dir),
                agent_id=args.agent_id,
                provider_command=args.provider_command,
                provider_timeout_seconds=args.provider_timeout_seconds,
                at=args.at,
            )
        )
        if archive_code:
            return archive_code

    successor_role = args.successor_role or predecessor.get("role", "")
    successor_task_id = args.successor_task_id or predecessor.get("task_id", "")
    successor_objective = args.successor_objective or predecessor.get("objective", "")
    successor_scope = args.successor_scope or predecessor.get("scope", "")
    token_budget = (
        args.token_budget
        if args.token_budget is not None
        else optional_int(predecessor.get("token_budget"))
    )
    max_heartbeats = (
        args.max_heartbeats
        if args.max_heartbeats is not None
        else optional_int(predecessor.get("max_heartbeats"))
    )
    register_code = command_register_agent(
        argparse.Namespace(
            state_dir=str(state_dir),
            agent_id=args.successor_agent_id,
            role=successor_role,
            task_id=successor_task_id,
            objective=successor_objective,
            scope=successor_scope,
            status="starting",
            token_budget=token_budget,
            max_heartbeats=max_heartbeats,
            plan_id=predecessor.get("plan_id", ""),
            at=args.at,
        )
    )
    if register_code:
        return register_code

    agents = load_agents(state_dir)
    agents[args.agent_id]["status"] = "stopping"
    agents[args.agent_id]["stop_reason"] = f"rotated: {args.reason}"
    agents[args.agent_id]["successor_agent_id"] = args.successor_agent_id
    save_agents(state_dir, agents)
    render_running_agents(state_dir, agents)

    create_code = command_session_create(
        argparse.Namespace(
            state_dir=str(state_dir),
            agent_id=args.successor_agent_id,
            role=successor_role,
            context_packet=str(successor_context),
            provider=provider,
            provider_command=args.provider_command,
            provider_timeout_seconds=args.provider_timeout_seconds,
            predecessor_agent_id=args.agent_id,
            reason=args.reason,
            at=args.at,
        )
    )
    if create_code:
        return create_code

    append_event_log(
        state_dir=state_dir,
        event_type="session-rotation",
        related_packet=str(successor_context),
        summary=f"rotated {args.agent_id} to {args.successor_agent_id}: {args.reason}",
        evidence=f"{save_request}; {successor_context}",
        ledger_update=(
            "predecessor stopped and successor session requested"
            if provider == "codex-app"
            else "predecessor stopped and successor session created"
        ),
        next_action=f"monitor {args.successor_agent_id} first heartbeat",
        at=format_time(parse_time(args.at)),
    )
    print(f"Rotated session {args.agent_id} -> {args.successor_agent_id}")
    print(f"Save-state request: {save_request}")
    print(f"Successor context: {successor_context}")
    if safety_status != "allowed":
        print(f"Safety status: {safety_status}")
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
        if event.get("provider") == "codex-app":
            read_event = latest_confirmed_read_event(state_dir, agent_id)
            if not read_event:
                stale.append(agent_id)
                append_session_event(
                    state_dir,
                    {
                        "at": timestamp,
                        "event": "session-stale",
                        "agent_id": agent_id,
                        "provider": "codex-app",
                        "provider_session_id": event.get("provider_session_id", ""),
                        "provider_session_path": "",
                        "provider_session_ref": event.get("provider_session_ref", ""),
                        "status": "stale",
                        "reason": "missing recent session-confirm-read evidence",
                    },
                )
                continue
            read_at = parse_time(str(read_event.get("at") or timestamp))
            max_age = timedelta(minutes=args.codex_app_read_max_minutes)
            if parse_time(args.at) - read_at > max_age:
                stale.append(agent_id)
                append_session_event(
                    state_dir,
                    {
                        "at": timestamp,
                        "event": "session-stale",
                        "agent_id": agent_id,
                        "provider": "codex-app",
                        "provider_session_id": event.get("provider_session_id", ""),
                        "provider_session_path": "",
                        "provider_session_ref": event.get("provider_session_ref", ""),
                        "status": "stale",
                        "reason": "session-confirm-read evidence is stale",
                    },
                )
                continue
            continue
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
                    "provider_session_ref": event.get("provider_session_ref", str(provider_path)),
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


def markdown_list_values(section_text: str) -> list[str]:
    values: list[str] = []
    for line in section_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        value = stripped[1:].strip().strip("`").strip()
        if value:
            values.append(value)
    return values


def load_master_boundary_patterns(state_dir: Path) -> tuple[list[str], list[str]]:
    boundary_path = state_dir / "master-boundary.md"
    if not boundary_path.exists():
        return [], [f"missing master boundary file: {boundary_path}"]
    sections = parse_markdown_sections(boundary_path.read_text(encoding="utf-8"))
    allowed = markdown_list_values(sections.get("## Allowed Master Write Paths", ""))
    errors: list[str] = []
    if not allowed:
        errors.append("master-boundary.md has no allowed Master write paths")
    return allowed, errors


def normalize_repo_path(value: str) -> str:
    return value.replace("\\", "/").strip().lstrip("./")


def path_matches_pattern(path: str, pattern: str) -> bool:
    path = normalize_repo_path(path)
    pattern = normalize_repo_path(pattern)
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatch(path, pattern)


def git_changed_paths(project_root: Path) -> tuple[list[str], str | None]:
    try:
        root_result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"cannot enforce boundary: git is unavailable ({exc})"
    if root_result.returncode != 0:
        return [], "cannot enforce boundary: project root is not inside a Git repository"
    git_root = Path(root_result.stdout.strip()).resolve()
    status = subprocess.run(
        ["git", "-C", str(git_root), "status", "--porcelain=v1", "--untracked-files=all"],
        text=True,
        capture_output=True,
        timeout=10,
    )
    if status.returncode != 0:
        return [], f"cannot enforce boundary: git status failed ({status.stderr.strip()})"
    paths: list[str] = []
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        paths.append(normalize_repo_path(path.strip('"')))
    return paths, None


def command_enforce_master_boundary(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    allowed, boundary_errors = load_master_boundary_patterns(state_dir)
    changed, git_error = git_changed_paths(project_root)
    if git_error:
        append_incident(
            state_dir=state_dir,
            severity="critical",
            summary=git_error,
            source="enforce-master-boundary",
            at=format_time(parse_time(args.at)),
        )
        print(git_error, file=sys.stderr)
        return 2
    if boundary_errors:
        for error in boundary_errors:
            print(error, file=sys.stderr)
        return 2
    blocked = [
        path
        for path in changed
        if not any(path_matches_pattern(path, pattern) for pattern in allowed)
    ]
    if blocked:
        append_incident(
            state_dir=state_dir,
            severity="critical",
            summary="Master boundary violation: " + ", ".join(blocked[:5]),
            source="enforce-master-boundary",
            at=format_time(parse_time(args.at)),
        )
        print("Master boundary violation:")
        for path in blocked:
            print(f"- {path}")
        return 1
    print("Master boundary clean")
    return 0


def extract_labeled_value(section_text: str, label: str) -> str:
    pattern = re.compile(rf"^\s*-\s*{re.escape(label)}\s*:\s*(.*)$", re.IGNORECASE)
    for line in section_text.splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return ""


def split_parallel_values(value: str) -> list[str]:
    parts = re.split(r"[,;\n]+", value)
    return [normalize_repo_path(part.strip()) for part in parts if part.strip()]


def is_broad_parallel_scope(value: str) -> bool:
    normalized = normalize_repo_path(value).lower()
    return normalized in {"*", "**", ".", "/", "all", "repo", "repository", "entire repo"} or "**" in normalized


def paths_overlap(left: str, right: str) -> bool:
    left = normalize_repo_path(left)
    right = normalize_repo_path(right)
    if left == right:
        return True
    return left.startswith(right.rstrip("/") + "/") or right.startswith(left.rstrip("/") + "/")


def assess_work_orders_for_parallelism(work_orders: list[Path]) -> tuple[str, list[str]]:
    problems: list[str] = []
    serial_reasons: list[str] = []
    parsed: list[dict] = []
    for path in work_orders:
        if not path.exists():
            problems.append(f"{path}: missing work order")
            continue
        sections = parse_markdown_sections(path.read_text(encoding="utf-8"))
        parallel = sections.get("## Parallel Safety", "")
        token = sections.get("## Token Budget", "")
        validation = sections.get("## Required Validation", "")
        write_set = split_parallel_values(extract_labeled_value(parallel, "Exclusive Write Set"))
        artifact_namespace = split_parallel_values(extract_labeled_value(parallel, "Artifact Namespace"))
        worktree_mode = extract_labeled_value(parallel, "Worktree Mode")
        worktree_id = extract_labeled_value(parallel, "Worktree Id")
        base_branch = extract_labeled_value(parallel, "Base Branch")
        local_mutation_policy = extract_labeled_value(parallel, "Local Mutation Policy")
        remote_mutation_policy = extract_labeled_value(parallel, "Remote Mutation Policy")
        merge_owner = extract_labeled_value(parallel, "Merge Owner")
        conflict_protocol = extract_labeled_value(parallel, "Conflict Protocol")
        token_budget = extract_labeled_value(token, "Token budget")
        max_heartbeats = extract_labeled_value(token, "Maximum heartbeats")
        missing = []
        if not write_set:
            missing.append("Exclusive Write Set")
        if not artifact_namespace:
            missing.append("Artifact Namespace")
        if not worktree_mode:
            missing.append("Worktree Mode")
        if not worktree_id:
            missing.append("Worktree Id")
        if not base_branch:
            missing.append("Base Branch")
        if not local_mutation_policy:
            missing.append("Local Mutation Policy")
        if not remote_mutation_policy:
            missing.append("Remote Mutation Policy")
        if not merge_owner:
            missing.append("Merge Owner")
        if not conflict_protocol:
            missing.append("Conflict Protocol")
        if not token_budget:
            missing.append("Token budget")
        if not max_heartbeats:
            missing.append("Maximum heartbeats")
        if not section_has_content(validation):
            missing.append("Required Validation")
        if missing:
            problems.append(f"{path}: missing {', '.join(missing)}")
            continue
        for item in write_set + artifact_namespace:
            if is_broad_parallel_scope(item):
                serial_reasons.append(f"{path}: broad parallel scope {item}")
        if worktree_mode not in {"codex-app", "local-git", "provider-command"}:
            problems.append(f"{path}: unsupported Worktree Mode {worktree_mode!r}")
        if is_broad_parallel_scope(worktree_id):
            serial_reasons.append(f"{path}: broad Worktree Id {worktree_id}")
        if "local" not in local_mutation_policy.lower() or "not" not in local_mutation_policy.lower():
            serial_reasons.append(f"{path}: Local Mutation Policy does not protect the local checkout")
        remote_policy_lower = remote_mutation_policy.lower()
        if not (
            ("not" in remote_policy_lower and ("push" in remote_policy_lower or "pr" in remote_policy_lower))
            or "release gate" in remote_policy_lower
        ):
            serial_reasons.append(f"{path}: Remote Mutation Policy does not require a release gate")
        if re.search(r"\b(depends on|after other agent|same output|shared output)\b", parallel, re.IGNORECASE):
            serial_reasons.append(f"{path}: dependent parallel safety language")
        parsed.append(
            {
                "path": path,
                "write_set": write_set,
                "artifact_namespace": artifact_namespace,
                "worktree_id": normalize_repo_path(worktree_id),
                "base_branch": base_branch,
            }
        )
    for index, left in enumerate(parsed):
        for right in parsed[index + 1 :]:
            for left_path in left["write_set"]:
                for right_path in right["write_set"]:
                    if paths_overlap(left_path, right_path):
                        serial_reasons.append(
                            f"{left['path']} and {right['path']}: overlapping write set {left_path} / {right_path}"
                        )
            for left_path in left["artifact_namespace"]:
                for right_path in right["artifact_namespace"]:
                    if paths_overlap(left_path, right_path):
                        serial_reasons.append(
                            f"{left['path']} and {right['path']}: overlapping artifact namespace {left_path} / {right_path}"
                        )
            if left["worktree_id"] == right["worktree_id"]:
                serial_reasons.append(
                    f"{left['path']} and {right['path']}: shared Worktree Id {left['worktree_id']}"
                )
    if problems:
        return "invalid-work-order", problems + serial_reasons
    if serial_reasons:
        return "serial-required", serial_reasons
    return "allow", ["work orders have disjoint write sets and artifact namespaces"]


def command_assess_parallelism(args: argparse.Namespace) -> int:
    verdict, reasons = assess_work_orders_for_parallelism(
        [Path(path).resolve() for path in args.work_order]
    )
    lines = [
        "# Parallelism Verdict",
        "",
        f"- Verdict: {verdict}",
        "",
        "## Reasons",
        "",
    ]
    lines.extend(f"- {reason}" for reason in reasons)
    text = "\n".join(lines) + "\n"
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output, text)
    print(text, end="")
    if verdict == "allow":
        return 0
    if verdict == "serial-required":
        return 1
    return 2


def append_round_log_event(state_dir: Path, entry: dict) -> None:
    append_jsonl_locked(state_dir / "state" / "round-log-events.jsonl", entry)


def round_log_root(repo_root: Path) -> Path:
    return repo_root / ".codex-round-log"


def read_json_object(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def round_log_manifest_record(snapshot_dir: Path) -> dict | None:
    manifest_path = snapshot_dir / "manifest.json"
    manifest = read_json_object(manifest_path)
    if not manifest:
        return None
    snapshot_id = str(manifest.get("id") or snapshot_dir.name)
    copied_paths = [
        normalize_repo_path(str(path))
        for path in manifest.get("copied_paths", [])
        if str(path).strip()
    ]
    deleted_paths = [
        normalize_repo_path(str(path))
        for path in manifest.get("deleted_paths", [])
        if str(path).strip()
    ]
    file_index_path = str(manifest.get("file_index_path") or "")
    return {
        "snapshot_id": snapshot_id,
        "snapshot_path": str(snapshot_dir),
        "manifest_path": str(manifest_path),
        "created_at": str(manifest.get("created_at") or ""),
        "mode": str(manifest.get("mode") or ""),
        "label": str(manifest.get("label") or ""),
        "branch": str(manifest.get("branch") or ""),
        "previous_snapshot_id": str(manifest.get("previous_snapshot_id") or ""),
        "copied_paths": copied_paths,
        "deleted_paths": deleted_paths,
        "changed_paths": copied_paths + deleted_paths,
        "file_count": len(copied_paths) + len(deleted_paths),
        "file_index_path": file_index_path,
    }


def list_round_log_snapshots(repo_root: Path) -> list[dict]:
    log_root = round_log_root(repo_root)
    if not log_root.exists():
        return []
    snapshots: list[dict] = []
    for path in log_root.iterdir():
        if not path.is_dir() or path.name in {".tmp", "exports"}:
            continue
        record = round_log_manifest_record(path)
        if record:
            snapshots.append(record)
    snapshots.sort(key=lambda item: (item.get("created_at") or "", item.get("snapshot_id") or ""))
    return snapshots


def latest_round_log_snapshot(repo_root: Path) -> dict | None:
    snapshots = list_round_log_snapshots(repo_root)
    return snapshots[-1] if snapshots else None


def inspect_round_log(repo_root: Path) -> dict:
    log_root = round_log_root(repo_root)
    if not log_root.exists():
        return {
            "status": "missing",
            "repo_root": str(repo_root),
            "round_log_root": str(log_root),
            "latest_snapshot_id": "",
            "snapshot_count": 0,
            "hook_failure": "",
        }
    snapshots = list_round_log_snapshots(repo_root)
    state = read_json_object(log_root / "state.json") or {}
    failure = read_json_object(log_root / "hook-failure.json")
    latest = snapshots[-1] if snapshots else None
    last_state_snapshot = str(state.get("last_snapshot_id") or "")
    latest_id = str((latest or {}).get("snapshot_id") or last_state_snapshot)
    if failure:
        status = "warning"
    elif latest:
        status = "available"
    else:
        status = "empty"
    return {
        "status": status,
        "repo_root": str(repo_root),
        "round_log_root": str(log_root),
        "latest_snapshot_id": latest_id,
        "snapshot_count": len(snapshots),
        "state_last_snapshot_id": last_state_snapshot,
        "active_hook_snapshot_id": str(state.get("active_hook_snapshot_id") or ""),
        "hook_failure": json.dumps(failure, ensure_ascii=False, sort_keys=True) if failure else "",
        "latest_snapshot": latest or {},
    }


def run_round_log_command(
    command: str,
    extra_args: list[str],
    timeout_seconds: float,
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        return None, f"Round-log command could not be parsed: {exc}"
    if not argv:
        return None, "Round-log command is empty"
    try:
        result = subprocess.run(
            [*argv, *extra_args],
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return None, f"Round-log command timed out after {timeout_seconds:g}s"
    except OSError as exc:
        return None, f"Round-log command failed to start: {exc}"
    return result, None


def render_round_log_control(state_dir: Path) -> None:
    events = load_jsonl_entries(state_dir / "state" / "round-log-events.jsonl")
    latest_status = next(
        (event for event in reversed(events) if event.get("event") == "round-log-status"),
        {},
    )
    latest_evidence = next(
        (event for event in reversed(events) if event.get("event") == "round-log-evidence"),
        {},
    )
    latest_export = next(
        (event for event in reversed(events) if event.get("event") == "round-log-export"),
        {},
    )
    lines = [
        "# Round Log Control",
        "",
        "## Purpose",
        "",
        "- Optional local history evidence for Codex implementation rounds.",
        "- Complements Git status; it does not replace Git boundary enforcement.",
        "",
        "## Provider",
        "",
        f"- Status: {latest_status.get('status', '')}",
        f"- Repo root: {latest_status.get('repo_root', '')}",
        f"- Round log root: {latest_status.get('round_log_root', '')}",
        f"- Latest snapshot id: {latest_status.get('latest_snapshot_id', '')}",
        f"- Snapshot count: {latest_status.get('snapshot_count', '')}",
        "",
        "## Evidence Binding",
        "",
        f"- Latest agent id: {latest_evidence.get('agent_id', '')}",
        f"- Latest plan id: {latest_evidence.get('plan_id', '')}",
        f"- Latest worktree id: {latest_evidence.get('worktree_id', '')}",
        f"- Latest evidence snapshot id: {latest_evidence.get('snapshot_id', '')}",
        f"- Latest manifest path: {latest_evidence.get('manifest_path', '')}",
        "",
        "## Snapshot Policy",
        "",
        "- Master may inspect status, record evidence, require evidence, and export readable evidence explicitly.",
        "- Master must not use round-log evidence as mutation authority.",
        "- Restore is a human-directed recovery operation, not an autonomous Master action.",
        "",
        "## Export Policy",
        "",
        f"- Latest export snapshot id: {latest_export.get('snapshot_id', '')}",
        f"- Latest export path: {latest_export.get('export_path', '')}",
        "- Export creates review artifacts and must not mutate source snapshots.",
        "",
        "## Restore Policy",
        "",
        "- Run restore only after an explicit user decision.",
        "- Prefer restore dry-run and current-state safety snapshot before any real restore.",
        "",
    ]
    atomic_write_text(state_dir / "round-log-control.md", "\n".join(lines) + "\n")


def command_round_log_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    repo_root, repo_error = git_repo_root(Path(args.project_root).resolve())
    timestamp = format_time(parse_time(args.at))
    if repo_error:
        print(f"Round-log status requires a Git repository: {repo_error}", file=sys.stderr)
        return 2
    assert repo_root is not None
    status = inspect_round_log(repo_root)
    command_stdout = ""
    command_stderr = ""
    command_returncode: int | None = None
    if args.round_log_command:
        result, command_error = run_round_log_command(
            args.round_log_command,
            ["list", "--repo", str(repo_root)],
            args.timeout_seconds,
        )
        if command_error:
            print(command_error, file=sys.stderr)
            return 2
        assert result is not None
        command_returncode = result.returncode
        command_stdout = result.stdout.strip()
        command_stderr = result.stderr.strip()
        if result.returncode != 0:
            status["status"] = "command-failed"
    event = {
        "at": timestamp,
        "event": "round-log-status",
        **status,
        "command_returncode": command_returncode,
        "command_stdout": command_stdout[-2000:],
        "command_stderr": command_stderr[-2000:],
    }
    append_round_log_event(state_dir, event)
    render_round_log_control(state_dir)
    print(f"Round log status: {status.get('status')}")
    print(f"Repo root: {status.get('repo_root')}")
    print(f"Round log root: {status.get('round_log_root')}")
    print(f"Latest snapshot id: {status.get('latest_snapshot_id')}")
    print(f"Snapshot count: {status.get('snapshot_count')}")
    if command_stdout:
        print("Command output:")
        print(command_stdout)
    if command_stderr:
        print("Command stderr:")
        print(command_stderr)
    if status.get("status") == "command-failed":
        return 2
    if args.require_active and status.get("status") not in {"available", "warning"}:
        return 1
    return 0


def command_record_round_log_evidence(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    agents = load_agents(state_dir)
    if args.agent_id not in agents:
        print(f"Unknown agent id: {args.agent_id}", file=sys.stderr)
        return 1
    repo_root, repo_error = git_repo_root(Path(args.project_root).resolve())
    if repo_error:
        print(f"Round-log evidence requires a Git repository: {repo_error}", file=sys.stderr)
        return 2
    assert repo_root is not None
    snapshot_dir = round_log_root(repo_root) / args.snapshot_id
    snapshot = round_log_manifest_record(snapshot_dir)
    if not snapshot:
        print(f"Valid round-log snapshot not found: {args.snapshot_id}", file=sys.stderr)
        return 1
    agent = agents[args.agent_id]
    if args.plan_id and agent.get("plan_id") and args.plan_id != agent.get("plan_id"):
        print(
            f"Plan mismatch for {args.agent_id}: agent has {agent.get('plan_id')}, evidence has {args.plan_id}",
            file=sys.stderr,
        )
        return 1
    expected_paths = [normalize_repo_path(path) for path in (args.expected_path or [])]
    if expected_paths:
        changed_paths = set(snapshot.get("changed_paths", []))
        missing = [path for path in expected_paths if path not in changed_paths]
        if missing:
            print(
                "Round-log snapshot does not contain expected paths: "
                + ", ".join(missing),
                file=sys.stderr,
            )
            return 1
    timestamp = format_time(parse_time(args.at))
    event = {
        "at": timestamp,
        "event": "round-log-evidence",
        "agent_id": args.agent_id,
        "plan_id": args.plan_id or agent.get("plan_id", ""),
        "worktree_id": args.worktree_id or "",
        "task_id": agent.get("task_id", ""),
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_path": snapshot["snapshot_path"],
        "manifest_path": snapshot["manifest_path"],
        "created_at": snapshot["created_at"],
        "mode": snapshot["mode"],
        "label": snapshot["label"],
        "branch": snapshot["branch"],
        "previous_snapshot_id": snapshot["previous_snapshot_id"],
        "file_count": snapshot["file_count"],
        "changed_paths": snapshot["changed_paths"],
        "receipt": args.receipt or "",
        "work_order": args.work_order or "",
        "note": args.note or "",
    }
    append_round_log_event(state_dir, event)
    agent["latest_round_snapshot_id"] = snapshot["snapshot_id"]
    agent["latest_round_snapshot_at"] = timestamp
    agent["round_log_manifest_path"] = snapshot["manifest_path"]
    save_agents(state_dir, agents)
    render_running_agents(state_dir, agents)
    render_round_log_control(state_dir)
    print(f"Recorded round-log evidence for {args.agent_id}: {snapshot['snapshot_id']}")
    return 0


def latest_round_log_evidence(
    state_dir: Path,
    agent_id: str,
    plan_id: str = "",
    worktree_id: str = "",
) -> dict | None:
    for event in reversed(load_jsonl_entries(state_dir / "state" / "round-log-events.jsonl")):
        if event.get("event") != "round-log-evidence":
            continue
        if event.get("agent_id") != agent_id:
            continue
        if plan_id and event.get("plan_id") != plan_id:
            continue
        if worktree_id and event.get("worktree_id") != worktree_id:
            continue
        return event
    return None


def command_require_round_log_evidence(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    event = latest_round_log_evidence(
        state_dir,
        args.agent_id,
        plan_id=args.plan_id or "",
        worktree_id=args.worktree_id or "",
    )
    if not event:
        print(f"No round-log evidence recorded for {args.agent_id}", file=sys.stderr)
        return 1
    now = parse_time(args.at)
    evidence_at = parse_time(str(event.get("at") or ""))
    age_minutes = (now - evidence_at).total_seconds() / 60.0
    if age_minutes > args.max_age_minutes:
        print(
            f"Round-log evidence for {args.agent_id} is stale: {age_minutes:.1f} minutes",
            file=sys.stderr,
        )
        return 1
    if args.project_root:
        repo_root, repo_error = git_repo_root(Path(args.project_root).resolve())
        if repo_error:
            print(f"Round-log evidence requires a Git repository: {repo_error}", file=sys.stderr)
            return 2
        assert repo_root is not None
        snapshot_id = str(event.get("snapshot_id") or "")
        if not round_log_manifest_record(round_log_root(repo_root) / snapshot_id):
            print(f"Round-log manifest is missing or unreadable: {snapshot_id}", file=sys.stderr)
            return 1
    print(f"Round-log evidence present for {args.agent_id}: {event.get('snapshot_id')}")
    return 0


def command_round_log_export(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    repo_root, repo_error = git_repo_root(Path(args.project_root).resolve())
    timestamp = format_time(parse_time(args.at))
    if repo_error:
        print(f"Round-log export requires a Git repository: {repo_error}", file=sys.stderr)
        return 2
    assert repo_root is not None
    if not round_log_manifest_record(round_log_root(repo_root) / args.snapshot_id):
        print(f"Valid round-log snapshot not found: {args.snapshot_id}", file=sys.stderr)
        return 1
    extra_args = ["export", "--snapshot", args.snapshot_id, "--repo", str(repo_root)]
    if args.output:
        extra_args.extend(["--output", str(Path(args.output).resolve())])
    result, command_error = run_round_log_command(
        args.round_log_command,
        extra_args,
        args.timeout_seconds,
    )
    if command_error:
        print(command_error, file=sys.stderr)
        return 2
    assert result is not None
    output_path = str(Path(args.output).resolve()) if args.output else str(
        round_log_root(repo_root) / "exports" / args.snapshot_id
    )
    event = {
        "at": timestamp,
        "event": "round-log-export",
        "snapshot_id": args.snapshot_id,
        "repo_root": str(repo_root),
        "export_path": output_path,
        "command_returncode": result.returncode,
        "command_stdout": result.stdout.strip()[-2000:],
        "command_stderr": result.stderr.strip()[-2000:],
    }
    append_round_log_event(state_dir, event)
    render_round_log_control(state_dir)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        print(f"Round-log export failed: {detail}", file=sys.stderr)
        return 2
    print(f"Round-log export recorded: {output_path}")
    if result.stdout.strip():
        print(result.stdout.strip())
    return 0


def repair_log_root(repo_root: Path) -> Path:
    return repo_root / "docs" / "repair-execution-log"


def repair_log_event_path(state_dir: Path) -> Path:
    return state_dir / "state" / "repair-log-events.jsonl"


def append_repair_log_event(state_dir: Path, entry: dict) -> None:
    append_jsonl_locked(repair_log_event_path(state_dir), entry)


def repair_log_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "record"


def markdown_cell(value: object) -> str:
    return table_value(value).replace("\r", " ").replace("\n", " ")


def append_markdown_locked(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with with_lock(lock_path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())


def ensure_repair_log_layout(repo_root: Path, force: bool = False) -> list[Path]:
    root = repair_log_root(repo_root)
    task_root = root / "task-records"
    created: list[Path] = []
    files = {
        root / "README.md": "\n".join(
            [
                "# Repair Execution Log",
                "",
                "## Purpose",
                "",
                "- Repository-local document memory for bounded task records and repeated repair cycles.",
                "- The log preserves current status, next allowed step, escalation trigger, and evidence paths.",
                "- Records may narrow or sequence an authorized task; they do not create root authorization.",
                "",
                "## Layout",
                "",
                "- `task-records/` stores bounded non-loop work.",
                "- `<cycle-id>-execution-log/` stores repeated repair-cycle plans and attempt records.",
                "- `plan-index.md` tracks repair cycles and their current status.",
                "",
            ]
        )
        + "\n",
        root / "lifecycle-summary.md": "\n".join(
            [
                "# Repair Lifecycle Summary",
                "",
                "## Active Workstreams",
                "",
                "| Workstream | Current Status | Current Record | Next Allowed Step |",
                "| --- | --- | --- | --- |",
                "|  |  |  |  |",
                "",
                "## Blocked Or Paused Work",
                "",
                "| Workstream | Reason | Required Decision |",
                "| --- | --- | --- |",
                "|  |  |  |",
                "",
                "## Superseded Or Closed Work",
                "",
                "| Workstream | Closing Record | Notes |",
                "| --- | --- | --- |",
                "|  |  |  |",
                "",
            ]
        )
        + "\n",
        root / "plan-index.md": "\n".join(
            [
                "# Repair Plan Index",
                "",
                "| Date | Cycle Id | Status | Current Record | Next Allowed Step | Escalation Trigger | Notes |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        + "\n",
        task_root / "README.md": "\n".join(
            [
                "# Task Records",
                "",
                "## Purpose",
                "",
                "- Lightweight durable records for bounded non-loop work.",
                "- A task record is an execution receipt, not a repair-cycle plan.",
                "- Use a repair cycle when repeated attempts, rollback/retry, or autonomous iteration begins.",
                "",
            ]
        )
        + "\n",
        task_root / "plan-index.md": "\n".join(
            [
                "# Task Record Plan Index",
                "",
                "| Date | Workstream | Status | Outcome | Record | Next Allowed Step | Escalation Trigger |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        + "\n",
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not force:
            continue
        atomic_write_text(path, text)
        created.append(path)
    return created


def repair_log_table_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    header: list[str] | None = None
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not header:
            header = [re.sub(r"[^a-z0-9]+", "_", cell.lower()).strip("_") for cell in cells]
            continue
        if all(set(cell.replace(":", "").strip()) <= {"-"} for cell in cells):
            continue
        values = {key: cells[index] if index < len(cells) else "" for index, key in enumerate(header)}
        values["_index_path"] = str(path)
        values["_line"] = line_number
        rows.append(values)
    return rows


def repair_row_status(row: dict) -> str:
    value = str(
        row.get("status")
        or row.get("outcome")
        or row.get("decision")
        or row.get("current_status")
        or ""
    )
    return re.sub(r"[\s_]+", "-", value.strip().lower())


def repair_row_record_path(row: dict) -> str:
    return str(
        row.get("record")
        or row.get("current_record")
        or row.get("record_path")
        or row.get("plan")
        or ""
    ).strip()


def repair_row_next_step(row: dict) -> str:
    return str(
        row.get("next_allowed_step")
        or row.get("next_step")
        or row.get("current_next_allowed_step")
        or ""
    ).strip()


def latest_repair_row(
    repo_root: Path,
    workstream: str = "",
    cycle_id: str = "",
) -> dict | None:
    root = repair_log_root(repo_root)
    candidates: list[Path] = []
    if cycle_id:
        candidates.append(root / f"{repair_log_slug(cycle_id)}-execution-log" / "plan-index.md")
    elif workstream:
        stream_slug = repair_log_slug(workstream)
        stream_index = root / "task-records" / stream_slug / "plan-index.md"
        if stream_index.exists():
            candidates.append(stream_index)
        candidates.append(root / "task-records" / "plan-index.md")
    else:
        candidates.extend([root / "plan-index.md", root / "task-records" / "plan-index.md"])

    rows: list[dict] = []
    for candidate in candidates:
        for row in repair_log_table_rows(candidate):
            if workstream:
                row_workstream = str(row.get("workstream") or "").strip().lower()
                if row_workstream and row_workstream != workstream.strip().lower():
                    continue
            if cycle_id:
                row_cycle = str(row.get("cycle_id") or "").strip().lower()
                if row_cycle and row_cycle != repair_log_slug(cycle_id).lower():
                    continue
            rows.append(row)
    return rows[-1] if rows else None


def inspect_repair_log(repo_root: Path, workstream: str = "", cycle_id: str = "") -> dict:
    root = repair_log_root(repo_root)
    task_index = root / "task-records" / "plan-index.md"
    cycle_index = root / "plan-index.md"
    missing: list[str] = []
    for path in [root / "README.md", root / "lifecycle-summary.md", cycle_index, task_index]:
        if not path.exists():
            missing.append(str(path.relative_to(repo_root).as_posix()))
    current = latest_repair_row(repo_root, workstream=workstream, cycle_id=cycle_id)
    if not root.exists():
        status = "missing"
    elif missing:
        status = "partial"
    elif current and repair_row_status(current) in DEFAULT_REPAIR_LOG_BLOCKED_STATUSES:
        status = "blocked"
    elif current:
        status = "active"
    else:
        status = "initialized"
    return {
        "status": status,
        "repo_root": str(repo_root),
        "repair_log_root": str(root),
        "task_index": str(task_index),
        "cycle_index": str(cycle_index),
        "missing": missing,
        "workstream": workstream,
        "cycle_id": cycle_id,
        "current_row": current or {},
        "current_status": repair_row_status(current or {}),
        "current_record": repair_row_record_path(current or {}),
        "current_next_allowed_step": repair_row_next_step(current or {}),
    }


def render_repair_log_control(state_dir: Path) -> None:
    events = load_jsonl_entries(repair_log_event_path(state_dir))
    latest = events[-1] if events else {}
    latest_status = next(
        (
            event
            for event in reversed(events)
            if event.get("event") in {"repair-log-status", "repair-log-current-row-required"}
        ),
        {},
    )
    latest_record = next(
        (
            event
            for event in reversed(events)
            if event.get("event") in {"task-recorded", "repair-cycle-opened", "repair-attempt-recorded"}
        ),
        {},
    )
    lines = [
        "# Repair Log Control",
        "",
        "## Purpose",
        "",
        "- Project-local document memory for bounded tasks and repeated repair cycles.",
        "- Keeps continuity in repository docs instead of raw conversation history.",
        "- Complements Master ledgers, guard obligations, round-log evidence, and Git status.",
        "",
        "## Provider",
        "",
        f"- Repair log root: {latest_status.get('repair_log_root', '')}",
        f"- Status: {latest_status.get('status', '')}",
        f"- Latest index: {latest_status.get('latest_index', '')}",
        f"- Latest row status: {latest_status.get('current_status', '')}",
        f"- Latest row next allowed step: {latest_status.get('current_next_allowed_step', '')}",
        "",
        "## Task Record Lane",
        "",
        "- Task records root: docs/repair-execution-log/task-records",
        "- Task index: docs/repair-execution-log/task-records/plan-index.md",
        "- One-off task records do not authorize autonomous loops.",
        "- A paused, rollback-only, superseded, or no-further-action task row blocks successor work.",
        "",
        "## Repair Cycle Lane",
        "",
        "- Cycle index: docs/repair-execution-log/plan-index.md",
        f"- Active cycle: {latest_status.get('cycle_id', '')}",
        f"- Active cycle plan: {latest_status.get('cycle_plan', '')}",
        f"- Active cycle record: {latest_status.get('current_record', '')}",
        "- Repair cycles require explicit current user, goal, or approved-plan authority.",
        "",
        "## Current Row Gate",
        "",
        "- Required before launching or accepting sub-agent work when prior document memory exists.",
        "- The current row must name status, record path, next allowed step, and escalation trigger.",
        "- Blocked statuses: paused, blocked, not-ready, repair-cycle-needed, rollback-only, no-further-action, superseded, complete, accepted.",
        "- Allowed statuses default to active, continue, in-progress, ready.",
        "",
        "## Record Policy",
        "",
        "- Record task outcomes that affect future work.",
        "- Open a repair cycle for repeated failure classes, rollback/retry sequences, or autonomous iteration.",
        "- Do not treat task records, repair records, or plan-index rows as root authorization.",
        "- Do not create command-level record spam; consolidate when record volume grows.",
        "",
        "## Audit Trail",
        "",
        "- Events file: state/repair-log-events.jsonl",
        f"- Latest event: {latest.get('event', '')}",
        f"- Latest event at: {latest.get('at', '')}",
        f"- Latest record path: {latest_record.get('record_path', '')}",
        "",
    ]
    atomic_write_text(state_dir / "repair-log-control.md", "\n".join(lines) + "\n")


def command_repair_log_init(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    repo_root, repo_error = git_repo_root(Path(args.project_root).resolve())
    timestamp = format_time(parse_time(args.at))
    if repo_error:
        print(f"Repair log init requires a Git repository: {repo_error}", file=sys.stderr)
        return 2
    assert repo_root is not None
    created = ensure_repair_log_layout(repo_root, force=args.force)
    event = {
        "at": timestamp,
        "event": "repair-log-init",
        "repo_root": str(repo_root),
        "repair_log_root": str(repair_log_root(repo_root)),
        "created": [path.relative_to(repo_root).as_posix() for path in created],
    }
    append_repair_log_event(state_dir, event)
    render_repair_log_control(state_dir)
    print(f"Repair log initialized: {repair_log_root(repo_root)}")
    if created:
        print("created:")
        for path in created:
            print(f"- {path.relative_to(repo_root).as_posix()}")
    else:
        print("created: none")
    return 0


def command_repair_log_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    repo_root, repo_error = git_repo_root(Path(args.project_root).resolve())
    timestamp = format_time(parse_time(args.at))
    if repo_error:
        print(f"Repair log status requires a Git repository: {repo_error}", file=sys.stderr)
        return 2
    assert repo_root is not None
    status = inspect_repair_log(repo_root, workstream=args.workstream or "", cycle_id=args.cycle_id or "")
    current_row = status.get("current_row") or {}
    status["latest_index"] = current_row.get("_index_path", "")
    event = {"at": timestamp, "event": "repair-log-status", **status}
    append_repair_log_event(state_dir, event)
    render_repair_log_control(state_dir)
    print(f"Repair log status: {status['status']}")
    print(f"Repair log root: {status['repair_log_root']}")
    print(f"Current status: {status['current_status'] or 'none'}")
    print(f"Current record: {status['current_record'] or 'none'}")
    print(f"Next allowed step: {status['current_next_allowed_step'] or 'none'}")
    if status["missing"]:
        print("Missing:")
        for missing in status["missing"]:
            print(f"- {missing}")
    if args.require_current:
        return require_current_repair_row(
            repo_root,
            args.workstream or "",
            args.cycle_id or "",
            set(args.allowed_status or DEFAULT_REPAIR_LOG_ALLOWED_STATUSES),
        )
    return 1 if args.require_initialized and status["status"] in {"missing", "partial"} else 0


def require_current_repair_row(
    repo_root: Path,
    workstream: str,
    cycle_id: str,
    allowed_statuses: set[str],
) -> int:
    row = latest_repair_row(repo_root, workstream=workstream, cycle_id=cycle_id)
    if not row:
        print("No current repair-log row found", file=sys.stderr)
        return 1
    status = repair_row_status(row)
    record = repair_row_record_path(row)
    next_step = repair_row_next_step(row)
    if status in DEFAULT_REPAIR_LOG_BLOCKED_STATUSES or status not in allowed_statuses:
        print(
            f"Current repair-log row status blocks work: {status or 'missing'}",
            file=sys.stderr,
        )
        return 1
    if not record:
        print("Current repair-log row has no record path", file=sys.stderr)
        return 1
    if not next_step:
        print("Current repair-log row has no next allowed step", file=sys.stderr)
        return 1
    print(f"Current repair-log row allows work: status={status} record={record}")
    print(f"Next allowed step: {next_step}")
    return 0


def command_require_current_repair_row(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    repo_root, repo_error = git_repo_root(Path(args.project_root).resolve())
    timestamp = format_time(parse_time(args.at))
    if repo_error:
        print(f"Repair log gate requires a Git repository: {repo_error}", file=sys.stderr)
        return 2
    assert repo_root is not None
    allowed = {
        repair_row_status({"status": value})
        for value in (args.allowed_status or DEFAULT_REPAIR_LOG_ALLOWED_STATUSES)
    }
    result = require_current_repair_row(repo_root, args.workstream or "", args.cycle_id or "", allowed)
    row = latest_repair_row(repo_root, workstream=args.workstream or "", cycle_id=args.cycle_id or "")
    append_repair_log_event(
        state_dir,
        {
            "at": timestamp,
            "event": "repair-log-current-row-required",
            "repo_root": str(repo_root),
            "workstream": args.workstream or "",
            "cycle_id": args.cycle_id or "",
            "result": "passed" if result == 0 else "failed",
            "current_status": repair_row_status(row or {}),
            "current_record": repair_row_record_path(row or {}),
            "current_next_allowed_step": repair_row_next_step(row or {}),
        },
    )
    render_repair_log_control(state_dir)
    return result


def command_record_task(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    repo_root, repo_error = git_repo_root(Path(args.project_root).resolve())
    timestamp_dt = parse_time(args.at)
    timestamp = format_time(timestamp_dt)
    if repo_error:
        print(f"Task record requires a Git repository: {repo_error}", file=sys.stderr)
        return 2
    assert repo_root is not None
    ensure_repair_log_layout(repo_root)
    workstream = args.workstream or "general"
    workstream_slug = repair_log_slug(workstream)
    title_slug = repair_log_slug(args.slug or args.title)
    date_prefix = timestamp_dt.date().isoformat()
    record_dir = repair_log_root(repo_root) / "task-records" / workstream_slug
    record_path = record_dir / f"{date_prefix}-{title_slug}.md"
    if record_path.exists() and not args.force:
        print(f"Task record already exists: {record_path}", file=sys.stderr)
        return 1
    record_text = "\n".join(
        [
            f"# {date_prefix} - {args.title}",
            "",
            "## Scope",
            "",
            "- task type: bounded task record",
            f"- user objective: {args.objective}",
            f"- source docs: {', '.join(args.source_doc or [])}",
            f"- explicit non-goals: {', '.join(args.explicit_non_goal or [])}",
            "",
            "## Work Performed",
            "",
            f"- files touched: {', '.join(args.files_touched or [])}",
            f"- commands run: {', '.join(args.commands_run or [])}",
            f"- artifacts produced: {', '.join(args.artifact or [])}",
            "",
            "## Evidence",
            "",
            f"- validation: {', '.join(args.validation or [])}",
            "- visual review:",
            "- performance:",
            f"- missing or inconclusive evidence: {args.missing_evidence or ''}",
            "",
            "## Decision",
            "",
            f"- outcome: {args.outcome}",
            f"- first failing boundary: {args.first_failing_boundary or ''}",
            f"- reason: {args.reason}",
            f"- next allowed step: {args.next_step}",
            "",
            "## Notes For Future Agents",
            "",
            f"- what to trust: {args.trust or ''}",
            f"- what not to treat as proof: {args.not_proof or ''}",
            f"- related records or artifact roots: {', '.join(args.related or [])}",
            "",
        ]
    )
    record_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(record_path, record_text)
    rel_record = record_path.relative_to(repo_root).as_posix()
    row = (
        f"| {date_prefix} | {markdown_cell(workstream)} | {markdown_cell(args.status)} | "
        f"{markdown_cell(args.outcome)} | {markdown_cell(rel_record)} | "
        f"{markdown_cell(args.next_step)} | {markdown_cell(args.escalation_trigger)} |\n"
    )
    global_index = repair_log_root(repo_root) / "task-records" / "plan-index.md"
    stream_index = record_dir / "plan-index.md"
    if not stream_index.exists():
        atomic_write_text(
            stream_index,
            "# Task Record Plan Index\n\n"
            "| Date | Workstream | Status | Outcome | Record | Next Allowed Step | Escalation Trigger |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n",
        )
    append_markdown_locked(global_index, row)
    append_markdown_locked(stream_index, row)
    event = {
        "at": timestamp,
        "event": "task-recorded",
        "repo_root": str(repo_root),
        "workstream": workstream,
        "status": args.status,
        "outcome": args.outcome,
        "record_path": rel_record,
        "next_allowed_step": args.next_step,
        "escalation_trigger": args.escalation_trigger,
    }
    append_repair_log_event(state_dir, event)
    render_repair_log_control(state_dir)
    print(f"Task record created: {record_path}")
    return 0


def command_open_repair_cycle(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    repo_root, repo_error = git_repo_root(Path(args.project_root).resolve())
    timestamp_dt = parse_time(args.at)
    timestamp = format_time(timestamp_dt)
    if repo_error:
        print(f"Repair cycle requires a Git repository: {repo_error}", file=sys.stderr)
        return 2
    assert repo_root is not None
    ensure_repair_log_layout(repo_root)
    cycle_id = repair_log_slug(args.cycle_id)
    cycle_dir = repair_log_root(repo_root) / f"{cycle_id}-execution-log"
    plan_path = cycle_dir / "plan.md"
    index_path = cycle_dir / "plan-index.md"
    records_dir = cycle_dir / "records"
    if plan_path.exists() and not args.force:
        print(f"Repair cycle already exists: {plan_path}", file=sys.stderr)
        return 1
    records_dir.mkdir(parents=True, exist_ok=True)
    plan_text = "\n".join(
        [
            f"# {args.repair_area} Repair Cycle",
            "",
            "## Cycle",
            "",
            f"- cycle id: {cycle_id}",
            f"- repair area: {args.repair_area}",
            f"- status: {args.status}",
            f"- branch: {args.branch or ''}",
            f"- controlling docs: {', '.join(args.controlling_doc or [])}",
            "",
            "## Objective",
            "",
            f"- objective: {args.objective}",
            f"- original target error: {args.target_error}",
            f"- first failing boundary: {args.first_failing_boundary}",
            f"- acceptance metric: {args.acceptance_metric}",
            "",
            "## Boundaries",
            "",
            f"- forbidden boundaries: {', '.join(args.forbidden_boundary or [])}",
            f"- non-goals: {', '.join(args.non_goal or [])}",
            "",
            "## Prior Attempt Review",
            "",
            f"- duplicates or conflicts: {args.prior_attempt_review or ''}",
            "- failure-bucket migration risk:",
            "",
            "## Budget",
            "",
            f"- attempt budget: {args.attempt_budget}",
            "- reassessment trigger: repeated failed attempts or no acceptance-metric movement",
            "",
            "## Current Next Allowed Step",
            "",
            f"- {args.next_step}",
            "",
        ]
    )
    atomic_write_text(plan_path, plan_text)
    atomic_write_text(
        index_path,
        "# Repair Cycle Plan Index\n\n"
        "| Date | Cycle Id | Status | Current Record | Next Allowed Step | Escalation Trigger | Notes |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n",
    )
    date_prefix = timestamp_dt.date().isoformat()
    rel_plan = plan_path.relative_to(repo_root).as_posix()
    row = (
        f"| {date_prefix} | {markdown_cell(cycle_id)} | {markdown_cell(args.status)} | "
        f"{markdown_cell(rel_plan)} | {markdown_cell(args.next_step)} | "
        f"{markdown_cell(args.escalation_trigger)} | {markdown_cell(args.objective)} |\n"
    )
    append_markdown_locked(index_path, row)
    append_markdown_locked(repair_log_root(repo_root) / "plan-index.md", row)
    event = {
        "at": timestamp,
        "event": "repair-cycle-opened",
        "repo_root": str(repo_root),
        "cycle_id": cycle_id,
        "status": args.status,
        "record_path": rel_plan,
        "next_allowed_step": args.next_step,
        "escalation_trigger": args.escalation_trigger,
    }
    append_repair_log_event(state_dir, event)
    render_repair_log_control(state_dir)
    print(f"Repair cycle opened: {plan_path}")
    return 0


def command_record_repair_attempt(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    repo_root, repo_error = git_repo_root(Path(args.project_root).resolve())
    timestamp_dt = parse_time(args.at)
    timestamp = format_time(timestamp_dt)
    if repo_error:
        print(f"Repair attempt requires a Git repository: {repo_error}", file=sys.stderr)
        return 2
    assert repo_root is not None
    cycle_id = repair_log_slug(args.cycle_id)
    cycle_dir = repair_log_root(repo_root) / f"{cycle_id}-execution-log"
    plan_path = cycle_dir / "plan.md"
    if not plan_path.exists():
        print(f"Repair cycle plan not found: {plan_path}", file=sys.stderr)
        return 1
    attempt_slug = repair_log_slug(args.slug or args.attempt_id)
    date_prefix = timestamp_dt.date().isoformat()
    record_path = cycle_dir / "records" / f"{date_prefix}-{attempt_slug}.md"
    if record_path.exists() and not args.force:
        print(f"Repair attempt record already exists: {record_path}", file=sys.stderr)
        return 1
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_text = "\n".join(
        [
            f"# {date_prefix} - {args.attempt_id}",
            "",
            "## Hypothesis",
            "",
            f"- {args.hypothesis}",
            "",
            "## Intended Boundary",
            "",
            f"- {args.intended_boundary}",
            "",
            "## Diff Scope",
            "",
            f"- files touched: {', '.join(args.files_touched or [])}",
            "",
            "## Validation",
            "",
            f"- commands and artifacts: {', '.join(args.validation or [])}",
            "",
            "## Acceptance Movement",
            "",
            f"- acceptance metric movement: {args.metric_status}",
            f"- failure-bucket movement: {args.failure_bucket or ''}",
            "",
            "## Decision",
            "",
            f"- decision: {args.decision}",
            f"- rollback/salvage/revert: {args.diff_decision or ''}",
            f"- next allowed step: {args.next_step}",
            "",
        ]
    )
    atomic_write_text(record_path, record_text)
    rel_record = record_path.relative_to(repo_root).as_posix()
    row = (
        f"| {date_prefix} | {markdown_cell(cycle_id)} | {markdown_cell(args.decision)} | "
        f"{markdown_cell(rel_record)} | {markdown_cell(args.next_step)} | "
        f"{markdown_cell(args.escalation_trigger)} | {markdown_cell(args.metric_status)} |\n"
    )
    append_markdown_locked(cycle_dir / "plan-index.md", row)
    append_markdown_locked(repair_log_root(repo_root) / "plan-index.md", row)
    event = {
        "at": timestamp,
        "event": "repair-attempt-recorded",
        "repo_root": str(repo_root),
        "cycle_id": cycle_id,
        "attempt_id": args.attempt_id,
        "decision": args.decision,
        "metric_status": args.metric_status,
        "record_path": rel_record,
        "next_allowed_step": args.next_step,
        "escalation_trigger": args.escalation_trigger,
    }
    append_repair_log_event(state_dir, event)
    render_repair_log_control(state_dir)
    print(f"Repair attempt recorded: {record_path}")
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


def proposal_field(path: Path, heading: str, field_name: str) -> str:
    sections = parse_markdown_sections(path.read_text(encoding="utf-8"))
    return markdown_bullet_field(sections.get(heading, ""), field_name) or ""


def append_learning_correction(state_dir: Path, entry: dict) -> None:
    append_jsonl_locked(state_dir / "state" / "learning-corrections.jsonl", entry)


def append_learning_cycle(state_dir: Path, entry: dict) -> None:
    append_jsonl_locked(state_dir / "state" / "learning-cycles.jsonl", entry)


def append_learning_update(state_dir: Path, entry: dict) -> None:
    append_jsonl_locked(state_dir / "state" / "learning-updates.jsonl", entry)


def append_learning_effectiveness(state_dir: Path, entry: dict) -> None:
    append_jsonl_locked(state_dir / "state" / "learning-effectiveness.jsonl", entry)


def append_governance_event(state_dir: Path, entry: dict) -> None:
    append_jsonl_locked(state_dir / "state" / "governance-events.jsonl", entry)


def append_acceptance_gate(state_dir: Path, entry: dict) -> None:
    append_jsonl_locked(state_dir / "state" / "acceptance-gates.jsonl", entry)


def latest_acceptance_status_by_scope(state_dir: Path, scope_id: str) -> dict[str, str]:
    latest: dict[str, str] = {}
    for entry in load_jsonl_entries(state_dir / "state" / "acceptance-gates.jsonl"):
        if str(entry.get("scope_id") or "") != scope_id:
            continue
        maturity = str(entry.get("maturity") or "")
        status = str(entry.get("status") or "")
        if maturity:
            latest[maturity] = status
    return latest


def command_governance_lint(args: argparse.Namespace) -> int:
    packet = Path(args.packet).resolve()
    errors = validate_governance_packet(packet, args.packet_type)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"Governance packet is valid: {packet}")
    return 0


def command_record_authority_required(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    timestamp = format_time(parse_time(args.at))
    agents = load_agents(state_dir)
    if args.agent_id not in agents:
        print(f"Unknown agent id: {args.agent_id}", file=sys.stderr)
        return 1
    agents[args.agent_id]["status"] = AUTHORITY_REQUIRED_STATUS
    agents[args.agent_id]["last_action"] = "authority boundary reached"
    agents[args.agent_id]["next_action"] = args.required_user_decision
    agents[args.agent_id]["risk"] = args.reason
    save_agents(state_dir, agents)
    render_running_agents(state_dir, agents)
    event = {
        "at": timestamp,
        "event_type": "authority-required",
        "agent_id": args.agent_id,
        "reason": args.reason,
        "evidence": args.evidence,
        "required_user_decision": args.required_user_decision,
    }
    append_governance_event(state_dir, event)
    append_event_log(
        state_dir=state_dir,
        event_type="authority-required",
        related_packet="governance-events.jsonl",
        summary=f"{args.agent_id}: {args.reason}",
        evidence=args.evidence,
        ledger_update=f"{args.agent_id} marked authority_required",
        next_action=args.required_user_decision,
        at=timestamp,
    )
    print(f"Marked {args.agent_id} authority_required")
    return 0


def command_record_governance_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    timestamp = format_time(parse_time(args.at))
    agents = load_agents(state_dir)
    if args.agent_id not in agents:
        print(f"Unknown agent id: {args.agent_id}", file=sys.stderr)
        return 1
    if args.status == AUTHORITY_REQUIRED_STATUS:
        print(
            "Use record-authority-required for observed out-of-root production mutation.",
            file=sys.stderr,
        )
        return 1
    agents[args.agent_id]["status"] = args.status
    agents[args.agent_id]["last_action"] = args.reason
    agents[args.agent_id]["next_action"] = args.next_action
    agents[args.agent_id]["risk"] = args.evidence
    save_agents(state_dir, agents)
    render_running_agents(state_dir, agents)
    event = {
        "at": timestamp,
        "event_type": "governance-status",
        "agent_id": args.agent_id,
        "status": args.status,
        "reason": args.reason,
        "evidence": args.evidence,
        "next_action": args.next_action,
    }
    append_governance_event(state_dir, event)
    append_event_log(
        state_dir=state_dir,
        event_type="governance-status",
        related_packet="governance-events.jsonl",
        summary=f"{args.agent_id}: {args.status}",
        evidence=args.evidence,
        ledger_update=f"{args.agent_id} marked {args.status}",
        next_action=args.next_action,
        at=timestamp,
    )
    print(f"Marked {args.agent_id} {args.status}")
    return 0


def command_record_acceptance_gate(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    if args.status == "passed":
        maturity_index = ACCEPTANCE_MATURITY_ORDER.index(args.maturity)
        lower_gates = ACCEPTANCE_MATURITY_ORDER[:maturity_index]
        latest = latest_acceptance_status_by_scope(state_dir, args.scope_id)
        missing_or_unpassed = [
            gate for gate in lower_gates if latest.get(gate) != "passed"
        ]
        if missing_or_unpassed:
            print(
                "Cannot pass acceptance gate before lower gates pass: "
                + ", ".join(missing_or_unpassed),
                file=sys.stderr,
            )
            return 1
    timestamp = format_time(parse_time(args.at))
    entry = {
        "at": timestamp,
        "scope_id": args.scope_id,
        "maturity": args.maturity,
        "status": args.status,
        "evidence": args.evidence,
    }
    append_acceptance_gate(state_dir, entry)
    append_governance_event(
        state_dir,
        {
            "at": timestamp,
            "event_type": "acceptance-gate",
            "scope_id": args.scope_id,
            "maturity": args.maturity,
            "status": args.status,
            "evidence": args.evidence,
        },
    )
    append_event_log(
        state_dir=state_dir,
        event_type="acceptance-gate",
        related_packet="acceptance-gates.jsonl",
        summary=f"{args.scope_id}: {args.maturity} {args.status}",
        evidence=args.evidence,
        ledger_update="acceptance gate state recorded",
        next_action="continue only within supported maturity",
        at=timestamp,
    )
    print(f"Recorded acceptance gate {args.scope_id} {args.maturity}={args.status}")
    return 0


def render_correction_ledger(state_dir: Path) -> None:
    corrections = load_jsonl_entries(state_dir / "state" / "learning-corrections.jsonl")
    failure_counts: dict[str, int] = {}
    for entry in corrections:
        failure_mode = str(entry.get("failure_mode") or "unspecified")
        failure_counts[failure_mode] = failure_counts.get(failure_mode, 0) + 1
    lines = [
        "# Correction Ledger",
        "",
        "## Learning Objective",
        "",
        "- Convert material user corrections, failed reviews, incidents, and repeated agent mistakes into reviewed behavior updates.",
        "- Keep learning outside production code; route implementation needs through normal work orders.",
        "",
        "## Recorded Corrections",
        "",
        "| Time | Correction Id | Project | Failure Mode | Confidence | Evidence |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if corrections:
        for entry in corrections[-30:]:
            lines.append(
                "| {time} | {correction_id} | {project} | {failure_mode} | {confidence} | {evidence} |".format(
                    time=table_value(entry.get("at")),
                    correction_id=table_value(entry.get("correction_id")),
                    project=table_value(entry.get("project")),
                    failure_mode=table_value(entry.get("failure_mode")),
                    confidence=table_value(entry.get("confidence")),
                    evidence=table_value(entry.get("evidence")),
                )
            )
    else:
        lines.append("|  |  |  |  |  |  |")
    lines.extend(["", "## Failure Mode Summary", ""])
    if failure_counts:
        for failure_mode, count in sorted(failure_counts.items()):
            lines.append(f"- {failure_mode}: {count}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Promotion Candidates",
            "",
            "- Use learning-cycle-start to create a shortlist before edits.",
            "",
            "## Skip Or Defer Reasons",
            "",
            "- One-off, low-confidence, sensitive, already covered, or likely to overfit.",
        ]
    )
    atomic_write_text(state_dir / "correction-ledger.md", "\n".join(lines) + "\n")


def render_learning_effectiveness(state_dir: Path) -> None:
    updates = load_jsonl_entries(state_dir / "state" / "learning-updates.jsonl")
    checks = load_jsonl_entries(state_dir / "state" / "learning-effectiveness.jsonl")
    latest_by_proposal: dict[str, dict] = {}
    for check in checks:
        proposal_id = str(check.get("proposal_id") or "")
        if proposal_id:
            latest_by_proposal[proposal_id] = check
    status_counts: dict[str, int] = {}
    for update in updates:
        proposal_id = str(update.get("proposal_id") or "")
        latest = latest_by_proposal.get(proposal_id, {})
        status = str(latest.get("status") or "not-yet-measured")
        status_counts[status] = status_counts.get(status, 0) + 1
    lines = [
        "# Learning Effectiveness",
        "",
        "## Accepted Learning Updates",
        "",
        "| Time | Proposal Id | Decision | Target | Summary | Latest Status |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if updates:
        for update in updates[-30:]:
            proposal_id = str(update.get("proposal_id") or "")
            latest = latest_by_proposal.get(proposal_id, {})
            lines.append(
                "| {time} | {proposal_id} | {decision} | {target} | {summary} | {status} |".format(
                    time=table_value(update.get("at")),
                    proposal_id=table_value(proposal_id),
                    decision=table_value(update.get("decision")),
                    target=table_value(update.get("target_type")),
                    summary=table_value(update.get("summary")),
                    status=table_value(latest.get("status") or "not-yet-measured"),
                )
            )
    else:
        lines.append("|  |  |  |  |  |  |")
    lines.extend(["", "## Recurrence Checks", ""])
    if checks:
        for check in checks[-30:]:
            lines.append(
                f"- {check.get('at', '')}: {check.get('proposal_id', '')} "
                f"{check.get('status', '')} - {check.get('evidence', '')}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Effectiveness Summary", ""])
    if status_counts:
        for status, count in sorted(status_counts.items()):
            lines.append(f"- {status}: {count}")
    else:
        lines.append("- no accepted updates yet")
    recurrence = [
        check for check in checks if check.get("status") == "recurrence-detected"
    ]
    lines.extend(["", "## Rework Queue", ""])
    if recurrence:
        for check in recurrence[-20:]:
            lines.append(
                f"- {check.get('proposal_id', '')}: {check.get('next_action', '')}"
            )
    else:
        lines.append("- none")
    atomic_write_text(state_dir / "learning-effectiveness.md", "\n".join(lines) + "\n")


def command_record_learning_correction(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    timestamp = format_time(parse_time(args.at))
    correction_id = f"corr-{time.time_ns()}"
    entry = {
        "at": timestamp,
        "correction_id": correction_id,
        "project": args.project,
        "source": args.source,
        "task": args.task,
        "agent_behavior": args.agent_behavior,
        "user_correction": args.user_correction,
        "evidence": args.evidence,
        "failure_mode": args.failure_mode,
        "confidence": args.confidence,
        "severity": args.severity,
    }
    append_learning_correction(state_dir, entry)
    render_correction_ledger(state_dir)
    append_event_log(
        state_dir=state_dir,
        event_type="learning-correction",
        related_packet="correction-ledger.md",
        summary=f"{args.failure_mode}: {args.user_correction}",
        evidence=args.evidence,
        ledger_update="correction ledger updated",
        next_action="include in next learning cycle",
        at=timestamp,
    )
    print(f"Recorded learning correction {correction_id}")
    return 0


def command_learning_cycle_start(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    timestamp = format_time(parse_time(args.at))
    cycle_id = args.cycle_id or f"learning-cycle-{time.time_ns()}"
    cycle_slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", cycle_id).strip("-") or "learning-cycle"
    cycle_dir = state_dir / "packets" / "learning" / cycle_slug
    cycle_dir.mkdir(parents=True, exist_ok=True)
    output = cycle_dir / "learning-cycle.md"
    corrections = load_jsonl_entries(state_dir / "state" / "learning-corrections.jsonl")
    recent = corrections[-20:]
    lines = [
        "# Learning Cycle",
        "",
        "## Cycle Scope",
        "",
        f"- Cycle id: {cycle_id}",
        f"- Window: {args.window}",
        f"- Project: {args.project}",
        f"- Focus: {args.focus or 'all material corrections'}",
        f"- Created at: {timestamp}",
        "",
        "## Evidence Sources",
        "",
    ]
    if args.source:
        lines.extend(f"- {source}" for source in args.source)
    else:
        lines.extend(
            [
                "- correction-ledger.md",
                "- anomaly-log.md",
                "- incident-log.md",
                "- review-verdict.md",
            ]
        )
    lines.extend(
        [
            "",
            "## Correction Shortlist",
            "",
            "| Correction Id | Failure Mode | Evidence | Candidate Target | Decision |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if recent:
        for correction in recent:
            lines.append(
                "| {correction_id} | {failure_mode} | {evidence} |  | needs triage |".format(
                    correction_id=table_value(correction.get("correction_id")),
                    failure_mode=table_value(correction.get("failure_mode")),
                    evidence=table_value(correction.get("evidence")),
                )
            )
    else:
        lines.append("|  |  |  |  | no corrections recorded |")
    lines.extend(
        [
            "",
            "## Decisions",
            "",
            "- Create, extend, validator, skip, or needs-more-evidence for each candidate.",
            "",
            "## Applied Updates",
            "",
            "- none yet",
            "",
            "## Validation",
            "",
            "- Run learning-proposal-lint on each proposal before acceptance.",
            "- Run validate after accepted state changes.",
            "",
            "## Effectiveness Follow Up",
            "",
            "- Define recurrence check for every accepted learning proposal.",
        ]
    )
    atomic_write_text(output, "\n".join(lines) + "\n")
    append_learning_cycle(
        state_dir,
        {
            "at": timestamp,
            "cycle_id": cycle_id,
            "window": args.window,
            "project": args.project,
            "focus": args.focus or "",
            "path": str(output),
            "corrections_considered": len(recent),
        },
    )
    append_event_log(
        state_dir=state_dir,
        event_type="learning-cycle-started",
        related_packet=str(output),
        summary=f"learning cycle {cycle_id}",
        evidence=str(output),
        ledger_update="learning cycle recorded",
        next_action="distill shortlist into learning proposals",
        at=timestamp,
    )
    print(f"Created learning cycle: {output}")
    return 0


def command_learning_proposal_lint(args: argparse.Namespace) -> int:
    proposal = Path(args.proposal).resolve()
    errors = validate_learning_proposal(proposal)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"Learning proposal is valid: {proposal}")
    return 0


def command_accept_learning_proposal(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    proposal = Path(args.proposal).resolve()
    errors = validate_learning_proposal(proposal)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    timestamp = format_time(parse_time(args.at))
    proposal_id = args.proposal_id or proposal_field(proposal, "## Trigger", "Proposal id")
    if not proposal_id:
        print("Learning proposal id is required", file=sys.stderr)
        return 1
    entry = {
        "at": timestamp,
        "proposal_id": proposal_id,
        "proposal": str(proposal),
        "summary": args.summary,
        "target_type": proposal_field(proposal, "## Target", "Target type"),
        "target_path": proposal_field(proposal, "## Target", "Target path"),
        "decision": proposal_field(proposal, "## Decision", "Proposed decision"),
        "confidence": proposal_field(proposal, "## Decision", "Confidence"),
        "policy_review": args.policy_review or "",
        "validation_evidence": args.validation_evidence or "",
    }
    append_learning_update(state_dir, entry)
    append_learning_effectiveness(
        state_dir,
        {
            "at": timestamp,
            "proposal_id": proposal_id,
            "status": "not-yet-measured",
            "evidence": "accepted learning update; recurrence not measured yet",
            "next_action": proposal_field(proposal, "## Validation", "Recurrence check"),
        },
    )
    render_learning_effectiveness(state_dir)
    append_event_log(
        state_dir=state_dir,
        event_type="learning-proposal-accepted",
        related_packet=str(proposal),
        summary=args.summary,
        evidence=args.validation_evidence or str(proposal),
        ledger_update="learning update accepted",
        next_action="run recurrence check",
        at=timestamp,
    )
    print(f"Accepted learning proposal {proposal_id}")
    return 0


def command_record_learning_effectiveness(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    timestamp = format_time(parse_time(args.at))
    entry = {
        "at": timestamp,
        "proposal_id": args.proposal_id,
        "status": args.status,
        "evidence": args.evidence,
        "next_action": args.next_action,
    }
    append_learning_effectiveness(state_dir, entry)
    render_learning_effectiveness(state_dir)
    append_event_log(
        state_dir=state_dir,
        event_type="learning-effectiveness-recorded",
        related_packet="learning-effectiveness.md",
        summary=f"{args.proposal_id}: {args.status}",
        evidence=args.evidence,
        ledger_update="learning effectiveness updated",
        next_action=args.next_action,
        at=timestamp,
    )
    print(f"Recorded learning effectiveness for {args.proposal_id}")
    return 0


def command_learning_summary(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ensure_state_storage(state_dir)
    corrections = load_jsonl_entries(state_dir / "state" / "learning-corrections.jsonl")
    cycles = load_jsonl_entries(state_dir / "state" / "learning-cycles.jsonl")
    updates = load_jsonl_entries(state_dir / "state" / "learning-updates.jsonl")
    checks = load_jsonl_entries(state_dir / "state" / "learning-effectiveness.jsonl")
    failure_counts: dict[str, int] = {}
    for correction in corrections:
        failure_mode = str(correction.get("failure_mode") or "unspecified")
        failure_counts[failure_mode] = failure_counts.get(failure_mode, 0) + 1
    recurrence_count = sum(
        1 for check in checks if check.get("status") == "recurrence-detected"
    )
    print(f"Corrections: {len(corrections)}")
    print(f"Learning cycles: {len(cycles)}")
    print(f"Accepted learning updates: {len(updates)}")
    print(f"Recurrence detected: {recurrence_count}")
    print("Failure modes:")
    if failure_counts:
        for failure_mode, count in sorted(failure_counts.items()):
            print(f"- {failure_mode}: {count}")
    else:
        print("- none")
    return 1 if recurrence_count else 0


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
    corrections = load_jsonl_entries(state_dir / "state" / "learning-corrections.jsonl")
    learning_updates = load_jsonl_entries(state_dir / "state" / "learning-updates.jsonl")
    learning_checks = load_jsonl_entries(state_dir / "state" / "learning-effectiveness.jsonl")
    governance_events = load_jsonl_entries(state_dir / "state" / "governance-events.jsonl")
    acceptance_gates = load_jsonl_entries(state_dir / "state" / "acceptance-gates.jsonl")
    repair_log_events = load_jsonl_entries(state_dir / "state" / "repair-log-events.jsonl")
    recurrence_count = sum(
        1 for check in learning_checks if check.get("status") == "recurrence-detected"
    )
    print(f"Active plan: {strategy.get('plan_id') if strategy else 'none'}")
    print(f"Active agents: {', '.join(sorted(agents)) if agents else 'none'}")
    print(f"Project tokens: {budget.get('project_used', 0)} / {budget.get('project_budget') or 'unbounded'}")
    print(f"Open anomalies: {len(anomalies)}")
    print(f"Open alerts: {len(alerts)}")
    print(f"Learning corrections: {len(corrections)}")
    print(f"Accepted learning updates: {len(learning_updates)}")
    print(f"Learning recurrences: {recurrence_count}")
    print(f"Governance events: {len(governance_events)}")
    print(f"Acceptance gates: {len(acceptance_gates)}")
    print(f"Repair-log events: {len(repair_log_events)}")
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
    register.add_argument("--status", default="active", choices=AGENT_STATES)
    register.add_argument("--token-budget", type=int)
    register.add_argument("--max-heartbeats", type=int)
    register.add_argument("--plan-id")
    register.add_argument("--at")
    register.set_defaults(func=command_register_agent)

    heartbeat = subparsers.add_parser("heartbeat", help="Record a sub-agent heartbeat.")
    heartbeat.add_argument("--state-dir", required=True)
    heartbeat.add_argument("--agent-id", required=True)
    heartbeat.add_argument("--state", required=True, choices=AGENT_STATES)
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

    strategy_lint = subparsers.add_parser("strategy-packet-lint", help="Validate a Strategy packet before Master acceptance or work launch.")
    strategy_lint.add_argument("--packet", required=True)
    strategy_lint.set_defaults(func=command_strategy_packet_lint)

    require_strategy = subparsers.add_parser("require-strategy-packet-before-work", help="Fail unless the current accepted Strategy packet is complete and matches the requested plan.")
    require_strategy.add_argument("--state-dir", required=True)
    require_strategy.add_argument("--plan-id", required=True)
    require_strategy.add_argument("--packet")
    require_strategy.set_defaults(func=command_require_strategy_packet_before_work)

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
    session_create.add_argument("--provider", choices=["file", "manual-provider", "codex", "codex-app"], default="file")
    session_create.add_argument("--provider-command")
    session_create.add_argument("--provider-timeout-seconds", type=float, default=60)
    session_create.add_argument("--predecessor-agent-id")
    session_create.add_argument("--reason")
    session_create.add_argument("--worktree-id")
    session_create.add_argument("--at")
    session_create.set_defaults(func=command_session_create)

    confirm_create = subparsers.add_parser("session-confirm-create", help="Confirm a Codex app thread was created for an agent.")
    confirm_create.add_argument("--state-dir", required=True)
    confirm_create.add_argument("--agent-id", required=True)
    confirm_create.add_argument("--thread-id", required=True)
    confirm_create.add_argument("--worktree-id")
    confirm_create.add_argument("--note")
    confirm_create.add_argument("--at")
    confirm_create.set_defaults(func=command_session_confirm_create)

    session_send = subparsers.add_parser("session-send", help="Send a message to a provider-backed session.")
    session_send.add_argument("--state-dir", required=True)
    session_send.add_argument("--agent-id", required=True)
    session_send.add_argument("--message", required=True)
    session_send.add_argument("--provider-command")
    session_send.add_argument("--provider-timeout-seconds", type=float, default=60)
    session_send.add_argument("--at")
    session_send.set_defaults(func=command_session_send)

    confirm_send = subparsers.add_parser("session-confirm-send", help="Confirm a Codex app send_message_to_thread operation.")
    confirm_send.add_argument("--state-dir", required=True)
    confirm_send.add_argument("--agent-id", required=True)
    confirm_send.add_argument("--message")
    confirm_send.add_argument("--thread-id")
    confirm_send.add_argument("--note")
    confirm_send.add_argument("--at")
    confirm_send.set_defaults(func=command_session_confirm_send)

    session_read = subparsers.add_parser("session-read", help="Read a provider-backed session transcript.")
    session_read.add_argument("--state-dir", required=True)
    session_read.add_argument("--agent-id", required=True)
    session_read.add_argument("--provider-command")
    session_read.add_argument("--provider-timeout-seconds", type=float, default=60)
    session_read.add_argument("--at")
    session_read.set_defaults(func=command_session_read)

    confirm_read = subparsers.add_parser("session-confirm-read", help="Confirm a Codex app read_thread operation.")
    confirm_read.add_argument("--state-dir", required=True)
    confirm_read.add_argument("--agent-id", required=True)
    confirm_read.add_argument("--summary", required=True)
    confirm_read.add_argument("--turn-count", type=int, default=0)
    confirm_read.add_argument("--thread-id")
    confirm_read.add_argument("--note")
    confirm_read.add_argument("--at")
    confirm_read.set_defaults(func=command_session_confirm_read)

    session_archive = subparsers.add_parser("session-archive", help="Archive a provider-backed session.")
    session_archive.add_argument("--state-dir", required=True)
    session_archive.add_argument("--agent-id", required=True)
    session_archive.add_argument("--provider-command")
    session_archive.add_argument("--provider-timeout-seconds", type=float, default=60)
    session_archive.add_argument("--at")
    session_archive.set_defaults(func=command_session_archive)

    confirm_archive = subparsers.add_parser("session-confirm-archive", help="Confirm a Codex app set_thread_archived operation.")
    confirm_archive.add_argument("--state-dir", required=True)
    confirm_archive.add_argument("--agent-id", required=True)
    confirm_archive.add_argument("--thread-id")
    confirm_archive.add_argument("--note")
    confirm_archive.add_argument("--at")
    confirm_archive.set_defaults(func=command_session_confirm_archive)

    validate_predecessor = subparsers.add_parser("validate-predecessor-state", help="Validate a predecessor state packet for strict session rotation.")
    validate_predecessor.add_argument("--packet", required=True)
    validate_predecessor.set_defaults(func=command_validate_predecessor_state)

    request_rotation = subparsers.add_parser("request-rotation", help="Request a predecessor state packet before strict rotation.")
    request_rotation.add_argument("--state-dir", required=True)
    request_rotation.add_argument("--agent-id", required=True)
    request_rotation.add_argument("--successor-agent-id", required=True)
    request_rotation.add_argument("--reason", required=True)
    request_rotation.add_argument("--provider-command")
    request_rotation.add_argument("--provider-timeout-seconds", type=float, default=60)
    request_rotation.add_argument("--at")
    request_rotation.set_defaults(func=command_request_rotation)

    rotate = subparsers.add_parser("rotate-session", help="Freeze a drifting sub-agent and launch a successor session.")
    rotate.add_argument("--state-dir", required=True)
    rotate.add_argument("--agent-id", required=True)
    rotate.add_argument("--successor-agent-id", required=True)
    rotate.add_argument("--reason", required=True)
    rotate.add_argument("--provider", choices=["file", "codex", "codex-app"])
    rotate.add_argument("--provider-command")
    rotate.add_argument("--provider-timeout-seconds", type=float, default=60)
    rotate.add_argument("--predecessor-state-packet")
    rotate.add_argument("--require-predecessor-state", action="store_true")
    rotate.add_argument("--emergency-without-predecessor-state", action="store_true")
    rotate.add_argument("--successor-role")
    rotate.add_argument("--successor-task-id")
    rotate.add_argument("--successor-objective")
    rotate.add_argument("--successor-scope")
    rotate.add_argument("--token-budget", type=int)
    rotate.add_argument("--max-heartbeats", type=int)
    rotate.add_argument("--budget-impact", type=int, default=0)
    rotate.add_argument("--at")
    rotate.set_defaults(func=command_rotate_session)

    session_reconcile = subparsers.add_parser("session-reconcile", help="Reconcile requested sessions with provider evidence.")
    session_reconcile.add_argument("--state-dir", required=True)
    session_reconcile.add_argument("--provider-command")
    session_reconcile.add_argument("--provider-timeout-seconds", type=float, default=60)
    session_reconcile.add_argument("--codex-app-read-max-minutes", type=float, default=DEFAULT_CODEX_APP_READ_MAX_MINUTES)
    session_reconcile.add_argument("--at")
    session_reconcile.set_defaults(func=command_session_reconcile)

    worktree_plan = subparsers.add_parser("worktree-plan", help="Record an isolated Worktree plan before launching a sub-agent.")
    worktree_plan.add_argument("--state-dir", required=True)
    worktree_plan.add_argument("--worktree-id", required=True)
    worktree_plan.add_argument("--provider", choices=["codex-app", "local-git", "provider-command"], default="codex-app")
    worktree_plan.add_argument("--base-branch", required=True)
    worktree_plan.add_argument("--purpose", required=True)
    worktree_plan.add_argument("--project-root")
    worktree_plan.add_argument("--local-mutation-policy", default="do not mutate local checkout")
    worktree_plan.add_argument("--remote-mutation-policy", default="do not push or create PR without release gate")
    worktree_plan.add_argument("--copy-ignored-policy", default="only .worktreeinclude-approved ignored files")
    worktree_plan.add_argument("--at")
    worktree_plan.set_defaults(func=command_worktree_plan)

    worktree_confirm_create = subparsers.add_parser("worktree-confirm-create", help="Confirm provider evidence that a planned Worktree exists.")
    worktree_confirm_create.add_argument("--state-dir", required=True)
    worktree_confirm_create.add_argument("--worktree-id", required=True)
    worktree_confirm_create.add_argument("--provider", choices=["codex-app", "local-git", "provider-command"])
    worktree_confirm_create.add_argument("--provider-worktree-ref")
    worktree_confirm_create.add_argument("--worktree-path")
    worktree_confirm_create.add_argument("--thread-id")
    worktree_confirm_create.add_argument("--base-branch")
    worktree_confirm_create.add_argument("--note")
    worktree_confirm_create.add_argument("--at")
    worktree_confirm_create.set_defaults(func=command_worktree_confirm_create)

    worktree_assign = subparsers.add_parser("worktree-assign-session", help="Bind an active session to a confirmed Worktree.")
    worktree_assign.add_argument("--state-dir", required=True)
    worktree_assign.add_argument("--worktree-id", required=True)
    worktree_assign.add_argument("--agent-id", required=True)
    worktree_assign.add_argument("--note")
    worktree_assign.add_argument("--at")
    worktree_assign.set_defaults(func=command_worktree_assign_session)

    worktree_reconcile = subparsers.add_parser("worktree-reconcile", help="Reconcile active Worktrees with provider/session evidence.")
    worktree_reconcile.add_argument("--state-dir", required=True)
    worktree_reconcile.add_argument("--codex-app-read-max-minutes", type=float, default=DEFAULT_WORKTREE_EVIDENCE_MAX_MINUTES)
    worktree_reconcile.add_argument("--at")
    worktree_reconcile.set_defaults(func=command_worktree_reconcile)

    worktree_close = subparsers.add_parser("worktree-close", help="Request Worktree close/archive without mutating local or remote branches.")
    worktree_close.add_argument("--state-dir", required=True)
    worktree_close.add_argument("--worktree-id", required=True)
    worktree_close.add_argument("--reason", required=True)
    worktree_close.add_argument("--at")
    worktree_close.set_defaults(func=command_worktree_close)

    worktree_confirm_close = subparsers.add_parser("worktree-confirm-close", help="Confirm provider evidence that a Worktree was closed or archived.")
    worktree_confirm_close.add_argument("--state-dir", required=True)
    worktree_confirm_close.add_argument("--worktree-id", required=True)
    worktree_confirm_close.add_argument("--note")
    worktree_confirm_close.add_argument("--at")
    worktree_confirm_close.set_defaults(func=command_worktree_confirm_close)

    validate_include = subparsers.add_parser("validate-worktreeinclude", help="Validate .worktreeinclude ignored-file copy policy.")
    validate_include.add_argument("--state-dir", required=True)
    validate_include.add_argument("--project-root", required=True)
    validate_include.add_argument("--at")
    validate_include.set_defaults(func=command_validate_worktreeinclude)

    round_status = subparsers.add_parser("round-log-status", help="Inspect optional codex-round-log snapshot evidence for a repository.")
    round_status.add_argument("--state-dir", required=True)
    round_status.add_argument("--project-root", required=True)
    round_status.add_argument("--round-log-command")
    round_status.add_argument("--timeout-seconds", type=float, default=60)
    round_status.add_argument("--require-active", action="store_true")
    round_status.add_argument("--at")
    round_status.set_defaults(func=command_round_log_status)

    round_evidence = subparsers.add_parser("record-round-log-evidence", help="Bind a round-log snapshot manifest to a sub-agent receipt.")
    round_evidence.add_argument("--state-dir", required=True)
    round_evidence.add_argument("--project-root", required=True)
    round_evidence.add_argument("--agent-id", required=True)
    round_evidence.add_argument("--snapshot-id", required=True)
    round_evidence.add_argument("--plan-id")
    round_evidence.add_argument("--worktree-id")
    round_evidence.add_argument("--receipt")
    round_evidence.add_argument("--work-order")
    round_evidence.add_argument("--expected-path", action="append")
    round_evidence.add_argument("--note")
    round_evidence.add_argument("--at")
    round_evidence.set_defaults(func=command_record_round_log_evidence)

    require_round_evidence = subparsers.add_parser("require-round-log-evidence", help="Fail unless recent round-log evidence is bound to an agent.")
    require_round_evidence.add_argument("--state-dir", required=True)
    require_round_evidence.add_argument("--agent-id", required=True)
    require_round_evidence.add_argument("--plan-id")
    require_round_evidence.add_argument("--worktree-id")
    require_round_evidence.add_argument("--project-root")
    require_round_evidence.add_argument("--max-age-minutes", type=float, default=DEFAULT_ROUND_LOG_EVIDENCE_MAX_MINUTES)
    require_round_evidence.add_argument("--at")
    require_round_evidence.set_defaults(func=command_require_round_log_evidence)

    round_export = subparsers.add_parser("round-log-export", help="Explicitly export a readable round-log snapshot for review evidence.")
    round_export.add_argument("--state-dir", required=True)
    round_export.add_argument("--project-root", required=True)
    round_export.add_argument("--snapshot-id", required=True)
    round_export.add_argument("--round-log-command", required=True)
    round_export.add_argument("--output")
    round_export.add_argument("--timeout-seconds", type=float, default=60)
    round_export.add_argument("--at")
    round_export.set_defaults(func=command_round_log_export)

    repair_init = subparsers.add_parser("repair-log-init", help="Initialize the project-local docs/repair-execution-log document-memory lane.")
    repair_init.add_argument("--state-dir", required=True)
    repair_init.add_argument("--project-root", required=True)
    repair_init.add_argument("--force", action="store_true")
    repair_init.add_argument("--at")
    repair_init.set_defaults(func=command_repair_log_init)

    repair_status = subparsers.add_parser("repair-log-status", help="Inspect the project-local repair-execution-log document memory.")
    repair_status.add_argument("--state-dir", required=True)
    repair_status.add_argument("--project-root", required=True)
    repair_status.add_argument("--workstream")
    repair_status.add_argument("--cycle-id")
    repair_status.add_argument("--require-initialized", action="store_true")
    repair_status.add_argument("--require-current", action="store_true")
    repair_status.add_argument("--allowed-status", action="append")
    repair_status.add_argument("--at")
    repair_status.set_defaults(func=command_repair_log_status)

    require_repair_row = subparsers.add_parser("require-current-repair-row", help="Fail unless the current repair-log row allows successor work.")
    require_repair_row.add_argument("--state-dir", required=True)
    require_repair_row.add_argument("--project-root", required=True)
    require_repair_row.add_argument("--workstream")
    require_repair_row.add_argument("--cycle-id")
    require_repair_row.add_argument("--allowed-status", action="append")
    require_repair_row.add_argument("--at")
    require_repair_row.set_defaults(func=command_require_current_repair_row)

    record_task = subparsers.add_parser("record-task", help="Create a bounded task record in docs/repair-execution-log/task-records.")
    record_task.add_argument("--state-dir", required=True)
    record_task.add_argument("--project-root", required=True)
    record_task.add_argument("--title", required=True)
    record_task.add_argument("--workstream", default="general")
    record_task.add_argument("--objective", required=True)
    record_task.add_argument("--status", default="complete")
    record_task.add_argument("--outcome", required=True, choices=["complete", "not-ready", "inconclusive", "reverted", "salvage", "paused"])
    record_task.add_argument("--reason", required=True)
    record_task.add_argument("--next-step", required=True)
    record_task.add_argument("--escalation-trigger", required=True)
    record_task.add_argument("--source-doc", action="append")
    record_task.add_argument("--explicit-non-goal", action="append")
    record_task.add_argument("--files-touched", action="append")
    record_task.add_argument("--commands-run", action="append")
    record_task.add_argument("--artifact", action="append")
    record_task.add_argument("--validation", action="append")
    record_task.add_argument("--missing-evidence")
    record_task.add_argument("--first-failing-boundary")
    record_task.add_argument("--trust")
    record_task.add_argument("--not-proof")
    record_task.add_argument("--related", action="append")
    record_task.add_argument("--slug")
    record_task.add_argument("--force", action="store_true")
    record_task.add_argument("--at")
    record_task.set_defaults(func=command_record_task)

    open_cycle = subparsers.add_parser("open-repair-cycle", help="Open a governed repeated-repair document-memory cycle.")
    open_cycle.add_argument("--state-dir", required=True)
    open_cycle.add_argument("--project-root", required=True)
    open_cycle.add_argument("--cycle-id", required=True)
    open_cycle.add_argument("--repair-area", required=True)
    open_cycle.add_argument("--objective", required=True)
    open_cycle.add_argument("--target-error", required=True)
    open_cycle.add_argument("--first-failing-boundary", required=True)
    open_cycle.add_argument("--acceptance-metric", required=True)
    open_cycle.add_argument("--next-step", required=True)
    open_cycle.add_argument("--attempt-budget", type=int, required=True)
    open_cycle.add_argument("--status", default="active")
    open_cycle.add_argument("--branch")
    open_cycle.add_argument("--controlling-doc", action="append")
    open_cycle.add_argument("--forbidden-boundary", action="append")
    open_cycle.add_argument("--non-goal", action="append")
    open_cycle.add_argument("--prior-attempt-review")
    open_cycle.add_argument("--escalation-trigger", default="two failed attempts or next step exits authority")
    open_cycle.add_argument("--force", action="store_true")
    open_cycle.add_argument("--at")
    open_cycle.set_defaults(func=command_open_repair_cycle)

    repair_attempt = subparsers.add_parser("record-repair-attempt", help="Record one repair-cycle hypothesis, evidence, decision, and next allowed step.")
    repair_attempt.add_argument("--state-dir", required=True)
    repair_attempt.add_argument("--project-root", required=True)
    repair_attempt.add_argument("--cycle-id", required=True)
    repair_attempt.add_argument("--attempt-id", required=True)
    repair_attempt.add_argument("--hypothesis", required=True)
    repair_attempt.add_argument("--intended-boundary", required=True)
    repair_attempt.add_argument("--files-touched", action="append")
    repair_attempt.add_argument("--validation", action="append")
    repair_attempt.add_argument("--metric-status", required=True, choices=["improved", "unchanged", "regressed", "closed", "inconclusive"])
    repair_attempt.add_argument("--failure-bucket")
    repair_attempt.add_argument("--decision", required=True, choices=["continue", "reassess", "accepted", "paused", "blocked", "reverted", "salvage"])
    repair_attempt.add_argument("--diff-decision")
    repair_attempt.add_argument("--next-step", required=True)
    repair_attempt.add_argument("--escalation-trigger", required=True)
    repair_attempt.add_argument("--slug")
    repair_attempt.add_argument("--force", action="store_true")
    repair_attempt.add_argument("--at")
    repair_attempt.set_defaults(func=command_record_repair_attempt)

    enforce_boundary = subparsers.add_parser("enforce-master-boundary", help="Fail when Master changes exceed the allowed state/doc boundary.")
    enforce_boundary.add_argument("--project-root", required=True)
    enforce_boundary.add_argument("--state-dir", required=True)
    enforce_boundary.add_argument("--at")
    enforce_boundary.set_defaults(func=command_enforce_master_boundary)

    assess_parallel = subparsers.add_parser("assess-parallelism", help="Assess whether work orders are safe to run in parallel.")
    assess_parallel.add_argument("--state-dir", required=True)
    assess_parallel.add_argument("--work-order", action="append", required=True)
    assess_parallel.add_argument("--output")
    assess_parallel.set_defaults(func=command_assess_parallelism)

    governance_lint = subparsers.add_parser("governance-lint", help="Validate authority, behavior-domain, heuristic, and maturity fields in a packet.")
    governance_lint.add_argument("--packet", required=True)
    governance_lint.add_argument(
        "--packet-type",
        required=True,
        choices=sorted(GOVERNANCE_PACKET_REQUIRED_FIELDS),
    )
    governance_lint.set_defaults(func=command_governance_lint)

    governance_status = subparsers.add_parser("record-governance-status", help="Record a recoverable governance status without treating it as an authority violation.")
    governance_status.add_argument("--state-dir", required=True)
    governance_status.add_argument("--agent-id", required=True)
    governance_status.add_argument("--status", required=True, choices=sorted(GOVERNANCE_STATUS_VALUES - {AUTHORITY_REQUIRED_STATUS}))
    governance_status.add_argument("--reason", required=True)
    governance_status.add_argument("--evidence", required=True)
    governance_status.add_argument("--next-action", required=True)
    governance_status.add_argument("--at")
    governance_status.set_defaults(func=command_record_governance_status)

    authority_required = subparsers.add_parser("record-authority-required", help="Freeze an agent at an authority boundary and record the required decision.")
    authority_required.add_argument("--state-dir", required=True)
    authority_required.add_argument("--agent-id", required=True)
    authority_required.add_argument("--reason", required=True)
    authority_required.add_argument("--evidence", required=True)
    authority_required.add_argument("--required-user-decision", required=True)
    authority_required.add_argument("--at")
    authority_required.set_defaults(func=command_record_authority_required)

    acceptance_gate = subparsers.add_parser("record-acceptance-gate", help="Record monotonic acceptance maturity for a scope.")
    acceptance_gate.add_argument("--state-dir", required=True)
    acceptance_gate.add_argument("--scope-id", required=True)
    acceptance_gate.add_argument("--maturity", required=True, choices=ACCEPTANCE_MATURITY_ORDER)
    acceptance_gate.add_argument("--status", required=True, choices=sorted(ACCEPTANCE_GATE_STATUSES))
    acceptance_gate.add_argument("--evidence", required=True)
    acceptance_gate.add_argument("--at")
    acceptance_gate.set_defaults(func=command_record_acceptance_gate)

    learning_correction = subparsers.add_parser("record-learning-correction", help="Record a correction or failure pattern for later distillation.")
    learning_correction.add_argument("--state-dir", required=True)
    learning_correction.add_argument("--project", required=True)
    learning_correction.add_argument("--source", required=True)
    learning_correction.add_argument("--task", required=True)
    learning_correction.add_argument("--agent-behavior", required=True)
    learning_correction.add_argument("--user-correction", required=True)
    learning_correction.add_argument("--evidence", required=True)
    learning_correction.add_argument("--failure-mode", required=True)
    learning_correction.add_argument("--confidence", choices=["low", "medium", "high"], required=True)
    learning_correction.add_argument("--severity", choices=["info", "warning", "critical"], default="warning")
    learning_correction.add_argument("--at")
    learning_correction.set_defaults(func=command_record_learning_correction)

    learning_cycle = subparsers.add_parser("learning-cycle-start", help="Create a governed learning-cycle packet from recorded corrections.")
    learning_cycle.add_argument("--state-dir", required=True)
    learning_cycle.add_argument("--window", required=True)
    learning_cycle.add_argument("--project", required=True)
    learning_cycle.add_argument("--focus")
    learning_cycle.add_argument("--source", action="append")
    learning_cycle.add_argument("--cycle-id")
    learning_cycle.add_argument("--at")
    learning_cycle.set_defaults(func=command_learning_cycle_start)

    learning_lint = subparsers.add_parser("learning-proposal-lint", help="Validate a learning proposal before Master acceptance.")
    learning_lint.add_argument("--proposal", required=True)
    learning_lint.set_defaults(func=command_learning_proposal_lint)

    accept_learning = subparsers.add_parser("accept-learning-proposal", help="Accept a reviewed learning proposal into Master learning state.")
    accept_learning.add_argument("--state-dir", required=True)
    accept_learning.add_argument("--proposal", required=True)
    accept_learning.add_argument("--proposal-id")
    accept_learning.add_argument("--summary", required=True)
    accept_learning.add_argument("--policy-review")
    accept_learning.add_argument("--validation-evidence")
    accept_learning.add_argument("--at")
    accept_learning.set_defaults(func=command_accept_learning_proposal)

    learning_effectiveness = subparsers.add_parser("record-learning-effectiveness", help="Record whether an accepted learning update prevented recurrence.")
    learning_effectiveness.add_argument("--state-dir", required=True)
    learning_effectiveness.add_argument("--proposal-id", required=True)
    learning_effectiveness.add_argument("--status", choices=sorted(LEARNING_EFFECTIVENESS_STATUSES), required=True)
    learning_effectiveness.add_argument("--evidence", required=True)
    learning_effectiveness.add_argument("--next-action", required=True)
    learning_effectiveness.add_argument("--at")
    learning_effectiveness.set_defaults(func=command_record_learning_effectiveness)

    learning_summary = subparsers.add_parser("learning-summary", help="Summarize corrections, learning updates, and recurrence checks.")
    learning_summary.add_argument("--state-dir", required=True)
    learning_summary.set_defaults(func=command_learning_summary)

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
