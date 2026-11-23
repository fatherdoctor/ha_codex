# Bring slash commands back to the chat page

Design only. No code changes in this pull request. Target: version 0.6; a later version is acceptable.

## Recommendation

Turn the slash list on again with a short set of commands that do what the Codex CLI's commands of the same name
do. Each command in the list is one of two kinds:

1. **Codex commands.** The page asks the worker, and the worker sends one `codex app-server` request for the open
   chat: `/status`, `/compact`, `/review` and `/mcp`.
2. **Page commands.** The page runs one of its own controls, as it did before 0.5. Only commands with the same name
   in the CLI stay: `/new`, `/stop`, `/model`, `/rename`, `/export` (the old `/share`), `/archive` and `/delete`.
   `/usage` stays as the second name of `/status`.

A message that starts with a slash and names no command in the list goes to Codex as plain text, as in 0.5. The
page blocks nothing that it does not list.

Why: in 0.5 the page sends every slash message to the model as text. The model then often tells the user to type
`/status` or a similar command, and the page has no such command. The CLI and the desktop app have these commands,
so a user who knows Codex expects them. The four Codex commands are the ones a user of the chat page can use
without a terminal: they read state or act on the open chat, and they change no file.

## What the pinned Codex offers

Read from the source of the pinned Codex CLI 0.166.0 (`codex-rs/tui/src/slash_command.rs` and
`codex-rs/app-server-protocol/src/protocol/common.rs` at tag `rust-v0.166.0`). That version marks none of the requests
below as experimental. A build checks each one again against the pinned binary.

| CLI command | What the CLI does | `app-server` request | On the page |
|---|---|---|---|
| `/status` | session settings, token use, rate limits | `account/rateLimits/read`, `thread/tokenUsage/updated` (a notification), the chat's own settings | **Codex command** |
| `/compact` | summarize the chat to free context | `thread/compact/start` | **Codex command** |
| `/review` | review the current changes | `review/start` | **Codex command** |
| `/mcp` | list the MCP servers and their tools | `mcpServerStatus/list` | **Codex command** |
| `/new`, `/rename`, `/archive`, `/delete` | chat actions | `thread/start`, `thread/name/set`, `thread/archive`, `thread/delete` | page command (the page's own controls already do these) |
| `/model` | model and reasoning effort | per turn | page command (opens the picker; the old `/reasoning` folds into it, as in the CLI) |
| `/stop` | in the CLI: stop background terminals | `thread/backgroundTerminals/clean` | page command, as before: stop the running answer (see "Open points") |
| `/export` | save the chat as markdown | none (client side) | page command (the old `/share`: download the chat) |
| `/usage` | account usage | `account/rateLimits/read`, `account/usage/read` | second name of `/status` |

The page commands `/size`, `/settings`, `/pin` and `/connection` have no CLI twin. They leave the list. Their
controls stay on the page.

## Not in this design

These CLI commands do not come to the page in this design. The page does not block them: typed, each one goes to
Codex as text, as in 0.5. This design adds no limit and keeps none.

| Group | Commands | Why not now |
|---|---|---|
| Terminal display | `/vim`, `/keymap`, `/theme`, `/statusline`, `/title`, `/pets`, `/raw`, `/tui`, `/copy`, `/clear`, `/quit`, `/exit`, `/ide`, `/voice`, `/app`, `/daemon` | They change the terminal program. The page has its own controls. |
| Set by the app's options | `/permissions`, `/config`, `/cd`, `/pwd`, `/logout`, `/setup-default-sandbox`, `/experimental` | The page and the app's options already set these (permission mode, working folder, sign-out). A second way to set them is a second place to keep correct. |
| Debug | `/debug-config`, `/rollout`, `/feedback`, `/test-approval`, `/warnings`, `/debug-m-drop`, `/debug-m-update` | For Codex developers. |
| Candidates for later | `/fork`, `/resume`, `/goal`, `/plan`, `/side`, `/btw`, `/diff`, `/init`, `/mention`, `/agents`, `/subagents`, `/ps`, `/recap`, `/approve`, `/skills`, `/memories`, `/hooks`, `/plugins`, `/apps`, `/presets`, `/import`, `/worktree`, `/daybreak` | Each needs its own page view or a decision on how it fits the app. A later design lists them one by one. |

## How the commands run

- **Page.** `SLASH_COMMANDS_ENABLED` becomes `true` in `codex-cli-worker/web/assets/chat.js`, and the list holds
  the commands above. Each Codex command has `kind: "codex"`. Its `run` sends `POST /tasks/<task_id>/command` with
  `{"name": ..., "args": ...}`. `/review` and `/compact` take the rest of the line as an argument (the CLI does the
  same for `/review`). The list keeps today's keys (arrows, Enter or Tab, Escape) and its `available()` rules.
- **Worker.** One new module, `codex-cli-worker/slash_commands.py`, holds the request for each command and the
  shape of its result. The route in `server.py` only checks the request and calls the module. Nothing about the
  commands goes into the long message handlers.
- **Which process.** A Codex command needs the chat's thread in a running `codex app-server`. With `warm_start` on,
  the worker uses the chat's thread in `codex_host` (it wakes the chat first, as for a message). With `warm_start`
  off, `/status` and `/mcp` use a short `app-server` process, as the usage and model reads do today;
  `/compact` and `/review` go through the message queue as an exchange of their own kind, so they run in the same
  process path as a message.
- **When.** As in the CLI: `/compact`, `/review`, `/archive` and `/delete` are not in the list while the chat runs
  an answer or has a queued message. `/status`, `/mcp`, `/stop` and `/new` are always in the list.
- **Permissions.** `/review` and `/compact` are turns in the chat. They run with the chat's permission mode and
  sandbox, and any permission request shows the normal card. `/status` and `/mcp` only read.

## How the results show

- `/status` and `/mcp` show a card in the chat that the page makes: a title, then label and value rows. The card
  is not a message: it does not go to the model, and the chat history does not keep it. It closes with the chat or
  a close button.
  - `/status`: model, reasoning effort, permission mode, working folder, the instruction files in use, plan,
    tokens used in this chat and its context window, and the rate limits (the same numbers as the usage panel).
    The worker records the last `thread/tokenUsage/updated` for each chat; until a turn has run, the row says
    "no answer yet".
  - `/mcp`: each MCP server, its state, and the names of its tools. No tool arguments and no secrets.
- `/compact` shows one line in the chat when Codex sends its context-compaction item, as the CLI does.
- `/review` shows as an answer in the chat. The review findings are the answer text. The worker sends the review
  turn's items through the same path as a message, so the work log, cards and stop button work as usual.
- A failed command shows one line in the chat with the reason (for example: "Codex could not start the review: no
  changes found").

## Failure cases

| What can go wrong | How the design catches it |
|---|---|
| A user types a CLI command the page does not list | It goes to Codex as text, as in 0.5. Nothing is blocked and no error shows. |
| The model still tells the user to type a command the page lacks | The four most common commands now exist. The rest are in "Not in this design" for a later design. |
| The pinned Codex changes a request name or its fields | The build adds a test for each request shape, and the pin check (`tests/test_fork_pins.py`) makes a Codex update its own pull request, where those tests run. |
| `/compact` or `/review` starts while an answer runs | They are not in the list then, and the worker refuses them with the same busy rule as a message. |
| The host pauses the chat between the click and the request | The worker wakes the chat first, as it does for a message; if the host has no room, the command waits in the queue. |
| `warm_start` is off | `/status` and `/mcp` use a short process; `/compact` and `/review` use the message path. Both ways get a test. |
| `/review` in a working folder with no git repository | Codex answers with its own error. The page shows it as a failed command line. |
| A status card shows a secret | The card shows only the named fields above, from the worker. The diagnostics scrub (`diagnostics_scrub.py`) runs over the values, and a test feeds a token-shaped value. |
| The command list hides a control that a user needs | Every removed page command keeps its control on the page (text size, Settings, pin). |
| A screen reader user cannot use the list | The list keeps today's `aria` attributes, which 0.5 removed when it turned the list off. |

## Build plan

Each step is its own pull request into the working branch, with tests that fail without it. Points: 1 point is a
small unit of build effort on the maintainer's scale.

1. **Page list and page commands** (1 point). Turn the list on; keep `/new`, `/stop`, `/model`, `/rename`,
   `/export`, `/archive`, `/delete`, `/status` (`/usage`); remove `/reasoning`, `/size`, `/settings`, `/pin`,
   `/connection` from the list. `/status` opens the usage panel until step 3. Update `tests/browser_slash.cjs`,
   `docs/app-reference.md` and the changelog.
2. **Worker route and module** (1.5 points). `slash_commands.py` and `POST /tasks/<task_id>/command`, with
   `/mcp` (`mcpServerStatus/list`) as the first command and the page card for it. Unit tests for the request shape,
   the refusal while busy and both `warm_start` settings.
3. **`/status`** (1 point). Record the last `thread/tokenUsage/updated` for each chat; build the status card. Test
   the token-shaped value case.
4. **`/compact` and `/review`** (2 points). Both as exchanges of their own kind; show the compaction line and the
   review answer. Tests with a fake `app-server` for each item and for "no changes".
5. **Lab check** (0.5 point). On a test machine: each command once, with `warm_start` on and off. Record what the
   chat shows in `codex-cli-worker/SANDBOX_TESTING.md`.

Total about 6 points.

## Open points

- `/stop`: in the page it stops the running answer; in the CLI it stops background terminals and the Escape key
  stops the answer. This design keeps the page meaning, because users of the page already know it and the page has
  no background-terminal view. A later design can rename it if the two meanings confuse users.
- The starter `AGENTS.md` that the app ships (`codex-cli-worker/starter_agents.md`) could name the page's
  commands, so the model points users to real ones. That is one sentence in step 1 or its own small pull request.
