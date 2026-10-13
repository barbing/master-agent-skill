"""One project handoff starts the runtime; no user-mediated role packet loop."""
from contextlib import closing
import argparse
import asyncio
import json
from pathlib import Path
import sys
import sqlite3

from .contracts import Principal,ProjectSpec, RuntimeErrorBase, ContractError, OPERATOR_POLICY
from .controller import Controller
from .store import Store
from .backend import select_cli, select_claude_cli
from .recovery import reconcile
from .admission import load_handoff_with_policy,describe_handoff
from .health import inspect_project_state
from .lease import project_lease


OPERATOR_COMMANDS = ('resolve', 'rearm', 'admission', 'budget', 'policy', 'answer', 'accept', 'reject')


async def resolve_clis(store, project_id, codex=None, claude=None):
    project = store.project(project_id)
    spec, policy = project['contract'], project['policy']
    providers = policy.providers(spec)
    # Validators always run under the Codex native sandbox; Codex actors need it too.
    resolved = codex or await select_cli(model=spec.model if 'codex' in providers else None)
    claude_cli = (claude or await select_claude_cli()) if 'claude' in providers else None
    return resolved, claude_cli


async def execute(store,project_id,cli,claude=None):
    resolved, claude_cli = await resolve_clis(store, project_id, cli, claude)
    return await Controller(store,project_id,cli=resolved,claude_cli=claude_cli).run()


def accepted_result(store, project_id):
    """Return the persisted integration receipt without constructing a controller."""
    with closing(store.connect()) as con:
        row=con.execute("SELECT payload FROM event WHERE project_id=? AND kind='integration-accepted' ORDER BY seq DESC LIMIT 1",(project_id,)).fetchone()
    if not row:
        raise RuntimeErrorBase('Accepted project has no integration artifact')
    return {'state':'accepted',**json.loads(row['payload']),'original_project_mutated':False,'resumed_completed_project':True}


def operator_action(args):
    """Journaled human decisions; refused while a controller holds the project."""
    database = args.state / 'runtime.sqlite'
    Store.open_readonly(database).status(args.project_id)
    store = Store(database)
    with project_lease(store.path.parent / 'projects' / args.project_id / 'controller.lock'):
        if args.command == 'resolve':
            store.operator_resolve_operation(args.project_id, args.operation, outcome=args.outcome,
                                             authorization=args.authorization)
            if args.rearm:
                operation = store.operation(args.operation)
                actor = store.actor(operation['actor_id'])
                if actor['module_key']:
                    store.operator_rearm_module(args.project_id, actor['module_key'], authorization=args.authorization)
        elif args.command == 'rearm':
            store.operator_rearm_module(args.project_id, args.module, authorization=args.authorization,
                                        extra_rounds=args.rounds)
        elif args.command == 'admission':
            store.operator_admission(args.project_id, decision=args.decision, authorization=args.authorization)
        elif args.command == 'budget':
            store.operator_set_budget(args.project_id, args.tokens, authorization=args.authorization)
        elif args.command == 'policy':
            try:
                updates = json.loads(args.updates)
            except ValueError as error:
                raise ContractError(f'Policy updates must be a JSON object: {error}') from error
            store.operator_set_policy(args.project_id, updates, authorization=args.authorization)
        elif args.command == 'answer':
            store.operator_answer(args.project_id, args.module, answer=args.text, authorization=args.authorization)
        elif args.command == 'accept':
            store.operator_accept(args.project_id, authorization=args.authorization)
        elif args.command == 'reject':
            store.operator_reject(args.project_id, feedback=args.feedback, modules=args.module or (),
                                  authorization=args.authorization)
    status = Store.open_readonly(database).status(args.project_id)
    return {'state': status['state'], 'operator_action': args.command, 'status': status,
            'health': inspect_project_state(Store.open_readonly(database), args.project_id)}


def main():
    parser = argparse.ArgumentParser(description="Opt-in persistent module runtime")
    parser.add_argument("--state", required=True, type=Path, help="Protected runtime directory outside project input")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Admit the Strategy handoff/project envelope")
    init.add_argument("handoff", type=Path)
    for name in ("status", "health", "run", "stop", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("project_id")
        if name in ('run','reconcile'):
            command.add_argument("--codex", help="Exact Codex CLI; otherwise inspect installed compatible runtimes")
            command.add_argument("--claude", help="Exact Claude Code CLI for Claude actors")
    resolve = commands.add_parser('resolve', help='Operator: close an unreconcilable operation without inferring results')
    resolve.add_argument('project_id'); resolve.add_argument('--operation', required=True)
    resolve.add_argument('--outcome', choices=('failed', 'cancelled'), default='failed')
    resolve.add_argument('--rearm', action='store_true', help='Also rearm the owning module with one more attempt')
    rearm = commands.add_parser('rearm', help='Operator: give a blocked module more attempts with the same owner/context')
    rearm.add_argument('project_id'); rearm.add_argument('--module', required=True)
    rearm.add_argument('--rounds', type=int, default=1)
    admission = commands.add_parser('admission', help='Operator: rerun or override a rejected Master admission')
    admission.add_argument('project_id'); admission.add_argument('--decision', choices=('rerun', 'override'), required=True)
    budget = commands.add_parser('budget', help='Operator: change the soft token envelope (never refunds usage)')
    budget.add_argument('project_id'); budget.add_argument('--tokens', type=int, required=True)
    policy = commands.add_parser('policy', help='Operator: revise runtime policy (JSON object of updates)')
    policy.add_argument('project_id'); policy.add_argument('--set', dest='updates', required=True)
    answer = commands.add_parser('answer', help="Answer an owner's question that waits for you")
    answer.add_argument('project_id'); answer.add_argument('--module', required=True)
    answer.add_argument('--text', required=True)
    accept = commands.add_parser('accept', help='Accept the integrated project after inspecting it')
    accept.add_argument('project_id')
    reject = commands.add_parser('reject', help='Return the integrated project with feedback for its owners')
    reject.add_argument('project_id'); reject.add_argument('--feedback', required=True)
    reject.add_argument('--module', action='append',
                        help='Module to send the feedback to (repeatable); without it the Master decides')
    for command in (resolve, rearm, admission, budget, policy, answer, accept, reject):
        command.add_argument('--authorization', required=True, help='Human reason recorded in the journal')
    args = parser.parse_args()
    try:
        if args.command == "init":
            # New projects run in operator mode unless their runtime_policy says otherwise.
            spec, runtime_policy = load_handoff_with_policy(args.handoff, defaults=OPERATOR_POLICY)
            store = Store(args.state / 'runtime.sqlite')
            result = {"project_id": store.create_project(spec, runtime_policy), "state": "admitted",
                      **describe_handoff(spec), "runtime_policy": runtime_policy.to_dict()}
        elif args.command in OPERATOR_COMMANDS:
            result = operator_action(args)
        else:
            database=args.state / 'runtime.sqlite'
            if not database.is_file():
                raise ValueError('Existing runtime state is required; init admits a new project')
            store = Store.open_readonly(database)
        if args.command == "status":
            result = store.status(args.project_id)
        elif args.command=='health':
            result=inspect_project_state(store,args.project_id)
        elif args.command == 'stop':
            current=Store.open_readonly(args.state / 'runtime.sqlite').status(args.project_id)
            if current['state'] not in {'accepted','cancelled'}:
                store=Store(args.state / 'runtime.sqlite')
                store.request_stop(args.project_id)
                current=Store.open_readonly(args.state / 'runtime.sqlite').status(args.project_id)
            status=current
            result={'state':status['state'],'status':status}
        elif args.command=='reconcile':
            # Validate the existing database and project before the writable
            # Store constructor can apply its normal schema/journal setup.
            store.status(args.project_id)
            store=Store(args.state / 'runtime.sqlite')
            async def recover():
                resolved, _ = await resolve_clis(store, args.project_id, args.codex, args.claude)
                return await reconcile(store,args.project_id,cli=resolved)
            result=asyncio.run(recover())
        elif args.command == 'run':
            current=store.status(args.project_id)
            if current['state']=='accepted':
                result=accepted_result(store,args.project_id)
            elif current['state']!='active':
                result={'state':current['state'],'status':current}
            else:
                writable=Store(args.state / 'runtime.sqlite')
                result = asyncio.run(execute(writable,args.project_id,args.codex,args.claude))
        print(json.dumps(result, indent=2))
        return (0 if result.get('state') in ('accepted', 'awaiting_acceptance') else 1) if args.command == 'run' else 0
    except (RuntimeErrorBase, ValueError, OSError, KeyError, TypeError, sqlite3.DatabaseError) as error:
        print(json.dumps({"error": type(error).__name__, "detail": str(error),
                          "scope": "no approval/policy fallback; preserve uncertain operations"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
