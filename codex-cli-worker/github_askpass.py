#!/usr/bin/python3 -I
"""Git's askpass helper for the add-on's GitHub sign-in; installed as /usr/local/bin/codex-github-askpass.

Git runs it as the worker's child, outside every sandbox, with the prompt as its only argument. It answers
only GitHub's own two prompts: the user name `x-access-token`, and the token for `x-access-token@github.com`.
Any other prompt (another host, a rewritten URL, a user name in the remote) gets nothing and exit status 1,
so the token is offered to GitHub and to nobody else. It prints only to standard output, which git reads.
"""

import json
import os
import sys

CREDENTIAL = "/data/github/credential.json"
USERNAME_PROMPT = "Username for 'https://github.com': "
PASSWORD_PROMPT = "Password for 'https://x-access-token@github.com': "


def main(argv):
    prompt = argv[1] if len(argv) == 2 else ""
    if prompt == USERNAME_PROMPT:
        sys.stdout.write("x-access-token\n")
        return 0
    if prompt != PASSWORD_PROMPT:
        return 1
    try:
        fd = os.open(CREDENTIAL, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, encoding="utf-8") as handle:
            token = json.load(handle).get("token")
    except (OSError, ValueError, AttributeError):
        return 1
    if not isinstance(token, str) or not token or "\n" in token:
        return 1
    sys.stdout.write(token + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
