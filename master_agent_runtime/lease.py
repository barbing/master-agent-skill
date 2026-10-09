"""One controller per project; OS releases the lease after a host crash."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from .contracts import ConflictError


@contextmanager
def project_lease(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+') as stream:
        try:fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ConflictError('Another controller owns this project; use status/stop instead of another run') from error
        stream.seek(0);stream.truncate();stream.write(str(os.getpid())+'\n');stream.flush()
        try:yield
        finally:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
