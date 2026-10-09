"""Host-sealed review evidence; native observations are not acceptance gates."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .artifacts import read_regular
from .contracts import ConflictError, ContractError, canonical, digest

MAX_COMMAND_OUTPUT = 128 * 1024
MAX_COMMANDS = 512


def command_observation(item, *, thread_id, turn_id, streamed_output=''):
    """Consume an authoritative completed native item, not model JSON claims."""
    output = item.get('aggregatedOutput')
    source = 'completed_item'
    if not isinstance(output, str):
        output = streamed_output
        source = 'streamed_deltas' if output else 'unavailable'
    raw = output.encode('utf-8')
    kept = raw[:MAX_COMMAND_OUTPUT].decode('utf-8', errors='replace')
    return {'item_id': item['id'], 'thread_id': thread_id, 'turn_id': turn_id,
            'command': item.get('command'), 'cwd': item.get('cwd'),
            'status': item.get('status'), 'exit_code': item.get('exitCode'),
            'output': kept, 'output_source': source, 'output_bytes': len(raw),
            'output_sha256': hashlib.sha256(raw).hexdigest(),
            'output_truncated': len(raw) > MAX_COMMAND_OUTPUT,
            'code_version_binding': 'development turn; may precede later edits; only sealed-tree rerun receipts certify the submitted version',
            'trust': 'native execution observation; test meaning and code coverage require review and sealed-tree rerun'}


def seal_review_evidence(destination: Path, *, bindings: dict, commands: list,
                         validation: list, test_paths: tuple[str, ...], manifest: dict,
                         collection_error=None) -> dict:
    if len(commands) > MAX_COMMANDS:
        raise ContractError('Native command evidence exceeds its bounded allowance')
    files = {}
    index = []
    for command in commands:
        if (command.get('thread_id') != bindings['thread_id']
                or command.get('turn_id') != bindings['turn_id']):
            raise ConflictError('Command evidence belongs to another native root/turn')
        name = 'commands/' + digest(command['item_id']) + '.json'
        payload = (canonical(command) + '\n').encode()
        files[name] = payload
        index.append({'item_id': command['item_id'], 'command_preview': (command.get('command') or '')[:600],
                      'exit_code': command.get('exit_code'), 'status': command.get('status'),
                      'output_truncated': command['output_truncated'], 'receipt': name,
                      'sha256': hashlib.sha256(payload).hexdigest()})
    logs = []
    for number, check in enumerate(validation):
        log = Path(check['log']) if check.get('log') else None
        if log is None:
            continue  # Explicitly synthetic unit validators have no native log.
        payload = log.read_bytes()
        if hashlib.sha256(payload).hexdigest() != check.get('log_sha256'):
            raise ConflictError('Validation output changed before evidence sealing')
        name = f'validation/{number}.log'
        files[name] = payload
        logs.append({'receipt': name, 'sha256': check['log_sha256'],
                     'status': check['status'], 'kind': check.get('kind', 'fixed')})
    tests = [row for row in manifest['files'] if row['path'] in test_paths]
    body = {'bindings': bindings, 'commands': index, 'validation': validation,
            'validation_logs': logs, 'owner_tests': tests,
            'missing_owner_tests': sorted(set(test_paths) - {row['path'] for row in tests}),
            'collection_error': collection_error,
            'trust': 'host-bound immutable evidence; native output is observation, admitted validation is rerun against the sealed candidate'}
    files['index.json'] = (canonical(body) + '\n').encode()
    evidence_hash = digest({name: hashlib.sha256(value).hexdigest() for name, value in files.items()})
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / evidence_hash
    if not target.exists():
        staging = Path(tempfile.mkdtemp(prefix='evidence-', dir=destination))
        try:
            for name, payload in files.items():
                path = staging / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
            staging.rename(target)
            for path in target.rglob('*'):
                if path.is_file(): path.chmod(0o444)
        finally:
            if staging.exists(): shutil.rmtree(staging)
    verify_review_evidence(target, expected_hash=evidence_hash, bindings=bindings)
    return {'path': str(target), 'hash': evidence_hash}


def verify_review_evidence(root: Path, *, expected_hash: str, bindings: dict) -> dict:
    if root.is_symlink(): raise ContractError('Evidence root must not be a symlink')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        names = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in dirs):
                raise ContractError('Evidence directories must not be symlinks')
            names.extend((Path(directory) / name).relative_to(root).as_posix() for name in files)
        contents = {name: read_regular(fd, name) for name in sorted(names)}
        actual = digest({name: hashlib.sha256(value).hexdigest() for name, value in contents.items()})
        if actual != expected_hash: raise ConflictError('Sealed review evidence changed')
        body = json.loads(contents['index.json'])
        if body['bindings'] != bindings: raise ConflictError('Review evidence binds a different candidate/mandate')
        return body
    finally:
        os.close(fd)
