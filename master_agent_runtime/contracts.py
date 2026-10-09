"""Operator-admitted project and module contracts, not model-authored role packets."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
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


class ScopeViolation(ContractError):
    """A private workspace holds changes its module grant cannot deliver."""


class ContextModified(ScopeViolation, ConflictError):
    """Readonly project context inside a private workspace was changed."""


class EnvironmentFailure(ContractError):
    """A host prerequisite failed (validator sandbox, executable); not an owner defect."""


class DispatchRejected(RuntimeErrorBase):
    """The native runtime definitively refused to start a turn; nothing ran."""


class TurnStopped(RuntimeErrorBase):
    """The host stopped or lost a native turn and observed its owned process exit.

    reason is one of: deadline, budget, native_failed, transport_lost,
    receipt_missing. The workspace of the stopped activation is fenced.
    """
    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


# .claude holds executable agent configuration; CLAUDE.md is loaded as instructions
# by Claude actors, so neither can travel inside a deliverable artifact.
PROTECTED_PARTS = {".git", ".gitignore", ".codex", ".agents", ".aws", ".claude", "CLAUDE.md"}
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


PROVIDERS = frozenset({"codex", "claude"})
POLICY_INTEGER_LIMITS = {
    "owner_turn_timeout_seconds": (1, 14400),
    "master_turn_timeout_seconds": (1, 3600),
    "interrupt_grace_seconds": (1, 600),
    "max_continuations": (0, 20),
    "max_transient_retries": (0, 10),
    "transient_backoff_seconds": (0, 3600),
}
POLICY_FLAGS = ("admission_review", "structured_review", "fail_fast_failed_checks")
PRICE_FIELDS = ("input", "cached_input", "output", "cache_write")


def _strict_fields(data, contract, location):
    if not isinstance(data, dict):
        raise ContractError(f"{location} must be a JSON object")
    unknown = sorted(set(data) - {item.name for item in fields(contract)})
    if unknown:
        raise ContractError(f"{location} has unknown fields: {', '.join(unknown)}")


@dataclass(frozen=True)
class ActorRuntime:
    """Native provider/model/effort for one actor; never a model-authored value."""
    provider: str = "codex"
    model: str | None = None
    effort: str | None = None

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ContractError(f"Unknown native provider {self.provider!r}; admitted: {sorted(PROVIDERS)}")
        for name in ("model", "effort"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ContractError(f"Actor runtime {name} must be a nonempty string when set")

    @classmethod
    def from_dict(cls, data, location="runtime"):
        _strict_fields(data, cls, location)
        return cls(**data)

    def to_dict(self):
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True)
class RuntimePolicy:
    """Operational settings stored beside, not inside, the engineering contract.

    Deadlines, continuation and retry bounds, review mechanics, provider
    routing and price tables change how the runtime operates. They never
    change objectives, scopes, validation or forbidden effects, so they do
    not alter the contract fingerprint that candidates and evidence bind.
    """
    owner_turn_timeout_seconds: int | None = None
    master_turn_timeout_seconds: int | None = None
    interrupt_grace_seconds: int = 60
    max_continuations: int = 2
    max_transient_retries: int = 3
    transient_backoff_seconds: int = 30
    admission_review: bool = True
    structured_review: bool = True
    fail_fast_failed_checks: bool = False
    provider: str = "codex"
    master: ActorRuntime | None = None
    modules: dict = field(default_factory=dict)
    prices: dict = field(default_factory=dict)

    def __post_init__(self):
        for name, (low, high) in POLICY_INTEGER_LIMITS.items():
            value = getattr(self, name)
            if value is None and name.endswith("timeout_seconds"):
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ContractError(f"Runtime policy {name} must be an integer in [{low}, {high}]")
        for name in POLICY_FLAGS:
            if not isinstance(getattr(self, name), bool):
                raise ContractError(f"Runtime policy {name} must be true or false")
        if self.provider not in PROVIDERS:
            raise ContractError(f"Unknown default provider {self.provider!r}; admitted: {sorted(PROVIDERS)}")
        if self.master is not None and not isinstance(self.master, ActorRuntime):
            raise ContractError("Runtime policy master must be an actor runtime")
        if not isinstance(self.modules, dict) or any(
                not isinstance(key, str) or not isinstance(value, ActorRuntime) for key, value in self.modules.items()):
            raise ContractError("Runtime policy modules must map module keys to actor runtimes")
        if not isinstance(self.prices, dict):
            raise ContractError("Runtime policy prices must map model names to USD-per-million prices")
        for model, row in self.prices.items():
            if not isinstance(model, str) or not model or not isinstance(row, dict) \
                    or not {"input", "output"} <= set(row) or set(row) - set(PRICE_FIELDS) \
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 for v in row.values()):
                raise ContractError(f"Invalid price row for model {model!r}; use input/output (and optional "
                                    "cached_input/cache_write) USD per million tokens")

    @classmethod
    def from_dict(cls, data, location="runtime_policy"):
        data = {} if data is None else data
        _strict_fields(data, cls, location)
        data = dict(data)
        if data.get("master") is not None:
            data["master"] = ActorRuntime.from_dict(data["master"], f"{location}.master")
        if "modules" in data:
            if not isinstance(data["modules"], dict):
                raise ContractError(f"{location}.modules must be a JSON object")
            data["modules"] = {key: ActorRuntime.from_dict(value, f"{location}.modules.{key}")
                               for key, value in data["modules"].items()}
        if "prices" in data and not isinstance(data["prices"], dict):
            raise ContractError(f"{location}.prices must be a JSON object")
        return cls(**data)

    def to_dict(self):
        default = RuntimePolicy()
        result = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if value == getattr(default, item.name):
                continue
            if isinstance(value, ActorRuntime):
                value = value.to_dict()
            elif item.name == "modules":
                value = {key: runtime.to_dict() for key, runtime in sorted(value.items())}
            elif item.name == "prices":
                value = {key: dict(sorted(row.items())) for key, row in sorted(value.items())}
            result[item.name] = value
        return result

    def validate_for(self, spec):
        keys = {module.key for module in spec.modules}
        unknown = sorted(set(self.modules) - keys)
        if unknown:
            raise ContractError(f"Runtime policy names unknown modules: {', '.join(unknown)}")
        return self

    def runtime_for(self, spec, module_key=None):
        """Resolve one actor's runtime. module_key None means the Master.

        Codex actors default to the contract's model/effort, preserving the
        existing behaviour. Other providers use only explicit policy values.
        """
        override = self.master if module_key is None else self.modules.get(module_key)
        provider = override.provider if override else self.provider
        model = override.model if override and override.model else (spec.model if provider == "codex" else None)
        effort = override.effort if override and override.effort else (spec.effort if provider == "codex" else None)
        return ActorRuntime(provider, model, effort)

    def providers(self, spec):
        return {self.runtime_for(spec).provider} | {self.runtime_for(spec, module.key).provider for module in spec.modules}

    def turn_timeout(self, spec, kind):
        configured = self.master_turn_timeout_seconds if kind == "master" else self.owner_turn_timeout_seconds
        return configured or spec.turn_timeout_seconds

    def price(self, model):
        return self.prices.get(model) if model else None
