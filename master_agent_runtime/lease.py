"""One controller per project; OS releases the lease after a host crash."""
from contextlib import contextmanager
import os
from pathlib import Path

from .contracts import ConflictError
from .oscompat import lock_file, unlock_file


@contextmanager
def project_lease(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+',encoding='utf-8') as stream:
        if not lock_file(stream):
            raise ConflictError('Another controller owns this project; use status/stop instead of another run')
        stream.seek(0);stream.truncate();stream.write(str(os.getpid())+'\n');stream.flush()
        try:yield
        finally:unlock_file(stream)
