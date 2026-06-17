#!/usr/bin/env python3
"""Reference provider-command adapter for Master Agent session control."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"sessions": {}, "events": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid provider state file: {path}: {exc}") from exc
    if not isinstance(state, dict):
        raise SystemExit(f"invalid provider state file: {path}: expected object")
    state.setdefault("sessions", {})
    state.setdefault("events", [])
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def session_payload(path: Path, session: dict) -> dict:
    payload = dict(session)
    payload.setdefault("provider_session_path", str(path))
    payload.setdefault("provider_session_ref", str(path))
    return payload


def handle_request(state_path: Path, request: dict) -> dict:
    event = str(request.get("event") or "")
    agent_id = str(request.get("agent_id") or "")
    if not event:
        raise SystemExit("provider request is missing event")
    if not agent_id:
        raise SystemExit("provider request is missing agent_id")

    state = load_state(state_path)
    state["events"].append(
        {
            "at": now(),
            "event": event,
            "agent_id": agent_id,
            "provider_session_id": request.get("provider_session_id", ""),
        }
    )
    sessions = state["sessions"]
    session = sessions.get(agent_id)

    if event == "session-create":
        session = {
            "provider_session_id": request.get("provider_session_id")
            or f"file-provider:{agent_id}:{uuid.uuid4().hex[:12]}",
            "provider_session_path": str(state_path),
            "provider_session_ref": str(state_path),
            "agent_id": agent_id,
            "role": request.get("role", ""),
            "context_packet": request.get("context_packet", ""),
            "predecessor_agent_id": request.get("predecessor_agent_id", ""),
            "inheritance_reason": request.get("inheritance_reason", ""),
            "status": "active",
            "messages": [
                {
                    "at": now(),
                    "sender": "provider",
                    "message": "session ready",
                }
            ],
        }
        sessions[agent_id] = session
    elif event == "session-send":
        if not session:
            session = {
                "provider_session_id": request.get("provider_session_id")
                or f"file-provider:{agent_id}:missing",
                "provider_session_path": str(state_path),
                "provider_session_ref": str(state_path),
                "agent_id": agent_id,
                "role": request.get("role", ""),
                "status": "missing",
                "messages": [],
            }
            sessions[agent_id] = session
        message = str(request.get("message") or "")
        session.setdefault("messages", []).append(
            {"at": now(), "sender": "master", "message": message}
        )
        session["messages"].append(
            {"at": now(), "sender": "provider", "message": f"ack:{message}"}
        )
        if session.get("status") != "archived":
            session["status"] = "active"
    elif event == "session-read":
        if not session:
            session = {
                "provider_session_id": request.get("provider_session_id")
                or f"file-provider:{agent_id}:missing",
                "provider_session_path": str(state_path),
                "provider_session_ref": str(state_path),
                "agent_id": agent_id,
                "role": request.get("role", ""),
                "status": "missing",
                "messages": [],
            }
            sessions[agent_id] = session
    elif event == "session-archive":
        if not session:
            session = {
                "provider_session_id": request.get("provider_session_id")
                or f"file-provider:{agent_id}:missing",
                "provider_session_path": str(state_path),
                "provider_session_ref": str(state_path),
                "agent_id": agent_id,
                "role": request.get("role", ""),
                "status": "missing",
                "messages": [],
            }
            sessions[agent_id] = session
        else:
            session["status"] = "archived"
    elif event == "session-reconcile":
        if not session:
            session = {
                "provider_session_id": request.get("provider_session_id")
                or f"file-provider:{agent_id}:missing",
                "provider_session_path": str(state_path),
                "provider_session_ref": str(state_path),
                "agent_id": agent_id,
                "role": request.get("role", ""),
                "status": "missing",
                "messages": [],
            }
    else:
        raise SystemExit(f"unsupported provider event: {event}")

    save_state(state_path, state)
    return session_payload(state_path, session)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reference Master Agent file session provider.")
    parser.add_argument("--state-file", required=True)
    args = parser.parse_args()

    try:
        request = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"invalid provider request JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(request, dict):
        print("invalid provider request JSON: expected object", file=sys.stderr)
        return 2

    try:
        payload = handle_request(Path(args.state_file).resolve(), request)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
