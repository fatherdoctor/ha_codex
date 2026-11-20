"""scripts/code_checks.py: the file-size limit, the ban on module globals by name, and the server.py patch count.

The real reader of the globals check is Python's own parser (`ast`): the check must agree with what Python runs,
not with how the text looks. The real reader of the size check is a line count as `wc -l` gives it, plus a last
line that has no newline.

Corpus families (CASES below; add a case there when a review finds a new shape):
- quotes: string keys written in the code are allowed; keys built from strings are not.
- comments: the pattern inside a comment or a string is not code and is allowed.
- nesting: the lookup inside parentheses, a subscript, a lambda, a nested function or a class.
- wrappers: vars(), locals(), builtins.globals(), .get(), .setdefault(), sys.modules[__name__] with getattr
  or __dict__.
- aliases: a name, a parameter default, a tuple item, an attribute or a walrus that holds globals() or a module;
  `from sys import modules`; importlib.import_module(); the scope rules (a name bound in one function is not the
  same name in another; a class body does not reach its methods).
- line ends and size: CRLF, no final newline, an empty file, exactly the limit, one line over.
- invisible characters: a byte-order mark at the start of a Python file still parses; a non-breaking space does
  not parse and fails closed.
- syntax: a Python file that does not parse fails closed.
- case and form: an upper-case suffix (`.PY`) is still a source file.
- n/a: separators, heredocs, git shapes, time of check and template readers (the input is a file tree that the
  check reads once; no shell, git or template engine reads it).
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("code_checks", ROOT / "scripts" / "code_checks.py")
code_checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(code_checks)

# (id, family, Python source, expected: "allow" or "refuse")
CASES = [
    ("Q1", "quotes", 'x = globals()["CONFIG_ROOT"]\n', "allow"),
    ("Q2", "quotes", "x = globals()['CONFIG_' + suffix]\n", "refuse"),
    ("Q3", "quotes", 'x = globals()[f"{name}"]\n', "refuse"),
    ("Q4", "quotes", 'x = globals().get("CONFIG_ROOT")\n', "allow"),
    ("C1", "comments", "# return globals()[name]\nx = 1\n", "allow"),
    ("C2", "comments", 'doc = "return globals()[name]"\n', "allow"),
    ("C3", "comments", '"""\nglobals()[name]\n"""\n', "allow"),
    ("N1", "nesting", "x = (globals())[name]\n", "refuse"),
    ("N2", "nesting", "x = globals()[names[0]]\n", "refuse"),
    ("N3", "nesting", "f = lambda n: globals()[n]\n", "refuse"),
    ("N4", "nesting", "class A:\n    def __getattr__(self, n):\n        return globals()[n]\n", "refuse"),
    ("N5", "nesting", "def f():\n    def g(n):\n        return globals()[n]\n    return g\n", "refuse"),
    ("W1", "wrappers", "x = vars()[name]\n", "refuse"),
    ("W2", "wrappers", "x = locals()[name]\n", "refuse"),
    ("W3", "wrappers", "import builtins\nx = builtins.globals()[name]\n", "refuse"),
    ("W4", "wrappers", "x = globals().get(name)\n", "refuse"),
    ("W5", "wrappers", "globals().setdefault(name, 1)\n", "refuse"),
    ("W6", "wrappers", "import sys\nx = getattr(sys.modules[__name__], name)\n", "refuse"),
    ("W7", "wrappers", "import sys\nx = sys.modules[__name__].__dict__[name]\n", "refuse"),
    ("W8", "wrappers", "g = globals()\nx = g[name]\n", "refuse"),
    ("W9", "wrappers", "def f(n):\n    g = globals()\n    return g.get(n)\n", "refuse"),
    ("W10", "wrappers", "globals()[name] = value\n", "refuse"),
    ("W11", "wrappers", "import sys\nsetattr(sys.modules[__name__], name, 1)\n", "refuse"),
    ("W12", "wrappers", "x = vars(obj)[name]\n", "allow"),
    ("W13", "wrappers", "from unittest.mock import patch\nwith patch.dict(globals(), {'A': 1}):\n    pass\n",
     "allow"),
    ("W14", "wrappers", "import sys\nx = getattr(sys.modules[__name__], 'CONFIG_ROOT')\n", "allow"),
    ("W15", "wrappers", "x = getattr(obj, name)\n", "allow"),
    ("W16", "wrappers", "x = settings[name]\n", "allow"),
    ("W17", "wrappers", "globals().update(values)\n", "refuse"),
    # Reviewer cases R1 to R24 (CX-STD review; there is no R20). R1 to R7, R9 and R17 had the wrong answer
    # before; the review gives their exact inputs. The others are written from the review's one-line notes.
    ("R1", "aliases", "import sys\nmod = sys.modules[__name__]\nx = getattr(mod, name)\n", "refuse"),
    ("R2", "aliases", "from sys import modules\nx = getattr(modules[__name__], name)\n", "refuse"),
    ("R3", "aliases", "import sys\nx = vars(sys.modules[__name__])[name]\n", "refuse"),
    ("R4", "aliases", "def f(n, g=globals()):\n    return g[n]\n", "refuse"),
    ("R5", "aliases", "g, h = globals(), 1\nx = g[name]\n", "refuse"),
    ("R6", "aliases", "class A:\n    def __init__(self):\n        self.g = globals()\n"
     "    def f(self, n):\n        return self.g[n]\n", "refuse"),
    ("R7", "aliases", "import importlib\nx = getattr(importlib.import_module(__name__), name)\n", "refuse"),
    ("R8", "wrappers", "x = globals().__getitem__(name)\n", "refuse"),
    ("R9", "aliases", "x = (g := globals())[name]\n", "refuse"),
    ("R10", "comments", "x = globals()[  # the name\n    name]\n", "refuse"),
    ("R11", "quotes", 'x = globals()["CONFIG" "_ROOT"]\n', "allow"),
    ("R12", "wrappers", "import sys\nm = sys.modules[name]\n", "allow"),
    ("R13", "wrappers", "import sys\nm = sys.modules.get(name)\n", "allow"),
    ("R14", "wrappers", "x = self.__dict__[n]\n", "allow"),
    ("R15", "wrappers", "for k, v in globals().items():\n    pass\n", "allow"),
    ("R16", "wrappers", "import os\nx = os.environ.get(name)\n", "allow"),
    ("R17", "aliases", "def a():\n    g = globals()\n    return g['X']\ndef b(n):\n    g = {}\n    return g[n]\n",
     "allow"),
    ("R18", "invisible", "x\u00a0= globals()[name]\n", "refuse"),
    ("R19", "wrappers", "import sys\nx = getattr(sys.modules[__name__], name, None)\n", "refuse"),
    ("R21", "quotes", 'KEY = "CONFIG_ROOT"\nx = globals()[KEY]\n', "refuse"),
    ("R22", "wrappers", "import sys\nx = sys.modules[__name__].__dict__.get(name)\n", "refuse"),
    ("R23", "wrappers", "x = globals().get(name, None)\n", "refuse"),
    ("R24", "wrappers", "x = name in globals()\n", "allow"),
    # Builder cases for the scope rules behind R4, R6 and R17.
    ("A1", "aliases", "g = globals()\ndef f(n):\n    return g[n]\n", "refuse"),
    ("A2", "aliases", "def f(n):\n    return g[n]\ng = globals()\n", "refuse"),
    ("A3", "aliases", "g = globals()\ndef f(n, g):\n    return g[n]\n", "allow"),
    ("A4", "aliases", "import sys as s\nm = s.modules[__name__]\nn2 = m\nx = getattr(n2, name)\n", "refuse"),
    ("A5", "aliases", "from importlib import import_module\nsetattr(import_module(__name__), name, 1)\n",
     "refuse"),
    ("A6", "aliases", "class A:\n    g = globals()\n    def f(self, n):\n        return g[n]\n", "allow"),
    ("A7", "aliases", "import json\nx = getattr(json, name)\n", "allow"),
    ("A8", "aliases", "x = eval(name)\n", "allow"),
]


class GlobalsCorpusTests(unittest.TestCase):
    def test_corpus(self):
        wrong = []
        for case_id, _family, source, expected in CASES:
            try:
                got = "refuse" if code_checks.globals_by_name(source) else "allow"
            except SyntaxError:
                got = "refuse"  # check_globals fails a file it cannot parse
            if got != expected:
                wrong.append(f"{case_id}: expected {expected}, got {got}")
        self.assertEqual(wrong, [], "corpus cases with the wrong answer")

    def test_corpus_has_two_cases_a_family_and_hostile_looking_allows(self):
        families = {}
        for _id, family, _source, expected in CASES:
            families.setdefault(family, set()).add(expected)
        for family in ("quotes", "comments", "nesting", "wrappers", "aliases"):
            self.assertGreaterEqual(sum(1 for c in CASES if c[1] == family), 2, family)
        self.assertIn("allow", families["wrappers"])

    def test_reports_the_enclosing_function(self):
        source = "class A:\n    def __getattr__(self, n):\n        return globals()[n]\n"
        self.assertEqual(code_checks.globals_by_name(source), [(3, "A.__getattr__")])

    def test_syntax_error_raises(self):
        with self.assertRaises(SyntaxError):
            code_checks.globals_by_name("def broken(:\n")


class TreeTests(unittest.TestCase):
    """A small invented file tree for each test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, relative: str, text: str | bytes):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def test_exactly_the_limit_passes_and_one_more_fails(self):
        self.write("a.py", "x = 1\n" * code_checks.SIZE_LIMIT)
        self.assertEqual(code_checks.check_sizes(self.root, exempt={}), [])
        self.write("b.js", "x;\n" * (code_checks.SIZE_LIMIT + 1))
        failures = code_checks.check_sizes(self.root, exempt={})
        self.assertEqual(len(failures), 1)
        self.assertIn("b.js has 1001 lines", failures[0])

    def test_last_line_without_newline_counts(self):
        self.write("a.css", "a{}\n" * code_checks.SIZE_LIMIT + "b{}")
        self.assertEqual(len(code_checks.check_sizes(self.root, exempt={})), 1)

    def test_crlf_lines_count_once(self):
        self.write("a.sh", b"echo\r\n" * code_checks.SIZE_LIMIT)
        self.assertEqual(code_checks.check_sizes(self.root, exempt={}), [])

    def test_empty_file_passes(self):
        self.write("empty.py", "")
        self.assertEqual(code_checks.check_sizes(self.root, exempt={}), [])
        self.assertEqual(code_checks.check_globals(self.root, exempt={}), [])

    def test_upper_case_suffix_is_source(self):
        self.write("BIG.PY", "x = 1\n" * (code_checks.SIZE_LIMIT + 1))
        self.assertEqual(len(code_checks.check_sizes(self.root, exempt={})), 1)

    def test_vendor_and_non_source_files_are_skipped(self):
        self.write("web/assets/vendor/lib.js", "x;\n" * 5000)
        self.write("DOCS.md", "line\n" * 5000)
        self.assertEqual(code_checks.check_sizes(self.root, exempt={}), [])

    def test_exempt_file_passes_and_stale_exemption_fails(self):
        self.write("big.py", "x = 1\n" * 1500)
        self.write("small.py", "x = 1\n")
        failures = code_checks.check_sizes(self.root, exempt={"big.py": "r", "small.py": "r", "gone.py": "r"})
        self.assertEqual(len(failures), 2)
        self.assertTrue(any("small.py is listed" in f for f in failures))
        self.assertTrue(any("gone.py is listed" in f for f in failures))

    def test_globals_exemption_is_by_file_and_function(self):
        source = "class W:\n    def __getattr__(self, n):\n        return globals()[n]\n"
        self.write("server.py", source)
        self.write("other.py", source)
        failures = code_checks.check_globals(self.root, exempt={("server.py", "W.__getattr__"): "r"})
        self.assertEqual(len(failures), 1)
        self.assertIn("other.py:3", failures[0])

    def test_unparsable_python_fails_closed(self):
        self.write("broken.py", "def broken(:\n")
        failures = code_checks.check_globals(self.root, exempt={})
        self.assertEqual(len(failures), 1)
        self.assertIn("could not be parsed", failures[0])

    def test_byte_order_mark_still_parses(self):
        self.write("bom.py", "\ufeffx = globals()[name]\n".encode("utf-8"))
        self.assertEqual(len(code_checks.check_globals(self.root, exempt={})), 1)

    def test_server_patch_count_is_a_warning_only(self):
        self.write("codex-cli-worker/tests/test_a.py",
                   'patch("server.DATA_ROOT", x)\npatch.object(server, "run", y)\n'
                   'monkeypatch.setattr(server, "X", 1)\npatch("other.X")\n')
        self.write("elsewhere/test_b.py", 'patch("server.DATA_ROOT", x)\n')
        self.write("codex-cli-worker/tests/test_code_checks.py", 'patch("server.DATA_ROOT", x)\n')
        self.assertEqual(code_checks.count_server_patches(self.root), {"codex-cli-worker/tests/test_a.py": 3})
        out = io.StringIO()
        with redirect_stdout(out):
            result = code_checks.main(["--root", str(self.root)])
        self.assertIn("warning: tests patch names inside server.py 3 times", out.getvalue())
        # The real exemption lists name files this invented tree lacks, so the run fails on them, not on the count.
        self.assertNotIn("test_a.py", "".join(l for l in out.getvalue().splitlines() if l.startswith("FAIL")))
        self.assertEqual(result, 1)


class RepositoryTests(unittest.TestCase):
    def test_this_repository_passes(self):
        out = io.StringIO()
        with redirect_stdout(out):
            result = code_checks.main(["--root", str(ROOT)])
        self.assertEqual(result, 0, out.getvalue())


if __name__ == "__main__":
    unittest.main()
