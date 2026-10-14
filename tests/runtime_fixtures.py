"""Scripted synthetic actors for control-loop tests; never live-model evidence."""
from contextlib import closing
import asyncio
import json
from pathlib import Path

from master_agent_runtime import ModuleSpec, ProjectSpec, Store, ValidationStep
from master_agent_runtime.contracts import RuntimePolicy
from master_agent_runtime.native import NativeRejected


CHECK = (ValidationStep(('python', 'frozen-oracle.py')),)


def fixture_validator(steps, candidate, context, evidence, **kwargs):
    """Independent read of the sealed files: every .py file must hold value 42."""
    files = list(Path(candidate).rglob('*.py'))
    passed = bool(files) and all('value = 42' in path.read_text() for path in files)
    return [{'status': 'passed' if passed else 'failed', 'oracle': 'frozen synthetic fixture requirement',
             'exit_code': 0 if passed else 1}]


def make_project(root, modules, *, budget=100000, policy=None, **spec_fields):
    """modules: iterable of (key, dependencies). Each owns <key>.py."""
    root = Path(root)
    project = root / 'project'
    project.mkdir(exist_ok=True)
    specs = []
    for key, dependencies in modules:
        (project / f'{key}.py').write_text('value = -1\n')
        specs.append(ModuleSpec(key, f'Provide value 42 in {key}.py', (f'{key}.py',), tuple(dependencies),
                                token_reservation=100, validation=CHECK))
    spec = ProjectSpec(str(project), 'Deliver every fixture module', tuple(specs), budget,
                       integration_validation=CHECK, **spec_fields)
    store = Store(root / 'state' / 'runtime.sqlite')
    return store, store.create_project(spec, RuntimePolicy.from_dict(policy or {}))


def default_master(actor, number, message, schema):
    required = (schema or {}).get('required', [])
    if 'action' in required:
        return {'action': 'answer', 'answer': 'Use the first option', 'rationale': 'The brief settles it',
                'question_for_user': None}
    if required == ['affected_modules', 'rationale']:
        return {'affected_modules': [], 'rationale': 'The feedback needs no file change'}
    if 'Review initial project admission' in message:
        return {'decision': 'approve', 'rationale': 'Feasible bounded mandates'}
    failed = '"status": "failed"' in message
    output = {'decision': 'request_changes' if failed else 'approve',
              'rationale': 'Replace wrong value with 42' if failed else 'Sealed files meet the fixture goal',
              'failure_kind': 'implementation' if failed else 'none'}
    if 'blocking_issues' in required:
        output['blocking_issues'] = ([{'obligation': 'check:0', 'evidence': 'frozen oracle log',
                                       'required_change': 'Replace wrong value with 42'}] if failed else [])
        output['advisories'] = []
    if 'previous_issues' in required:
        output['previous_issues'] = []
    if 'affected_modules' in required:
        output['affected_modules'] = []
    return output


def default_owner(actor, number, message):
    return {'write': {f'{actor.key}.py': 'value = 42\n'}}


class ScriptedActor:
    """Per-actor scripts decide each turn. Behaviour keys for owners:
    write {path: text}, sleep seconds, usage total, hang (wait for interrupt),
    raise exception, reject_start, text raw output, status submitted|needs_decision|blocked,
    and question/options/recommendation for a decision request."""
    owner_script = staticmethod(default_owner)
    master_script = staticmethod(default_master)
    log = []
    opens = []
    starts = {}
    workspaces = {}
    instructions = {}

    @classmethod
    def reset(cls):
        cls.owner_script = staticmethod(default_owner)
        cls.master_script = staticmethod(default_master)
        cls.log, cls.opens, cls.starts, cls.workspaces, cls.instructions = [], [], {}, {}, {}

    def __init__(self, actor_id, workspace, context, control, *, kind, **kwargs):
        self.actor_id = actor_id
        self.kind = kind
        # A verifier runs under the Master's readonly profile and answers through master_script.
        self.key = ('verifier:' + actor_id.rsplit(':', 1)[-1] if ':verifier:' in actor_id
                    else 'master' if kind == 'master' else actor_id.rsplit(':', 1)[-1])
        self.workspace = Path(workspace)
        self.thread_id = self.session_id = None
        self.interrupted = False
        self.workspaces.setdefault(self.key, []).append(self.workspace)

    async def open(self, *, existing_thread=None, instructions=''):
        self.thread_id = self.session_id = existing_thread or 'thread-' + self.actor_id
        self.opens.append((self.key, existing_thread))
        self.instructions[self.key] = instructions
        return {'thread': {'id': self.thread_id, 'sessionId': self.session_id}}

    async def start_turn(self, message, output_schema=None, client_message_id=None, **kwargs):
        self.message, self.schema = message, output_schema
        self.number = self.starts.get(self.key, 0) + 1
        self.starts[self.key] = self.number
        if self.kind == 'module':
            self.behaviour = type(self).owner_script(self, self.number, message) or {}
            if self.behaviour.get('reject_start'):
                self.log.append((self.key, self.number, 'rejected'))
                raise NativeRejected('synthetic definitive turn/start rejection')
        return f'{self.key}-turn-{self.number}'

    async def await_turn(self, turn_id, *, on_usage=None):
        if self.kind == 'master':
            output = dict(type(self).master_script(self, self.number, self.message, self.schema))
            delay = output.pop('__sleep__', None)  # a slow Master turn, for scheduling tests
            if delay:
                await asyncio.sleep(delay)
            self.log.append((self.key, self.number, output.get('decision')))
            if on_usage:
                on_usage(10 * self.number)
            return {'text': json.dumps(output), 'turn': {'id': turn_id, 'status': 'completed'}}
        behaviour = self.behaviour
        self.log.append((self.key, self.number, 'started'))
        for path, text in (behaviour.get('write') or {}).items():
            target = self.workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        if on_usage and behaviour.get('usage') is not None:
            on_usage(behaviour['usage'])
        if behaviour.get('sleep'):
            await asyncio.sleep(behaviour['sleep'])
        if behaviour.get('hang'):
            while not self.interrupted:
                await asyncio.sleep(0.02)
            self.log.append((self.key, self.number, 'interrupted'))
            raise NativeRejected('interrupted')
        if behaviour.get('raise') is not None:
            raise behaviour['raise']
        self.log.append((self.key, self.number, 'finished'))
        if 'text' in behaviour:
            return {'text': behaviour['text'], 'turn': {'id': turn_id, 'status': 'completed'}}
        output = {'status': behaviour.get('status', 'submitted'),
                  'summary': 'Synthetic owner wrote real fixture files', 'issues': []}
        output.update({key: behaviour[key] for key in ('question', 'options', 'recommendation') if key in behaviour})
        return {'text': json.dumps(output), 'turn': {'id': turn_id, 'status': 'completed'}}

    async def interrupt(self, turn_id):
        self.interrupted = True
        return {}

    async def close(self):
        return {'process_exited': True, 'synthetic_actor': True}


def events(store, project_id, kind=None):
    with closing(store.connect()) as con:
        rows = con.execute('SELECT kind,payload FROM event WHERE project_id=? ORDER BY seq', (project_id,)).fetchall()
    return [(row['kind'], json.loads(row['payload'])) for row in rows if kind is None or row['kind'] == kind]
