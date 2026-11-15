# Codex integration

Codex connects Home Assistant to the local Codex for Home Assistant app. It lets Home Assistant start Codex tasks against the configuration folder, monitor active work, reply to tasks that need input, and start the Codex sign-in flow.

## Installation

Use the My Home Assistant buttons in the repository README for the fastest setup.

1. Add `https://github.com/fatherdoctor/ha_codex` as a Home Assistant app repository.
2. Install and start the Codex for Home Assistant app.
3. Open the app web UI and choose **Settings > Sign in** to sign in to Codex. See the [sign-in prerequisites](../../README.md#sign-in).
4. Open HACS, search for **Codex** under **Integrations**, select it, and choose **Download**.
5. Restart Home Assistant.
6. Add the **Codex** integration from **Settings** > **Devices & services**.

As a manual fallback, copy `custom_components/codex_cli` into `/config/custom_components/codex_cli`, restart Home Assistant, and then add the integration from **Settings** > **Devices & services**.

## Configuration

The integration auto-detects the installed Codex for Home Assistant app through the local Supervisor API. It also provisions the worker's private API token through Supervisor-managed app stdin, keeps the worker token in memory, and stores only the non-secret worker URL in the Home Assistant config entry.

There is no worker URL or API token to enter.

## Entities

- Auth status: Shows whether Codex CLI is signed in.
- 5-hour limit: Shows the percentage remaining in the 5-hour window when Codex reports one. The entity remains for compatibility and is `unknown` with `reported: false` when that window is omitted.
- 5-hour reset: Shows when the 5-hour window resets, if a reset time can be parsed.
- Weekly limit: Shows the percentage remaining in the weekly window when Codex reports one.
- Weekly reset: Shows when the weekly window resets, if a reset time can be parsed.
- Active tasks: Shows the number of currently running Codex tasks.
- Last task: Shows the latest known task status and related attributes. When that task failed, the `error` attribute gives the reason.
- Task running: Binary sensor that is on while a task is active.

All entities are diagnostic entities on the Codex device.

Reset sensors are `unknown` when no reset time is available. Usage sensors keep the original reset description in `reset_text` and expose a parsed ISO datetime in `reset` when available.

## Actions

- `codex_cli.start_task`: Start a Codex task. Requires `prompt`.
- `codex_cli.continue_task`: Continue a saved task. Requires `task_id` and `message`. Supports completed, failed, cancelled, and waiting tasks with an available Codex session.
- `codex_cli.start_login`: Start the Codex sign-in flow; optional `force`.
- `codex_cli.logout`: Remove saved Codex CLI credentials from the worker.
- `codex_cli.get_login_status`: Return current sign-in status.
- `codex_cli.list_tasks`: Return all tasks or filter with `limit`, `offset`, `status`, `order`, and `summary`. Use `order: updated_desc` for recent activity and `summary: true` for compact entries.
- `codex_cli.get_task`: Return one task by task ID, including its conversation in `turns`.
- `codex_cli.cancel_task`: Cancel one task by task ID.
- `codex_cli.reply_task`: Send a reply to a waiting task. Requires `task_id` and `reply`. With the optional `turn_id`, taken from the `codex_cli_task_result` event or the **Last task** sensor, the reply is refused once that question is no longer the one waiting.

These actions do not queue messages. Starting or continuing a task while another task is active or messages are already queued returns an error; the worker chat UI provides the queue.

Example automation action:

```yaml
action: codex_cli.start_task
data:
  prompt: Can you inspect my Home dashboard and report any obvious issues?
response_variable: codex_result
```

### Action examples

List the 10 most recently active tasks. Optional filters are `limit` (1-500), `offset` (zero-based), `status`, `order` (`created_asc` or `updated_desc`) and `summary`. The response has `tasks`, `total`, `next_offset` and `active_task_id`. With no options, the action returns all tasks, oldest-created first. To read the full conversation, call `codex_cli.get_task` with a `task_id` and read `task.turns`.

```yaml
action: codex_cli.list_tasks
data:
  limit: 10
  order: updated_desc
  summary: true
response_variable: recent_chats
```

Continue a previous task. Completed, failed, cancelled and waiting tasks can continue if their saved Codex session is available. The task ID stays the same, each message adds an exchange, and the task-level status and result describe the latest exchange. A missing session returns an error; it never starts a new conversation. New tasks do not inherit context from other chats.

```yaml
action: codex_cli.continue_task
data:
  task_id: "YOUR_TASK_ID"
  message: "Apply the first suggestion from your previous answer."
response_variable: continued_chat
```

Answer a waiting question. `reply` is free text. When Codex offered answers, they are in the event's `choices` list, and sending one of them picks it. With `turn_id`, the reply is refused once that question is no longer the one waiting, so a late reply cannot answer a newer question.

```yaml
action: codex_cli.reply_task
data:
  task_id: "{{ trigger.event.data.task_id }}"
  turn_id: "{{ trigger.event.data.turn_id }}"
  reply: "Don't change anything"
```

## Events

When a task completes, fails or needs input, the worker fires the event `codex_cli_task_result`, in addition to the notification. An automation that listens for it can read `task_id`, `turn_id`, `status`, `summary`, `question`, `choices`, `details`, `attachments` and the nested `response` object from `trigger.event.data`.

## Task output

A task returns a structured response with `status`, `summary`, `question`, `details` and `choices`. The first four are plain strings, and the Home Assistant consumer or notification service that receives them decides how they look. `choices` is a list of up to three short answers that Codex offers with a question; it is empty otherwise. The chat page formats Markdown in them (see [Formatted messages](../../codex-cli-worker/DOCS.md#formatted-messages)).

To ask for a preferred style, add an instruction to `/config/AGENTS.md`, which you can edit from the app's web page. Codex treats it as guidance, not as a formatting guarantee. For Markdown-friendly output, add this:

```md
For the final structured response, use concise Markdown in summary, question,
and details when it improves readability. Use only short lists, emphasis, inline
code, and fenced code blocks. Do not use raw HTML, images, or embedded content.
```

## Assist example

[examples/scripts.yaml](../../examples/scripts.yaml) is a starter script that you can expose to Assist or another LLM integration.

## Data Updates

The integration polls the worker every 30 seconds. Actions that start, cancel, or reply to tasks request an immediate refresh after the worker responds.

## Troubleshooting

- If entities are unavailable, check that the Codex for Home Assistant app is running. The integration discovers its internal URL automatically.
- If the app's web UI says that the integration is not installed, out of date, or not connected, do what the line says: install or update the integration, restart Home Assistant, and add it under **Settings** > **Devices & services**.
- If setup cannot connect, restart the Codex for Home Assistant app, then reload or add the integration again so it can provision a fresh worker API token.
- If Codex is not signed in, run `codex_cli.start_login` or choose **Settings > Sign in** in the app web UI.
- If a task needs input, use `codex_cli.reply_task` with the task ID and reply text. The answers Codex offered are in the `choices` attribute of the **Last task** sensor and in the `codex_cli_task_result` event.
- If the worker's `notify_service` is a mobile app service, a question's notification has a button for each of those answers. The integration sends the tapped one to the worker. If the worker does not take it, for example because the question was already answered, a persistent notification says why.

## Removal

1. Delete the Codex integration from **Settings > Devices & services**.
2. Stop or uninstall the Codex for Home Assistant app if it is no longer needed.

## Known Limitations

- This integration controls a single local Codex for Home Assistant instance.
- It depends on the worker app for task execution, Codex authentication, and notification delivery.
- Usage-limit sensors are best-effort values parsed from interactive Codex `/status` output. Reported windows vary by account and plan; omitted windows remain `unknown` with `reported: false`.
