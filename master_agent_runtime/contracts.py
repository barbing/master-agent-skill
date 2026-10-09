"""Operator-admitted project and module contracts, not model-authored role packets."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
import hashlib
import json


class RuntimeErrorBase(Exception):
    pass


class ContractError(RuntimeErrorBase):
    pass


class AuthorityError(RuntimeErrorBase):
    pass


class ConflictError(RuntimeErrorBase):
    pass


class UnknownOutcome(RuntimeErrorBase):
    """An admitted operation may have reached the native runtime."""


class BudgetError(RuntimeErrorBase):
    pass


class ProjectCancelled(RuntimeErrorBase):
    pass


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


PROTECTED_PARTS = {".git", ".gitignore", ".codex", ".agents", ".aws"}
FORBIDDEN_EFFECTS = frozenset({"git_commit", "git_stage", "git_policy", "publish",
                             "deploy", "install", "credential_change", "policy_change"})


def relative_path(value: str) -> str:
    p = PurePosixPath(value)
    if (not value or p.is_absolute() or ".." in p.parts or "\\" in value
            or any(x in PROTECTED_PARTS for x in p.parts)
            or p.as_posix() in {".", ""}):
        raise ContractError(f"Invalid or protected project-relative path: {value!r}")
    return p.as_posix()


def in_scope(path: str, prefixes: tuple[str, ...] | list[str]) -> bool:
    path = relative_path(path)
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


@dataclass(frozen=True)
class ValidationStep:
    argv: tuple[str, ...]
    timeout_seconds: int = 60

    def __post_init__(self):
        if not self.argv or any(not isinstance(x, str) or not x for x in self.argv):
            raise ContractError("Validation must use nonempty structured argv")
        if not 1 <= self.timeout_seconds <= 600:
            raise ContractError("Validation timeout must be between 1 and 600 seconds")


@dataclass(frozen=True)
class ModuleSpec:
    key: str
    objective: str
    paths: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    token_reservation: int = 20000
    validation: tuple[ValidationStep, ...] = ()
    test_paths: tuple[str, ...] = ()
    test_validation: tuple[ValidationStep, ...] = ()

    def __post_init__(self):
        if not self.key or not all(c.isalnum() or c in "-_" for c in self.key):
            raise ContractError("Module keys use letters, digits, hyphens and underscores")
        if not self.objective.strip() or not self.paths:
            raise ContractError("A module needs a real objective and explicit production paths")
        normalized = tuple(relative_path(p) for p in self.paths)
        if normalized != self.paths or len(set(normalized)) != len(normalized):
            raise ContractError("Module paths must be normalized and unique")
        if self.token_reservation < 1:
            raise ContractError("Each admitted native turn needs a positive token reservation")
        if bool(self.test_paths) != bool(self.test_validation):
            raise ContractError("Owner tests need both explicit test paths and admitted rerun commands")
        if len(set(self.test_paths)) != len(self.test_paths) or any(
                relative_path(path) != path or not in_scope(path, self.paths) for path in self.test_paths):
            raise ContractError("Owner test paths must be unique and inside the module grant")


@dataclass(frozen=True)
class ProjectSpec:
    root: str
    objective: str
    modules: tuple[ModuleSpec, ...]
    token_budget: int
    max_parallel: int = 2
    turn_timeout_seconds: int = 180
    max_revision_rounds: int = 2
    model: str | None = None
    effort: str | None = None
    forbidden_effects: tuple[str, ...] = tuple(sorted(FORBIDDEN_EFFECTS))
    integration_validation: tuple[ValidationStep, ...] = ()
    max_evidence_rounds: int = 2

    def __post_init__(self):
        root = Path(self.root)
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise ContractError("Project root must be an existing absolute real directory")
        if not self.objective.strip() or not self.modules or self.token_budget < 1:
            raise ContractError("Project handoff needs objective, modules and resource envelope")
        if not 1 <= self.max_parallel <= 16 or not 1 <= self.turn_timeout_seconds <= 3600:
            raise ContractError("Invalid parallelism or native-turn deadline")
        if not 0 <= self.max_revision_rounds <= 10:
            raise ContractError("Invalid bounded automatic revision count")
        if not 0 <= self.max_evidence_rounds <= 10:
            raise ContractError("Invalid bounded evidence recovery count")
        keys = {m.key for m in self.modules}
        if len(keys) != len(self.modules):
            raise ContractError("Module keys must be unique")
        for m in self.modules:
            if m.key in m.dependencies or not set(m.dependencies) <= keys:
                raise ContractError("Unknown or self dependency")
        pending = {m.key: set(m.dependencies) for m in self.modules}
        visited = set()
        while pending:
            ready = {k for k, deps in pending.items() if deps <= visited}
            if not ready:
                raise ContractError("Module dependency graph contains a cycle")
            visited |= ready
            pending = {k: v for k, v in pending.items() if k not in ready}
        for i, a in enumerate(self.modules):
            for b in self.modules[i + 1:]:
                if any(in_scope(x, b.paths) for x in a.paths) or any(in_scope(x, a.paths) for x in b.paths):
                    raise ContractError("Module write scopes overlap; use one coherent owner")
        if not FORBIDDEN_EFFECTS <= set(self.forbidden_effects):
            raise ContractError("This initial runtime cannot admit irreversible/external effects")

    def to_dict(self) -> dict:
        data = asdict(self)
        # Preserve old stored contract identities when new optional features
        # are unused. Adding test scopes/commands is an explicit new contract.
        for module in data['modules']:
            if not module['test_paths']: module.pop('test_paths')
            if not module['test_validation']: module.pop('test_validation')
        if self.max_evidence_rounds == 2: data.pop('max_evidence_rounds')
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectSpec":
        data = dict(data)
        modules = []
        for item in data.pop("modules"):
            item = dict(item)
            item["paths"] = tuple(item["paths"])
            item["dependencies"] = tuple(item.get("dependencies", ()))
            item["validation"] = tuple(ValidationStep(tuple(x["argv"]), x.get("timeout_seconds", 60))
                                       for x in item.get("validation", ()))
            item['test_paths'] = tuple(item.get('test_paths', ()))
            item['test_validation'] = tuple(ValidationStep(tuple(x['argv']), x.get('timeout_seconds', 60))
                                            for x in item.get('test_validation', ()))
            modules.append(ModuleSpec(**item))
        data["integration_validation"] = tuple(ValidationStep(tuple(x["argv"]), x.get("timeout_seconds", 60))
            for x in data.get("integration_validation", ()))
        if "forbidden_effects" in data:
            data["forbidden_effects"] = tuple(data["forbidden_effects"])
        return cls(modules=tuple(modules), **data)

    @property
    def fingerprint(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True)
class Principal:
    """Created by the trusted native connection binding, never from model arguments."""
    actor_id: str
    thread_id: str
    epoch: int
