"""The worker marks itself not dumpable, so a sandboxed command cannot read its memory or environment."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


class UndumpableWorkerTests(unittest.TestCase):
    """``server.py: make_worker_undumpable`` sets the flag (moved here from the removed test_gate.py)."""

    def test_the_flag_is_set(self) -> None:
        # Only the function is run (server.py starts threads on import); PR_GET_DUMPABLE is 3.
        code = (
            "import ctypes, sys;"
            "src = open(sys.argv[1] + '/server.py').read();"
            "start = src.index('PR_SET_DUMPABLE = 4'); end = src.index('def main()');"
            "ns = {}; exec(src[start:end], ns);"
            "assert ns['make_worker_undumpable']();"
            "libc = ctypes.CDLL(None); print(libc.prctl(3, 0, 0, 0, 0))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, str(Path(__file__).resolve().parents[1])],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
