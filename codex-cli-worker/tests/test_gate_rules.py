"""The image folder /usr/share/ha_codex stays refused; repositories in /share/ha_<name> are usable (0.3.1)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import approvals  # noqa: E402
import gate_rules  # noqa: E402


# Every spelling of the image folder the check must keep refusing.
IMAGE_FOLDER_SPELLINGS = (
    # Absolute paths.
    "cat /usr/share/ha_codex/names.txt",
    "echo x > /usr/share/ha_codex/policy.json",
    "ls /usr//share//ha_codex",
    "cat /usr/local/../share/ha_codex/policy.json",
    "cat /share/ha_codex_lab_test/../../usr/share/ha_codex/policy.json",
    "git -C /usr/share/ha_codex status",
    # Relative paths: share/ha_ after cd /usr, ../share/ha_ from below /usr.
    "cd /usr && cat share/ha_codex/policy.json",
    "cd /usr; cat ./share/ha_codex/policy.json",
    "cd /usr/lib && cat ../share/ha_codex/policy.json",
    "cat share/ha_codex_lab_test/README.md",
    # Globs, character classes and braces.
    "cat /usr/share/ha_c*/policy.json",
    "ls /usr/sh*/ha_*",
    "cat /u*/s*/ha_c?dex/policy.json",
    "cat /*/*/ha_codex/policy.json",
    "cat /usr/share/h*/policy.json",
    "cat /usr/share/ha_[c]odex/policy.json",
    "cat /usr/{share,lib}/ha_codex/policy.json",
    "cat /usr/{share/ha_codex,lib}/policy.json",
    "cat /usr/sh{a,}re/ha_codex/policy.json",
    # Quoted or escaped.
    "cat '/usr/share/ha_codex'/policy.json",
    'cat "/usr"/share/"ha_codex"/policy.json',
    "cat /usr/sh\\are/ha_codex/policy.json",
    "cat /usr\\/share\\/ha_codex/policy.json",
    # Variables and command substitution.
    "X=/usr; cat $X/share/ha_codex/policy.json",
    "cat ${X}/share/ha_codex/policy.json",
    "D=/usr/share; cat $D/ha_codex/names.txt",
    "cat $(echo /usr)/share/ha_codex/policy.json",
    "cat `echo /usr`/share/ha_codex/policy.json",
)

# A repository the owner cloned or linked into the Home Assistant share folder.
SHARE_REPO_COMMANDS = (
    "git -C /share/ha_codex_lab_test status --short --branch",
    "git -C /share/ha_codex_lab_test commit -m 'Lab commit'",
    "git -C /share/ha_codex_lab_test add README.md",
    "cat /share/ha_codex_lab_test/README.md",
    "cd /share/ha_codex_lab_test && git status",
    "cd '/share/ha_codex_lab_test' && git commit -am x",
    "git --git-dir=/share/ha_codex_lab_test/.git log --oneline -1",
    "ls /share/ha_*",
    "echo $(git -C /share/ha_codex_lab_test rev-parse HEAD)",
)


class ImageFolderTests(unittest.TestCase):
    def hook_refuses(self, command: str) -> str:
        # The policy rule runs when Remote Control (the ChatGPT app) is on, as in the lab check that found the bug.
        return gate_rules.check_tool_call("PreToolUse", {"tool_input": {"command": command}}, True,
                                          gate_rules.STRICT_RULES)

    def test_every_image_folder_spelling_is_refused(self) -> None:
        for command in IMAGE_FOLDER_SPELLINGS:
            with self.subTest(command=command):
                self.assertTrue(gate_rules.names_image_folder(command))
                self.assertEqual(gate_rules._policy_restriction(command), "changes the add-on's permission policy")
                self.assertIn("permission policy", self.hook_refuses(command))
                self.assertTrue(approvals.command_names_protected_path(command))

    def test_share_repository_commands_are_allowed(self) -> None:
        for command in SHARE_REPO_COMMANDS:
            with self.subTest(command=command):
                self.assertFalse(gate_rules.names_image_folder(command))
                self.assertEqual(gate_rules._policy_restriction(command), "")
                self.assertEqual(self.hook_refuses(command), "")
                self.assertFalse(approvals.command_names_protected_path(command))

    def test_the_policy_rule_still_covers_its_other_places(self) -> None:
        for command in ("cat ha_codex/policy.json", "vi /data/codex-home/config.toml",
                        "cat /etc/codex/requirements.toml", "ls /data/codex-home/*"):
            with self.subTest(command=command):
                self.assertTrue(gate_rules._policy_restriction(command))


if __name__ == "__main__":
    unittest.main()
