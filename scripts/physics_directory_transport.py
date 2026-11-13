"""Internal POSIX worker launch anchored to an inherited owned directory.

This transports a package-owned command, not commands from a physics document.
The caller retains its directory descriptor. No path alias or publisher is
provided here; native workers must keep their stage I/O relative to this cwd.
"""
import errno
import os
from pathlib import Path
import stat
import subprocess
import sys


def _duplicate_directory(descriptor):
    if os.name != 'posix' or not hasattr(os, 'fchdir'):
        raise OSError(errno.ENOTSUP, 'Pinned physics worker directories require POSIX descriptor transport.')
    if type(descriptor) is not int or descriptor < 0:
        raise ValueError('Expected a live directory descriptor.')
    original = os.fstat(descriptor)
    if not stat.S_ISDIR(original.st_mode):
        raise ValueError('Worker descriptor is not a directory.')
    owned = os.dup(descriptor)
    try:
        current = os.fstat(owned)
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            raise ValueError('Worker directory changed while acquiring transport.')
        return owned
    except BaseException:
        os.close(owned)
        raise


def launch_at(directory_fd, command, *, stdin, stdout, stderr, env, start_new_session=True):
    """Launch trusted argv after a child bootstrap fchdir, without preexec_fn.

    Relative script/job names remain relative after exec. The child retains one
    directory FD for its lifetime; no descriptor ownership passes to the caller.
    The returned Popen remains owned and reaped by the existing physics runner.
    """
    if type(command) not in (list, tuple) or not command or any(type(arg) is not str or not arg or '\0' in arg for arg in command):
        raise ValueError('Expected nonempty package-owned worker argv.')
    owned = _duplicate_directory(directory_fd)
    try:
        bootstrap = [sys.executable, '-I', str(Path(__file__).absolute()), '--directory-fd', str(owned), '--', *command]
        return subprocess.Popen(bootstrap, stdin=stdin, stdout=stdout, stderr=stderr, env=env,
            shell=False, start_new_session=start_new_session, pass_fds=(owned,))
    finally:
        os.close(owned)


def _bootstrap(argv):
    if len(argv) < 4 or argv[0] != '--directory-fd' or argv[2] != '--' or not argv[1].isascii() or not argv[1].isdigit():
        raise ValueError('Invalid private directory transport arguments.')
    descriptor = int(argv[1])
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise ValueError('Inherited worker descriptor is not a directory.')
    os.fchdir(descriptor)
    os.set_inheritable(descriptor, True)
    os.environ['HATCH_PHYSICS_DIRECTORY_FD'] = str(descriptor)
    for key in ('TMPDIR', 'TEMP', 'TMP'):
        os.environ[key] = '.'
    os.execvpe(argv[3], argv[3:], os.environ)


if __name__ == '__main__':
    _bootstrap(sys.argv[1:])
