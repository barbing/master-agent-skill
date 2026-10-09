"""Readonly project health derived from the runtime's recorded state."""
from __future__ import annotations

from copy import deepcopy

from .store import Store


__all__ = ["inspect_project_state"]

_ACTIVE_STATUSES = frozenset({"intent", "running", "unknown", "stopping"})
_UNCERTAIN_STATUSES = frozenset({"intent", "unknown", "stopping"})


def inspect_project_state(store: Store, project_id: str) -> dict:
    """Report one Store.status observation without performing recovery.

    Pending reviews are sorted module keys. Usage is the store's observed
    accounting, not exact billing. A safe report permits Master resumption;
    dispatch still needs the runtime's authority and resource checks.
    Unknown projects retain Store.status's ContractError.
    """
    status = store.status(project_id)
    modules = status["modules"]
    active_operations = [deepcopy(operation) for operation in status["operations"]
                         if operation["status"] in _ACTIVE_STATUSES]
    uncertain_operations = [operation for operation in active_operations
                            if operation["status"] in _UNCERTAIN_STATUSES]
    # A root-session binding does not own admitted module execution.
    active_owners = {operation["actor_id"] for operation in active_operations
                     if operation["kind"] != "binding"}
    unowned_work = sorted(module["key"] for module in modules
                          if module["state"] == "working"
                          and module["owner"] not in active_owners)
    blocked_modules = sorted(module["key"] for module in modules
                             if module["state"] == "blocked")
    pending_reviews = sorted(module["key"] for module in modules
                             if module["state"] in {"awaiting_review", 'awaiting_evidence'})
    project_state = status["state"]
    reconciliation_required = bool(uncertain_operations or unowned_work)
    safe_to_run = (project_state == "active" and not active_operations
                   and not blocked_modules and not reconciliation_required)

    if reconciliation_required:
        reasons = []
        if uncertain_operations:
            reasons.append("uncertain operations " + ", ".join(
                operation["id"] for operation in uncertain_operations))
        if unowned_work:
            reasons.append("working modules without active ownership "
                           + ", ".join(unowned_work))
        next_action = ("Reconcile " + "; ".join(reasons)
                       + ". Confirm native outcomes and ownership before resuming; "
                       "retain reservations until settlement has actual evidence.")
        if project_state != "active":
            next_action += f" Project is {project_state}; execution is not admitted."
    elif project_state != "active":
        next_action = (f"Project is {project_state}; execution is not admitted. "
                       "Master should inspect lifecycle and outcome evidence.")
        if active_operations:
            next_action += " Observe remaining native operations and confirm their outcomes."
    elif active_operations:
        next_action = ("Observe running native operations before resuming Master scheduling: "
                       + ", ".join(operation["id"] for operation in active_operations) + ".")
    elif blocked_modules:
        next_action = ("Master must resolve blocked modules before resuming: "
                       + ", ".join(blocked_modules) + ".")
        limits = status.get('recovery_limits', {})
        for module in modules:
            if module['key'] not in blocked_modules or not module.get('failure_reason'): continue
            next_action += (f" {module['key']} owner={module['owner']}: {module.get('failure_kind')}; "
                            f"{module['failure_reason']} Action={module.get('recovery_action')}. "
                            f"Implementation requests={module['rounds']}/{limits.get('implementation', 'unknown')}; "
                            f"evidence requests={module.get('evidence_rounds', 0)}/{limits.get('evidence', 'unknown')}.")
    elif pending_reviews:
        next_action = ("Resume Master review of submitted candidates for modules: "
                       + ", ".join(pending_reviews) + ".")
        evidence_pending = [module['key'] for module in modules if module['state'] == 'awaiting_evidence']
        if evidence_pending:
            next_action += (' Collect recorded native evidence and rerun the unchanged admitted checks for '
                            + ', '.join(evidence_pending) + '; no code reimplementation is requested.')
    elif modules and all(module["state"] == "accepted" for module in modules):
        next_action = "Resume Master integration validation and review of the accepted modules."
    else:
        next_action = ("Resume Master scheduling of ready modules and requested revisions "
                       "within the admitted resource envelope.")

    return {
        "project_id": status["project_id"],
        "project_state": project_state,
        "safe_to_run": safe_to_run,
        "reconciliation_required": reconciliation_required,
        "active_operations": active_operations,
        "blocked_modules": blocked_modules,
        "pending_reviews": pending_reviews,
        "next_action": next_action,
        "usage": {
            "spent_tokens": status["spent_tokens"],
            "reserved_tokens": status["reserved_tokens"],
            "hard_billing_cap": False,
        },
    }
