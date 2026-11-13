"""Reading NekoServer's own source for the tests that prove a rule of the wizard
specification holds everywhere (test_spec_*.py): the Python files as syntax trees, the
Rust runtime as functions and the calls between them. Not a test file itself.
"""

import ast
import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = PROJECT / 'runtime' / 'src'
# Not part of what runs on a server: the tests, and the lab that builds test drives.
NOT_SHIPPED_CODE = ('tests', 'lab', 'runtime')


def python_files():
    """Every Python file that runs on a server: {path relative to the project: tree}."""
    found = {}
    for path in sorted(PROJECT.rglob('*.py')):
        relative = path.relative_to(PROJECT)
        if relative.parts[0] in NOT_SHIPPED_CODE or '__pycache__' in relative.parts:
            continue
        found[relative.as_posix()] = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    return found


def text_of(node):
    """The constant text inside an expression: 'a' + f'b{x}c' gives ['a', 'b', 'c']."""
    return [part.value for part in ast.walk(node) if isinstance(part, ast.Constant) and isinstance(part.value, str)]


def command_lists(tree):
    """Every list or tuple written in the code whose first item is a constant word: the
    way this project writes the commands it runs (['tailscale', 'status', '--json']).
    Gives (line, [each item's constant text, or None for a computed item])."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                yield node.lineno, [_word(item) for item in node.elts]


def _word(item):
    """A list item's text when it is written out as text, else None."""
    return item.value if isinstance(item, ast.Constant) and isinstance(item.value, str) else None


def imports(tree):
    """Every module a file imports, at the top or inside a function: (line, name)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, node.module or ''
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
            and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr) in ('import_module', '__import__')
        ):
            for name in text_of(node):
                yield node.lineno, name


def _block_end(code, start):
    """Where the { ... } block opening at `start` closes."""
    depth = 0
    for at in range(start, len(code)):
        depth += {'{': 1, '}': -1}.get(code[at], 0)
        if depth == 0:
            return at
    return len(code) - 1


def rust_code(name):
    """One runtime file without its tests (every item marked #[cfg(test)]) and without
    comments: a rule named in a comment is not a rule kept."""
    code = (RUNTIME / name).read_text(encoding='utf-8')
    code = '\n'.join('' if line.lstrip().startswith('//') else line for line in code.split('\n'))
    tests = code.find('#[cfg(test)]\nmod tests')  # the file's last part
    if tests >= 0:
        code = code[:tests]
    while (mark := code.find('#[cfg(test)]')) >= 0:
        brace, semicolon = code.find('{', mark), code.find(';', mark)
        if brace < 0 or 0 <= semicolon < brace:
            end = semicolon  # a test-only `use ...;`
        else:
            end = _block_end(code, brace)
        code = code[:mark] + code[end + 1 :]
    return code


def rust_functions():
    """Every function of the runtime outside its tests: {(file stem, name): body}."""
    functions = {}
    for path in sorted(RUNTIME.glob('*.rs')):
        code = rust_code(path.name)
        for match in re.finditer(r'^[ \t]*(?:pub(?:\([a-z]+\))? )?(?:const )?(?:unsafe )?fn (\w+)', code, re.M):
            start = code.find('{', match.end())
            semicolon = code.find(';', match.end())
            if start < 0 or 0 <= semicolon < start:
                continue  # a declaration without a body
            end = _block_end(code, start)
            if code[: match.start()].rstrip().endswith('#[test]'):
                continue  # a test written among the code
            functions[(path.stem, match.group(1))] = code[match.start() : end + 1]
    return functions


def rust_calls(functions):
    """Which runtime functions each one calls: {(file, name): {(file, name), ...}}.
    `backup::guard(` is the guard of backup.rs; a bare `guard(` is the one of the same
    file. Method calls (`x.run(`) are not followed."""
    names = {}
    for file, name in functions:
        names.setdefault(name, set()).add(file)
    calls = {}
    for (file, name), body in functions.items():
        inside = body[body.find('{') :]
        found = set()
        for match in re.finditer(r'(?<![\w.])(?:(\w+)::)?(\w+)\s*(?:::<[^>]*>)?\(', inside):
            module, called = match.group(1), match.group(2)
            if called not in names:
                continue
            if module in (None, 'self', 'Self'):
                if file in names[called]:
                    found.add((file, called))
            elif module == 'super' or module == 'crate':
                found.update((other, called) for other in names[called])
            elif module in names[called]:
                found.add((module, called))
        calls[(file, name)] = found
    return calls


def rust_reachable(calls, start):
    """Every function a run of `start` can get to."""
    seen, waiting = set(), [start]
    while waiting:
        current = waiting.pop()
        if current in seen:
            continue
        seen.add(current)
        waiting.extend(calls.get(current, ()))
    return seen


def rust_path(calls, start, goal):
    """One chain of calls from `start` to `goal` (the shortest), or None."""
    paths, seen = [[start]], {start}
    while paths:
        path = paths.pop(0)
        if path[-1] == goal:
            return path
        for called in sorted(calls.get(path[-1], ())):
            if called not in seen:
                seen.add(called)
                paths.append(path + [called])
    return None
