"""Cheap code checks for this repository: file size, module globals by name, and tests that patch server.py.

The code standard behind these checks has five rules: delete before you move; no source file past 1,000 lines
without a listed exemption; no special case bolted into a busy function; no helper that hands out any module
global by name; one model for each concept. Two of them can be checked by a machine, and this script checks them:

1. Size. No source file (Python, JavaScript, CSS, shell, HTML) is longer than 1,000 lines, unless SIZE_EXEMPT
   lists it. A listed file that is no longer too long, or no longer exists, also fails: remove it from the list.
2. Globals by name. No Python code reads or writes a module global through a name it computes:
   `globals()[name]`, `globals().get(name)`, `vars()[name]`, `getattr(sys.modules[__name__], name)` and the
   same through a variable that holds `globals()`. Such a helper hides which names a caller uses, so a reader
   cannot find them by search. GLOBALS_EXEMPT lists the places that still do it. A Python file that does not
   parse also fails: the check cannot confirm it.

It also prints a warning, never a failure: how many places in the tests patch a name inside server.py
(`patch("server.X")`, `patch.object(server, "X")`, `monkeypatch.setattr(server, "X", ...)`). Each one ties a
test to the inside of server.py, so the count should go down as server.py is split.

Usage: python scripts/code_checks.py [--root <folder>]
Exit 0 when every check passes (warnings included), 1 when a check fails. Only the standard library is used.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIZE_LIMIT = 1000
SOURCE_SUFFIXES = {".py", ".js", ".cjs", ".mjs", ".css", ".sh", ".html"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", "vendor", ".venv", "venv"}

# Files allowed past the limit, with the reason. The 0.6 refactor splits them; each one leaves this list in the
# pull request that brings it under the limit.
SIZE_EXEMPT = {
    "codex-cli-worker/server.py": "the app's whole API; split in the 0.6 refactor",
    "codex-cli-worker/web/assets/chat.js": "the whole chat page; split in the 0.6 refactor",
    "codex-cli-worker/web/assets/chat.css": "the whole chat page; split in the 0.6 refactor",
    "codex-cli-worker/web/assets/chat-settings.js": "the Settings window, cut from chat.js; the cut design "
    "(CX-RF-JS) keeps it as one file of about 1,200 lines",
    "codex-cli-worker/github_access.py": "GitHub sign-in and repository access; split in the 0.6 refactor",
    "codex-cli-worker/tests/test_server.py": "follows server.py; split with it",
    "codex-cli-worker/tests/test_approvals.py": "split in the 0.6 refactor",
    "codex-cli-worker/tests/test_github_access.py": "follows github_access.py; split with it",
    "codex-cli-worker/tests/test_live_proofs.py": "split in the 0.6 refactor",
    "codex-cli-worker/tests/browser_smoke.cjs": "the chat page's browser check; split with chat.js",
}

# (file, function) pairs allowed to read module globals by a computed name, with the reason.
GLOBALS_EXEMPT = {
    ("codex-cli-worker/server.py", "_ToolWorker.__getattr__"): "the tool worker's view of server.py; "
    "the 0.6 refactor replaces it with an explicit object",
}

NAMESPACE_CALLS = {"globals", "vars", "locals"}
LOOKUP_METHODS = {"get", "__getitem__", "__setitem__", "__delitem__", "setdefault", "pop", "update"}
SERVER_PATCH = re.compile(
    r"""patch\(\s*["']server\.\w+"""
    r"""|patch\.object\(\s*server\s*,"""
    r"""|setattr\(\s*server\s*,"""
)


def source_files(root: Path):
    """Yield (relative path, absolute path) for each source file under root, vendored code left out."""
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            path = Path(folder) / name
            if path.suffix.lower() in SOURCE_SUFFIXES:
                yield path.relative_to(root).as_posix(), path


def line_count(path: Path) -> int:
    """The number of lines as `wc -l` would count them, plus one for a last line with no newline."""
    data = path.read_bytes()
    count = data.count(b"\n")
    if data and not data.endswith(b"\n"):
        count += 1
    return count


def check_sizes(root: Path, exempt: dict[str, str] | None = None) -> list[str]:
    """Return one failure line for each file over the limit and each exemption that is no longer needed."""
    exempt = SIZE_EXEMPT if exempt is None else exempt
    failures = []
    seen = set()
    for relative, path in source_files(root):
        lines = line_count(path)
        if lines <= SIZE_LIMIT:
            continue
        seen.add(relative)
        if relative not in exempt:
            failures.append(f"size: {relative} has {lines} lines (limit {SIZE_LIMIT}); split it, or list it in "
                            "SIZE_EXEMPT in scripts/code_checks.py with the reason")
    for relative in sorted(set(exempt) - seen):
        failures.append(f"size: {relative} is listed in SIZE_EXEMPT but is not over {SIZE_LIMIT} lines "
                        "(or is gone); remove it from the list")
    return failures


def _is_namespace_call(node: ast.AST) -> bool:
    """True for globals(), vars() with no argument, locals(), and builtins.globals() and the like."""
    if not isinstance(node, ast.Call) or node.keywords:
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    if name not in NAMESPACE_CALLS:
        return False
    return not node.args


def _is_module_object(node: ast.AST) -> bool:
    """True for sys.modules[...] and sys.modules.get(...)."""
    if isinstance(node, ast.Subscript):
        node = node.value
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        node = node.func.value
    else:
        return False
    return isinstance(node, ast.Attribute) and node.attr == "modules"


def _is_constant_name(node: ast.AST) -> bool:
    """True when the key is a plain string written in the code, which a search can find."""
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


class _GlobalsByName(ast.NodeVisitor):
    """Find every place that reaches a module global through a computed name."""

    def __init__(self, aliases: set[str]):
        self.aliases = aliases
        self.scope: list[str] = []
        self.hits: list[tuple[int, str]] = []

    def _namespace(self, node: ast.AST) -> bool:
        if _is_namespace_call(node):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "__dict__" and _is_module_object(node.value):
            return True
        return isinstance(node, ast.Name) and node.id in self.aliases

    def _scoped(self, node, visit):
        self.scope.append(node.name)
        visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node):
        self._scoped(node, self.generic_visit)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Subscript(self, node):
        if self._namespace(node.value) and not _is_constant_name(node.slice):
            self.hits.append((node.lineno, ".".join(self.scope)))
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr in LOOKUP_METHODS and self._namespace(func.value)
                and not (node.args and _is_constant_name(node.args[0]))):
            self.hits.append((node.lineno, ".".join(self.scope)))
        elif (isinstance(func, ast.Name) and func.id in {"getattr", "setattr", "delattr"} and len(node.args) >= 2
              and _is_module_object(node.args[0]) and not _is_constant_name(node.args[1])):
            self.hits.append((node.lineno, ".".join(self.scope)))
        self.generic_visit(node)


def _namespace_aliases(tree: ast.AST) -> set[str]:
    """Names that are ever bound to globals(), vars() or a module's __dict__ anywhere in the file."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            value = node.value
            is_namespace = _is_namespace_call(value) or (
                isinstance(value, ast.Attribute) and value.attr == "__dict__" and _is_module_object(value.value))
            if not is_namespace:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            aliases.update(t.id for t in targets if isinstance(t, ast.Name))
    return aliases


def globals_by_name(source: str, filename: str = "<source>") -> list[tuple[int, str]]:
    """Return (line, enclosing function) for each computed-name lookup; raise SyntaxError when it cannot parse."""
    tree = ast.parse(source, filename=filename)
    finder = _GlobalsByName(_namespace_aliases(tree))
    finder.visit(tree)
    return finder.hits


def check_globals(root: Path, exempt: dict | None = None) -> list[str]:
    """Return one failure line for each computed-name lookup outside the exemption list."""
    exempt = GLOBALS_EXEMPT if exempt is None else exempt
    failures = []
    for relative, path in source_files(root):
        if path.suffix.lower() != ".py":
            continue
        try:
            hits = globals_by_name(path.read_text(encoding="utf-8-sig"), relative)
        except (SyntaxError, ValueError, UnicodeDecodeError) as error:
            failures.append(f"globals: {relative} could not be parsed, so it could not be checked ({error})")
            continue
        for line, scope in hits:
            if (relative, scope) not in exempt:
                where = scope or "module level"
                failures.append(f"globals: {relative}:{line} ({where}) reads a module global by a computed name; "
                                "name the global, or pass the object it needs")
    return failures


def count_server_patches(root: Path) -> dict[str, int]:
    """Return the number of server.py patches in each test file that has any."""
    counts = {}
    tests = root / "codex-cli-worker" / "tests"
    for relative, path in source_files(root):
        if path.suffix.lower() != ".py" or tests not in path.parents:
            continue
        found = len(SERVER_PATCH.findall(path.read_text(encoding="utf-8", errors="replace")))
        if found:
            counts[relative] = found
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    failures = check_sizes(root) + check_globals(root)
    patches = count_server_patches(root)
    for line in failures:
        print(f"FAIL {line}")
    total = sum(patches.values())
    if total:
        print(f"warning: tests patch names inside server.py {total} times in {len(patches)} files "
              "(not a failure; the number should go down as server.py is split)")
    if failures:
        print(f"code_checks: {len(failures)} failures")
        return 1
    print(f"code_checks: ok ({len(SIZE_EXEMPT)} files exempt from the size limit, "
          f"{len(GLOBALS_EXEMPT)} exempt from the globals rule)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
