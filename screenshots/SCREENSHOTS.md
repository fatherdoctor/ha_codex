# Screenshots

These screenshots show the add-on's chat page. They are taken by a script, not by hand: a headless browser opens
the page against the test fixture, which holds sample conversations and settings only. No real names, home
entity names, tokens or sign-in codes appear in them.

Each screenshot exists in four versions: `light` and `dark` color scheme, `desktop` (1280 pixels wide) and `phone`
(390 pixels wide). The file name is `<name>-<scheme>-<size>.png`, for example `chat-light-desktop.png`.

| Name | What it shows |
|---|---|
| `chat` | The chat page with one saved conversation open: a question about an automation and the answer that explains the change. |
| `confirm` | A command that waits for one confirmation: what changes, where, the effect, the working folder, the command, and **Allow once** and **Deny**. The phone versions start lower in the card, so that both buttons are in view. |
| `schedule-proposal` | **Settings** > **Scheduled runs**: a schedule that an agent proposed, which stays off until you approve it, and an approved schedule. |
| `sign-in` | **Settings** > **ChatGPT account** while a sign-in waits for its code. The code and the QR code are blurred. |

## Not taken yet

These screens belong to Home Assistant, not to the add-on page, so the test fixture cannot show them. They are taken
on a test install and added here when they exist.

| Name | What it shows |
|---|---|
| `sign-in-notification` | The Home Assistant notification that asks you to sign in, with the code blurred. |
| `repairs-sign-in` | **Settings** > **Repairs** with the notice that the sign-in has ended. |

## Refreshing the screenshots

Run the **README screenshots** workflow from the **Actions** tab (it also runs when a release is published). It
uploads the new files as an artifact named `screenshots`. Compare them with the files here and commit the ones that
changed.

The `confirm` screenshots change on every run: the chat header and the conversation list show the time at which the
script sent the request. Commit them only when something else in the picture changed. All other screenshots use a
fixed sample date and are the same on every run unless the page changed.

To take them on your own computer, install the test dependencies (Python: `flask`, `pyyaml`, `requests`, `qrcode`,
`websocket-client`; Node.js: `playwright`), then run, from the repository root:

```sh
python codex-cli-worker/tests/web_fixture.py &
node codex-cli-worker/tests/readme_screenshots.cjs
kill %1
CODEX_FIXTURE_SIGN_IN=pending python codex-cli-worker/tests/web_fixture.py &
CODEX_FIXTURE_SIGN_IN=pending node codex-cli-worker/tests/readme_screenshots.cjs
kill %1
```

The script writes to `screenshots/`. Set `SCREENSHOT_DIR` to write somewhere else.
