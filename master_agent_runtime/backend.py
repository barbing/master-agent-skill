"""Choose an existing compatible CLI while preserving the requested model."""
from pathlib import Path

from . import oscompat
from .contracts import ContractError
from .native import JsonRpcClient,ModelUnavailable
from .sandbox import sandbox_prefix


def native_cli(path):
    """An explicit --codex/--claude path as the runtime will run it; shims are resolved or refused."""
    native = oscompat.native_executable(path)
    if native is None:
        raise ContractError(oscompat.shim_refusal(path))
    return native


async def select_cli(*,model=None,candidates=None):
    paths = candidates if candidates is not None else oscompat.codex_candidates()
    failures=[];seen=set()
    for path in paths:
        if not path or not Path(path).is_file() or str(Path(path).resolve()) in seen:
            continue
        seen.add(str(Path(path).resolve()))
        if oscompat.is_shim(path):
            failures.append(oscompat.shim_refusal(path));continue
        client=JsonRpcClient((str(path),'app-server','--stdio'))
        try:
            sandbox_prefix(str(path))
            await client.start();await client.initialize()
            config=(await client.call('config/read',{'includeLayers':False})).get('config',{})
            requested=model or config.get('model')
            if (config.get('model_provider') or 'openai') == 'openai':
                data=(await client.call('model/list',{})).get('data',[])
                available={item.get('model') or item.get('id') for item in data}
                if requested and requested not in available:
                    failures.append(f'{path}: configured model {requested} is unavailable');continue
            return str(path)
        except Exception as error:
            failures.append(f'{path}: {type(error).__name__}: {error}')
        finally:
            await client.close()
    raise ModelUnavailable('No existing native CLI supports the configured project. '+ '; '.join(failures))


async def select_claude_cli(*,candidates=None):
    """Choose an existing Claude Code CLI new enough for restricted unattended turns."""
    import asyncio
    from .claude_native import MINIMUM_VERSION, parse_version
    if not oscompat.claude_sandbox_supported():
        raise ContractError(oscompat.CLAUDE_WINDOWS_REFUSAL)
    paths = candidates if candidates is not None else oscompat.claude_candidates()
    failures=[];seen=set()
    for path in paths:
        if not path or not Path(path).is_file() or str(Path(path).resolve()) in seen:
            continue
        seen.add(str(Path(path).resolve()))
        try:
            process=await asyncio.create_subprocess_exec(path,'--version',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,_=await asyncio.wait_for(process.communicate(),20)
        except (OSError,asyncio.TimeoutError) as error:
            failures.append(f'{path}: {type(error).__name__}: {error}');continue
        version=parse_version(out.decode(errors='replace'))
        if version is None or version<MINIMUM_VERSION:
            failures.append(f"{path}: version {out.decode(errors='replace').strip()!r} is older than {'.'.join(map(str,MINIMUM_VERSION))}")
            continue
        return str(path)
    raise ModelUnavailable('No existing Claude Code CLI supports restricted unattended turns. '+'; '.join(failures))
