# Instructions for Codex in this home

This file is `AGENTS.md` in the app's own folder (`/addon_config` inside the app). Codex reads it when a chat starts. The app wrote this starter text once, on its
first start, because the home had no instructions yet. The file is yours: change it, add the rules of your home, or
delete it. The editor is in the chat page under Settings > Workspace instructions.

## Where things are

- This machine runs Home Assistant for a home that people live in. Every change here is a change to a live home.
- `/config` is the Home Assistant configuration folder: `configuration.yaml`, `automations.yaml`, `scripts.yaml`,
  `scenes.yaml`, and the folders beside them.
- `/config/secrets.yaml` and `/config/.storage` hold passwords, tokens and Home Assistant's own records.
- `/media/.agents/codex` is your working folder. Put your notes, scratch files and drafts there, not in `/config`.
  The automatic backup does not keep it.
- Other folders under `/share` and `/media` belong to other apps or to the owner, for example another assistant's
  folder or a clone of a repository.

## Rules

1. Change the home only on the owner's word. Switch a device, lock or unlock a door, open or close a cover, arm or
   disarm an alarm, change an automation, script or scene, or reload or restart Home Assistant only when the owner
   asked for exactly that in this chat. When a request does not clearly ask for the change, say what you will do
   and wait for a yes. You may read states, history and files without asking.
2. Keep secrets secret. Open secret files only when the owner asks, and never paste them into chat. Never show,
   copy, send or commit a password, token, key or door code. Refer to a secret by its name, for example
   `!secret mqtt_password`.
3. Keep the old version. Before you change or delete a file that already exists, save its earlier version the way
   this app tells you to. In your answer, name each file you changed and say how to get the old version back.
4. Check before a restart. After you change YAML, run the Home Assistant configuration check. If the check fails,
   fix the file or put the old version back. Never reload or restart on a failed check.
5. Leave `/config/.storage` to Home Assistant. Do not edit its files while Home Assistant runs: it writes over
   changed files from memory. Use the API or settings pages.
6. Make one small change at a time, and do only what the owner asked for. Suggest other improvements; do not make
   them unasked.
7. When you are not sure what the owner means or what a change will do, stop and ask.

## Safeguards: defaults the owner can change

For the owner of this home: the rules in this file are defaults, not locks in the app. The owner can edit or
delete any of them in Settings > Workspace instructions. The app enforces only the sandbox and the permission
mode of each chat.

- Secrets: rule 2 also covers PINs and the files in `.storage`.
- Protected places: ask before you touch `/config/secrets.yaml`, `/config/.storage`, `/config/.git/hooks` or
  `/share/HomeAssistant` (the app's GitHub clone and link also leave the three folders alone).
- Other apps' folders: ask before you change or delete a file under `/share` or `/media` outside
  `/media/.agents/codex`, unless the owner asked for that change in this chat. Reading them needs no question.
- Backups: keep the old version of each file as rule 3 says, in the folder that the app names. Make no other copy.

## Memory

Keep facts that last between chats in `memory/` in your working folder: one fact per file, and an index
`memory/MEMORY.md`, one line per file. Read the index when a chat starts. Save what the owner asks you to remember
and lasting corrections; update a file rather than add a second; delete wrong facts. No secrets there; never commit
`memory/`.

## This app's sensors

The Codex device holds this app's entities: sign-in, usage limits and resets, running chats (Active tasks), the
last chat's status and error (Last task), and permission prompts.

## Git and GitHub

- Before every commit, look at what is staged. Remove anything that does not belong.
- Never stage `secrets.yaml`, `.storage`, `memory/`, databases, log files, backups, or personal or location data.
- Keep a `.gitignore` that lists these. Add it before the first commit.
- Ask the owner before the first push to any new or public repository. A new repository is private by default.

## About this home

Add what Codex cannot know by itself. For example: the names people use for rooms and devices, devices that must
never be switched off, hours when nothing in the home should change, and who decides.
