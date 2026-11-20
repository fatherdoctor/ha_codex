# Changelog

## 0.4.0 (unreleased)

### What's new
- **A warning when something needs you.** On the chat page, the Settings cog turns into a warning symbol while
  something needs you. On a phone, the conversations button shows it too. Yellow means an approval or a schedule
  proposal waits. Red means ChatGPT is signed out or unreadable, the GitHub sign-in expired or was revoked, or Remote
  is on and lost its connection. Use **Dismiss the warning** to hide it until something new happens; the ChatGPT
  warning cannot be dismissed.
- **New Courtyard mark.** You see it in the app icon and logo, the integration's brand images, the chat page's tab icon
  and header mark, and the README logo. The mark in the page header follows the page's theme (the Home Assistant theme
  when the page runs inside Home Assistant), not the system's light or dark setting. The tab icon still follows the
  system.
- **No model pinned by default.** The **Model pin** option is now empty, and empty means your account's default
  model. A value you set still pins the app. On the first start of this version, an install that still has the old
  default value (`gpt-6.1-sol`) has it cleared once, with a line in the app log. If you chose that model on purpose,
  set it again. A value you set later is kept.
- **Steps stay closed while Codex works.** Under the "Working on your request…" line, the steps stay closed until you
  tap it. Your choice holds until the turn ends. Then the steps close under "Worked for…" as before. A permission
  request stays visible outside the closed list.
- **Cleaner chat page buttons.** The plus on the attach button is now an icon centered in a round button, on a desktop
  and a phone. The **New chat** plus is a 20 px icon in the same style. The extra plus button beside the chat title on
  a phone is gone; **New chat** stays in the conversation list. The banner across the top of the page while Full
  access is on is gone. In a chat's Permissions menu, **Full access** is shown in bold while the setting is on, and
  the other choices are not.
- **The lead agent channel and the local mailbox are removed.** The lead token, its Settings section, the `/lead/`
  routes and the `/schedule-mailbox` routes are gone, and Codex no longer has the `read_mailbox` and `post_mailbox`
  tools. Saved schedules are kept. Saved messages stay in the schedule file, unread, so an older version still finds
  them after a rollback. The chat page also no longer shows the lead agent: an old chat that the lead channel started has
  no "Started by lead" label, and the schedule editor no longer mentions lead runs.
- **Starter `AGENTS.md` safeguards.** The starter file has a short section of safeguard defaults (secrets, protected
  places, backups). It says they are defaults you can change or delete, not locks in the app.
- **Clearer documentation.** The README, the integration README, `SECURITY.md`, `DOCS.md` and three option
  descriptions now say what the code does. The README no longer promises a question before every change, or a saved
  copy and a configuration check after every change; it says when each one happens. It says that an automation with
  no user may call the `codex_cli.*` actions. `DOCS.md` has a new section, "Task time limits", for
  `task_timeout_seconds`. The integration README's install steps match the app's own install of the integration. The
  usage sensors' two sources are named. The **Full snapshot** and **Codex sandbox** option descriptions say what a
  snapshot leaves out and where Full access is turned on. The support page, the bug report form and the update and
  rollback document speak for the project, not for one person, and give versions of this project in their examples.
  The changelog starts at this project's first version. `screenshots/SCREENSHOTS.md` lists the screenshots; the root
  `SCREENSHOTS.md` is removed.
- **A richer command list.** The command list above the message box now shows an icon for each command and marks the
  letters you typed, and a press outside the list closes it. New commands `/pin`, `/share`, `/archive` and `/delete`
  do what the chat menu does, and `/connection` opens the remote connection and its status. `/help` and
  **Settings** > **Commands** are gone: the list in the message box is the only one. When you close a dialog that a
  command opened, the cursor stays in the message box.
- **Changed: the workspace instructions file moved out of `/config`.** `AGENTS.md` now lives in the app's own
  `addon_config` folder (`/addon_configs/<app folder>/AGENTS.md` on the host), not in the top level of `/config`.
  Codex loads it as its global instructions, and **Settings** > **Workspace instructions** reads and writes it there.
  On the first start of this version, a file an older version left in `/config` is moved, not copied, after a backup
  copy at `/data/agents_before_move.md`. If both places hold a file, nothing moves and the app logs a line.

### Changed
- **No more checks after each answer.** The app no longer judges a turn after Codex answers: it takes no dashboard
  screenshots of its own (they could catch a dashboard still loading), adds no "Verification needs review" note and
  no "No previous version saved" lines. Those lines blamed the turn for files that something else, such as an
  automation or another app, changed while it ran. Codex checks its own work during the turn, as its rules say. The
  per-file copies of the files Codex changes are still made, and a change that fails the configuration check still
  points to them. The `backups` field is gone from the task API, `codex_cli.get_task` and the
  `codex_cli_task_result` event.

### Fixed
- The "Codex" label with the command symbol no longer sits above each reply on the chat page. Screen readers still
  announce each reply as "Codex reply"; the reply's time and status line stay.
- The mark in the chat page header now follows the page's theme instead of the system's light or dark setting. Open
  the chat page inside Home Assistant with a theme that differs from your system setting, and the mark matches the
  theme.
- The chat page's rules no longer tell Codex how to answer a question about Codex cloud tasks (the
  `codex cloud list --json` line from 0.3.4). That line belonged to a feature that is not built; it returns with the
  feature. Codex can still run those commands when you ask it to.
- **Repairs go away with the integration.** The sign-in, Remote control and GitHub repairs are deleted when the
  Codex integration is unloaded or removed. Before, an owner who removed the integration kept a "sign in" repair
  that nothing would clear.
- **The Codex device is shown as a service.** Home Assistant now marks the Codex device as a service (an app that
  runs on the same machine), not as a piece of hardware.

### Known issues
- Remote control still uses its own keys. The tool requests of a chat no longer do (see "Under the hood").
- `/status` reports the mailbox as not available, so the integration's unread-mail sensor shows unknown until a later
  version of the integration removes it.
- The new Codex host (see "Under the hood") is not connected. Every message still starts its own Codex process.

### Under the hood
- Tool requests no longer use a secret key for each message. Each request names its chat by the thread id that Codex
  adds to every tool call, and the worker answers it only while that chat's exchange runs. A sub-agent that the chat
  starts counts as the chat, and a thread that one chat holds is never given to another. The sandbox already stops a
  command in Read only, Default and Auto-review from reaching the tool sockets.
- A new module, `codex_host.py`, can keep one Codex process running and hold each chat as a thread in it, with at
  most three chats loaded at a time. Nothing starts it in this version. A later change connects it, with an option to
  turn it off.
- The worker no longer contains the `codex exec` start path kept from upstream; every exchange already ran through
  `codex app-server`. No behavior changes: the modes the protected policy refuses are still stopped before Codex
  starts. A new test fails if any shipped module names the `exec` subcommand. The approval smoke test now runs in the
  built image, against the Codex CLI that ships.
- Two new checks run the packaged Codex CLI against a local stand-in model: one for the tool sockets in each mode, one
  for how `codex app-server` handles several chats in one process.
- Clean-up: five unused chat page styles are removed, and so are the unused `CORE_URL` and `ALLOW_MARK` constants and
  three code comments that named an audit record the app does not keep. The three test files that prove a removed
  part stays removed are now one, `tests/test_removed_surfaces.py`. `AGENTS.md` no longer describes the bundled house
  rules file, which was removed in an earlier version. The schedule store lost the code paths only the lead channel
  used: Codex can propose a new schedule but not edit a saved proposal, and a run is always reserved for one schedule.
  A bare old lead token in Codex output is still redacted, because a rollback to 0.3 makes it valid again.
- The permission bridge (`approvals.py`) can now run one turn on a Codex process that it does not own. In that mode it
  sends only `turn/start` on a thread that is already loaded, with the chat's model in the turn, and it uses request
  IDs that no other chat on the process uses. A cancel sends `turn/interrupt` and leaves the process input open for
  the other chats. Nothing uses this mode yet. Each exchange still starts its own Codex process.

## 0.3.6 - 2026-11-18

- The app's own guards are removed. Codex's own permission modes and the sandbox are now the whole protection, as in
  the Claude add-on. Gone: the command check that ran before each tool call (the hook and its rules), the managed
  command-prefix rules, the refusal of named paths on the approval card (a command or file change that names the
  app's settings or a private file can now be allowed once), the check after each exchange that undid a planted
  shell, REST or Python command, the config watch and its **Mark reviewed** button, the self-tests in front of remote
  control (**Turn on** is always available), and the GitHub read-only limit (a Read only chat can pull and push; Read
  only is Codex's read-only sandbox only). Kept: sign-in, the sandbox and its hidden files, Codex's managed
  requirements (no full access unless the owner turns on **Full access**, only `on-request` approvals, and no hooks, so a hook planted in `/config` never
  runs), the read tools, the GitHub push guard and the schedule tools. Review what a chat changed before a reload or
  restart: an edit that adds a command now stays as Codex wrote it.
- Home Assistant shows a Codex notification only while a chat waits for you: a question, or a permission request. A
  failed action or a failed chat no longer makes one; the failure shows in the chat. The one exception is a block by
  OpenAI's safety filter, which still makes one notification, `codex_safety_block_<task_id>`, until you dismiss it. Each waiting chat has one
  notification, `codex_cli_wait_<task_id>`, and the app dismisses it when you answer, stop or delete the chat, when
  the request expires, or when the app restarts, unless the chat waits again by then. With a companion app notify
  service, the push carries the same id as its tag, and the app clears it from the phone at the same time. A tap on a
  phone notification that the worker refuses no longer makes a notification either; the integration logs why. The
  sign-in and GitHub notifications are not about a chat and are unchanged.
- The Permissions button offers the four modes of the ChatGPT app: Default, Auto-review, Read only and Full access.
  Each mode is only Codex's own settings, passed when the app starts Codex for a chat
  (`codex-cli-worker/DOCS.md`, "Permission modes"). Auto-review hands each request to Codex's own reviewer, and
  the chat shows what it allowed or denied. Full access runs Codex without its sandbox and without questions; it is
  off until an administrator turns it on in **Settings > Full access** and confirms a warning. The app's own checks
  of these settings are removed: the protected policy no longer pins the approval policy and reviewer, the chat
  page no longer refuses a thread's settings, and remote control no longer checks the managed requirements file.
- **Text size** for the chat page: Normal, Larger (115 %) or Largest (130 %), from **Settings > Text size**. It
  scales the messages, code, buttons, the chat list, Settings, the sign-in text, the schedule dialog and the list of
  steps together, is remembered on each device, and still fits a phone screen at Largest.
- **Slash commands** in the message box: typing `/` lists `/new`, `/stop`, `/model`, `/reasoning`, `/rename`,
  `/usage` (or `/status`), `/size`, `/settings` and `/help` with a line on each, filtered as you type and picked by a
  tap or with the arrow keys and Enter. Only the commands the page can run at that moment are listed. The page runs
  them itself; they are never sent to Codex. **Settings** > **Commands** lists them.
- A new install starts with workspace instructions. On its first start, the app writes a short starter
  `/config/AGENTS.md` when the home has none: what `/config` is, and seven general rules for Codex (change the home
  only on the owner's word, keep secrets secret, keep the old version of a changed file, check the configuration
  before a restart, and three more). The file is yours to change or delete under **Settings** > **Workspace
  instructions**. The app never replaces an existing file and writes the starter once only, so an install that has
  started before is not changed by this update. The text is `codex-cli-worker/starter_agents.md`.
- Scheduling moves to a chat's menu. **Schedule** opens one editor with a name, a prompt, a model and a trigger:
  Hourly, Daily, Weekly or Custom. Custom keeps every earlier timing: every N minutes or hours, a cron line, or one
  date and time. Permissions, the schedule's state, its recent runs, Pause and Delete are folded under **Advanced
  settings**. A new schedule starts from the chat's title and first message, and each run still starts a new chat.
  A schedule can now name its own model; one that names none follows the add-on's model as before. The form in
  **Settings > Scheduled runs** is gone. The list stays there, with every schedule and the proposals that wait for
  your approval, and its **Edit** button opens the same editor. Schedules saved by an earlier version keep running
  unchanged: they belong to no chat, so open them from Settings or from the menu of a chat one of their runs started.
  The lead agent and Codex can still only propose a name, a prompt, a mode and a timing.
- A device command takes one step instead of three. Codex can give the device's name, as you said it, to the
  `home_assistant_call` `call` tool in place of the entity id. The worker sends the call only when exactly one
  entity of the service's domain has that name (whole words; an exact name wins over longer ones), and its
  answer reads back the state right after the call. When none or several fit, nothing is sent and Codex asks you which one. You
  still answer Codex's question before each call.
- The app's own grant layer is removed: the five pre-approved action groups (Settings > Permissions overrides), the
  guard-rails block they read from `/config/AGENTS.md`, and the `home_assistant_actions` `act` tool with its
  **Allow once** card. Codex's own permission requests are now the only way a change to the house is approved. A
  device command still finds the entity and reads its state back with the `home_assistant` tools; the change itself
  now needs your normal **Allow once**, as every other action does. A test build that used the groups leaves
  `/data/codex-home/house-rules/permission-overrides.json`, `/data/house-rules/confirmed.json`,
  `/data/permission-overrides-backups` and `/data/permission-overrides.sock` behind; the app no longer reads them, and you can delete them.

- A typed OpenAI safety-filter block now stops the task and sends one notice, through the notify service when one is
  set and as a persistent notification otherwise; the general "Codex task failed" notice is not sent for it.
  The notice names the task by id and says to rephrase or retry. The worker does not retry the block automatically.
  It also fires `codex_cli_safety_blocked`, without the prompt or provider error text.
- A chat on the page changes the house through one new tool, `home_assistant_call` `call`: one Home Assistant action
  (a service call), reloads and restarts included. Codex asks you each time, with no remembered answer, and shows
  the service, the target and the data. The tool gives Codex no
  token: the worker sends the call once, with its own access. It refuses no service by name: your answer is the
  gate, and a call can reach the Supervisor (for example `update.install`) or run your own `shell_command`. A
  scheduled run does not get the tool. The keys of the app's tools are now kept out of the commands Codex runs. The
  instructions point changes to the tool; `HA_TOKEN` is now only for an API call that is not a service.
- The message box no longer has a hint line under it ("Continue this conversation with its saved context." and the
  new-chat and queue versions); the note above the box still says when a message waits in the queue. The Permissions
  button's chevron and the send button's arrow are now icons centered on their buttons, as in the ChatGPT desktop app,
  instead of text characters that sat low on the line. `tests/browser_composer.cjs` checks both on a phone and a
  desktop, in light and dark.
- A chat's menu now lists Rename, Pin, Share, Archive and Delete, in the ChatGPT app's order. **Share** downloads
  the whole chat, or its last lines, as a Markdown file with every message, the steps Codex printed (thinking
  included) and each answer, with values that look like keys or tokens masked, and also a bare JSON Web Token, an `sk-` key, a labeled password, secret or pin and the password in a URL (`GET /tasks/<task_id>/share`). The menu no longer has New chat, Conversations, Copy
  thread ID or Status, and the conversation header no longer has its own menu. Settings no longer has Interaction logs with its Export logs button; the
  integration's Download diagnostics carries the same anonymized data.
- The foot of the chat list shows **Codex**, **GitHub** and **Remote**, each with a word or two (for example
  "Codex · Ready", "GitHub · Not connected", "Remote · Off") and a dot. Selecting one opens Settings at that part
  of Accounts and connections. They replace the Home Assistant row and the Remote connection row; the integration's
  state still shows as a line at the top of the chat when it needs you. The Status window is removed: its Context
  and Usage and reset fields always showed Unavailable, because the app reads no context figure from Codex, and the
  quota and reset times already show under the rows. The Codex access line moves to the ChatGPT account part of
  Settings. `tests/browser_fb2_band.cjs` and `tests/browser_account_access.cjs` check the rows, their states and
  where each one opens.
- In **Settings**, one top-level section is open at a time: opening one closes the others. **Browser permissions**
  moves into a new **Advanced** section at the bottom of Settings, folded by default; nothing about it is removed.
  The **Add-on configuration** row now opens the app's Configuration tab. It linked to `/hassio/addon/<slug>/config`,
  a page Home Assistant 2026.2 moved to `/config/app/<slug>/config` and 2026.5, this app's minimum, removed, so the
  row opened a page that does not exist. `tests/browser_fb2_split.cjs` checks the sections and the link.
- **Pause the assistant** is removed: the Assistant part of Settings, the banner above the chat with **Resume the
  assistant**, the `GET` and `POST /pause` routes and the `pause` field of `GET /status`. New chats, replies, queued
  messages, scheduled runs and lead-agent tasks no longer wait on it. To hold the work that starts on its own, pause
  each schedule in **Settings > Scheduled runs**, and rotate the lead token to stop the lead agent. **Stop task**
  still stops a running task. A `/data/paused.json` left by an earlier build is no longer read; an install that was
  paused runs again after the update, and you can delete the file.
- The chat page shows Codex's own permission request for the Home Assistant tool (`call` of the `home_assistant_call` server) as a card:
  the tool, the action in plain words (naming each entity, area, device, floor and label of its target), and every argument as JSON, including the full text of a message, with
  **Allow once** and **Deny**. **Allow once** allows that one call; Codex asks again before the next one. A request
  that cannot be shown in full has **Allow once** turned off. Questions from every other tool are still declined, and the
  chat says so.

- The ChatGPT app gets the same `call` tool while remote control is on. Codex asks in the app before each call; the
  tool is not approved ahead. Its key is new at each start of remote control and stops working when remote control
  stops. The app's instructions now point changes to the tool too. The managed Codex config in the image
  (`/etc/codex/managed_config.toml`, one setting, no hooks) keeps the tool keys out of the commands Codex runs for
  every client.

- The GitHub messages say what still works and what to do next. The Home Assistant repair for a sign-in that
  expires soon now says that GitHub still works and gives the date; before, it said the assistant could not fetch,
  pull or push. A token connection that expired or was refused is sent to Use a token instead, because Connect
  again starts an App sign-in. The repair has its own text for access changed and for cannot be checked. A failed
  Connect again now shows its reason on the page while the old sign-in stays; before, the page hid it. A refused
  GitHub change names GitHub ("Only a Home Assistant administrator can manage GitHub.") instead of permission
  requests or browser permissions, and the Read test and two repository refusals give a next step. Who may make a
  change, and every check, stay the same.
- Settings and its windows (Status, Remote connection, Permissions, Workspace rules) look more like Home Assistant's
  own settings pages. Text, number and list fields are filled fields with a line under them, as on the add-on's
  Configuration tab. Each part's main button is filled in the primary color, and the other buttons are text buttons.
  The close buttons and the check mark of the chosen permission mode use Home Assistant's icons. The page now takes
  Home Assistant's font, and an answer's lines sit a little closer. The page copies its theme from Home Assistant,
  so a custom theme and dark mode reach every field, switch and button. Opened alone, outside Home Assistant, the
  page keeps its own colors. No setting, permission or approval rule changed.
- The two bands above the chat move to the foot of the side bar. One thin row above the messages keeps the
  chat title and the chat menu. The foot of the side bar holds Home Assistant with the integration state (opens
  Status), Remote connection with its state (opens the pairing window), the 5-hour and weekly quota with the plan
  type and reset times, and Settings. A row that needs your attention shows an amber dot. While a task runs, the
  send button is the stop button, even with a draft or a file in the message box.
- "Your chats" is one list card with one line for each chat: an icon, the title and its age. The open chat has a
  light tint. The divider between the side bar and the chat is now visible, resizes from 200 to 480 px, and goes
  back to 260 px on a double-click.
- The chat reads more like the desktop app. A gray "Worked for" line above each answer opens its checks, and on the
  latest answer its steps too (the worker keeps the steps of the latest exchange only). A failed check keeps one
  short line under the answer. An answered or expired permission request is one
  closed line that opens to the same details. On a wide screen the chat and the prompt box share one 720 px
  column. These are display changes only: no approval, permission or sign-in rule changed.
- A permission request that waits for you now shows at the bottom of the chat, below the "Working" lines, and the
  chat scrolls to it once when it first appears. Before, it sat above those lines and was easy to miss. A request
  you already answered stays where it was. No approval or permission rule changed.
- Four small fixes from upstream 0.1.82, ported by hand. A command or file request that arrives before Codex has
  started the exchange is now refused and stops the exchange; before, a request with an empty turn id matched the
  empty turn id of an exchange that had not started. A request that repeats the id of a request still waiting
  stops the exchange also after the limit of 50 requests; before, it was denied like a new request. Codex's plan
  shows as one to-do step in the activity list, updated in place; a plan update of the wrong form is ignored, and
  the text is redacted like every other step. A chat rebuilt from the task index after its `task.json` was lost
  keeps the turn id of its latest exchange, so the steps saved for it stay linked. Nothing from upstream's Full
  access changes is included.
- Settings keeps what needs a button, a sign-in or a confirm step. Plain values move to the app's Configuration
  tab, so each value has one place to change it. A new option, Interactions to keep (10 to 100), replaces the field
  on the page; an install that saved a number keeps it, and a new install keeps 25. The remote control machine name
  is set on the Configuration tab only, so the worker no longer writes any app option. The empty Appearance and
  Advanced sections are gone, and Workspace rules opens from Workspace instructions. A new Add-on configuration row
  at the foot of Settings opens the Configuration tab; when Supervisor cannot name the app, the row says where to
  find the tab and is not a link. The number of interactions to keep now applies before the worker closes the
  records an interrupted run left open, so a restart keeps as many records as the option says.

- A plain device command such as "set the office overhead to 50%" is faster. On 0.3.3 Codex had no tool on the
  chat page that finds an entity by name, so it searched `.storage/core.entity_registry` with shell commands, and
  the page's rules told it to look for `/config/AGENTS.md` in every message. The `home_assistant` `verify` tool
  has a new read-only `find` operation: it takes the words you used and an optional domain, and returns at most
  10 matching entities with their entity id, friendly name and state. Entities with a credential-like name and
  password-mode text entities are left out, as they are for the ChatGPT app's state reads. The page's rules now
  send a device command through three tool calls (find, act, read the state back) and tell Codex not to search
  files for an entity.
- Codex reads `/config/AGENTS.md` again only when it changed. The `exchange_info` tool now says in each message
  whether the file exists and when it last changed. A change to the file still reaches an open chat on its next
  message, and a removed file stops applying. The guard-rails block is still read by the worker before every
  pre-approved action. No approval, sandbox or permission rule changed.
- "Set the light to 100%" works again without an approval. A pre-approved light action now accepts
  `brightness_pct` and Home Assistant's other common light fields: `brightness_step`, `brightness_step_pct`,
  `hs_color`, `xy_color`, `color_name` and `flash`, next to `brightness`, `color_temp_kelvin`, `transition` and
  `rgb_color`. Each value must have the right type and stay in the range Home Assistant's light service accepts;
  a value outside it is refused.
- When a device action is refused only because its permission group is off or because its data has a field
  that is not on the list, the `act` tool now shows the owner an **Allow once** / **Deny** request in the chat
  and waits up to three minutes. Allow once runs that one action after every check runs again; it turns on no
  group and allows no later action. Deny, or no answer, leaves everything unchanged. Before, the refusal asked
  for an approval that the page could not show, so the exchange ended with nothing done. The owner is
  asked only when every other check passes. Security and people actions (locks, alarm, covers, cameras,
  notifications) never ask, because their data can hold a code; while that group is off, the refusal tells the
  owner how to turn it on in Settings → Permissions overrides. A target in the action data (`entity_id`,
  `device_id`, `area_id`, `floor_id`, `label_id`) is refused and never asks, so Allow once can only reach the
  target the request names. Guard rails, templates, unknown targets and the other refusals are unchanged.
- A task that ends with a plain answer no longer sends a Home Assistant notification; the answer is in the
  chat. A notification still goes out when a task asks a question, waits for a permission decision, or fails.
  Before, every completed chat task sent a **Codex task completed** notification with its answer.
  A chat task the owner cancels sends no notification either; before, it sent **Codex task cancelled**. The
  `codex_cli_task_result` event still reports the cancel.
- The **Copy response** button under each answer is gone, with its manual copy dialog. **Copy thread ID** in the
  thread menu still copies.
- The message box starts at one line of text above the control row, as in the ChatGPT desktop app, and grows as you
  type, up to 180 pixels.
- The **Settings** window keeps one size. Opening a section scrolls inside the window instead of making it taller.
- **ChatGPT app pairing** has its own collapsed header inside **Accounts and connections**, like **ChatGPT account**
  and **GitHub**. Its header shows the state in one word, for example "Off", in red when the connection failed.
- **Advanced** is a section like the others, with the same arrow and a status line.
- Every section header in **Settings** has the same arrow size and the same space to the divider above and below it.
  An open section has the same space between its last line or button and the next divider.
- The bold **Verification** box under an answer is gone. The checks, the readback and the note on screenshots sit in
  one closed **Checks and readback** line. The line names a bad result, for example "1 failed", in the error color.
  It opens by itself only while a batch of blocked browser requests needs a decision, while a retry runs, and to
  show the result of a retry just started from the page. A saved chat opened later shows it closed.
- Every arrow in **Settings**, **Scheduled runs**, the thread step list, the checks block and the reasoning picker
  is a Material Design Icons arrow, the icon set Home Assistant uses (Apache-2.0; see `web/NOTICE`). The browser's own
  triangle is gone from the last collapsed parts that still had it, such as **Use a token instead**.
- The GitHub sign-in notice in Home Assistant names the new place: **Settings > Accounts and connections > GitHub**.
- A question about Codex cloud tasks, such as "Can you see my cloud tasks?", now gets an answer from
  `codex cloud list --json` and `codex cloud status`. On 0.3.3 Codex looked at document sessions instead and
  said it could see none. The page's rules now name the two commands, say that the list holds only Codex cloud
  tasks of the signed-in account, and tell Codex to say so plainly when the list is empty. In a sandboxed mode
  each command still raises the usual Allow once card. No approval, sandbox or permission rule changed.

- The approval card and the remote-control hook now read command text with one shared copy of the comment
  and word code (`shell_words.py`), so a later fix cannot leave the two checks reading a command differently.
  Each still answers unreadable text its own way. No command is judged differently.
- When the approval card refuses **Allow once** because it cannot read a command's heredoc or quoting, it now
  says that, instead of saying the command touches the app's permission policy.
- A scheduling or mailbox request whose text holds a character that cannot be stored (a lone surrogate) is now
  refused with a sentence that says so, instead of the bare word `utf-8`.
- Settings has one **Accounts and connections** section for every sign-in and pairing: ChatGPT account, GitHub,
  and the ChatGPT app pairing. It starts collapsed. Its header joins the three states ("ChatGPT signed in · GitHub
  connected · app off"), and when one needs the owner the header names only that one, in red. ChatGPT and GitHub
  keep their own collapsed headers, codes and countdowns inside it. The app pairing part shows the remote
  connection state with **Get a pairing code**, **Manage remote connection** and **Turn off**; it replaces the
  separate **Manage remote connection** row. A pairing code has no countdown: the worker does not know how long
  Codex keeps it. **Turn off** stops the connection and does not remove the pairing in the ChatGPT app.
- When a request gets HTTP 401 because the Home Assistant ingress session expired (a tab that slept), the page
  asks Home Assistant for a new session once, through the parent window as the Home Assistant frontend does, and
  sends the request again. Requests that fail together share one renewal. If the renewal fails, a banner says the
  session expired and offers **Reload**; the page no longer shows "The worker returned HTTP 401".
- A thread started from the chat page no longer shows the page's instructions as your message in the ChatGPT app.
  The page sends its fixed rules as Codex developer instructions, and the message holds only your text, so the
  app shows your words as the message and the title. The details of each message (the backup folder, attached
  documents and images, and the notes on direct API calls, the local documentation and GitHub) come from a new
  read-only `exchange_info` tool on the Home Assistant data server. A page message on a thread that the page did
  not start (a ChatGPT app thread, or one from 0.3.2 or earlier) begins with a one-line note to call that tool,
  which then also returns the rules. Threads from 0.3.2 or earlier keep their old instructions in their history.
- Codex's answers are stored in the thread as plain text, so the ChatGPT app shows the answer and not a JSON
  object. The page no longer sends an output format. Codex calls two new tools on the Home Assistant data server
  instead: `ask_owner` with a question and up to three choices, and `report_failure` with a reason. Neither asks
  for permission. The page takes its status, question and choices from these calls. A failure that the worker
  finds itself (an exit with an error, a timeout, a stall, a configuration that fails its check) still comes
  first; it now also wins over a question, so a task that leaves a broken configuration is reported as failed and
  does not ask a question. A JSON answer from a thread of 0.3.2 or earlier still shows as before. The page shows a
  question on its own line only when the answer text does not already hold it.

## 0.3.2 (unreleased)

- Codex now refuses risky commands in two layers. Managed Codex command rules refuse six literal command
  prefixes: the Supervisor (`ha supervisor`, `ha super`, `ha su`), credentials, the app's policy folder and
  full access. They apply with Remote Control off too. The command hook stays the main check. The app tests
  that Codex loads the rules before Remote Control starts. A rules warning stops chat turns and keeps Remote
  Control off until the app restarts after a repair. A failed or timed-out rule test is tried again on the next
  **Turn on**. The probe's process group is always stopped. DOCS says how an owner allow rule and the prefix
  limits change these checks.
- A Turn on request that races a rules warning now leaves Remote Control saved as off.
- The command hook joins a backslash and newline inside a word, as the shell does, so `h\` on one line and
  `a su info` on the next is read as `ha su info`. Continuations it cannot read (in a heredoc, a substitution or
  unreadable quoting) are refused with a request for a one-line command. Its text floor also reads the command
  with every backslash and newline removed, in any quote or comment state. So a Supervisor command spelled
  across lines is refused also in single quotes, after a carriage return, inside `${...}`, and in a shell or
  wrapper that the parser does not open (`dash -c`, `setsid sh -c`, `bash --norc -c`).
- The app no longer makes a ChatGPT sign-in code by itself at start. A code works for 15 minutes and only
  while its sign-in runs, so a code made at start was often stale before anyone used it. While Codex is not
  signed in, the Home Assistant notification now points to **Sign in**, and the owner starts a sign-in there
  when ready. A mark file that older versions left in the app's storage is removed at start.
- The ChatGPT and GitHub sign-in codes show how long they still work, with a live countdown. The worker sends
  the seconds left by its own clock, so a phone with a wrong clock still shows the right time, and the page sets
  the time again from each status reply, so a phone that slept catches up. When a code runs out, the page says so and offers **Get a new code**, which cancels the old sign-in and starts a new one.
- A ChatGPT sign-in that stops without signing in now says why when it can: the code expired, or the error line
  that Codex printed (with any code in it replaced). The page shows the reason, and **Sign in** becomes
  **Get a new code**. A canceled sign-in can no longer mark the next one as failed or clear its process.
- **Cancel** on a ChatGPT sign-in no longer raises a false "Codex sign-in failed" notification. The app now
  marks the sign-in canceled before it stops the sign-in process.
- Settings on a phone: every settings section starts collapsed, with a one-line status in its header (for
  example "Connected as owner, 1 repository" or "3 of 5 on"); a section that needs attention says so in red:
  a sign-in that waits or failed, house rules that wait for review, a schedule that waits for approval, or
  schedules paused without the Home Assistant time zone. Tap a header to
  open its section. Button rows wrap, and at phone width the buttons of a row share it in equal columns, so the
  ChatGPT buttons no longer render tall and narrow and the GitHub buttons no longer run past the edge.
- The name the ChatGPT app shows for this Home Assistant can be changed on the page, under remote control,
  while remote control is off. The page saves it as the **Remote control machine name** option through
  Supervisor, so the app's Configuration tab shows the same name. Only that option changes; the other options are
  sent back as Supervisor reports them (defaults merged with the stored values, as Save in the Configuration tab
  does), with any `!secret` reference kept. A name can have up to 63 characters.
- A first message such as "Test" now gets a normal reply. The task prompt asks Codex to write its summary as a
  reply to the person, not as a report from the worker, and it names `/config/AGENTS.md` only when that file
  exists, in chat tasks and in remote-control sessions. Before, a missing file could show in the answer as
  "/config/AGENTS.md was not found". The page now shows a missing `AGENTS.md` once, above the message box, as a
  setup hint that you can dismiss.

## 0.3.1 (unreleased)

- A repository in `/share` whose name starts with `ha_` can be used again with Remote Control on. The check
  that keeps Codex out of the app's policy folder `/usr/share/ha_codex` refused every command that named
  `/share/ha_` anywhere, so even `git -C /share/ha_<name> status` was refused as a policy change. The check now
  allows the plain absolute `/share/ha_<name>` and its subfolders. Every other way to reach the policy folder is
  still refused: a relative `share/ha_...` (the text cannot tell whether the command runs in `/usr`),
  `../share/ha_...`, globs such as `/usr/sh*/ha_*`, braces, quotes, backslashes, and a variable or command
  substitution before `/share/ha_` or `/ha_codex`. The command check and the approval check use the same rule.
- The credentialed `git fetch` and `git push` now pin the TLS trust: the system CA bundle and CA folder are set
  for git, and `http.sslCAInfo`, `http.sslCAPath`, `http.proxy`, `http.extraHeader`, `http.cookieFile`,
  `http.saveCookies` and `http.curloptResolve` are reset for the run. A CA file or folder left in a repository's
  git settings, in any `http.<url>` form, is never trusted for the GitHub sign-in.

## 0.3.0 (unreleased)

- GitHub sign-in (release 0.3). **Settings > GitHub**, beside the ChatGPT account, connects GitHub with
  buttons only: **Connect GitHub** shows GitHub's device code and the `github.com/login/device` link on the
  page, then **Read test**, **Check now**, **Accept this access**, **Disconnect** (with the link to revoke on
  GitHub) and **Use a token instead** for one fine-grained token. **Clone** (into `/share/<repository>`) and **Link**
  (a folder under `/share` or `/config`) set up a repository. The code and the token are never logged or kept in browser storage.
- The app keeps the sign-in in `/data/github`, hidden in every Codex sandbox and left out of the app backup.
  Codex never holds it: the new `github_git` tool (repos, fetch, pull, push, clone, link) asks the app to run
  git, and every push runs the kit's push guard first. Read-only chats and schedules can only list and fetch.
  A push that adds a `guard: allow` mark is refused, because an allow mark needs the owner. GitHub refuses
  changes to `.github/workflows/*` with Contents-only access. A page button for allow marks and a list of the
  git folders on this box come in 0.3.1.
- The app checks the sign-in at start, hourly and on **Check now**. A change fires the event
  `codex_cli_github_access`, sends one notification (`codex_cli_github`), shows a banner above the chat, and
  the integration raises the repair `github_access_lost` until the check is OK or the sign-in is disconnected.
  The new option `github_client_id` is for an owner who registered their own GitHub App.

- Integration actions and browser-session endpoints now require a loaded config entry. The browser broker
  belongs to that entry and revokes its sessions on unload or failed setup. Initial worker authentication
  rejection now requests reauthentication instead of retrying as a connection failure.
- The sign-in action explains its `force` default and how it handles an existing sign-in or a flow already
  running. The integration quality checklist now distinguishes implemented rules from unfinished work.

- The app now installs its own integration. At each start it copies the Codex integration of the same
  release, carried in the app image, to `/config/custom_components/codex_cli`, then asks for one restart of
  Home Assistant in a notification. It writes only when the folder is missing or was written by the app
  (a marker file in the folder names the copy), keeps the previous copy in the app's own storage, and
  swaps the new folder in by renaming, so a failed copy leaves the old folder in place. The new copy is
  written in `/config/.codex_cli_install`, outside `custom_components`, so Home Assistant never loads a
  partial copy. A folder installed another way (Marketplace, HACS or by hand), or changed after the app wrote
  it, is never changed; a notification says so once per app version. A restart notice that Home Assistant
  could not show is posted again at the next start.
  The new `install_integration` option (default on) turns the copy off. Uninstalling the app leaves the
  integration in place.
- The app announces itself through Supervisor discovery at each start, so after the restart
  **Settings** > **Devices & services** shows **Discovered: Codex**. Setup from discovery now asks for
  confirmation before it adds the integration, and shows a connection error on the form instead of ending.
- The app repository's maintainer is now The Backshop.
- The app announces itself through Supervisor discovery only once Home Assistant has loaded the integration,
  so a fresh install no longer logs `Cannot find integration codex_cli` with a traceback before the restart.
  It checks once a minute in the background and never delays the worker's start.
- The "integration left as installed" notification is removed once the folder is the app's own again.
- A restart of the app while Codex is not signed in no longer posts a new one-time code each time. The app
  starts one sign-in by itself; later starts say to choose **Sign in** instead, until a start finds Codex
  signed in.
- Smaller fixes: the install notice no longer says to finish setup for an integration that may already be set
  up; a file name that cannot be encoded is reported like any failed copy; a damaged notice record is not
  replayed; the documentation says how to go back to a version before 0.3.0.

- A legacy mailbox above 128 messages now keeps stable page offsets during a read.
  Read copies leave to make room only when a new message arrives.
- Expire agent copies 48 hours after their first read so old mailbox pages keep stable positions.
  Settings acknowledges only the owner mailbox; agent labels follow the active turn.
  Prior read marks receive a one-time upgrade grace period.
- Upgrade note: legacy messages without a Codex read mark remain unread. If its mailbox is full,
  read those messages through Codex or clear its mailbox in Settings. Then send more mail to `all`.
  This preserves unread history without an assumed read date.

- Let the authenticated lead direct worker tasks on its own initiative, including an explicit task attached
  to a mailbox note. Hold one durable waiting task while busy and report start/result in the same thread;
  keep native human approval and unattended daily limits. The worker cannot start lead work.
- Give each reader a 128-message mailbox, retain unread mail across absences, expire only read agent mail
  after 48 hours, and refuse all-unread full delivery with a visible status. Add per-mailbox clear confirmation,
  UTC message envelopes, local-time/plain views, due status, source labels and bounded lead mailbox pages.

## 0.1.81.2 (unreleased)

- Document why the pinned CLI cannot supply a ChatGPT sign-in deadline: access tokens refresh and
  refresh-grant expiry is not exposed. Keep the existing signed-out Repair without a guessed expiry
  sensor, warning countdown or advance push; test that elapsed time alone does not require sign-in.
- A scheduling or mailbox tool asked for more than 20 items now gets the first 20 and a `next_offset`,
  instead of a refusal. Codex asked `read_mailbox` for 50 messages when told to summarize the unread mail,
  and the only answer was "Invalid, refused or expired scheduling request.", so it could not correct
  itself. A limit below 1 reads 1 item. Refusals that remain (an offset out of range, a limit that is not a
  whole number, an extra argument, an ended exchange, a full proposal list) now say their reason.

- Read command text with an unfinished quote again, line by line, in the command hook, so the always-refused
  Supervisor rule still applies to it. Lines that can be read keep their quoting, so a quoted script given to a
  shell wrapper is still checked; only a line that cannot be read loses its quotes. A `#` now starts a comment
  only at the start of a word, as it does in the shell. Add a text floor, read without quotes or backslashes,
  for `ha` followed by flags and `supervisor` or `su`; it also refuses such a spelling inside a comment.
- The approval card no longer offers **Allow once** for a command with a heredoc or here-string (any `<<`),
  a line joined with a backslash, or quoting it cannot read, since such text could hide a write to a
  protected place and an allowed command runs outside the sandbox. The check does not try to read heredoc
  bodies: a quote in a body could pair with a quote in a later comment and make other text look like it.
- Explain the first confirmation needed to use action groups, name disabled groups in refusal diagnostics,
  and direct entity lookups to the worker's authenticated read tool before changes.

- Scheduling and mailbox tools work from a chat again. Codex asked for approval of each call to
  `propose_schedule`, `list_schedules`, `read_mailbox` and `post_mailbox`, and the chat page has no card for
  that kind of request, so every call was declined without anything shown. The four tools are now approved
  ahead, as the verification and history tools are; each keeps its own limits (proposals stay disabled until
  an administrator approves them, and mailbox messages confer no authority). Any other tool question the page
  cannot show is still declined, and the chat now says so.
- The command hook now answers within 1.5 seconds of starting. Codex 0.166 runs a tool call whose hook
  passes its 10-second timeout, so a slow check could let a call through. The check now runs in a child
  process that the hook stops at the deadline, refusing the request with a plain reason, even when the check
  is stuck inside one long call. The child also does all of the file reading and module loading, so a stalled
  disk cannot delay the answer, and the hook does not wait for a stopped child to exit before it answers.
  An answer that is ready only after the deadline is refused too, rather than passed on late. A program
  that calls the hook's `main()` repeatedly in one process no longer collects stopped child processes.
  `tests/measure_gate.py` prints the hook's own time for each call.
- Judge pre-approved actions by their direct service and targets only. Remove scene/member walks,
  platform lists and reload-definition comparisons; downstream behavior belongs to the owner's setup.
- Keep confirmed house rules, mode checks, direct limits, audit-before-write, receipts and setup-secret
  handoff. Switch saves after first use still leave changed rules unconfirmed until Review and confirm.
  Recheck active authorization, group settings and confirmed rules after the last direct-target read.

- Do not report generic task HTTP errors as expired ChatGPT sign-in; test sign-in recovery through the full task lifecycle.

- Keep generic passive HTTP errors out of the expired-sign-in Repair; expose only fixed usage-error messages and categories, never raw upstream errors.

- State the Codex account requirement before sign-in, show plan information and uncertain access in Status,
  and refresh sign-in metadata daily without a model task. Restrict the sign-in Repair to known signed-out
  or expired state; a good sign-in or successful matching turn clears it.

- Verify atomically saved mailbox event IDs before emission; failed saves or verification reads defer events and retry without replay.

- Add durable unread mailbox counts for lead and owner, an unread-only lead filter, and page acknowledgments.
  Expose a lead unread sensor and deduplicated, body-free arrival events through the normal status poll.
  Mail remains data and never starts work by itself.
- Clarify remote pairing, Core address discovery, and workspace instructions in the newcomer guide.

- Document the line-based command check’s known YAML blind spots and the protections that still apply.

- Add the Quiet Studio chat layout with Home Assistant themes, a bottom composer, photo/file menu,
  per-chat narrowing permissions, thread status and archive/restore. Separate remote connection and
  Advanced workspace-rules views; keep pairing URLs distinct from manual codes and administrator-only.
  Auto-review and Full access remain unavailable; no managed security policy changes.

- Close diagnostics interrupted by a worker restart as unknown, retaining room for new completed work.
  Create the private entity key without hard links and explain recovery while collecting other metadata.
  Protect private diagnostic files, retain only the newest corrupt store, distinguish policy causes and
  clarify privacy-test inputs and backup limits.

- **Anonymized interaction diagnostics.** Add bounded worker metadata and local exports in Settings and Home Assistant diagnostics. Replace the previous integration entry/coordinator snapshot to exclude prompts, titles and connection data. Home Assistant still adds its own system envelope. Use private keyed entity references, administrator-checked downloads, failure-isolated collection and grouped relay reads. Keep 25 interactions by default, configurable from 10 to 100; preserve running work and scale the size cap. Remote ChatGPT thread events remain explicitly excluded.

- The app is now listed as Codex for Home Assistant; the `codex_cli_worker` slug is unchanged, and an installed copy shows the new name after it updates.

- **Core address and first-run relay reads.** Discover Core's actual port and SSL setting from Supervisor at startup and each task/relay launch. Make the address option an empty override and retire old default values. Offer a bounded read-only relay tool using a temporary capability; no hand-made Core token is needed for entity reads and the Supervisor token stays in the worker. Unsupported shell reads still request native network approval; direct commands still need a separately configured credential.

- **ABI and fallback documentation (N8/N9).** Describe domain/stack and network_v8 mediation, the tool-label socket-family restrictions, and the plain relay route's continuing user-namespace requirement. HAOS chat-path acceptance remains the lead's. Under the named lead exception, N10 requires errno 13 on direct proc-denial rows; pinned command/exec rows retain 1, 2 or 13.

- **Scheduled work and local lead channel.** Persist owner-approved interval, Home Assistant local-time cron and one-off schedules, bounded run history and a plain-text local mailbox. Agent proposals require owner review; native permissions remain pending human decisions. Add a separately authenticated lead API and turn-scoped proposal/mailbox tools. Preserve existing sandbox and refusal paths. The lead accepted Home Assistant phone permission notification plus the responsive page; no ChatGPT approval mirror. Background outcomes stay in the run log, with only permission-wait pushes. Review fixes protect approved schedules and database sidecars, cap proposals and mailbox writes, allow mailbox clearing, preserve one-offs on busy races and refuse masked-prompt edits.

- **No bundled house rules.** Stop packaging and installing house-practice instructions. Preserve owner workspace instructions and all enforced protections. Upgrade cleanup removes only the exact legacy bundled home copy, preserving edited files and symlinks. Documentation points optional house practices to the starter kit.

- **HAOS AppArmor ABI compatibility.** Declare ABI 3.0 so HAOS's 3.1.7 parser enables the stack rule instead of applying its legacy untagged-policy defaults. Keep the exact relay-only stack target. Prefer the AppArmor-specific proc attribute, with legacy fallback only when it is absent; access denials still fail closed. Document the intentional same-stack sibling ptrace restriction.

- **Root relay namespace mapping.** Both relay launch paths map outside root to inner UID/GID 1000 before dropping all capabilities. The fallback omits UTS setup but keeps the mapping and verifies its non-root IDs, zero capabilities and NNP before exec. Existing kernel file ownership, the private hostname and the exact tool stack remain. CI uses the pinned release CLI to test real-root startup, both loaded profile names, pipe/PTY execution and root-owned `/config` reads/writes under each sandbox policy.

- **NNP-safe relay tools.** Add the restrictive tool component to the relay label without removing the relay component, while no_new_privs is already set. An immutable guard verifies the actual stack and NNP before starting native Bubblewrap; a failed transition never launches a tool in only the relay label. Use a minimal fresh device tree plus the shared-memory bind. Loaded-profile diagnostics now require both label components and NNP for pipes and PTYs. Name fixed launcher failure stages, isolate launcher Python, and document version-update profile reload and the HAOS app container name. Actual HAOS enforcement and phone acceptance remain lead checks.

- **Relay execution review fixes.** Give the relay its own AppArmor label and return tool sandboxes to narrow network rules. Preserve working standard devices in the outer namespace: native failing-first checks reproduce `EACCES` opening `/dev/null` and PTYs without that bind; redundant `/opt` exec rules were not the cause. Drop all capabilities on the plain fallback, create the fixed control-mask anchor at worker startup, and pass fresh relay-only instructions as launch arguments. Preserve the existing SQLite home, saved choice and stable `ha_codex` enrollment client key. Ship an account-free loaded-profile probe runnable through the lead's SSH add-on. HAOS isolation, native phone approval and reconnect after an image update remain lead lab checks.

- **Parity review follow-up.** Keep normal remote requests in Codex's approval flow while retaining named security refusals. Restore the generic rule to edit automation, script and helper YAML with a backup instead of deleting through the config API. Keep the root-only watch exemption and bounded recovery store from the merged watch fixes.

- **Remote requests keep native approvals.** Normal light, lock, network and checked-file PermissionRequests receive no hook decision, so Codex can ask on the phone; the hook never auto-approves. Keep named refusals for credentials/process environments, full access, app settings/policy/rules and planted command/template definitions. The startup self-test checks normal ASK and security denial. The shipped generic practice and owner-confirmed device actions follow the parity ruling.

Permissions that ask, and only an administrator can answer. Sign-in without a terminal, a pause switch and a model pin.

- **Pairing isolation and administrator access.** Mask the fixed control-socket parent inside every sandbox. Require a verified administrator for remote-control actions and for code/QR reads; shorten readiness checks and explain a still-starting process honestly.
- **First remote-control pairing.** Ask the supervised app-server for a manual pairing code over its own private control socket. Clear old codes on failure or restart, keep them off logs and the integration and `/status`, and report an unavailable code honestly.
- **Watch closure fixes.** Exempt only the root UI-owned YAML files from automatic undo; matching names in packages or other subfolders remain checked. Reuse verified SHA-256 recovery copies across files and restarts, with a 128-copy/64 MiB store limit. When a new copy will not fit, report without undoing or deleting older recovery copies.

- **Remote-control review fixes.** Check move destinations for planted YAML; recognize the actual Codex 0.166 downgrade wording. Save and report replaced bytes before watch undo, and report UI-owned YAML without whole-file rollback. Cap hook-checked files at 512 KiB and wait for the watch report in the timing-sensitive test. The container smoke accepts only requirements refusal or a proven restricted fallback.

- **Remote control on HAOS.** Use the existing Bubblewrap child profile for the private machine name, without widening the parent; a failed namespace setup starts Codex without the custom name and explains that on the page. Pairing follows the same path. Permit the nested sandbox wrapper in the child profile.
- Check YAML patches against the complete proposed file, including edits beneath existing shell, REST and command-line definitions. Use the captured Codex 0.166 Bash/apply_patch command-string fixtures. Refuse uncertain or over-budget checks; the upstream hook-timeout fail-open behavior remains a documented limitation.
- Managed requirements exclude the `never` approval policy. Integration-owned full-access requests fail clearly; relay requests can fall back to read-only. Settings and logs explain that fallback and show an observed downgrade warning without claiming visibility into every relay thread.
- While remote control is on, undo new or changed always-on configuration blocks using verified earlier bytes or a complete new-file absence manifest. Unsafe or missing backups remain review findings; remote-off scans do not restore files. Keep successful undo results until review, including after restart. Correct masked-file reads to `Permission denied`.
- Fix closure findings: keep definition, platform and templated-action bodies checked; recognize flow-style script targets; tell the owner to review unchecked edits before any reload or restart.
- Fix command-check review findings: compare changed files only with verified earlier bytes, restore the remote-control repair text, check indirect script routes to always-on kinds, and allow changed triggers with unchanged always-on calls through normal approval.
- **No full-access mode.** `danger-full-access` is gone from the app's options. A protected policy inside the image (`/usr/share/ha_codex/policy.json`) lists the allowed sandbox modes and keeps the approval policy at `on-request`; an option it does not allow stops the app at startup, and each task is refused with a "needs support" message instead of asking anyone to approve an escalation. Codex's own configuration no longer says `approval_policy = "never"`.
- **A policy Codex cannot edit.** The policy and the house rules (`AGENTS.md`) live in the image, outside `/config`. The rules are copied into Codex's home folder at every start, replacing any edited copy. A file change that touches the policy, the rules or the app's settings can only be denied, and so can a command whose text names one of them. A task under a refused sandbox mode now stops before any baseline or backup work.
- **Approvals need a Home Assistant administrator.** Allow once and Deny are taken only on the Codex page, from the user Home Assistant names for the page, after the worker confirms that user is an active administrator. The integration's API token can no longer answer permission requests. Who answered is recorded on the request.
- **The integration's actions check the caller.** Every `codex_cli` action refuses a signed-in user who is not an administrator; calls with no user, such as automations, still run. A choice tapped on a notification is taken only from an administrator's phone.
- **Prompt counts.** Each conversation counts its permission requests by kind, and the new **Permission prompts** sensor shows the latest conversation's counts.
- Sign-in without a terminal: a banner above the chat while the app is not signed in; the one-time code in large letters that stays unchanged until the sign-in ends or is canceled, in a font that tells 0 from O; the QR code served by the app (it no longer depends on `/local/`); a **Cancel** button; a second **Sign in** keeps the running sign-in. When the worker learns that OpenAI refused the saved sign-in (expired or revoked), the banner says so.
- New **Signed in** binary sensor in the integration, and a clearer repair when the sign-in has expired or was revoked.
- New `model_pin` option (default `gpt-6.1-sol`): every run names the pinned model, and a request for any other model is stopped with an error instead of running on it. `none` turns it off.
- **Pause the assistant** in Settings: new messages are refused and queued ones wait until it is resumed; kept across restarts.
- **A revoked sign-in reads as signed out.** `codex login status` only checks the local file, so a sign-in OpenAI had revoked still showed as signed in while every task failed with a 401. The worker now remembers OpenAI's own answer from the usage read and from task errors: a refused sign-in shows as signed out (with `server_check: rejected`), the sign-in page offers a new sign-in, and the task says the sign-in expired instead of showing a transport error. A new sign-in clears it.
- **Stall watchdog.** A task now stops with a plain message when Codex sends nothing for 10 minutes (`task_stall_seconds`, 0 turns it off) while no permission request is waiting. After a network drop a turn could otherwise hang with no error until the full task timeout.
- **Starts at boot.** The app now uses `boot: auto`, so it comes back after a reboot, and with it remote control when it was left on.
- **Usage from the account, not the screen.** The 5-hour and weekly usage sensors now come from Codex's own `account/rateLimits/read`, placed by window length. If that read fails, the old `/status` screen read is used and the sensors say the figures are estimated (`source` and `note` attributes).
- Remote control from the ChatGPT app can be managed from the app's Settings page: turn on and off, status, machine name (default **Home Assistant**), pairing code as text and QR code, a new pairing code, and recent messages. The app owns `codex app-server --listen unix://… --remote-control` as one supervised process with restart backoff, with only the explicitly configured Core API pair and no Supervisor token. It stays off until turned on, and **Turn on** requires the installed command check to pass its self-test.
- New option **Remote control machine name**.
- A command check outside the model for remote control: managed Codex requirements, the hook, its self-test and the configuration watch. Credential and explicit full-access checks stay on. Device and message category refusals are removed; planted-command and templated-action checks remain on. The hook fixtures follow the captured Codex 0.166 input format. Every sandboxed command sees masked paths for `secrets.yaml`, Home Assistant's sign-in store and the app's options and API token; reads fail with `Permission denied`. `/run/s6` is an empty tmpfs, and the worker is marked not dumpable, to limit process-environment access; loaded-profile proc-view isolation is a separate lead lab check.
- **Parity with the Claude add-on.** Remove the category-refusal option and device/message lists. The owner can confirm any device action; limits specific to an installation belong in `/config/AGENTS.md`. Credential, protected-policy and named remote security checks stay on. Definition and platform bodies and templated action names remain checked; templated targets and unchanged call lines follow normal approval.
- Permission cards show what changes, where and the possible effect. Shipped practice asks an ambiguity question before acting and keeps privacy, backups, the configuration check and honest reporting. This preserves the surviving confirmation guidance from draft #9.


## 0.1.81.1

First version of this project, built on release 0.1.81 of the project it started from (commit `63269cc6`). This version and 0.1.81.2 have that release's number plus one more number. Later versions have three numbers.

- The app bundles the **packaged** Codex CLI **0.166.0** from OpenAI's release, checked against a SHA-256 sum per architecture, in place of the npm build 0.165.0 (the npm build cannot run `codex remote-control`).
- The CLI cannot update itself: it is unpacked read-only under `/opt/codex/0.166.0`, and `check_for_update_on_startup` stays off. Moving the CLI is a new release.
- The package's own `bwrap` points at the app's HAOS sandbox wrapper, so the sandbox readiness check proves the path every task uses.
- The base image is pinned by digest instead of `:latest`.
- Repository, image, issue and documentation links point at this repository. The maintainer is The Backshop.

## Earlier versions

This project started from release 0.1.81 of [moryoav/home-assistant-codex](https://github.com/moryoav/home-assistant-codex). The versions before that are in that project's changelog.
