"""Recognize ordinary installer calls without matching documentation or imports.

This checks model tool arguments, not arbitrary obfuscated programs. Sandbox
isolation and the trusted environment builder remain separate controls.
"""
import ast
import re
import shlex
from pathlib import PurePosixPath

INSTALLATION_ERROR = (
    'Dependency and system package installation is managed by the platform. Check '
    'runtime_environment.prepared_dependencies and python_distributions; '
    'use installed packages directly. For a genuinely missing package, use '
    'request_python_dependencies, or request_capability for a catalog capability. '
    'Business network access does not grant installation permission.'
)
INSTALL_WORDS = {'install', 'add', 'i', 'ci', 'sync', 'update', 'upgrade'}


def installation_command(argv, depth=0):
    if not isinstance(argv, (list, tuple)) or not argv or depth > 4:
        return False
    argv = list(argv)
    program = PurePosixPath(str(argv[0])).name.casefold()
    if program == 'env':
        rest = argv[1:]
        while rest and isinstance(rest[0], str) and (rest[0].startswith('-') or '=' in rest[0]):
            rest.pop(0)
        return installation_command(rest, depth + 1)
    words = {v.casefold() for v in argv[1:] if isinstance(v, str)}
    if (re.fullmatch(r'pip(?:\d+(?:\.\d+)?)?', program) or program in {'npm', 'pnpm', 'yarn', 'uv'}) and words & INSTALL_WORDS:
        return True
    if program in {'npx', 'pnpx', 'uvx'}:
        return True
    if program in {'apt', 'apt-get', 'apk'} and words & {'install', 'add'}:
        return True
    if program.startswith('python'):
        for i, value in enumerate(argv[1:], 1):
            if value == '-m':
                return i + 1 < len(argv) and argv[i + 1] == 'pip' and bool(words & INSTALL_WORDS)
            if value == '-c':
                return i + 1 < len(argv) and isinstance(argv[i + 1], str) and python_installs_dependencies(argv[i + 1], depth + 1)
    if program in {'sh', 'bash', 'dash', 'zsh'}:
        for i, value in enumerate(argv[1:], 1):
            if isinstance(value, str) and value.startswith('-') and 'c' in value:
                return i + 1 < len(argv) and shell_installs_dependencies(argv[i + 1], depth + 1)
    return False


def shell_installs_dependencies(command, depth=0):
    if not isinstance(command, str) or depth > 4:
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|')
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    part = []
    for token in [*tokens, ';']:
        if token in {';', '&&', '||', '|', '&'}:
            if installation_command(part, depth + 1):
                return True
            part = []
        else:
            part.append(token)
    return False


def python_installs_dependencies(code, depth=0):
    if depth > 4:
        return False
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return False
    aliases, values = {}, {}

    def name(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return name(node.value) + '.' + node.attr
        return ''

    def value(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if name(node) == 'sys.executable':
                return 'python3'
            return values.get(node.id)
        if isinstance(node, ast.Attribute) and name(node) == 'sys.executable':
            return 'python3'
        if isinstance(node, (ast.List, ast.Tuple)):
            return [value(item) for item in node.elts]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = value(node.left), value(node.right)
            if isinstance(left, (str, list)) and isinstance(right, type(left)):
                return left + right
        if isinstance(node, ast.JoinedStr):
            return ''.join(str(value(item.value) or '') if isinstance(item, ast.FormattedValue) else str(value(item)) for item in node.values)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split('.')[0]] = item.name if item.asname else item.name.split('.')[0]
        elif isinstance(node, ast.ImportFrom):
            for item in node.names:
                aliases[item.asname or item.name] = (node.module or '') + '.' + item.name
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = value(node.value)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = name(node.func)
        first = value(node.args[0]) if node.args else next((value(k.value) for k in node.keywords if k.arg in {'args', 'command', 'cmd'}), None)
        if function in {'subprocess.run', 'subprocess.call', 'subprocess.check_call', 'subprocess.check_output', 'subprocess.Popen',
                        'os.system', 'os.popen', 'asyncio.create_subprocess_shell'}:
            if installation_command(first, depth + 1) or shell_installs_dependencies(first, depth + 1):
                return True
        elif function == 'asyncio.create_subprocess_exec':
            if installation_command([value(arg) for arg in node.args], depth + 1):
                return True
        elif function.startswith('pip.') and function.endswith('.main'):
            if first is None or isinstance(first, (list, tuple)) and any(v in INSTALL_WORDS for v in first if isinstance(v, str)):
                return True
    return False
