# Compatibility drivers

These scripts check that the app and its integration still work on a new Home Assistant Core or OS, a new Codex
CLI, or after a change on the ChatGPT side. Each script is one step: it uses the app the way a person does, on a
lab Home Assistant, and ends with a fixed pass rule. They are not unit tests and `pytest` does not collect them.

A compatibility runner (outside this repository) prepares the lab, starts each step and keeps the results. You can
also run a step by hand against your own lab:

```sh
export COMPAT_HA_URL=http://compat-lab.local:8123
export COMPAT_HA_TOKEN_FILE=~/compat-lab.token
python3 tests/compat/cx01_install.py --core 2026.12.0
```

## Exit codes

| Code | Result | Meaning |
|---|---|---|
| 0 | PASS | the pass rule held |
| 77 | NOT RUN | the step could not run here; the last line says why (no lab settings, no sign-in, no browser) |
| any other | FAIL | the pass rule did not hold, or the driver stopped; the last line says why |

NOT RUN is never counted as a pass. A missing setting, an expired ChatGPT sign-in or a missing browser is NOT RUN,
because it says nothing about compatibility.

## The lab

Run these steps only on a lab Home Assistant that nobody lives with. The steps install and remove the app, restart
Core and edit a file under `/config/compat/`. To prevent a mistake with the address, every step first checks that
the instance has this entity on, and stops with NOT RUN when it does not:

```yaml
input_boolean:
  compat_lab:
    name: Compatibility lab
    initial: true
```

The machine that runs the drivers needs Python 3.12 or later with `requests` and `websocket-client`
(`pip install requests websocket-client`). Without them a driver stops with a traceback, which counts as a FAIL.

The lab also needs:

- an administrator's long-lived access token in the file that `COMPAT_HA_TOKEN_FILE` names;
- for cx-02 on, a ChatGPT sign-in in the app (the runner restores a saved sign-in file before cx-02); cx-02 can
  take five and a half minutes, because the app asks OpenAI about the sign-in at most once in 300 seconds;
- for cx-03, Playwright for Python with Chromium (`pip install playwright`, then `playwright install chromium`);
- for cx-04, `COMPAT_LAB_SHELL`: a command that runs `sh` on the lab with the configuration folder at `/config`,
  for example `ssh compat-lab` to the SSH app.

| Variable | What it is |
|---|---|
| `COMPAT_HA_URL` | the lab's Home Assistant address |
| `COMPAT_HA_TOKEN_FILE` | a file with an administrator's long-lived access token for the lab |
| `COMPAT_ADDON_REPO` | the app repository to install from; default `https://github.com/fatherdoctor/ha_codex` |
| `COMPAT_LAB_SHELL` | a command that runs `sh` on the lab (cx-04) |
| `COMPAT_RUN_DIR` | a folder for screenshots; the runner sets it |

The drivers reach the app's API through Home Assistant Ingress, like the chat page. They never read or print the
app's worker token, and they print nothing from the sign-in.

## The steps

Steps cx-01 to cx-04 are built. The others are planned; a runner reports a step without a script as NOT RUN.

| Step | Script | What it proves | Passes when |
|---|---|---|---|
| cx-01 install | `cx01_install.py` | a new install works: store, install, start, integration written, restart, discovery, setup | the app is started, the integration's entry is loaded, no new ERROR line from either |
| cx-02 sign-in | `cx02_sign_in.py` | the ChatGPT sign-in still works with this version | the app's server check answers ok within 330 seconds; an expired sign-in is NOT RUN |
| cx-03 chat task | `cx03_chat_task.py` | the chat page sends a prompt and shows the answer | the fixed word arrives on the page, the turn completes, no console error |
| cx-04 file edit | `cx04_file_edit.py` | a chat can change a file, and the safety checks after an edit still run | the line is in the scratch file, the rest is unchanged, the previous version was saved and the configuration check is valid |
| cx-05 relay read | `cx05_relay_read.py` (planned) | a chat reads an entity state through the relay read tool | the answer holds the stand-in entity's state |
| cx-06 approval prompt | `cx06_approval.py` (planned) | a request in the default mode reaches the page | the prompt appears; Deny leaves no change; Approve makes it |
| cx-07 pre-approved action | `cx07_preapproved.py` (planned) | an action the owner allowed runs without a prompt | skipped until that feature ships |
| cx-08 refusals | `cx08_gate_refusals.py` (planned) | what the sandbox and the permission modes refuse is still refused | each one is refused with its reason; the file is unchanged |
| cx-09 remote control | `cx09_remote_proxy.py` (planned) | a client can drive `codex app-server` with the remote-control settings | thread start, one turn, one refused request, thread end |
| cx-10 remote control, phone | `cx10_remote_phone.py` (planned) | the ChatGPT app reaches the app | a person starts a thread from the phone and gets the fixed word back |
| cx-11 schedule | `cx11_schedule.py` (planned) | a scheduled chat runs on time | skipped until that feature ships |
| cx-12 dashboard capture | `cx12_capture.py` (planned) | the app's dashboard capture works | an image of the expected size that is not blank |
| cx-13 log export | `cx13_log_export.py` (planned) | the diagnostics export is complete and holds no secret | the file parses, holds the run's chat ids and no token-shaped string |
| cx-14 uninstall | `cx14_uninstall.py` (planned) | the app and the integration remove cleanly | no entity, no entry and no file left outside the documented data folder |

The scratch file of cx-04 is not in the lab's configuration, so nothing loads it. Each chat that a step starts is
saved in the app's chat list like any other chat.
