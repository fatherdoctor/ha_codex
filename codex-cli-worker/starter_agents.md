# Instructions for Codex in this home

This file is `/config/AGENTS.md`. Codex reads it when a chat starts. The app wrote this starter text once, on its
first start, because the home had no instructions yet. The file is yours: change it, add the rules of your home, or
delete it. The editor is in the chat page under Settings > Workspace instructions.

## Where things are

- This machine runs Home Assistant for a home that people live in. Every change here is a change to a live home.
- `/config` is the Home Assistant configuration folder: `configuration.yaml`, `automations.yaml`, `scripts.yaml`,
  `scenes.yaml`, and the folders beside them.
- `/config/secrets.yaml` and `/config/.storage` hold passwords, tokens and Home Assistant's own records.

## Rules

1. Change the home only on the owner's word. Switch a device, lock or unlock a door, open or close a cover, arm or
   disarm an alarm, change an automation, script or scene, or reload or restart Home Assistant only when the owner
   asked for exactly that in this chat. When a request does not clearly ask for the change, say what you will do
   and wait for a yes. You may read states, history and files without asking.
2. Keep secrets secret. Never show, copy, send or commit a password, token, key or door code, and never paste the
   content of `secrets.yaml`. Refer to a secret by its name, for example `!secret mqtt_password`.
3. Keep the old version. Before you change or delete a file that already exists, save its earlier version the way
   this app tells you to. In your answer, name each file you changed and say how to get the old version back.
4. Check before a restart. After you change YAML, run the Home Assistant configuration check. If the check fails,
   fix the file or put the old version back. Never reload or restart on a failed check.
5. Leave `/config/.storage` to Home Assistant. Change what it holds through the Home Assistant settings pages or the
   API, not by editing the files.
6. Make one small change at a time, and do only what the owner asked for. Suggest other improvements; do not make
   them unasked.
7. When you are not sure what the owner means or what a change will do, stop and ask.

## About this home

Add what Codex cannot know by itself. For example: the names people use for rooms and devices, devices that must
never be switched off, hours when nothing in the home should change, and who decides.
