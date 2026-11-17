"""Every command in tests/fixtures/supervisor_command_corpus.tsv against the shells and the hook.

The corpus records what bash and dash did with each command: which Supervisor or remote family
a print-only ``ha`` stub received. This test runs each command again under both shells, so a
recorded answer that the shells no longer give fails here first, and then asks the hook. A
command that ran a Supervisor family must be refused in both remote-control modes; one that ran
only a remote family must be refused with remote control on. The corpus header says how to add a
case.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import gate_rules

RULES = gate_rules.load_rules(ROOT / "policy" / "refused-domains.json")
CORPUS = Path(__file__).resolve().parent / "fixtures" / "supervisor_command_corpus.tsv"
SHELLS = ("bash", "dash")
ALWAYS_FAMILIES = {"su", "super", "supervisor"}
REMOTE_FAMILIES = {"host", "core", "os", "addons", "backups", "network", "hardware"}
# The stub writes each argument followed by a NUL byte and ends each call with byte 1, so an
# argument with a newline or a blank in it stays one argument.
STUB = '#!/bin/sh\nfor a; do printf "%s\\0" "$a" >> "$HA_STUB_OUT"; done\nprintf "\\1" >> "$HA_STUB_OUT"\n'


def load_corpus() -> list[tuple[str, str, str]]:
    """Return (id, expect, command) for each case, with the command's escapes decoded."""
    cases = []
    for line in CORPUS.read_text(encoding="ascii").splitlines():
        if not line or line.startswith("#"):
            continue
        case_id, expect, written = line.split("\t")
        cases.append((case_id, expect, written.encode("ascii").decode("unicode_escape")))
    return cases


def family_received(record: bytes) -> str | None:
    """Return the first family word the stub received after ha's global flags, or None."""
    for call in record.split(b"\1"):
        args = [arg.decode("utf-8", "replace") for arg in call.split(b"\0")[:-1]]
        index = 0
        while index < len(args) and args[index].startswith("-"):
            flag = args[index]
            index += 1
            if flag == "--":
                break
            if flag in gate_rules.HA_VALUE_FLAGS:
                index += 1
        if index < len(args) and args[index] in ALWAYS_FAMILIES | REMOTE_FAMILIES:
            return args[index]
    return None


def shells_answer(command: str, folder: Path, work: Path) -> str:
    """Run the command under each shell with the stub and return its expect value."""
    record = folder / "record"
    env = {**os.environ, "PATH": str(folder) + os.pathsep + os.environ.get("PATH", ""), "HA_STUB_OUT": str(record)}
    env.pop("x", None)
    env.pop("EMPTY", None)
    received = {}
    for shell in SHELLS:
        record.write_bytes(b"")
        try:
            subprocess.run([shell, "-c", command], capture_output=True, env=env, timeout=10, check=False,
                           stdin=subprocess.DEVNULL, cwd=work)
        except subprocess.TimeoutExpired:
            pass
        family = family_received(record.read_bytes())
        if family:
            received[shell] = family
        for item in work.iterdir():
            if item.is_file():
                item.unlink()
    if not received:
        return "none"
    kind = "su" if any(family in ALWAYS_FAMILIES for family in received.values()) else "remote"
    return kind + ":" + ",".join(sorted(received))


def refused(command: str, remote_on: bool) -> bool:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    return bool(gate_rules.check_tool_call("PreToolUse", payload, remote_on, RULES))


class SupervisorCorpusTests(unittest.TestCase):
    def test_the_corpus_has_cases_of_each_kind(self) -> None:
        kinds = [expect.split(":")[0] for _, expect, _ in load_corpus()]
        for kind in ("su", "remote", "none"):
            with self.subTest(kind=kind):
                self.assertGreater(kinds.count(kind), 20)

    @unittest.skipUnless(all(shutil.which(shell) for shell in SHELLS), "needs bash and dash")
    def test_the_shells_still_give_the_recorded_answer(self) -> None:
        wrong = []
        with tempfile.TemporaryDirectory() as stub_folder, tempfile.TemporaryDirectory() as work_folder:
            folder = Path(stub_folder)
            (folder / "ha").write_text(STUB, encoding="utf-8")
            (folder / "ha").chmod(0o755)
            for case_id, expect, command in load_corpus():
                answer = shells_answer(command, folder, Path(work_folder))
                if answer != expect:
                    wrong.append(f"{case_id}: recorded {expect}, the shells gave {answer}")
        self.assertEqual(wrong, [])

    def test_the_hook_refuses_each_hostile_case(self) -> None:
        missed = []
        for case_id, expect, command in load_corpus():
            kind = expect.split(":")[0]
            if kind == "su" and not (refused(command, False) and refused(command, True)):
                missed.append(case_id)
            if kind == "remote" and not refused(command, True):
                missed.append(case_id)
        self.assertEqual(missed, [])


if __name__ == "__main__":
    unittest.main()
