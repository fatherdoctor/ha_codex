"""Every command in tests/fixtures/supervisor_command_corpus.tsv against the shells and the hook.

The corpus records what bash and dash did with each command: which Supervisor or remote family
a print-only ``ha`` stub received. This test runs each command again under both shells. A shell
that no longer gives its recorded answer fails the test, so the corpus cannot quietly go stale.
A shell may give a family where none was recorded: the answer can depend on the machine (dash
has no ``time`` keyword, so ``time ha ...`` runs only where a ``time`` program is installed).
Then the hook is held to the stricter answer. A command that ran a Supervisor family must be
refused in both remote-control modes; one that ran only a remote family must be refused with
remote control on. A case marked as a stated limit (``su-limit``, ``remote-limit``) is one that
DOCS.md names as out of the check's reach: the shells must still run it, and the hook must still
let it pass, so a limit that the hook comes to refuse is moved to ``su`` or ``remote`` and the
texts are changed with it. The corpus header says how to add a case.
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


def shells_answer(command: str, folder: Path, work: Path) -> dict[str, str]:
    """Run the command under each shell with the stub and return the family each shell ran."""
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
    return received


def stated_limit(expect: str) -> str | None:
    """Return su or remote for a case marked as a stated limit, or None for any other case."""
    kind = expect.split(":")[0]
    return kind.removesuffix("-limit") if kind.endswith("-limit") else None


def missed(command: str, kind: str) -> bool:
    """Say whether the hook lets a command of this kind pass where it must refuse it."""
    if kind == "su":
        return not (refused(command, False) and refused(command, True))
    return kind == "remote" and not refused(command, True)


def kind_of(families: list[str]) -> str:
    """Return su, remote or none for the families that the shells ran."""
    if any(family in ALWAYS_FAMILIES for family in families):
        return "su"
    return "remote" if families else "none"


def stricter(first: str, second: str) -> str:
    order = ("none", "remote", "su")
    return max(first, second, key=order.index)


def refused(command: str, remote_on: bool) -> bool:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    return bool(gate_rules.check_tool_call("PreToolUse", payload, remote_on, RULES))


class SupervisorCorpusTests(unittest.TestCase):
    def test_the_corpus_has_cases_of_each_kind(self) -> None:
        kinds = [expect.split(":")[0] for _, expect, _ in load_corpus()]
        self.assertLessEqual(set(kinds), {"su", "remote", "none", "su-limit", "remote-limit"})
        for kind in ("su", "remote", "none"):
            with self.subTest(kind=kind):
                self.assertGreater(kinds.count(kind), 20)

    @unittest.skipUnless(all(shutil.which(shell) for shell in SHELLS), "needs bash and dash")
    def test_the_shells_give_the_recorded_answer_and_the_hook_refuses(self) -> None:
        wrong, passed = [], []
        with tempfile.TemporaryDirectory() as stub_folder, tempfile.TemporaryDirectory() as work_folder:
            folder = Path(stub_folder)
            (folder / "ha").write_text(STUB, encoding="utf-8")
            (folder / "ha").chmod(0o755)
            for case_id, expect, command in load_corpus():
                received = shells_answer(command, folder, Path(work_folder))
                recorded_kind, _, recorded_shells = expect.partition(":")
                recorded_kind = stated_limit(expect) or recorded_kind
                for shell in filter(None, recorded_shells.split(",")):
                    if kind_of([received[shell]] if shell in received else []) != recorded_kind:
                        wrong.append(f"{case_id}: {shell} recorded {recorded_kind}, gave {received.get(shell)}")
                if stated_limit(expect):
                    continue
                if missed(command, stricter(recorded_kind, kind_of(list(received.values())))):
                    passed.append(case_id)
        self.assertEqual(wrong, [])
        self.assertEqual(passed, [])

    def test_the_hook_refuses_each_recorded_hostile_case(self) -> None:
        """The same check from the recorded answers alone, for a machine without bash or dash."""
        passed = [case_id for case_id, expect, command in load_corpus()
                  if not stated_limit(expect) and missed(command, expect.split(":")[0])]
        self.assertEqual(passed, [])

    def test_each_stated_limit_still_passes(self) -> None:
        """A stated limit that the hook now refuses is no longer a limit: move it and change the texts."""
        refused_now = [case_id for case_id, expect, command in load_corpus()
                       if stated_limit(expect) and not missed(command, stated_limit(expect))]
        self.assertEqual(refused_now, [])


if __name__ == "__main__":
    unittest.main()
