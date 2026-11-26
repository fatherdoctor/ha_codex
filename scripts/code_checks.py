"""Cheap code checks for this repository: file size, module globals by name, and tests that patch server.py.

The code standard behind these checks has five rules: delete before you move; no source file past 1,000 lines
without a listed exemption; no special case bolted into a busy function; no helper that hands out any module
global by name; one model for each concept. Two of them can be checked by a machine, and this script checks them:

1. Size. No source file (Python, JavaScript, CSS, shell, HTML) is longer than 1,000 lines, unless SIZE_EXEMPT
   lists it. A listed file that is no longer too long, or no longer exists, also fails: remove it from the list.
2. Globals by name. No Python code reads or writes a module global through a name it computes:
   `globals()[name]`, `globals().get(name)`, `vars()[name]`, `getattr(sys.modules[__name__], name)`,
   `vars(module)[name]`, `getattr(importlib.import_module(...), name)` and the same through a name, a parameter
   default, a tuple item, an attribute such as `self.g` or a walrus that holds `globals()` or a module. Such a
   helper hides which names a caller uses, so a reader cannot find them by search. GLOBALS_EXEMPT lists the places
   that still do it. A Python file that does not parse also fails: the check cannot confirm it.
   A name counts in its own scope, as Python reads it: `g = globals()` in one function does not make `g` a
   namespace in another. Forms the check does not follow, on purpose, because a review finds them more cheaply:
   `eval(name)` and `exec`, a namespace passed through a function's return value or kept in a list or dict, and
   `getattr(module, name)` on a module that was imported by its own name (`import json`), which reads that
   module's names, not this one's.

It also prints a warning, never a failure: how many places in the tests patch a name inside server.py
(`patch("server.X")`, `patch.object(server, "X")`, `monkeypatch.setattr(server, "X", ...)`). Each one ties a
test to the inside of server.py, so the count should go down as server.py is split. The tests of this script
hold such patches as test data, so the count leaves that file out.

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
OWN_TESTS = "codex-cli-worker/tests/test_code_checks.py"  # its patch strings are test data

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
    if not isinstance(node, ast.Call) or node.keywords or node.args:
        return False
    return _called_name(node) in NAMESPACE_CALLS


def _called_name(node: ast.Call) -> str | None:
    """The last part of the called name: "f" for f(...) and for a.b.f(...)."""
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _is_constant_name(node: ast.AST) -> bool:
    """True when the key is a plain string written in the code, which a search can find."""
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _dotted(node: ast.AST) -> str | None:
    """"self.g" for the expression self.g, "g" for g; None for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


# What an expression holds, as far as this check cares.
NAMESPACE = "namespace"  # a dict of module globals: globals(), vars(), locals(), vars(module), module.__dict__
MODULE = "module"  # a module object: sys.modules[...], importlib.import_module(...)
MODULE_TABLE = "module table"  # sys.modules itself, also after `from sys import modules`


class _Scopes:
    """The names each scope binds, and which of them hold a namespace, a module or the module table.

    A scope is the module, a function, a lambda or a class body. A name used in a function is looked up in that
    function first; a function that binds the name to anything else hides an outer binding (so `g = {}` in one
    function is not the `g = globals()` of another). Class bodies are skipped for the functions inside them, as in
    Python. Attribute names such as `self.g` are kept for the whole file, because they are not bound by scope.
    """

    def __init__(self):
        self.bound: dict[ast.AST, set[str]] = {}
        self.kinds: dict[ast.AST, dict[str, str]] = {}
        self.attributes: dict[str, str] = {}

    def kind_of_name(self, name: str, stack: list[ast.AST]) -> str | None:
        for depth, scope in enumerate(reversed(stack)):
            if depth and isinstance(scope, ast.ClassDef):
                continue
            kind = self.kinds.get(scope, {}).get(name)
            if kind:
                return kind
            if name in self.bound.get(scope, set()):
                return None
        return None

    def kind(self, node: ast.AST, stack: list[ast.AST]) -> str | None:
        """Return NAMESPACE, MODULE, MODULE_TABLE or None for an expression."""
        if isinstance(node, ast.NamedExpr):
            return self.kind(node.value, stack)
        if _is_namespace_call(node):
            return NAMESPACE
        if isinstance(node, ast.Call) and not node.keywords:
            name = _called_name(node)
            if name == "vars" and len(node.args) == 1 and self.kind(node.args[0], stack) == MODULE:
                return NAMESPACE
            if name in {"import_module", "__import__"}:
                return MODULE
            if (name == "get" and isinstance(node.func, ast.Attribute)
                    and self.kind(node.func.value, stack) == MODULE_TABLE):
                return MODULE
            return None
        if isinstance(node, ast.Subscript):
            return MODULE if self.kind(node.value, stack) == MODULE_TABLE else None
        if isinstance(node, ast.Attribute):
            if node.attr == "__dict__" and self.kind(node.value, stack) == MODULE:
                return NAMESPACE
            if node.attr == "modules":
                return MODULE_TABLE
            return self.attributes.get(_dotted(node) or "")
        if isinstance(node, ast.Name):
            return self.kind_of_name(node.id, stack)
        return None


class _Binder(ast.NodeVisitor):
    """First pass: record what each scope binds and what each binding holds."""

    def __init__(self, scopes: _Scopes):
        self.scopes = scopes
        self.stack: list[ast.AST] = []

    def _bind(self, target: ast.AST, kind: str | None):
        scope = self.stack[-1]
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind(element, None)
        elif isinstance(target, ast.Starred):
            self._bind(target.value, None)
        elif isinstance(target, ast.Name):
            self.scopes.bound.setdefault(scope, set()).add(target.id)
            if kind:
                self.scopes.kinds.setdefault(scope, {})[target.id] = kind
        elif isinstance(target, ast.Attribute) and kind and _dotted(target):
            self.scopes.attributes[_dotted(target)] = kind

    def _assign(self, target: ast.AST, value: ast.AST | None):
        if (isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List))
                and len(target.elts) == len(value.elts)):
            for element, part in zip(target.elts, value.elts):
                self._assign(element, part)
            return
        self._bind(target, self.scopes.kind(value, self.stack) if value is not None else None)

    def _enter(self, node: ast.AST, parameters: ast.arguments | None, body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self._bind(ast.Name(id=node.name), None)
        # Defaults are read in the outer scope; the parameter they fill belongs to the function.
        parameter_kinds = []
        if parameters is not None:
            positional = parameters.posonlyargs + parameters.args
            defaults = [None] * (len(positional) - len(parameters.defaults)) + list(parameters.defaults)
            pairs = list(zip(positional, defaults)) + list(zip(parameters.kwonlyargs, parameters.kw_defaults))
            pairs += [(extra, None) for extra in (parameters.vararg, parameters.kwarg) if extra]
            for argument, default in pairs:
                kind = self.scopes.kind(default, self.stack) if default is not None else None
                parameter_kinds.append((argument.arg, kind))
        self.stack.append(node)
        for name, kind in parameter_kinds:
            self._bind(ast.Name(id=name), kind)
        for child in body:
            self.visit(child)
        self.stack.pop()

    def visit_Module(self, node):
        self._enter(node, None, node.body)

    def visit_FunctionDef(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._enter(node, node.args, node.body)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self._enter(node, node.args, [node.body])

    def visit_ClassDef(self, node):
        for base in node.bases + node.decorator_list:
            self.visit(base)
        self._enter(node, None, node.body)

    def visit_Assign(self, node):
        self.visit(node.value)
        for target in node.targets:
            self._assign(target, node.value)

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self.visit(node.value)
        self._assign(node.target, node.value)

    def visit_AugAssign(self, node):
        self.visit(node.value)
        self._bind(node.target, None)

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        self._assign(node.target, node.value)

    def visit_For(self, node):
        self.visit(node.iter)
        self._bind(node.target, None)
        for child in node.body + node.orelse:
            self.visit(child)

    visit_AsyncFor = visit_For

    def visit_withitem(self, node):
        self.visit(node.context_expr)
        if node.optional_vars is not None:
            self._assign(node.optional_vars, node.context_expr)

    def visit_Import(self, node):
        for alias in node.names:
            self._bind(ast.Name(id=(alias.asname or alias.name).split(".")[0]), None)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            kind = MODULE_TABLE if node.module == "sys" and alias.name == "modules" else None
            self._bind(ast.Name(id=alias.asname or alias.name), kind)


class _GlobalsByName(ast.NodeVisitor):
    """Second pass: find every place that reaches a module global through a computed name."""

    def __init__(self, scopes: _Scopes):
        self.scopes = scopes
        self.stack: list[ast.AST] = []
        self.hits: list[tuple[int, str]] = []

    def _hit(self, node: ast.AST):
        names = [s.name for s in self.stack if hasattr(s, "name")]
        self.hits.append((node.lineno, ".".join(names)))

    def _kind(self, node: ast.AST) -> str | None:
        return self.scopes.kind(node, self.stack)

    def _scoped(self, node):
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    visit_Module = visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = visit_Lambda = _scoped

    def visit_Subscript(self, node):
        if self._kind(node.value) == NAMESPACE and not _is_constant_name(node.slice):
            self._hit(node)
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr in LOOKUP_METHODS and self._kind(func.value) == NAMESPACE
                and not (node.args and _is_constant_name(node.args[0]))):
            self._hit(node)
        elif (isinstance(func, ast.Name) and func.id in {"getattr", "setattr", "delattr"} and len(node.args) >= 2
              and self._kind(node.args[0]) == MODULE and not _is_constant_name(node.args[1])):
            self._hit(node)
        self.generic_visit(node)


def globals_by_name(source: str, filename: str = "<source>") -> list[tuple[int, str]]:
    """Return (line, enclosing function) for each computed-name lookup; raise SyntaxError when it cannot parse."""
    tree = ast.parse(source, filename=filename)
    scopes = _Scopes()
    # Twice, so a function also sees a module-level alias written below it.
    _Binder(scopes).visit(tree)
    _Binder(scopes).visit(tree)
    finder = _GlobalsByName(scopes)
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
        if path.suffix.lower() != ".py" or tests not in path.parents or relative == OWN_TESTS:
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
