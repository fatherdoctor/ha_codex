"""Read shell command text as words, without running it: one copy for both command checks.

The approval card (``approvals.py``) and the remote-control hook (``gate_rules.py``)
both read a command this way. Each keeps its own answer for text that cannot be read:
the approval card fails closed, and the hook reads the text again line by line.
A fix to how comments or words are read belongs here, so the two checks never read
the same command differently. The hook also joins backslash-newline
line breaks here first (``join_shell_continuations``); it reads comments too, so it
sits beside the other comment reader.

The image installs this file in two places: next to the worker (``/``) and next to
the hook (``/usr/share/ha_codex/``). The hook's marker file lists the second copy.
"""

from __future__ import annotations

import shlex

# Characters after which a ``#`` starts a word. An escaped character is never one of them.
WORD_BREAKS = frozenset(" \t\r\n;&|()<>")


def strip_comments(text: str) -> str:
    """Remove shell comments: from a ``#`` that starts a word outside quotes to the end of its line.

    A ``#`` inside a word, such as ``a#b``, is part of that word, as it is for the shell.
    """
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(text):
        char = text[i]
        if quote == "'":
            if char == "'":
                quote = ""
        elif char == "\\":
            # A backslash keeps the next character, except inside single quotes.
            out.append(text[i:i + 2])
            i += 2
            continue
        elif quote == '"':
            if char == '"':
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (not out or out[-1] in WORD_BREAKS):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
            continue
        out.append(char)
        i += 1
    return "".join(out)


def join_shell_continuations(text: str) -> str | None:
    """Remove only shell line joins; return None for forms we cannot read safely.

    Single quotes retain the literal pair. A quoted script for a shell wrapper
    gets another check when its own command is read.
    """
    if "\\\n" not in text:
        return text
    out: list[str] = []
    quote = ""
    comment = False
    i = 0
    while i < len(text):
        char = text[i]
        if not comment and quote != "'":
            if text.startswith(("$(", "`"), i):
                return None
            if not quote and text.startswith(("<<", "$'", "<(", ">("), i):
                return None
        if comment:
            # A backslash inside a comment does not escape its terminating newline.
            if char == "\n":
                comment = False
        elif char == "\\" and quote != "'":
            if text.startswith("\\\n", i):
                i += 2
                continue
            out.append(text[i:i + 2])
            i += 2
            continue
        elif quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (not out or out[-1] in WORD_BREAKS):
            comment = True
        out.append(char)
        i += 1
    return None if quote else "".join(out)


def split_words(text: str, punctuation: str) -> list[str]:
    """Split text the way the shell does, with comments already removed; raise ValueError if unreadable."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=punctuation)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)
