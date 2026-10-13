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
    questions = status.get("pending_questions") or []
    waiting_user = [item for item in questions if item.get("state") == "awaiting_user"]
    project_state = status["state"]
    admission = status.get("admission")
    failed_admissions = sum(1 for operation in status["operations"]
                            if operation["kind"] == "admission" and operation["status"] == "failed")
    reconciliation_required = bool(uncertain_operations or unowned_work)
    safe_to_run = (project_state == "active" and not active_operations
                   and not blocked_modules and not reconciliation_required
                   and admission != "rejected")

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
                       "retain reservations until settlement has actual evidence. "
                       "If reconcile holds an operation that nobody can confirm, an operator may close it "
                       "without inferring a submission: resolve " + status["project_id"]
                       + " --operation OPERATION --outcome failed --authorization 'reason'.")
        if project_state != "active":
            next_action += f" Project is {project_state}; execution is not admitted."
    elif project_state == "awaiting_acceptance":
        integration = status.get("integration") or {}
        next_action = ("The integrated project is waiting for your acceptance. Inspect the files at "
                       f"{integration.get('path')} and the report at {integration.get('report')}, then either accept "
                       f"it: accept {status['project_id']} --authorization 'reason', or return it with feedback: "
                       f"reject {status['project_id']} --feedback 'what is wrong' [--module KEY] --authorization 'reason'.")
    elif project_state != "active":
        next_action = (f"Project is {project_state}; execution is not admitted. "
                       "Master should inspect lifecycle and outcome evidence.")
        if active_operations:
            next_action += " Observe remaining native operations and confirm their outcomes."
    elif active_operations:
        next_action = ("Observe running native operations before resuming Master scheduling: "
                       + ", ".join(operation["id"] for operation in active_operations) + ".")
    elif admission == "rejected":
        next_action = ("The Master's project admission requested changes, so no module work is admitted. "
                       "Correct the contract and admit a new project, or record a human decision: admission "
                       + status["project_id"] + " --decision rerun|override --authorization 'reason'.")
    elif blocked_modules:
        next_action = ("Master must resolve blocked modules before resuming: "
                       + ", ".join(blocked_modules) + ".")
        limits = status.get('recovery_limits', {})
        for module in modules:
            if module['key'] not in blocked_modules or not module.get('failure_reason'): continue
            extra = module.get('operator_rounds') or 0
            implementation_limit = limits.get('implementation', 'unknown')
            evidence_limit = limits.get('evidence', 'unknown')
            if extra and isinstance(implementation_limit, int):
                implementation_limit, evidence_limit = implementation_limit + extra, evidence_limit + extra
            next_action += (f" {module['key']} owner={module['owner']}: {module.get('failure_kind')}; "
                            f"{module['failure_reason']} Action={module.get('recovery_action')}. "
                            f"Implementation requests={module['rounds']}/{implementation_limit}; "
                            f"evidence requests={module.get('evidence_rounds', 0)}/{evidence_limit}.")
        next_action += (" Independent modules keep running; to give a blocked module more attempts with the same owner "
                        "and context: rearm " + status["project_id"] + " --module KEY --authorization 'reason'.")
    elif waiting_user:
        next_action = ("Owners are waiting for your answer: "
                       + " ".join(f"{item['module']}: {item.get('question_for_user') or item.get('question')}"
                                  + (f" Options: {item['options']}." if item.get("options") else "")
                                  + (f" Owner recommends: {item['recommendation']}." if item.get("recommendation") else "")
                                  for item in waiting_user)
                       + " Answer with: answer " + status["project_id"] + " --module KEY --text 'answer' "
                       "--authorization 'reason'. Other modules continue on the next run.")
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
        continuing = [module['key'] for module in modules if module['state'] == 'continuing']
        if continuing:
            next_action += (" Owners continue interrupted work from preserved checkpoints: "
                            + ", ".join(sorted(continuing)) + ".")
        asked = sorted(item["module"] for item in questions if item.get("state") == "awaiting_decision")
        if asked:
            next_action += " The Master answers the owners' questions first: " + ", ".join(asked) + "."
        if admission == "pending":
            next_action += " The next run first asks the Master for project admission; no module work starts before approval."
            if failed_admissions:
                next_action += (f" {failed_admissions} earlier admission turn(s) returned no decision; "
                                "inspect the native runtime if this repeats.")

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
