# Home Assistant's configuration at /homeassistant (CX-HA-PATH)

Design only. Nothing in this document is built yet. Base: `release/0.6` at `d130c6d2`, the target of the 0.6 release.

## Goal

The app maps Home Assistant's configuration folder at `/homeassistant`, the Supervisor's default mount point for
`homeassistant_config` and the place the Claude add-on uses. Today `config.yaml` overrides that default with
`path: /config`. After the change:

- `/homeassistant` is Home Assistant's configuration folder inside the container.
- `/addon_config` stays the app's own folder. It does not move to `/config`.
- `/config` does not exist inside the container. Nothing creates it, and no link points it at `/homeassistant`.
- Home Assistant, the Samba share, the file editor and other apps see no change. The mount point is inside this
  app's container only.

Why: an owner who uses both assistants copies paths between them. Today `/homeassistant/automations.yaml` from the
Claude add-on fails here, and `/config/automations.yaml` from this app fails there.

## 1. Where /config is named today

`git grep -c /config` on `release/0.6` (vendor files left out): 360 lines in 96 files. Not every line is a path in the
container. Three groups:

### 1a. Paths in the container: these change

| Place | What names /config | Change |
|---|---|---|
| `codex-cli-worker/config.yaml` | `map: homeassistant_config, path: /config` | Remove the `path` line, so the Supervisor default `/homeassistant` applies; or write `path: /homeassistant`. The build writes it out, so a reader sees the mount point |
| `server.py` constants | `CONFIG_ROOT = Path("/config")`, `TASK_WRITABLE_ROOTS = ("/config", "/share")` | `CONFIG_ROOT = Path("/homeassistant")`; the roots read `str(CONFIG_ROOT)`, one source |
| `server.py` working folder | `cwd="/config"` in 7 calls (Codex starts, sign-in, model list, the app-server, the thread parameters at line 5516) | `cwd=str(CONFIG_ROOT)` everywhere. The thread parameters are sent on resume too, so an old chat resumes in `/homeassistant` |
| `server.py` page instructions | `Workspace: /config` (2 places), the backup rule (`/config/automations.yaml goes to ...`), the secret-file rules | Built from `CONFIG_ROOT`. One new sentence, see section 2 |
| `server.py` docstrings and comments | about 20 lines ("a path under /config", "the /config change review") | Say "Home Assistant's configuration folder" or `CONFIG_ROOT`, so the next move is one line |
| `remote_control.py` | `cwd="/config"` (2 calls) | `CONFIG_ROOT`, imported. Remote Control is paused in 0.6, but its code stays correct |
| `integration_sync.py` | `TARGET_DIR`, `WORK_DIR`, one message string | Built from the same root. This writes the bundled integration to `/homeassistant/custom_components/codex_cli`, which is the same folder on the host |
| `github_access.py` | `config_root: Path = Path("/config")`, three refusal messages ("under /share or /config") | The default from the caller; the messages name `/homeassistant` |
| `codex-bwrap` | `HIDDEN_FILES` (`/config/secrets.yaml`, `/config/.storage/auth`, ...), `READ_ONLY_DIRS="/config/.storage"`, one comment | Same files under `/homeassistant`. `release/0.4` removed these `/config` masks (nothing in the configuration folder is hidden, as in the Claude add-on); after the sync of `release/0.4` into `release/0.6` only the comment may remain. The build rebases on that sync and changes what is there then |
| `apparmor.txt` | `/config/ rw`, `/config/** rwk`, `/config/** rix` (2) | The same rules for `/homeassistant`. No rule for `/config` stays |
| `run.sh`, `Dockerfile` | comments only | Name `/homeassistant`. Neither creates `/config`; the build adds no line that does |
| `web/index.html` | the GitHub link form: "Existing folder under /share or /config", placeholder | `/homeassistant` |
| `starter_agents.md` | 4 lines (`/config` is the configuration folder, `/config/secrets.yaml`, `/config/.storage`, `/config/.git/hooks`) | The paths only. A rewrite of this file is in draft separately; the build changes path text and nothing else, and rebases over that rewrite if it lands first |
| Working folder (after the sync) | `codex_workdir()` falls back to `CONFIG_ROOT`; the log lines name it | Follows `CONFIG_ROOT`, no own change |

### 1b. Not paths in the container: these stay

| Place | Why it stays |
|---|---|
| `/config/app/<slug>/config` and `/config/app/<slug>/info` (`server.py`, `chat.js`) | Home Assistant's frontend address of the app's pages, not a folder |
| `http://supervisor/core/api/config/core/check_config`, `/core/api/config/config_entries/flow_handlers` | Home Assistant API paths |
| `lovelace/config`, `lovelace/config/save` | WebSocket message types |
| `.git/config`, `config.yaml` | File names, not the mount point |
| `chat.js` line 1679, a comment that quotes "Allowed once: ls /config" | Rewrite the example to `/homeassistant`, as a comment fix |

The build checks the stay list with one test (section 4, step 1) instead of by eye.

### 1c. Tests and documents

- Tests: 140 lines in 47 files under `codex-cli-worker/tests/`. Most build fixtures from the constant through
  `tests/app_folders.py`; the rest name `/config` in literal paths, in expected page text and in the sandbox
  checks (`test_bwrap.py`, `apparmor_317_check.py`, `test_secret_guard.py`, `test_tasks_home.py`).
- Documents: `docs/app-reference.md` (43), `CHANGELOG.md` (23 lines, history: only the new entry changes),
  `README.md` (7), `AUDITING.md` (6), `CONTRIBUTING.md` (5), `DOCS.md` (4), `SANDBOX_TESTING.md` (4),
  `AGENTS.md` (3), `docs/updates-and-rollback.md` (2), `custom_components/codex_cli/README.md` (2).
- `.github/workflows/build.yaml` and `ci/build.yaml`: comments and the `config.yaml` file name only. No workflow
  change is needed.
- `codex-cli-worker/policy/`: no line names `/config`. The policy is not touched.

## 2. Upgrade of an existing install

The app's own records move with the constant. Text that the owner wrote does not. Each place:

| What | Where it is | What happens at the first start of 0.6 |
|---|---|---|
| Chats, backups of earlier exchanges, the change review | `/data/codex_tasks`; backup copies are kept by their path relative to the configuration folder | Nothing to do: relative paths restore into `/homeassistant`, which is the same folder on the host |
| A chat's Codex thread | Codex's own records in `/data/codex-home` | Resume sends `cwd` = `/homeassistant` (section 1a). Earlier turns in the thread still say `/config/...`, so Codex may try such a path once. The page instructions get one sentence: "Home Assistant's configuration folder is /homeassistant. /config does not exist here; a path in this chat or in the owner's text that starts with /config/ means the same file under /homeassistant/. Inside Home Assistant's own YAML, /config/ is Home Assistant's own path and stays as it is." This is sent on resume as well |
| The owner's workspace instructions | `/addon_config/AGENTS.md` | Not changed by the app. The scan below reports `/config/` lines |
| The working folder's own `AGENTS.md` and notes | `/media/.agents/codex` (after the sync) | Not changed. Reported by the scan |
| Scheduled prompts | `/data/schedules.sqlite3` | Not changed. Reported by the scan, by schedule name |
| Home Assistant's own files (`configuration.yaml`, packages, `shell_command`, `allowlist_external_dirs`) | `/homeassistant/...` | Not changed, and they must not be: Home Assistant runs in its own container, where its folder is `/config`, so `/config/...` inside its YAML is correct. The scan does not read them, and the page instructions tell Codex not to rewrite them |
| The `task_root` option saved by a version before 0.4 | `/data/options.json` | Only an install that goes from before 0.4 straight to 0.6 still has it. The 0.4 move reads it; a saved value that starts with `/config/` is read as the same path under `/homeassistant/` |

**The scan (one time, read-only).** At the first start of a version with this change, the app reads the three owner
places above and counts lines that contain `/config/` as a path (not `/config/app/`). It never rewrites them: they
are the owner's text, and a wrong rewrite inside a prompt is worse than a path that fails with "No such file".
When the count is not zero, the app:

1. writes one log line per place with the count, and the schedule names (never the text);
2. shows one Home Assistant notification, "Codex: Home Assistant's configuration is now at /homeassistant", which
   names the places to edit and links the chat page. It is dismissed like the app's other notifications;
3. records that the scan ran (a marker file in `/data`), so it does not run again. The marker carries the version,
   and its code has a removal date: remove the scan in the first release after 0.7.

When the count is zero, the app writes one log line and no notification.

**Upgrade note** (CHANGELOG, under `## 0.6.0 (unreleased)`, and one line for the drop list):

> Home Assistant's configuration folder is now at `/homeassistant` inside the app, as in the Claude add-on and the
> Supervisor's default. `/config` no longer exists inside the app. Home Assistant and your files do not move. If your
> workspace instructions, your working folder's notes or a scheduled prompt say `/config/...`, change them to
> `/homeassistant/...`; the app tells you once where it found such a line.

Drop list line: `Config folder now /homeassistant inside the app (like the Claude add-on); edit /config/ paths in your
own instructions and schedules.`

## 3. What can go wrong

| Failure | How the design catches it |
|---|---|
| A `/config` path is missed in code, so a command or a write fails at run time | The step 1 test fails on any `/config` in the worker's Python, shell and page files outside the stay list; the container check fails when `/config` exists in the image |
| A frontend URL such as `/config/app/<slug>/config` is changed by mistake, so the Configuration link breaks | The stay list is in the test, and `browser_fb2_split.cjs` and `browser_update_banner.cjs` already assert the two URLs |
| The sandbox roots, masks or AppArmor rules still name `/config`, so writes fail or a mask covers nothing | `test_bwrap.py`, `test_tasks_home.py` and `apparmor_317_check.py` assert `/homeassistant`; the lab pass repeats the sandbox write in `SANDBOX_TESTING.md` |
| An old chat resumes and Codex uses `/config/...` from its earlier turns | `cwd` is sent on resume; the page-instruction sentence maps the old path; the command fails with "No such file", never writes elsewhere |
| A compatibility link `/config -> /homeassistant` is added "to be safe" | Not allowed: the goal is one name for the folder, as in the Claude add-on, and a link keeps paths working here that fail there, so the move stays hidden. The container check asserts `/config` does not exist |
| Codex "fixes" `/config/...` in Home Assistant's own YAML to `/homeassistant/...`, which breaks Home Assistant | The page-instruction sentence says that `/config/` in Home Assistant's YAML is Home Assistant's own path; a test checks the sentence. The lab step asks for a YAML edit and checks the path is kept |
| The app rewrites owner text wrongly | It never rewrites; the scan only counts and reports |
| The scan reports a secret or a prompt's text | The log and the notification hold counts, place names and schedule names only; a test checks that the text of a planted line is not in either |
| The integration is installed into a folder that is not Home Assistant's | `integration_sync.py` uses the one constant; its tests read `/homeassistant/custom_components/codex_cli` |
| A rollback to 0.5 after the move | 0.5 maps `/config` again and works; the scan marker is ignored by 0.5; the owner's edited text says `/homeassistant/`, which 0.5 cannot read. The rollback section of `docs/updates-and-rollback.md` says so |
| Parity: the Claude add-on maps another path in a later version | The goal is the Supervisor default; the document says so, and a later change is one line in `config.yaml` plus the constant |

No new limit is added (rule of parity with the Claude add-on): the same folders are writable, under a new name.

## 4. Build plan

One pull request to `release/0.6`, after the sync of `release/0.4` into `release/0.6`, rebased over the `server.py`
refactor pull requests that merge first. No version line.

| Step | What | Points |
|---|---|---|
| 1 | `config.yaml` map; `CONFIG_ROOT` and every path in 1a from it; `apparmor.txt`, `codex-bwrap`, `index.html`, `starter_agents.md` paths; a test that lists every `/config` in the worker's `.py`, `.sh`, `codex-bwrap`, `apparmor.txt`, `web/` files and fails on one outside the stay list of 1b; the container check asserts `/config` does not exist and `/homeassistant` is mapped | 1.5 |
| 2 | The tests in 1c moved to `/homeassistant`; the page-instruction sentence and a test that it is sent on resume; `task_root` read with the `/config/` to `/homeassistant/` mapping, with a test | 0.75 |
| 3 | The one-time scan, the log lines and the notification, with the marker and its removal date; tests: a planted `/config/` line in each place is counted, `/config/app/` is not, the text never reaches the log or the notification, a second start does not scan | 0.5 |
| 4 | Documents of 1c, the CHANGELOG upgrade note, the rollback paragraph, and one lab step in `SANDBOX_TESTING.md`: after the upgrade, `ls /config` fails, `pwd` in Read only prints the working folder, a write under `/homeassistant` in `workspace-write` needs no card | 0.25 |
| | Total | 3 |

Steps 1 and 2 must land together: step 1 alone leaves the suite red. Step 3 may be its own commit in the same pull
request.
