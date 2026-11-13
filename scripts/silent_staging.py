"""Private descriptor-pinned payload staging for silent-media directories."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile
import errno


@contextmanager
def payload_stage(parent_fd, prefix):
    """Keep the publication source inside a pinned private wrapper.

    Renaming/replacing the wrapper's public basename cannot redirect writes,
    verification or the final source lookup. Cleanup traverses only its original
    descriptor; a replacement at the former wrapper pathname stays untouched.
    """
    wrapper = Path(tempfile.mkdtemp(prefix=prefix, dir=Path('/proc/self/fd') / str(parent_fd)))
    descriptor = os.open(wrapper.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    payload_fd = None
    identity = os.fstat(descriptor)
    try:
        os.mkdir('payload', 0o700, dir_fd=descriptor)
        payload_fd = os.open('payload', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        yield Path('/proc/self/fd') / str(payload_fd), descriptor
    finally:
        # Remove only the payload actually created/opened by this invocation.
        # Never enumerate/delete unrelated entries, even if the wrapper name
        # was replaced between creation and opening its descriptor.
        if payload_fd is not None:
            try:
                current_payload = os.stat('payload', dir_fd=descriptor, follow_symlinks=False)
                opened_payload = os.fstat(payload_fd)
                if (current_payload.st_dev, current_payload.st_ino) == (opened_payload.st_dev, opened_payload.st_ino):
                    shutil.rmtree(Path('/proc/self/fd') / str(descriptor) / 'payload')
            except FileNotFoundError:
                pass  # The verified payload has been published.
            finally:
                os.close(payload_fd)
        try:
            current = os.stat(wrapper.name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                os.rmdir(wrapper.name, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                raise
        finally:
            os.close(descriptor)
