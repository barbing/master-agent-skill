"""Read and describe project handoffs without starting the runtime."""
from __future__ import annotations

from dataclasses import MISSING, fields
import json
from pathlib import Path

from .contracts import ContractError, ModuleSpec, ProjectSpec, RuntimePolicy, ValidationStep

__all__ = ["load_handoff", "load_handoff_with_policy", "describe_handoff"]

POLICY_KEY = "runtime_policy"


def _check_fields(data: object, contract: type, location: str) -> None:
    """Use the contract's own fields, including fields from nested steps."""
    if not isinstance(data, dict):
        raise ContractError(f"{location} must be a JSON object")
    declared = {field.name: field for field in fields(contract) if field.init}
    unknown = sorted(data.keys() - declared.keys())
    if unknown:
        raise ContractError(f"{location} has unknown fields: {', '.join(unknown)}")
    missing = sorted(name for name, field in declared.items()
                     if name not in data and field.default is MISSING
                     and field.default_factory is MISSING)
    if missing:
        raise ContractError(f"{location} is missing required fields: {', '.join(missing)}")


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number {value} is not permitted")


def load_handoff(path: str | Path) -> ProjectSpec:
    """Load the existing contract, retaining actionable handoff failure details."""
    return load_handoff_with_policy(path)[0]


def load_handoff_with_policy(path: str | Path) -> tuple[ProjectSpec, RuntimePolicy]:
    """Load the contract and its optional runtime_policy (kept outside contract identity)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"),
                          parse_constant=_reject_constant)
        policy_data = None
        if isinstance(data, dict) and POLICY_KEY in data:
            policy_data = data[POLICY_KEY]
            data = {key: value for key, value in data.items() if key != POLICY_KEY}
        _check_fields(data, ProjectSpec, "handoff")
        for index, module in enumerate(data["modules"]):
            location = f"modules[{index}]"
            _check_fields(module, ModuleSpec, location)
            for group in ('validation', 'test_validation'):
                for step_index, step in enumerate(module.get(group, ())):
                    _check_fields(step, ValidationStep, f"{location}.{group}[{step_index}]")
        for index, step in enumerate(data.get("integration_validation", ())):
            _check_fields(step, ValidationStep, f"integration_validation[{index}]")
        spec = ProjectSpec.from_dict(data)
        # Admission must also support the contract's canonical identity. This
        # catches numeric overflow and invalid Unicode without a second schema.
        spec.fingerprint
        policy = RuntimePolicy.from_dict(policy_data, POLICY_KEY).validate_for(spec)
        return spec, policy
    except (ContractError, OSError, ValueError, TypeError, KeyError,
            AttributeError, OverflowError, RecursionError) as error:
        raise ContractError(f"Invalid handoff {path!r}: {error}") from error


def describe_handoff(spec: ProjectSpec) -> dict:
    """Return detached contract/resource data without filesystem or native work."""
    return {
        "root": spec.root,
        "objective": spec.objective,
        "module_count": len(spec.modules),
        "modules": sorted(module.key for module in spec.modules),
        "spec_hash": spec.fingerprint,
        "native_calls": 0,
        "token_budget": spec.token_budget,
        "max_parallel": spec.max_parallel,
        "turn_timeout_seconds": spec.turn_timeout_seconds,
        "max_revision_rounds": spec.max_revision_rounds,
        'max_evidence_rounds': spec.max_evidence_rounds,
        'owner_tests': {module.key:list(module.test_paths) for module in spec.modules},
        "token_reservations": {module.key: module.token_reservation
                               for module in sorted(spec.modules, key=lambda module: module.key)},
        "model": spec.model,
        "effort": spec.effort,
        "forbidden_effects": list(spec.forbidden_effects),
    }
