#!/usr/bin/env python3
"""Copy Master Agent System templates into a project state directory."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from master_agent_tool import default_roles, render_role_catalog
from state_io import atomic_write_json, atomic_write_text


TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "assets" / "templates"
DEFAULT_STATE_DIR = Path("docs") / "master-agent"
USAGE_SOURCES = ("measured", "estimated", "self-reported")
USAGE_CONFIDENCES = ("low", "medium", "high")


def empty_usage_breakdown(keys: tuple[str, ...]) -> dict[str, int]:
    return {key: 0 for key in keys}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap a project-local Master Agent state pack."
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root to receive the state pack. Defaults to the current directory.",
    )
    parser.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help="State directory relative to project root. Defaults to docs/master-agent.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files. Without this flag, existing files are left unchanged.",
    )
    return parser.parse_args()


def ensure_within_project(project_root: Path, target_dir: Path) -> None:
    try:
        target_dir.relative_to(project_root)
    except ValueError as exc:
        raise SystemExit(
            f"Refusing to write outside project root: {target_dir}"
        ) from exc


def ensure_state_storage(target_dir: Path) -> None:
    storage_dir = target_dir / "state"
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
        atomic_write_json(agents_path, {})
    if not heartbeats_path.exists():
        atomic_write_text(heartbeats_path, "")
    if not strategy_sync_path.exists():
        atomic_write_text(strategy_sync_path, "")
    if not anomalies_path.exists():
        atomic_write_text(anomalies_path, "")
    if not budget_path.exists():
        atomic_write_json(
            budget_path,
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
    if not roles_path.exists():
        atomic_write_json(roles_path, default_roles())
    if not runtime_path.exists():
        atomic_write_json(
            runtime_path,
            {
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
            },
        )
    if not session_control_path.exists():
        atomic_write_text(session_control_path, "")
    if not incidents_path.exists():
        atomic_write_text(incidents_path, "")
    if not alerts_path.exists():
        atomic_write_text(alerts_path, "")
    if not schema_path.exists():
        atomic_write_json(
            schema_path,
            {
                "schema_version": "1.0",
                "compatible_tool": "master_agent_tool.py",
                "migration_history": [],
            },
        )


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    if not project_root.exists():
        print(f"Project root does not exist: {project_root}", file=sys.stderr)
        return 2
    if not project_root.is_dir():
        print(f"Project root is not a directory: {project_root}", file=sys.stderr)
        return 2

    target_dir = (project_root / args.state_dir).resolve()
    ensure_within_project(project_root, target_dir)

    if not TEMPLATE_DIR.exists():
        print(f"Template directory does not exist: {TEMPLATE_DIR}", file=sys.stderr)
        return 2

    target_dir.mkdir(parents=True, exist_ok=True)
    ensure_state_storage(target_dir)

    created = []
    skipped = []
    overwritten = []

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

    render_role_catalog(
        target_dir,
        json.loads((target_dir / "state" / "roles.json").read_text(encoding="utf-8")),
    )

    print(f"Master Agent state pack: {target_dir}")
    for label, paths in (
        ("created", created),
        ("overwritten", overwritten),
        ("skipped", skipped),
    ):
        if paths:
            print(f"{label}:")
            for path in paths:
                print(f"  {path.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
