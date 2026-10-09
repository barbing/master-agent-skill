"""One project handoff starts the runtime; no user-mediated role packet loop."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import sqlite3

from .contracts import Principal,ProjectSpec, RuntimeErrorBase
from .controller import Controller
from .store import Store
from .backend import select_cli
from .recovery import reconcile
from .admission import load_handoff,describe_handoff
from .health import inspect_project_state


async def execute(store,project_id,cli):
    resolved=cli or await select_cli(model=store.project(project_id)['contract'].model)
    return await Controller(store,project_id,cli=resolved).run()


def accepted_result(store, project_id):
    """Return the persisted integration receipt without constructing a controller."""
    with store.connect() as con:
        row=con.execute("SELECT payload FROM event WHERE project_id=? AND kind='integration-accepted' ORDER BY seq DESC LIMIT 1",(project_id,)).fetchone()
    if not row:
        raise RuntimeErrorBase('Accepted project has no integration artifact')
    return {'state':'accepted',**json.loads(row['payload']),'original_project_mutated':False,'resumed_completed_project':True}


def main():
    parser = argparse.ArgumentParser(description="Opt-in persistent module runtime (initial P0/P1)")
    parser.add_argument("--state", required=True, type=Path, help="Protected runtime directory outside project input")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Admit the Strategy handoff/project envelope")
    init.add_argument("handoff", type=Path)
    for name in ("status", "health", "run", "stop", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("project_id")
        if name in ('run','reconcile'):
            command.add_argument("--codex", help="Exact CLI; otherwise inspect installed compatible runtimes")
    args = parser.parse_args()
    try:
        if args.command == "init":
            spec = load_handoff(args.handoff)
            store = Store(args.state / 'runtime.sqlite')
            result = {"project_id": store.create_project(spec), "state": "admitted",**describe_handoff(spec)}
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
                resolved=args.codex or await select_cli(model=store.project(args.project_id)['contract'].model)
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
                result = asyncio.run(execute(writable,args.project_id,args.codex))
        print(json.dumps(result, indent=2))
        return (0 if result.get('state') == 'accepted' else 1) if args.command == 'run' else 0
    except (RuntimeErrorBase, ValueError, OSError, KeyError, TypeError, sqlite3.DatabaseError) as error:
        print(json.dumps({"error": type(error).__name__, "detail": str(error),
                          "scope": "no approval/policy fallback; preserve uncertain operations"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
