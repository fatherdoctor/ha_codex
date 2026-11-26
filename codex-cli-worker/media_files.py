"""Where the app keeps the images Codex generates: a dated folder in Codex's working folder in /media.

The layout is `<working folder>/files/<YYYY-MM-DD>/<chat id>/<attachment id><suffix>`:

* /media, not the app's /data: the app's backup holds /data, and Home Assistant's automatic backup leaves /media
  out, so generated images no longer make every backup larger.
* by date, so the owner can find and remove old files from the Media browser, Samba or a terminal; the date is the
  worker's local date when the file is written.
* one folder per chat, named with the chat id, so each file links back to its chat; the chat page still shows
  each file in its turn from the record the server keeps.
* the attachment id (random) names the file, so two files never take the same name.

The worker writes here only when Codex works from that folder (no link anywhere in its path). Below it, a link in
the path stops the walk and returns None; the caller then keeps the file in /data as before.
Nothing here deletes a file: they stay until the owner removes them.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

FILES_FOLDER = "files"


def files_root(workdir: Path) -> Path:
    """The folder that holds every dated folder: <working folder>/files."""
    return workdir / FILES_FOLDER


def chat_folder(workdir: Path, task_id: str, day: date) -> Path | None:
    """Make and return <working folder>/files/<day>/<task_id>; None when it cannot be made as a plain folder.

    Codex may write in its working folder, so any part of the path may be a link or a file. The folders are made
    one at a time below the working folder, and the walk stops at the first part that is a link, so the worker
    never makes a folder through a link.
    """
    folder = workdir
    for part in (FILES_FOLDER, day.isoformat(), task_id):
        folder = folder / part
        try:
            if folder.is_symlink():
                return None
            folder.mkdir(exist_ok=True)
        except OSError:
            return None
    return folder if folder.is_dir() else None
