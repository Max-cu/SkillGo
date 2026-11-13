"""Platform-owned command capture. Full streams stay in the task workspace."""
import base64
import json

# This is only an inline transport window, never a log-file or task limit.
INLINE_OUTPUT_BYTES = 8 * 1024 * 1024

CAPTURE_PROGRAM = r"""
import base64
import json
import os
import stat
import subprocess
import sys
import uuid

parent_fd = os.open('/workspace', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
log_fd = None
try:
    for directory in ('work', 'command-output'):
        try:
            os.mkdir(directory, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        entry = os.stat(directory, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(entry.st_mode):
            raise PermissionError('Command log directories must be real workspace directories')
        next_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        opened = os.fstat(next_fd)
        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
            os.close(next_fd)
            raise PermissionError('Command log directory changed while opening')
        os.close(parent_fd)
        parent_fd = next_fd

    directory = uuid.uuid4().hex
    os.mkdir(directory, mode=0o700, dir_fd=parent_fd)
    log_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    stdout_fd = os.open('stdout.log', flags, 0o600, dir_fd=log_fd)
    stderr_fd = os.open('stderr.log', flags, 0o600, dir_fd=log_fd)
    root = '/workspace/work/command-output/' + directory

    with os.fdopen(stdout_fd, 'w+b') as stdout, os.fdopen(stderr_fd, 'w+b') as stderr:
        try:
            exit_code = subprocess.call(sys.argv[2:], stdout=stdout, stderr=stderr)
        except OSError as exc:
            stderr.write((type(exc).__name__ + ': ' + str(exc)).encode('utf-8', errors='replace'))
            exit_code = 127 if isinstance(exc, FileNotFoundError) else 126
        if exit_code < 0:
            exit_code = 128 - exit_code

        stdout.flush()
        stderr.flush()
        sizes = [os.fstat(stream.fileno()).st_size for stream in (stdout, stderr)]
        inline = sum(sizes) <= int(sys.argv[1])

        def content(stream, size, log_path):
            stream.seek(0)
            if inline:
                return stream.read()
            head = stream.read(8192)
            if size <= 8192:
                return head
            stream.seek(max(8192, size - 8192))
            notice = ('\n[Full output saved in command log ' + log_path + '; inline excerpt only]\n').encode('utf-8')
            return head + notice + stream.read()

        result = {
            'exit_code': exit_code,
            'stdout': base64.b64encode(content(stdout, sizes[0], root + '/stdout.log')).decode('ascii'),
            'stderr': base64.b64encode(content(stderr, sizes[1], root + '/stderr.log')).decode('ascii'),
            'output_files': {'stdout': root + '/stdout.log', 'stderr': root + '/stderr.log'} if not inline or exit_code != 0 else None,
            'output_bytes': {'stdout': sizes[0], 'stderr': sizes[1]},
            'inline_excerpt': not inline,
        }

    if inline and exit_code == 0:
        for filename in ('stdout.log', 'stderr.log'):
            try:
                os.unlink(filename, dir_fd=log_fd)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(directory, dir_fd=parent_fd)
        except OSError:
            pass
    print(json.dumps(result), flush=True)
finally:
    if log_fd is not None:
        os.close(log_fd)
    os.close(parent_fd)
"""


def decode_capture(raw):
    """Decode a trusted envelope, retaining byte-exact streams and log paths."""
    payload = json.loads(raw)
    return {
        **payload,
        'stdout': base64.b64decode(payload['stdout'], validate=True),
        'stderr': base64.b64decode(payload['stderr'], validate=True),
    }
