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
                        Principal, ProjectSpec, canonical, digest)


SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
 id TEXT PRIMARY KEY, spec TEXT NOT NULL, spec_hash TEXT NOT NULL,
 epoch INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'active',
 spent INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS actor (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES project(id),
 kind TEXT NOT NULL CHECK(kind IN ('master','module')), module_key TEXT,
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
                                    'failure_kind': 'TEXT', 'failure_reason': 'TEXT', 'recovery_action': 'TEXT'},
                         'candidate': {'evidence': 'TEXT', 'evidence_hash': 'TEXT'}}
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

    def create_project(self, spec: ProjectSpec) -> str:
        project_id = uuid.uuid4().hex
        master = f"{project_id}:master"
        with self.transaction() as con:
            con.execute("INSERT INTO project(id,spec,spec_hash) VALUES(?,?,?)",
                        (project_id, canonical(spec.to_dict()), spec.fingerprint))
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

    def dispatch(self, master: Principal, actor_id: str, message: str, *, kind="work") -> str:
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
                    if dep["state"] != "accepted":
                        raise ConflictError("Required module interface is not accepted/current")
                if module["state"] not in {"ready", "revision_requested"}:
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

    def invalidate_module(self, master, module_key, reason):
        """New admitted revision, same owner; transitively invalidate old inputs."""
        with self.transaction() as con:
            actor = self.authorize(con, master, "master")
            project = con.execute("SELECT spec FROM project WHERE id=?", (actor["project_id"],)).fetchone()
            spec = ProjectSpec.from_dict(json.loads(project["spec"]))
            if module_key not in {m.key for m in spec.modules} or not reason.strip():
                raise ContractError("Invalid module revision request")
            affected = {module_key}
            while True:
                expanded = affected | {m.key for m in spec.modules if set(m.dependencies) & affected}
                if expanded == affected:
                    break
                affected = expanded
            for key in affected:
                row = con.execute('SELECT * FROM module WHERE project_id=? AND key=?', (actor['project_id'], key)).fetchone()
                if row['state'] == 'blocked':
                    raise ConflictError('Blocked work cannot be rearmed through dependency invalidation')
                con.execute("UPDATE module SET revision=revision+1,state='ready',accepted_candidate=NULL WHERE project_id=? AND key=?", (actor["project_id"], key))
                owner = f"{actor['project_id']}:module:{key}"
                con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'correction',?,?)", (uuid.uuid4().hex, owner, canonical({"instructions": reason, "input_revision_changed": True}), time.time()))
            self.event(con, actor["project_id"], "module-inputs-invalidated", {"modules": sorted(affected), "reason": reason})
            return sorted(affected)

    def mark_started(self, operation_id, remote_id):
        if not remote_id:
            raise ContractError("Native turn id required")
        with self.transaction() as con:
            row = con.execute("SELECT * FROM operation WHERE id=?", (operation_id,)).fetchone()
            if not row or row["status"] not in {"intent", "unknown"}:
                raise ConflictError("Operation cannot transition to native running")
            con.execute("UPDATE operation SET status='running',remote_id=? WHERE id=?", (remote_id, operation_id))

    def mark_unknown(self, operation_id, reason):
        with self.transaction() as con:
            con.execute("UPDATE operation SET status='unknown',error=? WHERE id=? AND status IN ('intent','running','stopping')", (reason, operation_id))

    def record_usage(self, actor_id, cumulative_total: int):
        if not isinstance(cumulative_total, int) or cumulative_total < 0:
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
            return delta

    def settle(self, operation_id, *, state="done", evidence: str):
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
            self.event(con, row["project_id"], "native-operation-settled", {"operation": operation_id, "state": state, "evidence": evidence})

    def submit(self, principal: Principal, candidate_id, *, tree_hash, snapshot,
               inputs: dict, validation: list, summary: str, operation_id: str):
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
            con.execute("INSERT INTO candidate(id,project_id,module_key,owner,tree_hash,snapshot,inputs,mandate_revision,validation,summary) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (candidate_id, actor["project_id"], module["key"], actor["id"], tree_hash, snapshot,
                         canonical(inputs), module["revision"], canonical(validation), summary))
            con.execute("UPDATE module SET state='awaiting_review',current_candidate=? WHERE owner=?", (candidate_id, actor["id"]))
            msg = uuid.uuid4().hex
            con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'submission',?,?)",
                        (msg, f"{actor['project_id']}:master", canonical({"candidate_id": candidate_id, "module": module["key"]}), time.time()))
            self.event(con, actor["project_id"], "module-submitted", {"candidate": candidate_id, "owner": actor["id"]})

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
               failure_kind='implementation'):
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
            con.execute("INSERT INTO review(id,candidate_id,reviewer,decision,rationale,at) VALUES(?,?,?,?,?,?)",
                        (uuid.uuid4().hex, candidate_id, master.actor_id, decision, rationale, time.time()))
            con.execute("UPDATE candidate SET state=? WHERE id=?", ("approved" if decision == "approve" else "changes_requested", candidate_id))
            if decision == "approve":
                con.execute("UPDATE module SET state='accepted',accepted_candidate=?,failure_kind=NULL,failure_reason=NULL,recovery_action=NULL WHERE project_id=? AND key=?", (candidate_id, cand["project_id"], cand["module_key"]))
            else:
                spec_row = con.execute("SELECT spec FROM project WHERE id=?", (cand["project_id"],)).fetchone()
                spec = ProjectSpec.from_dict(json.loads(spec_row["spec"]))
                if failure_kind == 'evidence':
                    state = 'awaiting_evidence' if module['evidence_rounds'] < spec.max_evidence_rounds else 'blocked'
                    action = 'collect_and_rerun_evidence' if state != 'blocked' else 'evidence_recovery_exhausted'
                    con.execute('UPDATE module SET state=?,evidence_rounds=evidence_rounds+1 WHERE project_id=? AND key=?', (state, cand['project_id'], cand['module_key']))
                elif failure_kind == 'uncertain_execution':
                    state, action = 'blocked', 'inspect_recorded_execution_outcomes'
                    con.execute('UPDATE module SET state=? WHERE project_id=? AND key=?', (state, cand['project_id'], cand['module_key']))
                else:
                    state = 'revision_requested' if module['rounds'] < spec.max_revision_rounds else 'blocked'
                    action = 'same_owner_code_or_test_correction' if state != 'blocked' else 'implementation_revision_exhausted'
                    con.execute('UPDATE module SET state=?,rounds=rounds+1 WHERE project_id=? AND key=?', (state, cand['project_id'], cand['module_key']))
                    con.execute("INSERT INTO message(id,target,kind,payload,at) VALUES(?,?,'correction',?,?)",
                                (uuid.uuid4().hex, module['owner'], canonical({'candidate_id': candidate_id, 'instructions': rationale}), time.time()))
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

    def record_integration(self, master, path, module_bindings, validation):
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
            self.event(con, actor["project_id"], "integration-accepted" if passed else "integration-failed", {
                "path": path, "modules": module_bindings, "validation": validation})
            if passed:
                con.execute("UPDATE project SET state='accepted' WHERE id=?", (actor["project_id"],))

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
            return {"project_id": project_id, "state": project["state"],
                    "token_budget": json.loads(project['spec'])['token_budget'], "spent_tokens": project["spent"],
                    'recovery_limits': {'implementation': json.loads(project['spec']).get('max_revision_rounds', 2),
                                        'evidence': json.loads(project['spec']).get('max_evidence_rounds', 2)},
                    "reserved_tokens": project["reserved"], "modules": modules,
                    "actors": [dict(x) for x in con.execute("SELECT * FROM actor WHERE project_id=? ORDER BY id", (project_id,))],
                    "operations": [dict(x) for x in con.execute("SELECT * FROM operation WHERE actor_id IN (SELECT id FROM actor WHERE project_id=?) ORDER BY at", (project_id,))]}
        finally:
            con.close()
