"""Choose an existing compatible CLI while preserving the requested model."""
from pathlib import Path
import shutil

from .native import JsonRpcClient,ModelUnavailable
from .sandbox import sandbox_prefix


async def select_cli(*,model=None,candidates=None):
    paths = candidates if candidates is not None else (
        shutil.which('codex'),
        '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex',
        '/Applications/Codex.app/Contents/Resources/codex')
    failures=[];seen=set()
    for path in paths:
        if not path or not Path(path).is_file() or str(Path(path).resolve()) in seen:
            continue
        seen.add(str(Path(path).resolve()))
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
