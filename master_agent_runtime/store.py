"""Transactional project/module state; conversation remains in the native runtime."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import os
import hashlib
import shutil
import sqlite3
import tempfile
import time
import uuid

from .contracts import (AuthorityError, BudgetError, ConflictError, ContractError,
                        Principal, ProjectSpec, RuntimePolicy, canonical, digest)


ACTIVE_STATUSES = ('intent', 'running', 'unknown', 'stopping')
DISPATCHABLE_MODULE_STATES = ('ready', 'revision_requested', 'continuing')
ADMISSION_EVENTS = ('master-project-admission', 'operator-admission-override', 'operator-admission-rerun')
STOP_REASONS = ('deadline', 'budget', 'native_failed', 'transport_lost', 'receipt_missing')


def _admission_state(con, project_id):
    latest = con.execute("SELECT kind,payload FROM event WHERE project_id=? AND kind IN (?,?,?) ORDER BY seq DESC LIMIT 1",
                         (project_id, *ADMISSION_EVENTS)).fetchone()
    if latest is None:
        # Legacy stores predate the verdict event. Only Master review or
        # integration work implies an earlier approval; an admission turn by
        # itself (failed, held or settled without a recorded verdict) never
        # does, so admission is asked again.
        work = con.execute("SELECT 1 FROM operation WHERE actor_id=? AND kind NOT IN ('binding','admission')",
                           (f"{project_id}:master",)).fetchone()
        return "admitted" if work else "pending"
    if latest["kind"] == "master-project-admission":
        return "admitted" if _json(latest["payload"], {}).get("decision") == "approve" else "rejected"
    return "admitted" if latest["kind"] == "operator-admission-override" else "pending"


def _require_authorization(authorization):
    if not isinstance(authorization, str) or not authorization.strip():
        raise AuthorityError('Explicit operator authorization text is required')
    return authorization.strip()


def _json(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
 id TEXT PRIMARY KEY, spec TEXT NOT NULL, spec_hash TEXT NOT NULL,
 epoch INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'active',
 spent INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS actor (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES project(id),
 kind TEXT NOT NULL CHECK(kind IN ('master','module','verifier')), module_key TEXT,
 thread_id TEXT UNIQUE, session_id TEXT UNIQUE, epoch INTEGER NOT NULL DEFAULT 1,
 last_usage INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'registered');
CREATE TABLE IF NOT EXISTS module (
 project_id TEXT NOT NULL REFERENCES project(id), key TEXT NOT NULL,
 owner TEXT NOT NULL REFERENCES actor(id), state TEXT NOT NULL DEFAULT 'ready',
 revision INTEGER NOT NULL DEFAULT 1, accepted_candidate TEXT,
 current_candidate TEXT, rounds INTEGER NOT NULL DEFAULT 0,
 evidence_rounds INTEGER NOT NULL DEFAULT 0, failure_kind TEXT,
 failure_reason TEXT, recovery_action TEXT,
 PRIMARY KEY(project_id,key));
CREATE TABLE IF NOT EXISTS operation (
 id TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES actor(id), kind TEXT NOT NULL,
 status TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 reserve INTEGER NOT NULL, remote_id TEXT, usage INTEGER NOT NULL DEFAULT 0,
 error TEXT, at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_operation ON operation(actor_id)
 WHERE status IN ('intent','running','unknown','stopping');
CREATE TABLE IF NOT EXISTS message (
 id TEXT PRIMARY KEY, target TEXT NOT NULL REFERENCES actor(id), kind TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS candidate (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, module_key TEXT NOT NULL,
 owner TEXT NOT NULL, tree_hash TEXT NOT NULL, snapshot TEXT NOT NULL,
 inputs TEXT NOT NULL, mandate_revision INTEGER NOT NULL,
 validation TEXT NOT NULL, summary TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'submitted',
 evidence TEXT, evidence_hash TEXT);
CREATE TABLE IF NOT EXISTS review (
 id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, reviewer TEXT NOT NULL,
 decision TEXT NOT NULL, rationale TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS event (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
 kind TEXT NOT NULL, payload TEXT NOT NULL, at REAL NOT NULL);
"""


class _FreshReadonlyCursor(sqlite3.Cursor):
    def _check_source(self):
        self.connection._check_source()

    def execute(self, sql, parameters=()):
        self._check_source()
        return super().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        self._check_source()
        return super().executemany(sql, seq_of_parameters)

    def fetchone(self):
        self._check_source()
        return super().fetchone()

    def fetchmany(self, size=None):
        self._check_source()
        if size is None:
            return super().fetchmany()
        return super().fetchmany(size)

    def fetchall(self):
        self._check_source()
        return super().fetchall()

    def __iter__(self):
        self._check_source()
        return super().__iter__()

    def __next__(self):
        self._check_source()
        return super().__next__()

    def executescript(self, sql_script):
        self._check_source()
        return super().executescript(sql_script)


class _FreshReadonlyConnection(sqlite3.Connection):
    def cursor(self, factory=_FreshReadonlyCursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        return self.cursor().executescript(sql_script)

    def _check_source(self):
        path = getattr(self, '_source_path', None)
        if path is not None and Store._source_signature(path) != self._source_signature:
            raise sqlite3.OperationalError(
                'Runtime database changed since readonly snapshot; retry inspection')

    def close(self):
        scratch = getattr(self, '_scratch_path', None)
        scratch_dir = getattr(self, '_scratch_dir', None)
        try:
            super().close()
        finally:
            if scratch_dir is not None:
                shutil.rmtree(scratch_dir, ignore_errors=True)


class Store:
    def __init__(self, path: Path | str, *, readonly=False):
        self.path = Path(path).resolve()
        self._readonly = readonly
        self._snapshot_readonly = False
        if readonly:
            if not self.path.is_file(): raise ContractError('Existing runtime database is required')
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self.connect()
        try:
            con.executescript(SCHEMA)
            # Additive migration for existing writable stores. Read-only
            # inspection never migrates or changes a predecessor database.
            additions = {'module': {'evidence_rounds': 'INTEGER NOT NULL DEFAULT 0',
                                    'failure_kind': 'TEXT', 'failure_reason': 'TEXT', 'recovery_action': 'TEXT',
                                    'operator_rounds': 'INTEGER NOT NULL DEFAULT 0',
                                    'continuations': 'INTEGER NOT NULL DEFAULT 0',
                                    'transient_failures': 'INTEGER NOT NULL DEFAULT 0',
                                    'retry_after': 'REAL', 'checkpoint': 'TEXT', 'checkpoint_hash': 'TEXT',
                                    'decisions': 'INTEGER NOT NULL DEFAULT 0', 'pending_question': 'TEXT'},
                         'candidate': {'evidence': 'TEXT', 'evidence_hash': 'TEXT', 'at': 'REAL'},
                         'project': {'policy': "TEXT NOT NULL DEFAULT '{}'",
                                     'usage_detail': "TEXT NOT NULL DEFAULT '{}'",
                                     'cost_usd': 'REAL NOT NULL DEFAULT 0',
                                     'unpriced_tokens': 'INTEGER NOT NULL DEFAULT 0'},
                         'actor': {'last_usage_detail': "TEXT NOT NULL DEFAULT '{}'",
                                   'last_cost_usd': 'REAL NOT NULL DEFAULT 0', 'runtime': 'TEXT'},
                         'operation': {'exit_observed': 'INTEGER NOT NULL DEFAULT 0', 'outcome': 'TEXT',
                                       'usage_detail': "TEXT NOT NULL DEFAULT '{}'"},
                         'review': {'details': 'TEXT'}}
            for table, columns in additions.items():
                present = {row['name'] for row in con.execute(f'PRAGMA table_info({table})')}
                for name, declaration in columns.items():
                    if name not in present: con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
        finally:
            con.close()

    @classmethod
    def open_readonly(cls, path: Path | str):
        """Open an existing database for inspection without touching its directory.

        Reads are served from a private SQLite backup.  The source's main/WAL
        identity is checked while making the backup and before every query, so
        a caller either sees a coherent recent view or gets a retryable error.
        """
        target = Path(path).resolve()
        if not target.is_file():
            raise ContractError('Existing runtime database is required')
        store = cls.__new__(cls)
        store.path = target
        store._readonly = True
        store._snapshot_readonly = True
        return store

    @staticmethod
    def _source_signature(path):
        signature = []
        for suffix in ('', '-wal'):
            item = Path(str(path) + suffix)
            try:
                stat = item.stat()
            except FileNotFoundError:
                signature.append((suffix, None))
            else:
                signature.append((suffix, stat.st_dev, stat.st_ino, stat.st_size,
                                  stat.st_mtime_ns, stat.st_ctime_ns,
                                  hashlib.sha256(item.read_bytes()).digest()))
        return tuple(signature)

    def _snapshot_connect(self):
        if not self.path.is_file():
            raise ContractError('Runtime database disappeared; retry inspection')
        scratch = Path(os.environ.get('TMPDIR') or tempfile.gettempdir()).resolve()
        scratch.mkdir(parents=True, exist_ok=True)
        scratch_dir = Path(tempfile.mkdtemp(prefix='store-read-', dir=scratch))
        filename = scratch_dir / self.path.name
        try:
            before = self._source_signature(self.path)
            shutil.copyfile(self.path, filename)
            if Path(str(self.path) + '-wal').is_file():
                shutil.copyfile(str(self.path) + '-wal', str(filename) + '-wal')
            after = self._source_signature(self.path)
            if before != after:
                raise sqlite3.OperationalError(
                    'Runtime database changed during readonly snapshot; retry inspection')
            con = sqlite3.connect(filename.as_uri() + '?mode=ro', uri=True,
                                  timeout=2, isolation_level=None,
                                  factory=_FreshReadonlyConnection)
            con.row_factory = sqlite3.Row
            con._source_path = self.path
            con._source_signature = after
            con._scratch_path = filename
            con._scratch_dir = scratch_dir
            con.execute('PRAGMA query_only=ON')
            return con
        except BaseException:
            shutil.rmtree(scratch_dir, ignore_errors=True)
            raise

    def connect(self):
        if self._snapshot_readonly:
            return self._snapshot_connect()
        con = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=15,
                              isolation_level=None) if self._readonly else sqlite3.connect(self.path, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        if self._readonly:
            con.execute('PRAGMA query_only=ON')
            return con
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    @contextmanager
    def transaction(self):
        if self._readonly: raise AuthorityError('Read-only inspection cannot mutate runtime state')
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def event(con, project_id, kind, payload):
        con.execute("INSERT INTO event(project_id,kind,payload,at) VALUES(?,?,?,?)",
                    (project_id, kind, canonical(payload), time.time()))

    def create_project(self, spec: ProjectSpec, policy: RuntimePolicy | None = None) -> str:
        policy = (policy or RuntimePolicy()).validate_for(spec)
        project_id = uuid.uuid4().hex
        master = f"{project_id}:master"
        with self.transaction() as con:
            con.execute("INSERT INTO project(id,spec,spec_hash,policy) VALUES(?,?,?,?)",
                        (project_id, canonical(spec.to_dict()), spec.fingerprint, canonical(policy.to_dict())))
            con.execute("INSERT INTO actor(id,project_id,kind) VALUES(?,?,'master')",
                        (master, project_id))
            for mod in spec.modules:
                owner = f"{project_id}:module:{mod.key}"
                con.execute("INSERT INTO actor(id,project_id,kind,module_key) VALUES(?,?,'module',?)",
                            (owner, project_id, mod.key))
                con.execute("INSERT INTO module(project_id,key,owner) VALUES(?,?,?)",
                            (project_id, mod.key, owner))
            self.event(con, project_id, "project-admitted", {"spec_hash": spec.fingerprint})
        return project_id

    def configure_test_runtime(self,project_id,*,model,token_budget,authorization,effort=None):
        """Trusted operator action, outside native model tools; retain all usage.

        No business objective, module scope, oracle, identity or review state is
        changed. Models cannot reach this protected control store from their
        execution namespace. A human's test-only instruction is required.
        """
        if not authorization or not model.startswith('gpt-'):
            raise AuthorityError('Explicit operator test authorization and model required')
        with self.transaction() as con:
            row=con.execute('SELECT * FROM project WHERE id=?',(project_id,)).fetchone()
            if not row or row['state']!='active':raise ConflictError('Only an active test project can be reconfigured')
            live=con.execute("SELECT 1 FROM operation WHERE actor_id IN (SELECT id FROM actor WHERE project_id=?) AND status IN ('intent','running','unknown','stopping')",(project_id,)).fetchone()
            if live:raise ConflictError('Reconcile owned native operations before changing test settings')
            if token_budget < row['spent']+row['reserved']:raise BudgetError('New test envelope cannot erase incurred usage or liability')
            data=json.loads(row['spec']);before={key:data.get(key) for key in ('model','effort','token_budget')}
            data.update(model=model,token_budget=token_budget,effort=effort)
            revised=ProjectSpec.from_dict(data)
            con.execute('UPDATE project SET spec=?,spec_hash=? WHERE id=?',(canonical(revised.to_dict()),revised.fingerprint,project_id))
            self.event(con,project_id,'operator-test-runtime-revised',{'authorization':authorization,'before':before,
                'after':{'model':model,'effort':effort,'token_budget':token_budget},'retained_spent':row['spent'],'retained_reserved':row['reserved'],
                'previous_spec_hash':row['spec_hash'],'new_spec_hash':revised.fingerprint})

    def project(self, project_id):
        con = self.connect()
        try:
            row = con.execute("SELECT * FROM project WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise ContractError("Unknown project")
            result = dict(row)
            result["contract"] = ProjectSpec.from_dict(json.loads(row["spec"]))
            result["policy"] = RuntimePolicy.from_dict(_json(result.get("policy"), {}))
            return result
        finally:
            con.close()

    def actor(self, actor_id):
        con = self.connect()
        try:
            row = con.execute("SELECT * FROM actor WHERE id=?", (actor_id,)).fetchone()
            if not row:
                raise AuthorityError("Unknown actor")
            return dict(row)
        finally:
            con.close()

    def modules(self, project_id):
        con = self.connect()
        try:
            return [dict(x) for x in con.execute("SELECT * FROM module WHERE project_id=? ORDER BY key", (project_id,))]
        finally:
            con.close()

    @staticmethod
    def authorize(con, principal: Principal, kind: str | None = None):
        row = con.execute("SELECT * FROM actor WHERE id=?", (principal.actor_id,)).fetchone()
        if (not row or not principal.thread_id or row["thread_id"] != principal.thread_id
                or row["epoch"] != principal.epoch or row["state"] == "revoked"
                or (kind and row["kind"] != kind)):
            raise AuthorityError("Unbound, stale or unauthorized native principal")
        project = con.execute("SELECT * FROM project WHERE id=?", (row["project_id"],)).fetchone()
        if project["state"] != "active":
            raise AuthorityError("Project no longer admits effects/control")
        return row

    def bind(self, actor_id, thread_id, session_id):
        # A separate native root is required, not a fork that silently shares its
        # parent's session tree. Existing bindings can only resume the same root.
        if not thread_id or session_id != thread_id:
            raise AuthorityError("Managed module/master actors require their own native root session")
        with self.transaction() as con:
            row = con.execute("SELECT * FROM actor WHERE id=?", (actor_id,)).fetchone()
            if not row or row["state"] == "revoked":
                raise AuthorityError("Unknown/revoked actor")
            if row["thread_id"] and row["thread_id"] != thread_id:
                raise ConflictError("Cannot silently replace persistent module context")
            con.execute("UPDATE actor SET thread_id=?,session_id=? WHERE id=?", (thread_id, session_id, actor_id))
            self.event(con, row["project_id"], "native-root-bound", {"actor": actor_id, "thread": thread_id})
        return Principal(actor_id, thread_id, row["epoch"])

    def master(self, project_id):
        return self.actor(f"{project_id}:master")

    def register_verifier(self, master: Principal, module_key: str) -> str:
        """The Master assigns a module its independent verifier: a readonly actor with its own native session.

        Its module is named only in its id: module_key stays NULL, so every owner path (workspaces, write
        grants, checkpoints, rearm) and every runtime lookup treats it as a Master-side readonly actor."""
        self._allow_verifier_actors(self.actor(master.actor_id)["project_id"])
        with self.transaction() as con:
            reviewer = self.authorize(con, master, "master")
            project_id = reviewer["project_id"]
            if not con.execute("SELECT 1 FROM module WHERE project_id=? AND key=?", (project_id, module_key)).fetchone():
                raise ContractError("Unknown module for a verifier")
            actor_id = f"{project_id}:verifier:{module_key}"
            if not con.execute("SELECT 1 FROM actor WHERE id=?", (actor_id,)).fetchone():
                con.execute("INSERT INTO actor(id,project_id,kind) VALUES(?,?,'verifier')", (actor_id, project_id))
                self.event(con, project_id, "verifier-registered", {"module": module_key, "actor": actor_id})
        return actor_id

    def _allow_verifier_actors(self, project_id):
        """Stores created before verifier actors check actor kinds against ('master','module') only.

        SQLite cannot alter a CHECK constraint, so the documented table rebuild widens it once, on the
        first verifier this store registers: same rows, columns and references, foreign keys verified
        before commit. Stores that never use verifiers are never rebuilt."""
        def actor_sql(con):
            return con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='actor'").fetchone()["sql"]
        legacy, widened = "CHECK(kind IN ('master','module'))", "CHECK(kind IN ('master','module','verifier'))"
        con = self.connect()
        try:
            if widened in actor_sql(con):
                return
            con.execute("PRAGMA foreign_keys=OFF")  # only outside a transaction
            con.execute("BEGIN IMMEDIATE")
            try:
                sql = actor_sql(con)
                if widened not in sql:
                    # SQLite quotes a table name it has renamed.
                    head = next((prefix for prefix in ('CREATE TABLE actor (', 'CREATE TABLE "actor" (')
                                 if sql.startswith(prefix)), None)
                    if sql.count(legacy) != 1 or head is None:
                        raise ContractError("Unrecognized actor table: this store cannot register verifier actors")
                    con.execute("CREATE TABLE actor_widened (" + sql[len(head):].replace(legacy, widened))
                    con.execute("INSERT INTO actor_widened SELECT * FROM actor")
                    con.execute("DROP TABLE actor")
                    con.execute("ALTER TABLE actor_widened RENAME TO actor")
                    if con.execute("PRAGMA foreign_key_check").fetchone():
                        raise ConflictError("The actor table rebuild would break a reference; nothing was changed")
                    self.event(con, project_id, "store-actor-kinds-widened", {"kinds": ["master", "module", "verifier"]})
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        finally:
            con.execute("PRAGMA foreign_keys=ON")
            con.close()

    def recover_unused_master_binding(self,project_id,*,evidence):
        """A missing rollout is replaceable only before any admitted model work."""
        if not evidence:raise AuthorityError('Definitive native absence evidence required')
        with self.transaction() as con:
            actor=con.execute('SELECT * FROM actor WHERE id=?',(project_id+':master',)).fetchone()
            work=con.execute("SELECT 1 FROM operation WHERE actor_id=? AND kind!='binding'",(actor['id'],)).fetchone()
            live=con.execute("SELECT 1 FROM operation WHERE actor_id=? AND status IN ('intent','running','unknown','stopping')",(actor['id'],)).fetchone()
            if work or live:raise ConflictError('Cannot replace Master context with admitted or unresolved work')
            self.event(con,project_id,'unused-master-root-absence-recovered',{'previous_thread':actor['thread_id'],'evidence':evidence,'epoch':actor['epoch']+1})
            con.execute("UPDATE actor SET thread_id=NULL,session_id=NULL,epoch=epoch+1,state='registered' WHERE id=?",(actor['id'],))

    def inputs(self, project_id, module_key):
        project = self.project(project_id)
        spec = next(m for m in project["contract"].modules if m.key == module_key)
        modules = {m["key"]: m for m in self.modules(project_id)}
        return {key: {"revision": modules[key]["revision"], "candidate": modules[key]["accepted_candidate"]}
                for key in spec.dependencies}

    def dispatch(self, master: Principal, actor_id: str, message: str, *, kind="work", provisional=()) -> str:
        """provisional names dependencies the host lets this owner build against an interface contract
        before they are accepted; their binding then records no accepted candidate."""
        with self.transaction() as con:
            reviewer = self.authorize(con, master, "master")
            actor = con.execute("SELECT * FROM actor WHERE id=?", (actor_id,)).fetchone()
            if not actor or actor["project_id"] != reviewer["project_id"] or actor["state"] == "revoked":
                raise AuthorityError("Dispatch target is outside project or revoked")
            project = con.execute("SELECT * FROM project WHERE id=?", (reviewer["project_id"],)).fetchone()
            spec = ProjectSpec.from_dict(json.loads(project["spec"]))
            if actor["kind"] == "module":
                module = con.execute("SELECT * FROM module WHERE owner=?", (actor_id,)).fetchone()
                mod_spec = next(m for m in spec.modules if m.key == module["key"])
                input_binding = {}
                for dependency in mod_spec.dependencies:
                    dep = con.execute("SELECT * FROM module WHERE project_id=? AND key=?", (project["id"], dependency)).fetchone()
                    input_binding[dependency] = {"revision": dep["revision"], "candidate": dep["accepted_candidate"]}
                    if dep["state"] != "accepted" and dependency not in provisional:
                        raise ConflictError("Required module interface is not accepted/current")
                if module["state"] not in DISPATCHABLE_MODULE_STATES:
                    raise ConflictError("Module is not ready for execution")
                live = con.execute("SELECT count(*) FROM operation o JOIN actor a ON a.id=o.actor_id WHERE a.project_id=? AND a.kind='module' AND o.status IN ('intent','running','unknown','stopping')", (project["id"],)).fetchone()[0]
                if live >= spec.max_parallel:
                    raise ConflictError("No concurrent module slot available")
                reserve = mod_spec.token_reservation
            else:
                input_binding = {}
                reserve = min(m.token_reservation for m in spec.modules)
            if project["spent"] + project["reserved"] + reserve > spec.token_budget:
                raise BudgetError("Project token envelope has no room for this reservation")
            operation = uuid.uuid4().hex
            payload = {"message": message, "module_revision": module["revision"] if actor["kind"] == "module" else None,
                       "issuer": master.actor_id, "issuer_epoch": master.epoch,
                       "spec_hash": project["spec_hash"], "inputs": input_binding}
            if actor["kind"] == "module":
                payload["module_state"] = module["state"]
            try:
                con.execute("INSERT INTO operation(id,actor_id,kind,status,payload,payload_hash,reserve,at) VALUES(?,?,?,'intent',?,?,?,?)",
                            (operation, actor_id, kind, canonical(payload), digest(payload), reserve, time.time()))
            except sqlite3.IntegrityError as error:
                raise ConflictError("An admitted or uncertain native operation already owns this actor") from error
            con.execute("UPDATE project SET reserved=reserved+? WHERE id=?", (reserve, project["id"]))
            if actor["kind"] == "module":
                con.execute("UPDATE module SET state='working' WHERE owner=?", (actor_id,))
            con.execute("UPDATE actor SET state='working' WHERE id=?", (actor_id,))
            self.event(con, project["id"], "dispatch-intent", {"operation": operation, "actor": actor_id})
            return operation

    def operation(self, operation_id):
        con = self.connect()
        try:
            row = con.execute("SELECT * FROM operation WHERE id=?", (operation_id,)).fetchone()
            if not row:
                raise ContractError("Unknown operation")
            return dict(row)
        finally:
            con.close()

    def binding_intent(self, actor_id, payload):
        """Trusted host journals a root-session bind before external native IO."""
        with self.transaction() as con:
            actor = con.execute("SELECT * FROM actor WHERE id=?", (actor_id,)).fetchone()
            if not actor or actor["state"] == "revoked":
                raise AuthorityError("Unknown/revoked native binding target")
            project = con.execute("SELECT state FROM project WHERE id=?", (actor["project_id"],)).fetchone()
            if project["state"] != "active":
                raise AuthorityError("Cancelled project cannot create native bindings")
            operation = uuid.uuid4().hex
            try:
                con.execute("INSERT INTO operation(id,actor_id,kind,status,payload,payload_hash,reserve,at) VALUES(?,?,'binding','intent',?,?,0,?)",
                            (operation, actor_id, canonical(payload), digest(payload), time.time()))
            except sqlite3.IntegrityError as error:
                raise ConflictError("Prior native binding/execution is unsettled") from error
            self.event(con, actor["project_id"], "native-binding-intent", {"actor": actor_id, "operation": operation})
            return operation

    def queue_correction(self, master: Principal, module_key, instructions):
        """A correction the Master sends an owner that is not at work; it reads it on its next turn."""
        if not isinstance(instructions, str) or not instructions.strip():
            raise ContractError("A correction needs instructions")
        with self.transaction() as con:
            actor = self.authorize(con, master, "master")
            module = con.execute("SELECT * FROM module WHERE project_id=? AND key=?",
                                 (actor["project_id"], module_key)).fetchone()
            if not module:
                raise ContractError("Unknown module for a correction")
            if module["state"] not in DISPATCHABLE_MODULE_STATES:
                raise ConflictError("Only an owner that is not at work can be sent a queued correction")
            con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'correction',?,?)",
                        (uuid.uuid4().hex, module["owner"], canonical({"instructions": instructions}), time.time()))
            self.event(con, actor["project_id"], "module-correction-queued", {"module": module_key})

    def invalidate_module(self, master, module_key, reason, contract_edges=frozenset()):
        """New admitted revision, same owner; transitively invalidate old inputs.

        contract_edges holds (provider, consumer) pairs covered by an interface contract the consumer was
        built against: such consumers keep their sealed work and wait to be rebased, instead of a redo.
        """
        with self.transaction() as con:
            actor = self.authorize(con, master, "master")
            project = con.execute("SELECT spec FROM project WHERE id=?", (actor["project_id"],)).fetchone()
            spec = ProjectSpec.from_dict(json.loads(project["spec"]))
            if module_key not in {m.key for m in spec.modules} or not reason.strip():
                raise ContractError("Invalid module revision request")
            return self._invalidate(con, actor["project_id"], spec, (module_key,), reason, contract_edges)

    def _invalidate(self, con, project_id, spec, keys, reason, contract_edges=frozenset()):
        affected = set(keys)
        rebase = set()
        changed = True
        while changed:
            changed = False
            for module in spec.modules:
                if module.key in affected:
                    continue
                touched = set(module.dependencies) & (affected | rebase)
                if not touched:
                    continue
                if any((dependency, module.key) not in contract_edges for dependency in touched):
                    affected.add(module.key)
                    rebase.discard(module.key)
                    changed = True
                elif module.key not in rebase:
                    rebase.add(module.key)
                    changed = True
        waiting = []
        for key in sorted(rebase):
            row = con.execute('SELECT * FROM module WHERE project_id=? AND key=?', (project_id, key)).fetchone()
            # Sealed work built against the contract waits for the provider's next accepted version;
            # owners still at work submit provisionally because a dependency is no longer accepted.
            if row['current_candidate'] and row['state'] in ('accepted', 'awaiting_review', 'awaiting_evidence', 'awaiting_inputs'):
                con.execute("UPDATE module SET state='awaiting_inputs',accepted_candidate=NULL,retry_after=NULL "
                            "WHERE project_id=? AND key=?", (project_id, key))
                waiting.append(key)
        for key in affected:
            row = con.execute('SELECT * FROM module WHERE project_id=? AND key=?', (project_id, key)).fetchone()
            if row['state'] == 'blocked':
                raise ConflictError('Blocked work cannot be rearmed through dependency invalidation')
            con.execute("UPDATE module SET revision=revision+1,state='ready',accepted_candidate=NULL,"
                        "checkpoint=NULL,checkpoint_hash=NULL,continuations=0,retry_after=NULL "
                        "WHERE project_id=? AND key=?", (project_id, key))
            owner = f"{project_id}:module:{key}"
            con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'correction',?,?)", (uuid.uuid4().hex, owner, canonical({"instructions": reason, "input_revision_changed": True}), time.time()))
        self.event(con, project_id, "module-inputs-invalidated",
                   {"modules": sorted(affected), "reason": reason, **({"awaiting_inputs": waiting} if rebase else {})})
        return sorted(affected)

    def mark_started(self, operation_id, remote_id):
        if not remote_id:
            raise ContractError("Native turn id required")
        with self.transaction() as con:
            row = con.execute("SELECT * FROM operation WHERE id=?", (operation_id,)).fetchone()
            if not row or row["status"] not in {"intent", "unknown"}:
                raise ConflictError("Operation cannot transition to native running")
            con.execute("UPDATE operation SET status='running',remote_id=? WHERE id=?", (remote_id, operation_id))

    def note_remote_turn(self, operation_id, remote_id):
        """Record a started native turn id without changing the operation status."""
        with self.transaction() as con:
            con.execute("UPDATE operation SET remote_id=? WHERE id=? AND remote_id IS NULL "
                        "AND status IN ('intent','running','unknown','stopping')", (remote_id, operation_id))

    def mark_unknown(self, operation_id, reason):
        with self.transaction() as con:
            con.execute("UPDATE operation SET status='unknown',error=? WHERE id=? AND status IN ('intent','running','stopping')", (reason, operation_id))

    def record_usage(self, actor_id, cumulative_total: int, detail=None, *, cost_usd=None, price=None, cost_pending=False):
        """Record a cumulative native usage observation for one actor.

        detail is an optional cumulative per-field token breakdown; cost_usd
        an optional provider-reported cumulative estimate. cost_pending marks
        an observation whose provider reports cumulative cost when the turn
        ends, so its tokens are not counted as unpriced. Counter resets keep
        prior usage (no refund). Costs are estimates, never billing.
        """
        if not isinstance(cumulative_total, int) or isinstance(cumulative_total, bool) or cumulative_total < 0:
            raise ContractError("Invalid native usage counter")
        with self.transaction() as con:
            actor = con.execute("SELECT * FROM actor WHERE id=?", (actor_id,)).fetchone()
            if not actor:
                raise AuthorityError("Unknown usage actor")
            # On an observed counter reset, preserve recorded prior usage. It is
            # an accounting observation, not proof of an exact billing cap.
            delta = cumulative_total - actor["last_usage"] if cumulative_total >= actor["last_usage"] else cumulative_total
            con.execute("UPDATE actor SET last_usage=? WHERE id=?", (cumulative_total, actor_id))
            con.execute("UPDATE project SET spent=spent+? WHERE id=?", (delta, actor["project_id"]))
            con.execute("UPDATE operation SET usage=usage+? WHERE actor_id=? AND status IN ('intent','running','unknown','stopping')", (delta, actor_id))
            detail_delta = {}
            if isinstance(detail, dict):
                last = _json(actor["last_usage_detail"], {})
                current = {name: value for name, value in detail.items()
                           if isinstance(value, int) and not isinstance(value, bool) and value >= 0}
                for name, value in current.items():
                    previous = last.get(name, 0)
                    detail_delta[name] = value - previous if value >= previous else value
                con.execute("UPDATE actor SET last_usage_detail=? WHERE id=?", (canonical({**last, **current}), actor_id))
                if detail_delta:
                    project = con.execute("SELECT usage_detail FROM project WHERE id=?", (actor["project_id"],)).fetchone()
                    totals = _json(project["usage_detail"], {})
                    for name, value in detail_delta.items():
                        totals[name] = totals.get(name, 0) + value
                    con.execute("UPDATE project SET usage_detail=? WHERE id=?", (canonical(totals), actor["project_id"]))
                    for row in con.execute("SELECT id,usage_detail FROM operation WHERE actor_id=? AND status IN ('intent','running','unknown','stopping')", (actor_id,)).fetchall():
                        totals = _json(row["usage_detail"], {})
                        for name, value in detail_delta.items():
                            totals[name] = totals.get(name, 0) + value
                        con.execute("UPDATE operation SET usage_detail=? WHERE id=?", (canonical(totals), row["id"]))
            cost_delta = None
            if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool) and cost_usd >= 0:
                previous = actor["last_cost_usd"] or 0.0
                cost_delta = cost_usd - previous if cost_usd >= previous else cost_usd
                con.execute("UPDATE actor SET last_cost_usd=? WHERE id=?", (float(cost_usd), actor_id))
            elif price and detail_delta:
                total_input = detail_delta.get("inputTokens", 0)
                cached = detail_delta.get("cachedInputTokens", 0)
                written = detail_delta.get("cacheWriteInputTokens", 0)
                uncached = max(total_input - cached - written, 0)
                cost_delta = (uncached * price["input"] + cached * price.get("cached_input", price["input"])
                              + written * price.get("cache_write", price["input"])
                              + detail_delta.get("outputTokens", 0) * price["output"]) / 1_000_000
            if cost_delta is not None:
                con.execute("UPDATE project SET cost_usd=cost_usd+? WHERE id=?", (cost_delta, actor["project_id"]))
            elif delta and not cost_pending:
                con.execute("UPDATE project SET unpriced_tokens=unpriced_tokens+? WHERE id=?", (delta, actor["project_id"]))
            return delta

    def settle(self, operation_id, *, state="done", evidence: str, checkpoint=None):
        if state not in {"done", "failed", "cancelled"} or not evidence:
            raise ContractError("Terminal settlement requires actual runtime/effect evidence")
        with self.transaction() as con:
            row = con.execute("SELECT o.*,a.project_id,a.kind AS actor_kind FROM operation o JOIN actor a ON a.id=o.actor_id WHERE o.id=?", (operation_id,)).fetchone()
            if not row or row["status"] not in {"intent", "running", "unknown", "stopping"}:
                raise ConflictError("Operation already settled or unknown")
            con.execute("UPDATE operation SET status=?,error=? WHERE id=?", (state, evidence, operation_id))
            con.execute("UPDATE project SET reserved=reserved-? WHERE id=?", (row["reserve"], row["project_id"]))
            con.execute("UPDATE actor SET state='idle' WHERE id=?", (row["actor_id"],))
            if row["actor_kind"] == "module" and row["kind"] != "binding" and state != "done":
                con.execute("UPDATE module SET state='blocked' WHERE owner=?", (row["actor_id"],))
            if checkpoint and row["actor_kind"] == "module" and row["kind"] == "work":
                # The owner's files at a blocked stop carry into its next activation.
                con.execute("UPDATE module SET checkpoint=?,checkpoint_hash=? WHERE owner=?",
                            (checkpoint["path"], checkpoint["tree_hash"], row["actor_id"]))
            self.event(con, row["project_id"], "native-operation-settled", {"operation": operation_id, "state": state, "evidence": evidence})

    # ---- Owner decision requests ----

    def settle_decision(self, operation_id, *, request, evidence, checkpoint=None, max_decisions=3):
        """Settle an owner turn that ended with a decision request, keeping its files.

        The Master answers within the brief or escalates to the user. Past the
        per-module bound the question goes to the user directly.
        """
        question = str((request or {}).get("question") or "").strip()
        if not question or not evidence:
            raise ContractError("A decision request needs a question and settlement evidence")
        with self.transaction() as con:
            row = con.execute("SELECT o.*,a.project_id,a.kind AS actor_kind FROM operation o "
                              "JOIN actor a ON a.id=o.actor_id WHERE o.id=?", (operation_id,)).fetchone()
            if not row or row["actor_kind"] != "module" or row["kind"] != "work" or row["status"] not in ACTIVE_STATUSES:
                raise ConflictError("Only an unsettled module work turn can request a decision")
            module = con.execute("SELECT * FROM module WHERE owner=?", (row["actor_id"],)).fetchone()
            if module["state"] != "working":
                raise ConflictError("Module is not working; no decision can be requested")
            con.execute("UPDATE operation SET status='done',error=?,outcome='needs_decision' WHERE id=?",
                        (evidence, operation_id))
            con.execute("UPDATE project SET reserved=reserved-? WHERE id=?", (row["reserve"], row["project_id"]))
            con.execute("UPDATE actor SET state='idle' WHERE id=?", (row["actor_id"],))
            if checkpoint:
                con.execute("UPDATE module SET checkpoint=?,checkpoint_hash=? WHERE owner=?",
                            (checkpoint["path"], checkpoint["tree_hash"], row["actor_id"]))
            options = [str(item).strip() for item in (request.get("options") or []) if str(item).strip()][:10]
            recommendation = request.get("recommendation")
            pending = {"module": module["key"], "question": question, "options": options,
                       "recommendation": str(recommendation).strip() if recommendation else None,
                       "summary": str(request.get("summary") or ""), "operation": operation_id}
            decisions = (module["decisions"] or 0) + 1
            if decisions > max_decisions:
                state = "awaiting_user"
                pending.update(escalated_by="host", reason=f"decision limit of {max_decisions} for this module reached")
                con.execute("UPDATE module SET state=?,decisions=?,pending_question=?,failure_kind='decision_escalated',"
                            "failure_reason=?,recovery_action='user_answer',transient_failures=0,retry_after=NULL WHERE owner=?",
                            (state, decisions, canonical(pending), pending["reason"], row["actor_id"]))
                self.event(con, row["project_id"], "decision-escalated", pending)
            else:
                state = "awaiting_decision"
                con.execute("UPDATE module SET state=?,decisions=?,pending_question=?,failure_kind=NULL,failure_reason=NULL,"
                            "recovery_action='master_answer',transient_failures=0,retry_after=NULL WHERE owner=?",
                            (state, decisions, canonical(pending), row["actor_id"]))
                con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'decision_request',?,?)",
                            (uuid.uuid4().hex, f"{row['project_id']}:master", canonical(pending), time.time()))
                self.event(con, row["project_id"], "decision-requested", pending)
            self.event(con, row["project_id"], "native-operation-settled",
                       {"operation": operation_id, "state": "done", "evidence": evidence})
            return state

    @staticmethod
    def _ack_decision_requests(con, project_id, module_key):
        for item in con.execute("SELECT id,payload FROM message WHERE target=? AND kind='decision_request' "
                                "AND status='pending'", (f"{project_id}:master",)).fetchall():
            if _json(item["payload"], {}).get("module") == module_key:
                con.execute("UPDATE message SET status='acked' WHERE id=?", (item["id"],))

    def _deliver_answer(self, con, project_id, module_key, answer, rationale, source, expected, authorization=None):
        module = con.execute("SELECT * FROM module WHERE project_id=? AND key=?", (project_id, module_key)).fetchone()
        if not module:
            raise ContractError("Unknown module")
        if module["state"] not in expected:
            raise ConflictError(f"Module {module_key} has no pending question to answer")
        pending = _json(module["pending_question"], {})
        payload = {"question": pending.get("question"), "answer": answer, "rationale": rationale, "source": source}
        if authorization:
            payload["authorization"] = authorization
        con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'decision',?,?)",
                    (uuid.uuid4().hex, module["owner"], canonical(payload), time.time()))
        # The owner resumes the same session from the files it had when it asked.
        con.execute("UPDATE module SET state='continuing',pending_question=NULL,failure_kind=NULL,failure_reason=NULL,"
                    "recovery_action='decision_answered',transient_failures=0,retry_after=NULL "
                    "WHERE project_id=? AND key=?", (project_id, module_key))
        self._ack_decision_requests(con, project_id, module_key)
        self.event(con, project_id, "decision-answered", {"module": module_key, **payload})

    def answer_decision(self, master: Principal, module_key, *, answer, rationale):
        if not isinstance(answer, str) or not answer.strip():
            raise ContractError("A decision answer needs text")
        with self.transaction() as con:
            actor = self.authorize(con, master, "master")
            self._deliver_answer(con, actor["project_id"], module_key, answer.strip(), rationale, "master",
                                 ("awaiting_decision",))

    def escalate_decision(self, master: Principal, module_key, *, question_for_user, rationale, source="master"):
        with self.transaction() as con:
            actor = self.authorize(con, master, "master")
            module = con.execute("SELECT * FROM module WHERE project_id=? AND key=?",
                                 (actor["project_id"], module_key)).fetchone()
            if not module or module["state"] != "awaiting_decision":
                raise ConflictError(f"Module {module_key} has no pending question to escalate")
            pending = _json(module["pending_question"], {})
            pending.update(escalated_by=source, master_rationale=rationale,
                           question_for_user=(question_for_user or "").strip() or pending.get("question"))
            con.execute("UPDATE module SET state='awaiting_user',pending_question=?,failure_kind='decision_escalated',"
                        "failure_reason=?,recovery_action='user_answer' WHERE project_id=? AND key=?",
                        (canonical(pending), pending["question_for_user"], actor["project_id"], module_key))
            self._ack_decision_requests(con, actor["project_id"], module_key)
            self.event(con, actor["project_id"], "decision-escalated", pending)

    def actor_usage(self, actor_id):
        con = self.connect()
        try:
            return con.execute("SELECT coalesce(sum(usage),0) FROM operation WHERE actor_id=?", (actor_id,)).fetchone()[0]
        finally:
            con.close()

    def submit(self, principal: Principal, candidate_id, *, tree_hash, snapshot,
               inputs: dict, validation: list, summary: str, operation_id: str, provisional=False):
        """provisional seals work built against an interface contract before every dependency was accepted:
        it waits in awaiting_inputs, unvalidated, until the host rebases it onto the accepted inputs."""
        with self.transaction() as con:
            actor = self.authorize(con, principal, "module")
            module = con.execute("SELECT * FROM module WHERE owner=?", (actor["id"],)).fetchone()
            live = con.execute("SELECT 1 FROM operation WHERE actor_id=? AND status IN ('intent','running','unknown','stopping')", (actor["id"],)).fetchone()
            if live:
                raise ConflictError("Candidate cannot be submitted with unsettled owned execution")
            if module["state"] != "working":
                raise ConflictError("No completed admitted module work to submit")
            operation = con.execute("SELECT * FROM operation WHERE id=? AND actor_id=?", (operation_id, actor["id"])).fetchone()
            if not operation or operation["status"] != "done":
                raise ConflictError("Submission must bind completed admitted execution")
            admitted = json.loads(operation["payload"])
            if admitted["module_revision"] != module["revision"] or admitted["inputs"] != inputs:
                raise ConflictError("Submission changed the admitted mandate or input binding")
            con.execute("INSERT INTO candidate(id,project_id,module_key,owner,tree_hash,snapshot,inputs,mandate_revision,validation,summary,at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (candidate_id, actor["project_id"], module["key"], actor["id"], tree_hash, snapshot,
                         canonical(inputs), module["revision"], canonical(validation), summary, time.time()))
            # A submission ends the continuation chain: the sealed candidate
            # now carries the owner's work forward instead of a checkpoint.
            state = "awaiting_inputs" if provisional else "awaiting_review"
            con.execute("UPDATE module SET state=?,current_candidate=?,checkpoint=NULL,checkpoint_hash=NULL,"
                        "continuations=0,transient_failures=0,retry_after=NULL WHERE owner=?", (state, candidate_id, actor["id"]))
            if not provisional:
                msg = uuid.uuid4().hex
                con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'submission',?,?)",
                            (msg, f"{actor['project_id']}:master", canonical({"candidate_id": candidate_id, "module": module["key"]}), time.time()))
            self.event(con, actor["project_id"], "module-submitted",
                       {"candidate": candidate_id, "owner": actor["id"], **({"provisional": True} if provisional else {})})

    def rebase(self, master: Principal, module_key, candidate_id, *, inputs: dict, validation: list):
        """Bind a waiting candidate's sealed files to its now-accepted inputs as a new candidate for review.

        The host re-ran the module's admitted checks on the same sealed tree in the composed view of the
        accepted inputs. Code, owner, mandate and summary are unchanged; only the input binding and the
        validation are new, so review and evidence bind the inputs the checks actually used.
        """
        with self.transaction() as con:
            actor = self.authorize(con, master, "master")
            module = con.execute("SELECT * FROM module WHERE project_id=? AND key=?", (actor["project_id"], module_key)).fetchone()
            if not module or module["state"] != "awaiting_inputs" or not module["current_candidate"]:
                raise ConflictError("Only a sealed candidate waiting for its inputs can be rebased")
            for key, binding in inputs.items():
                dependency = con.execute("SELECT * FROM module WHERE project_id=? AND key=?", (actor["project_id"], key)).fetchone()
                if (not dependency or dependency["state"] != "accepted"
                        or binding != {"revision": dependency["revision"], "candidate": dependency["accepted_candidate"]}):
                    raise ConflictError("Rebase inputs are not the accepted current dependencies")
            spec = ProjectSpec.from_dict(json.loads(con.execute("SELECT spec FROM project WHERE id=?", (actor["project_id"],)).fetchone()["spec"]))
            if set(inputs) != set(next(m.dependencies for m in spec.modules if m.key == module_key)):
                raise ConflictError("Rebase must bind exactly the module's dependencies")
            old = con.execute("SELECT * FROM candidate WHERE id=?", (module["current_candidate"],)).fetchone()
            con.execute("INSERT INTO candidate(id,project_id,module_key,owner,tree_hash,snapshot,inputs,mandate_revision,validation,summary,at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (candidate_id, old["project_id"], old["module_key"], old["owner"], old["tree_hash"], old["snapshot"],
                         canonical(inputs), module["revision"], canonical(validation), old["summary"], time.time()))
            con.execute("UPDATE candidate SET state='rebased' WHERE id=?", (old["id"],))
            con.execute("UPDATE module SET state='awaiting_review',current_candidate=?,retry_after=NULL WHERE project_id=? AND key=?",
                        (candidate_id, actor["project_id"], module_key))
            con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'submission',?,?)",
                        (uuid.uuid4().hex, master.actor_id, canonical({"candidate_id": candidate_id, "module": module_key}), time.time()))
            self.event(con, actor["project_id"], "candidate-rebased",
                       {"module": module_key, "from": old["id"], "candidate": candidate_id, "inputs": inputs})

    def candidate(self, candidate_id):
        con = self.connect()
        try:
            row = con.execute("SELECT * FROM candidate WHERE id=?", (candidate_id,)).fetchone()
            if not row:
                raise ContractError("Unknown candidate")
            result = dict(row)
            result["inputs"] = json.loads(row["inputs"])
            result["validation"] = json.loads(row["validation"])
            return result
        finally:
            con.close()

    def review(self, master: Principal, candidate_id, decision, rationale, *, verified_tree_hash: str,
               failure_kind='implementation', details=None):
        if decision not in {"approve", "request_changes"} or not rationale.strip():
            raise ContractError("Review needs explicit decision and substantive rationale")
        if failure_kind not in {'none', 'implementation', 'evidence', 'uncertain_execution'}:
            raise ContractError('Unknown review failure class')
        if decision == 'approve': failure_kind = 'none'
        elif failure_kind == 'none': raise ContractError('Rejected work requires an actionable failure class')
        with self.transaction() as con:
            actor = self.authorize(con, master, "master")
            cand = con.execute("SELECT * FROM candidate WHERE id=?", (candidate_id,)).fetchone()
            if not cand or cand["project_id"] != actor["project_id"]:
                raise AuthorityError("Review target outside project")
            module = con.execute("SELECT * FROM module WHERE project_id=? AND key=?", (cand["project_id"], cand["module_key"])).fetchone()
            if (module["state"] != "awaiting_review" or module["current_candidate"] != candidate_id
                    or module["revision"] != cand["mandate_revision"] or cand["tree_hash"] != verified_tree_hash):
                raise ConflictError("Review does not bind the current immutable candidate/mandate")
            for dep, expected in json.loads(cand["inputs"]).items():
                row = con.execute("SELECT * FROM module WHERE project_id=? AND key=?", (cand["project_id"], dep)).fetchone()
                if (not row or row["state"] != "accepted" or row["revision"] != expected["revision"]
                        or row["accepted_candidate"] != expected["candidate"]):
                    raise ConflictError("Review inputs became stale")
            checks = json.loads(cand["validation"])
            if decision != 'approve' and any(x.get('status') != 'passed' for x in checks):
                failure_kind = 'implementation'
            if decision == "approve" and (not checks or any(x.get("status") != "passed" for x in checks)):
                raise ConflictError("Cannot approve without actual successful admitted validation")
            details = details if isinstance(details, dict) else None
            con.execute("INSERT INTO review(id,candidate_id,reviewer,decision,rationale,at,details) VALUES(?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, candidate_id, master.actor_id, decision, rationale, time.time(),
                         canonical(details) if details else None))
            con.execute("UPDATE candidate SET state=? WHERE id=?", ("approved" if decision == "approve" else "changes_requested", candidate_id))
            if decision == "approve":
                con.execute("UPDATE module SET state='accepted',accepted_candidate=?,failure_kind=NULL,failure_reason=NULL,recovery_action=NULL WHERE project_id=? AND key=?", (candidate_id, cand["project_id"], cand["module_key"]))
            else:
                spec_row = con.execute("SELECT spec FROM project WHERE id=?", (cand["project_id"],)).fetchone()
                spec = ProjectSpec.from_dict(json.loads(spec_row["spec"]))
                extra = module['operator_rounds'] or 0
                if failure_kind == 'evidence':
                    state = 'awaiting_evidence' if module['evidence_rounds'] < spec.max_evidence_rounds + extra else 'blocked'
                    action = 'collect_and_rerun_evidence' if state != 'blocked' else 'evidence_recovery_exhausted'
                    con.execute('UPDATE module SET state=?,evidence_rounds=evidence_rounds+1 WHERE project_id=? AND key=?', (state, cand['project_id'], cand['module_key']))
                elif failure_kind == 'uncertain_execution':
                    state, action = 'blocked', 'inspect_recorded_execution_outcomes'
                    con.execute('UPDATE module SET state=? WHERE project_id=? AND key=?', (state, cand['project_id'], cand['module_key']))
                else:
                    state = 'revision_requested' if module['rounds'] < spec.max_revision_rounds + extra else 'blocked'
                    action = 'same_owner_code_or_test_correction' if state != 'blocked' else 'implementation_revision_exhausted'
                    con.execute('UPDATE module SET state=?,rounds=rounds+1 WHERE project_id=? AND key=?', (state, cand['project_id'], cand['module_key']))
                    correction = {'candidate_id': candidate_id, 'instructions': rationale}
                    if details:
                        for name in ('blocking_issues', 'advisories'):
                            if details.get(name):
                                correction[name] = details[name]
                    con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'correction',?,?)",
                                (uuid.uuid4().hex, module['owner'], canonical(correction), time.time()))
                con.execute('UPDATE module SET failure_kind=?,failure_reason=?,recovery_action=? WHERE project_id=? AND key=?',
                            (failure_kind, rationale, action, cand['project_id'], cand['module_key']))
            self.event(con, cand["project_id"], "master-review", {"candidate": candidate_id, "decision": decision, 'failure_kind': failure_kind})

    def attach_evidence(self, master, candidate_id, *, path, evidence_hash, validation=None, recovered=False):
        """Only the host-bound Master can attach proof to the current tree.

        Recovery never replaces code, changes grants or refunds counters/usage.
        The controller verifies both immutable artifacts before this transition.
        """
        with self.transaction() as con:
            actor = self.authorize(con, master, 'master')
            candidate = con.execute('SELECT * FROM candidate WHERE id=?', (candidate_id,)).fetchone()
            if not candidate or candidate['project_id'] != actor['project_id']:
                raise AuthorityError('Evidence candidate is outside this project')
            module = con.execute('SELECT * FROM module WHERE project_id=? AND key=?', (candidate['project_id'], candidate['module_key'])).fetchone()
            expected = 'awaiting_evidence' if recovered else 'awaiting_review'
            if module['state'] != expected or module['current_candidate'] != candidate_id or module['revision'] != candidate['mandate_revision']:
                raise ConflictError('Evidence does not bind current pending work; terminal decisions cannot be reopened')
            for key, binding in json.loads(candidate['inputs']).items():
                dependency = con.execute('SELECT * FROM module WHERE project_id=? AND key=?', (candidate['project_id'], key)).fetchone()
                if dependency['state'] != 'accepted' or binding != {'candidate': dependency['accepted_candidate'], 'revision': dependency['revision']}:
                    raise ConflictError('Evidence refers to stale dependencies')
            checks = json.loads(candidate['validation']) if validation is None else validation
            previous = json.loads(candidate['validation'])
            if len(checks) != len(previous) or any(
                    (old.get('argv'), old.get('kind'), old.get('oracle')) !=
                    (new.get('argv'), new.get('kind'), new.get('oracle'))
                    for old, new in zip(previous, checks)):
                raise ConflictError('Evidence recovery cannot remove or replace admitted validation checks')
            con.execute("UPDATE candidate SET evidence=?,evidence_hash=?,validation=?,state='submitted' WHERE id=?", (path, evidence_hash, canonical(checks), candidate_id))
            if recovered: con.execute("UPDATE module SET state='awaiting_review' WHERE project_id=? AND key=?", (candidate['project_id'], candidate['module_key']))
            self.event(con, candidate['project_id'], 'candidate-evidence-attached', {'candidate': candidate_id, 'evidence_hash': evidence_hash, 'recovered': recovered})

    def pending_messages(self, actor_id):
        con = self.connect()
        try:
            return [dict(x) for x in con.execute("SELECT * FROM message WHERE target=? AND status='pending' ORDER BY at,id", (actor_id,))]
        finally:
            con.close()

    def acknowledge(self, principal: Principal, message_id):
        with self.transaction() as con:
            self.authorize(con, principal)
            changed = con.execute("UPDATE message SET status='acked' WHERE id=? AND target=? AND status='pending'", (message_id, principal.actor_id)).rowcount
            if not changed:
                raise AuthorityError("Message is not pending in this actor's inbox")

    def adopt_master(self, project_id):
        with self.transaction() as con:
            row = con.execute("SELECT * FROM actor WHERE id=?", (f"{project_id}:master",)).fetchone()
            if not row or not row["thread_id"]:
                raise AuthorityError("Master has no persistent native binding")
            con.execute("UPDATE actor SET epoch=epoch+1 WHERE id=?", (row["id"],))
            con.execute("UPDATE project SET epoch=epoch+1 WHERE id=?", (project_id,))
            self.event(con, project_id, "master-adopted-module-directory", {"epoch": row["epoch"] + 1})
            return Principal(row["id"], row["thread_id"], row["epoch"] + 1)

    def cancel_project(self, principal: Principal):
        with self.transaction() as con:
            actor = self.authorize(con, principal, "master")
            con.execute("UPDATE project SET state='cancelling' WHERE id=?", (actor["project_id"],))
            con.execute("UPDATE operation SET status='stopping' WHERE actor_id IN (SELECT id FROM actor WHERE project_id=?) AND status IN ('intent','running','unknown')", (actor["project_id"],))
            self.event(con, actor["project_id"], "project-cancellation-requested", {})

    def request_stop(self, project_id):
        """Request cancellation without requiring a native Master binding."""
        with self.transaction() as con:
            project = con.execute('SELECT state FROM project WHERE id=?', (project_id,)).fetchone()
            if not project:
                raise ContractError('Unknown project')
            if project['state'] in {'accepted', 'cancelled'}:
                return
            con.execute("UPDATE project SET state='cancelling' WHERE id=?", (project_id,))
            con.execute("UPDATE operation SET status='stopping' WHERE actor_id IN "
                        "(SELECT id FROM actor WHERE project_id=?) "
                        "AND status IN ('intent','running','unknown')", (project_id,))
            self.event(con, project_id, 'project-cancellation-requested', {})
            active = con.execute("SELECT 1 FROM operation WHERE actor_id IN "
                                 "(SELECT id FROM actor WHERE project_id=?) "
                                 "AND status IN ('intent','running','unknown','stopping') LIMIT 1",
                                 (project_id,)).fetchone()
            if not active:
                con.execute("UPDATE project SET state='cancelled' WHERE id=?", (project_id,))
                self.event(con, project_id, 'project-cancellation-settled',
                           {'basis': 'all owned operations settled/fenced'})

    def finalize_cancellation(self,project_id):
        with self.transaction() as con:
            project=con.execute('SELECT state FROM project WHERE id=?',(project_id,)).fetchone()
            active=con.execute("SELECT 1 FROM operation WHERE actor_id IN (SELECT id FROM actor WHERE project_id=?) AND status IN ('intent','running','unknown','stopping')",(project_id,)).fetchone()
            if project and project['state']=='cancelling' and not active:
                con.execute("UPDATE project SET state='cancelled' WHERE id=?",(project_id,))
                self.event(con,project_id,'project-cancellation-settled',{'basis':'all owned operations settled/fenced'})
                return True
            return False

    def record_integration(self, master, path, module_bindings, validation, *, user_gate=False, extra=None):
        """Record an integration outcome. With user_gate a passing integration
        waits for the user's acceptance instead of accepting the project."""
        with self.transaction() as con:
            actor = self.authorize(con, master, "master")
            modules = list(con.execute("SELECT * FROM module WHERE project_id=?", (actor["project_id"],)))
            if set(module_bindings) != {m["key"] for m in modules}:
                raise ConflictError("Integration must bind exactly the admitted modules")
            if any(m["state"] != "accepted" or module_bindings.get(m["key"]) != {
                    "candidate": m["accepted_candidate"], "revision": m["revision"]} for m in modules):
                raise ConflictError("Integration inputs are incomplete or stale")
            live = con.execute("SELECT 1 FROM operation WHERE actor_id IN (SELECT id FROM actor WHERE project_id=?) AND status IN ('intent','running','unknown','stopping')", (actor["project_id"],)).fetchone()
            if live:
                raise ConflictError("Integration has unsettled owned operations")
            passed = bool(validation) and all(x.get("status") == "passed" for x in validation)
            payload = {"path": path, "modules": module_bindings, "validation": validation, **(extra or {})}
            if passed and user_gate:
                self.event(con, actor["project_id"], "integration-passed", payload)
                con.execute("UPDATE project SET state='awaiting_acceptance' WHERE id=?", (actor["project_id"],))
                return
            self.event(con, actor["project_id"], "integration-accepted" if passed else "integration-failed", payload)
            if passed:
                con.execute("UPDATE project SET state='accepted' WHERE id=?", (actor["project_id"],))

    # ---- The user as final checker ----

    def operator_accept(self, project_id, *, authorization):
        """The user accepts the integrated project after inspecting it."""
        authorization = _require_authorization(authorization)
        with self.transaction() as con:
            project = con.execute("SELECT state FROM project WHERE id=?", (project_id,)).fetchone()
            if not project or project["state"] != "awaiting_acceptance":
                raise ConflictError("Project is not awaiting your acceptance")
            row = con.execute("SELECT payload FROM event WHERE project_id=? AND kind='integration-passed' "
                              "ORDER BY seq DESC LIMIT 1", (project_id,)).fetchone()
            if not row:
                raise ConflictError("No passed integration is recorded for acceptance")
            payload = json.loads(row["payload"])
            modules = list(con.execute("SELECT * FROM module WHERE project_id=?", (project_id,)))
            if any(m["state"] != "accepted" or payload["modules"].get(m["key"]) != {
                    "candidate": m["accepted_candidate"], "revision": m["revision"]} for m in modules):
                raise ConflictError("Modules changed after the integration passed; run the project again")
            self.event(con, project_id, "user-accepted", {"authorization": authorization, "path": payload["path"]})
            self.event(con, project_id, "integration-accepted",
                       {**payload, "accepted_by": "user", "authorization": authorization})
            con.execute("UPDATE project SET state='accepted' WHERE id=?", (project_id,))
            return payload

    def operator_reject(self, project_id, *, feedback, modules=(), authorization):
        """The user returns the integrated project with feedback.

        Named modules (and their dependents) go back to their owners now;
        without names the Master maps the feedback to modules on the next run.
        """
        authorization = _require_authorization(authorization)
        if not isinstance(feedback, str) or not feedback.strip():
            raise ContractError("A rejection needs feedback for the owners")
        with self.transaction() as con:
            project = con.execute("SELECT * FROM project WHERE id=?", (project_id,)).fetchone()
            if not project or project["state"] != "awaiting_acceptance":
                raise ConflictError("Project is not awaiting your acceptance")
            spec = ProjectSpec.from_dict(json.loads(project["spec"]))
            named = sorted(set(modules))
            unknown = sorted(set(named) - {m.key for m in spec.modules})
            if unknown:
                raise ContractError(f"Unknown modules: {', '.join(unknown)}")
            con.execute("UPDATE project SET state='active' WHERE id=?", (project_id,))
            affected = []
            if named:
                affected = self._invalidate(con, project_id, spec, named,
                                            "The user rejected the integrated project: " + feedback.strip())
                for key in named:
                    con.execute("UPDATE module SET operator_rounds=operator_rounds+1 WHERE project_id=? AND key=?",
                                (project_id, key))
            self.event(con, project_id, "user-rejected", {"feedback": feedback.strip(), "modules": named,
                                                          "affected": affected, "authorization": authorization})
            return affected

    def untriaged_rejection(self, project_id):
        """The latest user rejection that named no modules and has not been mapped yet."""
        con = self.connect()
        try:
            row = con.execute("SELECT kind,payload FROM event WHERE project_id=? AND kind IN "
                              "('user-rejected','user-feedback-triaged') ORDER BY seq DESC LIMIT 1",
                              (project_id,)).fetchone()
        finally:
            con.close()
        if row and row["kind"] == "user-rejected":
            payload = json.loads(row["payload"])
            if not payload.get("modules"):
                return payload
        return None

    def record_triage(self, master, *, affected, rationale, feedback):
        """Master maps unnamed user feedback to modules; none means back to the user."""
        with self.transaction() as con:
            actor = self.authorize(con, master, "master")
            project_id = actor["project_id"]
            project = con.execute("SELECT spec FROM project WHERE id=?", (project_id,)).fetchone()
            spec = ProjectSpec.from_dict(json.loads(project["spec"]))
            named = sorted(set(affected))
            if set(named) - {m.key for m in spec.modules}:
                raise ContractError("Triage named unknown modules")
            result = []
            if named:
                result = self._invalidate(con, project_id, spec, named,
                    f"The user rejected the integrated project: {feedback}\nMaster triage: {rationale}")
                for key in named:
                    con.execute("UPDATE module SET operator_rounds=operator_rounds+1 WHERE project_id=? AND key=?",
                                (project_id, key))
            elif con.execute("SELECT 1 FROM event WHERE project_id=? AND kind='integration-passed'",
                             (project_id,)).fetchone():
                # Nothing to change: the unchanged integration goes back to the user.
                con.execute("UPDATE project SET state='awaiting_acceptance' WHERE id=?", (project_id,))
            self.event(con, project_id, "user-feedback-triaged",
                       {"affected": result, "rationale": rationale, "feedback": feedback})
            return result

    def operator_answer(self, project_id, module_key, *, answer, authorization):
        """The user answers an owner's escalated (or still pending) question."""
        authorization = _require_authorization(authorization)
        if not isinstance(answer, str) or not answer.strip():
            raise ContractError("An answer needs text")
        with self.transaction() as con:
            project = con.execute("SELECT state FROM project WHERE id=?", (project_id,)).fetchone()
            if not project or project["state"] != "active":
                raise ConflictError("Only an active project's questions can be answered")
            self._deliver_answer(con, project_id, module_key, answer.strip(), "Answered by the user", "user",
                                 ("awaiting_user", "awaiting_decision"), authorization=authorization)

    def status(self, project_id):
        con = self.connect()
        try:
            con.execute('BEGIN')
            row = con.execute('SELECT * FROM project WHERE id=?', (project_id,)).fetchone()
            if not row: raise ContractError('Unknown project')
            project = dict(row)
            modules = [dict(x) for x in con.execute('SELECT * FROM module WHERE project_id=? ORDER BY key', (project_id,))]
            # Old stores retain their schema and verdict. Derive legacy cause
            # text for inspection without migrating/reclassifying their rows.
            for module in modules:
                if module['state'] != 'blocked' or module.get('failure_reason'): continue
                review = con.execute('SELECT r.rationale FROM review r JOIN candidate c ON c.id=r.candidate_id WHERE c.project_id=? AND c.module_key=? ORDER BY r.at DESC LIMIT 1', (project_id, module['key'])).fetchone()
                if review:
                    module.update(failure_kind='legacy_review_rejection', failure_reason=review['rationale'],
                                  recovery_action='inspect_preserved_rejection_no_automatic_rearming')
            questions = [{"module": module["key"], "state": module["state"], **_json(module.get("pending_question"), {})}
                         for module in modules if module["state"] in ("awaiting_decision", "awaiting_user")]
            role = {"master": 0, "owners": 0, "verifiers": 0}
            for usage, kind in con.execute("SELECT o.usage,a.kind FROM operation o JOIN actor a ON a.id=o.actor_id "
                                           "WHERE a.project_id=?", (project_id,)):
                role[{"master": "master", "verifier": "verifiers"}.get(kind, "owners")] += usage or 0
            integration = con.execute("SELECT kind,payload FROM event WHERE project_id=? AND kind IN "
                                      "('integration-passed','integration-accepted') ORDER BY seq DESC LIMIT 1",
                                      (project_id,)).fetchone()
            integration = ({"kind": integration["kind"], "path": _json(integration["payload"], {}).get("path"),
                            "report": _json(integration["payload"], {}).get("report")} if integration else None)
            return {"project_id": project_id, "state": project["state"],
                    "pending_questions": questions, "usage_by_role": role, "integration": integration,
                    "token_budget": json.loads(project['spec'])['token_budget'], "spent_tokens": project["spent"],
                    "usage_detail": _json(project.get("usage_detail"), {}),
                    "estimated_cost_usd": project.get("cost_usd", 0.0),
                    "unpriced_tokens": project.get("unpriced_tokens", 0),
                    "runtime_policy": _json(project.get("policy"), {}),
                    "admission": _admission_state(con, project_id),
                    'recovery_limits': {'implementation': json.loads(project['spec']).get('max_revision_rounds', 2),
                                        'evidence': json.loads(project['spec']).get('max_evidence_rounds', 2)},
                    "reserved_tokens": project["reserved"], "modules": modules,
                    "actors": [dict(x) for x in con.execute("SELECT * FROM actor WHERE project_id=? ORDER BY id", (project_id,))],
                    "operations": [dict(x) for x in con.execute("SELECT * FROM operation WHERE actor_id IN (SELECT id FROM actor WHERE project_id=?) ORDER BY at", (project_id,))]}
        finally:
            con.close()

    # ---- Stopped turns, continuations and transient native rejections ----

    def note_exit_observed(self, operation_id):
        with self.transaction() as con:
            con.execute("UPDATE operation SET exit_observed=1 WHERE id=?", (operation_id,))

    def settle_interrupted(self, operation_id, *, reason, evidence, instructions, checkpoint=None,
                           max_continuations=2, consume=True):
        """Settle a stopped native turn whose owned process exit was observed.

        The owner keeps its context; a bounded continuation carries its
        checkpointed files forward. Usage is never refunded. Returns the
        module state that results (or None for non-module operations).
        """
        if reason not in STOP_REASONS or not evidence:
            raise ContractError("Stopped-turn settlement needs a known reason and actual evidence")
        with self.transaction() as con:
            row = con.execute("SELECT o.*,a.project_id,a.kind AS actor_kind,a.module_key FROM operation o "
                              "JOIN actor a ON a.id=o.actor_id WHERE o.id=?", (operation_id,)).fetchone()
            if not row or row["status"] not in ACTIVE_STATUSES:
                raise ConflictError("Operation already settled or unknown")
            con.execute("UPDATE operation SET status='failed',error=?,exit_observed=1,outcome=? WHERE id=?",
                        (evidence, reason, operation_id))
            con.execute("UPDATE project SET reserved=reserved-? WHERE id=?", (row["reserve"], row["project_id"]))
            con.execute("UPDATE actor SET state='idle' WHERE id=?", (row["actor_id"],))
            state = None
            if row["actor_kind"] == "module" and row["kind"] == "work":
                module = con.execute("SELECT * FROM module WHERE owner=?", (row["actor_id"],)).fetchone()
                if checkpoint:
                    con.execute("UPDATE module SET checkpoint=?,checkpoint_hash=? WHERE owner=?",
                                (checkpoint["path"], checkpoint["tree_hash"], row["actor_id"]))
                if module["state"] == "working":
                    if not consume or module["continuations"] < max_continuations:
                        state = "continuing"
                        con.execute("UPDATE module SET state='continuing',continuations=continuations+?,failure_kind=?,"
                                    "failure_reason=?,recovery_action='automatic_continuation' WHERE owner=?",
                                    (1 if consume else 0, reason, evidence, row["actor_id"]))
                        con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'continuation',?,?)",
                                    (uuid.uuid4().hex, row["actor_id"], canonical({
                                        "instructions": instructions, "reason": reason,
                                        "checkpoint": bool(checkpoint), "operation": operation_id}), time.time()))
                    else:
                        state = "blocked"
                        con.execute("UPDATE module SET state='blocked',failure_kind=?,failure_reason=?,"
                                    "recovery_action='continuation_limit_reached_operator_rearm' WHERE owner=?",
                                    (reason, evidence, row["actor_id"]))
            self.event(con, row["project_id"], "native-turn-stopped", {
                "operation": operation_id, "actor": row["actor_id"], "reason": reason, "evidence": evidence,
                "checkpoint": checkpoint["tree_hash"] if checkpoint else None, "module_state": state,
                "usage_retained": row["usage"]})
            return state

    def release_rejected_dispatch(self, operation_id, *, reason, max_retries=3, backoff_seconds=30):
        """A definitive native refusal to start a turn: nothing ran, so release it.

        The module returns to its prior dispatchable state with exponential
        backoff, or blocks after the bounded number of rejections.
        """
        with self.transaction() as con:
            row = con.execute("SELECT o.*,a.project_id,a.kind AS actor_kind FROM operation o "
                              "JOIN actor a ON a.id=o.actor_id WHERE o.id=?", (operation_id,)).fetchone()
            if not row or row["status"] not in ("intent", "unknown"):
                raise ConflictError("Only an unstarted operation can be released after a definitive rejection")
            con.execute("UPDATE operation SET status='failed',error=?,outcome='dispatch_rejected' WHERE id=?",
                        ("native turn/start definitively rejected; no turn exists: " + reason, operation_id))
            con.execute("UPDATE project SET reserved=reserved-? WHERE id=?", (row["reserve"], row["project_id"]))
            con.execute("UPDATE actor SET state='idle' WHERE id=?", (row["actor_id"],))
            state = None
            if row["actor_kind"] == "module" and row["kind"] == "work":
                module = con.execute("SELECT * FROM module WHERE owner=?", (row["actor_id"],)).fetchone()
                failures = module["transient_failures"] + 1
                if failures <= max_retries:
                    state = _json(row["payload"], {}).get("module_state") or "ready"
                    if state not in DISPATCHABLE_MODULE_STATES:
                        state = "ready"
                    con.execute("UPDATE module SET state=?,transient_failures=?,retry_after=? WHERE owner=?",
                                (state, failures, time.time() + backoff_seconds * 2 ** (failures - 1), row["actor_id"]))
                else:
                    state = "blocked"
                    con.execute("UPDATE module SET state='blocked',transient_failures=?,failure_kind='native_rejected',"
                                "failure_reason=?,recovery_action='inspect_native_runtime_then_operator_rearm' WHERE owner=?",
                                (failures, reason, row["actor_id"]))
            self.event(con, row["project_id"], "native-dispatch-rejected",
                       {"operation": operation_id, "reason": reason, "module_state": state})
            return state

    def reject_unsubmittable(self, operation_id, *, reason, checkpoint=None, max_revision_rounds=2):
        """Return completed work that cannot be sealed (scope violation) to its owner."""
        with self.transaction() as con:
            row = con.execute("SELECT o.*,a.project_id,a.kind AS actor_kind FROM operation o "
                              "JOIN actor a ON a.id=o.actor_id WHERE o.id=?", (operation_id,)).fetchone()
            if not row or row["actor_kind"] != "module" or row["kind"] != "work" or row["status"] != "done":
                raise ConflictError("Only completed module work can be returned as unsubmittable")
            module = con.execute("SELECT * FROM module WHERE owner=?", (row["actor_id"],)).fetchone()
            if module["state"] != "working":
                raise ConflictError("Module is not awaiting publication of completed work")
            if checkpoint:
                con.execute("UPDATE module SET checkpoint=?,checkpoint_hash=? WHERE owner=?",
                            (checkpoint["path"], checkpoint["tree_hash"], row["actor_id"]))
            if module["rounds"] < max_revision_rounds + (module["operator_rounds"] or 0):
                state, action = "revision_requested", "same_owner_code_or_test_correction"
                con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'correction',?,?)",
                            (uuid.uuid4().hex, row["actor_id"], canonical({
                                "instructions": reason, "unsubmittable": True, "operation": operation_id}), time.time()))
            else:
                state, action = "blocked", "implementation_revision_exhausted"
            con.execute("UPDATE module SET state=?,rounds=rounds+1,failure_kind='implementation',failure_reason=?,"
                        "recovery_action=? WHERE owner=?", (state, reason, action, row["actor_id"]))
            self.event(con, row["project_id"], "candidate-unsubmittable",
                       {"operation": operation_id, "reason": reason, "module_state": state,
                        "checkpoint": checkpoint["tree_hash"] if checkpoint else None})
            return state

    def annotate_module(self, project_id, module_key, *, failure_kind, reason, action):
        with self.transaction() as con:
            con.execute("UPDATE module SET failure_kind=?,failure_reason=?,recovery_action=? WHERE project_id=? AND key=?",
                        (failure_kind, reason, action, project_id, module_key))
            self.event(con, project_id, "module-annotated",
                       {"module": module_key, "failure_kind": failure_kind, "reason": reason, "action": action})

    def note_review_unavailable(self, project_id, module_key, *, reason, max_retries=3, backoff_seconds=30):
        """A Master review could not complete; retry later or block after the bound.

        An unanswerable decision request goes to the user instead of blocking.
        """
        with self.transaction() as con:
            module = con.execute("SELECT * FROM module WHERE project_id=? AND key=?", (project_id, module_key)).fetchone()
            if not module or module["state"] not in ("awaiting_review", "awaiting_evidence", "awaiting_decision"):
                return None
            failures = module["transient_failures"] + 1
            if failures <= max_retries:
                state = module["state"]
                con.execute("UPDATE module SET transient_failures=?,retry_after=? WHERE project_id=? AND key=?",
                            (failures, time.time() + backoff_seconds * 2 ** (failures - 1), project_id, module_key))
            elif module["state"] == "awaiting_decision":
                state = "awaiting_user"
                pending = _json(module["pending_question"], {})
                pending.update(escalated_by="host", reason="The Master could not answer: " + reason)
                con.execute("UPDATE module SET state='awaiting_user',transient_failures=?,retry_after=NULL,pending_question=?,"
                            "failure_kind='decision_unavailable',failure_reason=?,recovery_action='user_answer' "
                            "WHERE project_id=? AND key=?",
                            (failures, canonical(pending), pending["reason"], project_id, module_key))
                self._ack_decision_requests(con, project_id, module_key)
                self.event(con, project_id, "decision-escalated", pending)
            else:
                state = "blocked"
                con.execute("UPDATE module SET state='blocked',transient_failures=?,failure_kind='review_unavailable',"
                            "failure_reason=?,recovery_action='operator_rearm_review' WHERE project_id=? AND key=?",
                            (failures, reason, project_id, module_key))
            self.event(con, project_id, "master-review-unavailable",
                       {"module": module_key, "reason": reason, "module_state": state})
            return state

    def clear_review_retry(self, project_id, module_key):
        with self.transaction() as con:
            con.execute("UPDATE module SET transient_failures=0,retry_after=NULL WHERE project_id=? AND key=?",
                        (project_id, module_key))

    def record_actor_runtime(self, actor_id, runtime):
        with self.transaction() as con:
            actor = con.execute("SELECT project_id FROM actor WHERE id=?", (actor_id,)).fetchone()
            if not actor:
                raise AuthorityError("Unknown actor")
            con.execute("UPDATE actor SET runtime=? WHERE id=?", (canonical(runtime), actor_id))
            self.event(con, actor["project_id"], "native-runtime-observed", {"actor": actor_id, **runtime})

    # ---- Master admission ----

    def admission_state(self, project_id):
        """'admitted', 'rejected' or 'pending', from journaled decisions only."""
        con = self.connect()
        try:
            return _admission_state(con, project_id)
        finally:
            con.close()

    def record_admission(self, project_id, *, decision, rationale, operation=None, source="master"):
        with self.transaction() as con:
            self.event(con, project_id, "master-project-admission",
                       {"decision": decision, "rationale": rationale, "operation": operation, "source": source})

    # ---- Journaled operator actions (trusted host; caller holds the project lease) ----

    def operator_resolve_operation(self, project_id, operation_id, *, outcome, authorization):
        """Close an operation a person has confirmed cannot be reconciled.

        Never infers a submission. Module work becomes blocked with an
        explicit rearm path; incurred usage stays recorded.
        """
        authorization = _require_authorization(authorization)
        if outcome not in ("failed", "cancelled"):
            raise ContractError("An operator can resolve an operation only as failed or cancelled")
        with self.transaction() as con:
            row = con.execute("SELECT o.*,a.project_id,a.kind AS actor_kind FROM operation o "
                              "JOIN actor a ON a.id=o.actor_id WHERE o.id=?", (operation_id,)).fetchone()
            if not row or row["project_id"] != project_id:
                raise ContractError("Unknown operation for this project")
            if row["status"] not in ACTIVE_STATUSES:
                raise ConflictError("Operation is already settled")
            con.execute("UPDATE operation SET status=?,error=?,outcome='operator_resolved' WHERE id=?",
                        (outcome, "operator-resolved: " + authorization, operation_id))
            con.execute("UPDATE project SET reserved=reserved-? WHERE id=?", (row["reserve"], project_id))
            con.execute("UPDATE actor SET state='idle' WHERE id=?", (row["actor_id"],))
            if row["actor_kind"] == "module" and row["kind"] == "work":
                con.execute("UPDATE module SET state='blocked',failure_kind='uncertain_execution',failure_reason=?,"
                            "recovery_action='operator_resolved_rearm_available' WHERE owner=? AND state='working'",
                            ("Operator resolved an unreconcilable operation: " + authorization, row["actor_id"]))
            self.event(con, project_id, "operator-resolved-operation", {
                "operation": operation_id, "previous_status": row["status"], "outcome": outcome,
                "authorization": authorization, "usage_retained": row["usage"], "reserve_released": row["reserve"]})
            project = con.execute("SELECT state FROM project WHERE id=?", (project_id,)).fetchone()
            active = con.execute("SELECT 1 FROM operation WHERE actor_id IN (SELECT id FROM actor WHERE project_id=?) "
                                 "AND status IN ('intent','running','unknown','stopping')", (project_id,)).fetchone()
            if project["state"] == "cancelling" and not active:
                con.execute("UPDATE project SET state='cancelled' WHERE id=?", (project_id,))
                self.event(con, project_id, "project-cancellation-settled", {"basis": "operator resolution of last operation"})

    def operator_rearm_module(self, project_id, module_key, *, authorization, extra_rounds=1):
        """Give a blocked module bounded additional attempts with the same owner/context."""
        authorization = _require_authorization(authorization)
        if not isinstance(extra_rounds, int) or isinstance(extra_rounds, bool) or not 1 <= extra_rounds <= 10:
            raise ContractError("Extra rounds must be an integer between 1 and 10")
        with self.transaction() as con:
            project = con.execute("SELECT state FROM project WHERE id=?", (project_id,)).fetchone()
            if not project or project["state"] != "active":
                raise ConflictError("Only an active project can be rearmed")
            module = con.execute("SELECT * FROM module WHERE project_id=? AND key=?", (project_id, module_key)).fetchone()
            if not module:
                raise ContractError("Unknown module")
            if module["state"] != "blocked":
                raise ConflictError("Only a blocked module can be rearmed")
            live = con.execute("SELECT 1 FROM operation WHERE actor_id=? AND status IN ('intent','running','unknown','stopping')",
                               (module["owner"],)).fetchone()
            if live:
                raise ConflictError("Resolve the owner's unsettled operation before rearming")
            kind = module["failure_kind"]
            if kind == "review_unavailable" and module["current_candidate"]:
                state = "awaiting_review"
            elif kind == "evidence" and module["current_candidate"]:
                state = "awaiting_evidence"
            elif module["checkpoint"]:
                state = "continuing"
            else:
                state = "revision_requested"
            con.execute("UPDATE module SET state=?,operator_rounds=operator_rounds+?,continuations=0,transient_failures=0,"
                        "retry_after=NULL,recovery_action='operator_rearmed' WHERE project_id=? AND key=?",
                        (state, extra_rounds, project_id, module_key))
            if state in ("revision_requested", "continuing"):
                con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'correction',?,?)",
                            (uuid.uuid4().hex, module["owner"], canonical({
                                "instructions": f"Operator rearmed this module ({authorization}). Previous stop: "
                                                f"{kind or 'blocked'}: {module['failure_reason'] or 'no recorded reason'}",
                                "operator_rearm": True}), time.time()))
            self.event(con, project_id, "operator-rearmed-module", {
                "module": module_key, "new_state": state, "failure_kind": kind, "failure_reason": module["failure_reason"],
                "authorization": authorization, "extra_rounds": extra_rounds})
            return state

    def operator_admission(self, project_id, *, decision, authorization):
        authorization = _require_authorization(authorization)
        if decision not in ("rerun", "override"):
            raise ContractError("Admission decision must be rerun or override")
        if self.admission_state(project_id) == "admitted":
            raise ConflictError("Project admission is already approved")
        with self.transaction() as con:
            project = con.execute("SELECT state FROM project WHERE id=?", (project_id,)).fetchone()
            if not project or project["state"] != "active":
                raise ConflictError("Only an active project can be admitted")
            kind = "operator-admission-rerun" if decision == "rerun" else "operator-admission-override"
            self.event(con, project_id, kind, {"authorization": authorization})

    def operator_set_budget(self, project_id, token_budget, *, authorization):
        authorization = _require_authorization(authorization)
        if not isinstance(token_budget, int) or isinstance(token_budget, bool) or token_budget < 1:
            raise ContractError("Token budget must be a positive integer")
        with self.transaction() as con:
            row = con.execute("SELECT * FROM project WHERE id=?", (project_id,)).fetchone()
            if not row or row["state"] != "active":
                raise ConflictError("Only an active project's envelope can change")
            if token_budget < row["spent"] + row["reserved"]:
                raise BudgetError("A new envelope cannot erase incurred usage or held reservations")
            data = json.loads(row["spec"])
            before = data["token_budget"]
            data["token_budget"] = token_budget
            revised = ProjectSpec.from_dict(data)
            con.execute("UPDATE project SET spec=?,spec_hash=? WHERE id=?",
                        (canonical(revised.to_dict()), revised.fingerprint, project_id))
            self.event(con, project_id, "operator-budget-revised", {
                "authorization": authorization, "before": before, "after": token_budget,
                "retained_spent": row["spent"], "retained_reserved": row["reserved"],
                "previous_spec_hash": row["spec_hash"], "new_spec_hash": revised.fingerprint})

    def operator_set_policy(self, project_id, updates, *, authorization):
        """Revise operational policy; never changes contract identity or bound actors' providers."""
        authorization = _require_authorization(authorization)
        if not isinstance(updates, dict) or not updates:
            raise ContractError("Policy updates must be a nonempty object")
        with self.transaction() as con:
            row = con.execute("SELECT * FROM project WHERE id=?", (project_id,)).fetchone()
            if not row or row["state"] != "active":
                raise ConflictError("Only an active project's policy can change")
            spec = ProjectSpec.from_dict(json.loads(row["spec"]))
            before = RuntimePolicy.from_dict(_json(row["policy"], {}))
            after = RuntimePolicy.from_dict({**before.to_dict(), **updates}).validate_for(spec)
            for actor in con.execute("SELECT * FROM actor WHERE project_id=? AND thread_id IS NOT NULL", (project_id,)):
                key = actor["module_key"]
                if before.runtime_for(spec, key).provider != after.runtime_for(spec, key).provider:
                    raise ConflictError(f"Actor {actor['id']} is bound to a {before.runtime_for(spec, key).provider} "
                                        "session; changing its provider would replace its context")
            con.execute("UPDATE project SET policy=? WHERE id=?", (canonical(after.to_dict()), project_id))
            self.event(con, project_id, "operator-policy-revised",
                       {"authorization": authorization, "before": before.to_dict(), "after": after.to_dict()})
            return after
