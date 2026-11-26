"use strict";
// The conversation and the frame around it: pickers, the chat list, the thread, the message box, slash commands
// and the start. Loads after chat-core.js and chat-settings.js; CX-RF-JS.b splits it in two.
/** The settings of a chat that chose nothing: every value follows the add-on setting or the model. */
const NO_SELECTION = Object.freeze({
  model: null,
  reasoning_effort: null,
  fast: null,
});
/**
 * Return the model, reasoning level and Fast mode selected for the open chat.
 * A null value means the chat follows the add-on setting (for Fast mode, the
 * model's own default).
 */
function selectedSettings() {
  return state.chatSettings.get(state.id) || NO_SELECTION;
}
/** The models a chat can run on, with the add-on's own model when it is not one of the usual choices. */
function chatModels() {
  const {
    models = [],
    default_model: id,
    default_efforts: efforts,
    default_fast: fast = null,
  } = state.catalog || {};
  return !id || models.some((model) => model.id === id)
    ? models
    : [{ id, label: id, efforts, fast }, ...models];
}
/**
 * Return the entry of the model a chat runs on: the one it selected, or else
 * the add-on's own model. Undefined when that model is not among the choices.
 */
function selectedModel(settings = selectedSettings()) {
  return chatModels().find(
    (model) => model.id === (settings.model || state.catalog?.default_model),
  );
}
/**
 * Return the reasoning levels a chat can choose from: those of its model, or
 * those of the add-on's model, or none before the chat options are loaded.
 */
function selectedEfforts(settings = selectedSettings()) {
  return (
    selectedModel(settings)?.efforts || state.catalog?.default_efforts || []
  );
}
/**
 * Return the reasoning level that applies to a chat: its own selection, or
 * else the level the worker gives its model (the defaults' level for a chat
 * on the default model). Falls back to medium when neither is known or the
 * chat's model does not offer that level.
 */
function effectiveEffort(settings = selectedSettings()) {
  const effort =
    settings.reasoning_effort ||
    (settings.model && selectedModel(settings)?.default_effort) ||
    state.catalog?.defaults.reasoning_effort ||
    "medium";
  return selectedModel(settings) && !selectedEfforts(settings).includes(effort)
    ? "medium"
    : effort;
}
/**
 * Return whether a chat runs on Fast: null when its model has no Fast tier,
 * else the chat's own choice, or the model's default when it chose nothing.
 * The worker decides the same way (run_fast in server.py).
 */
function effectiveFast(settings = selectedSettings()) {
  const model = selectedModel(settings);
  const offered = model ? (model.fast ?? null) : null;
  if (offered === null) return null;
  return settings.fast ?? offered;
}
/**
 * Show the lightning toggle in the panel for the open chat: pressed while it
 * runs on Fast, disabled with a plain tooltip when its model has no Fast tier.
 */
function renderFast() {
  const fast = effectiveFast();
  const button = $("effort-fast");
  button.disabled = $("picker-button").disabled || fast === null;
  button.setAttribute("aria-pressed", String(fast === true));
  button.title =
    fast === null
      ? "Fast mode is not available for this model"
      : `Fast mode: ${fast ? "on (about 2x speed, uses more of your plan)" : "off"}`;
}
/** Turn Fast mode on or off for the open chat; nothing happens when its model has no Fast tier. */
async function toggleFast() {
  const fast = effectiveFast();
  if (fast === null) return;
  await saveSelection({ ...selectedSettings(), fast: !fast });
}
/** True while the picker panel or its model list is open. */
function pickerOpen() {
  return !$("effort-popover").hidden || !$("model-popover").hidden;
}
/**
 * Close the picker: the reasoning panel and the model list behind it. With
 * focus, the chip gets the focus back when the picker was open.
 */
function closePicker(focus = false) {
  const wasOpen = pickerOpen();
  $("effort-popover").hidden = true;
  $("model-popover").hidden = true;
  $("picker-button").setAttribute("aria-expanded", "false");
  $("effort-model").setAttribute("aria-expanded", "false");
  renderChip();
  if (focus && wasOpen) $("picker-button").focus();
}
/**
 * Fill the reasoning panel for a level, by default the one that applies to
 * the open chat: its name and description, the model name, and the slider
 * with one stop per level the model offers.
 */
function renderEffort(effort = effectiveEffort()) {
  const efforts = selectedEfforts();
  const index = Math.max(0, efforts.indexOf(effort));
  $("effort-title").textContent = effortLabels[effort] || effort;
  $("effort-model-name").textContent = modelLabel();
  $("effort-slider").max = Math.max(0, efforts.length - 1);
  $("effort-slider").value = index;
  $("effort-slider").setAttribute(
    "aria-valuetext",
    effortLabels[effort] || effort,
  );
  $("effort-slider").style.setProperty(
    "--effort-fill",
    `${(index / Math.max(1, efforts.length - 1)) * 100}%`,
  );
  $("effort-dots").replaceChildren(...efforts.map(() => textNode("span", "")));
  $("effort-description").textContent =
    effortDescriptions[effort] || "Using the add-on default reasoning level.";
  renderFast();
}
/** The name of the model a chat runs on, whether it selected one or follows the add-on setting. */
function modelLabel(settings = selectedSettings()) {
  return selectedModel(settings)?.label || settings.model || "Model";
}
/**
 * Show the chip: the model name, a small bolt while the chat runs on Fast,
 * and the reasoning level in grey, or "Select effort" while the picker is
 * open, as in the ChatGPT desktop app.
 */
function renderChip() {
  const settings = selectedSettings();
  const effort = effectiveEffort(settings);
  const open = pickerOpen();
  $("picker-model").textContent = modelLabel(settings);
  $("effort-label").textContent = effortLabels[effort] || effort;
  $("picker-model").hidden = open;
  $("picker-fast").hidden = open || effectiveFast(settings) !== true;
  $("effort-label").hidden = open;
  $("picker-open-label").hidden = !open;
  $("picker-button").title = settings.reasoning_effort
    ? "Model and reasoning (Ctrl+Shift+M)"
    : "Model and add-on default reasoning (Ctrl+Shift+M)";
}
/**
 * Show the open chat's model and reasoning level on the chip. The picker is
 * disabled and closed until the chat options are loaded, while the chat loads
 * or its task is queued or running, and while a message or a stop request is
 * being sent. While a selection is being saved the controls are disabled but
 * the picker stays open, so the owner sees the result in place.
 */
function renderPicker() {
  const disabled =
    !state.catalog ||
    state.loading ||
    state.busy ||
    state.settingsBusy ||
    Boolean(state.task && ["queued", "running"].includes(state.task.status)) ||
    // A waiting message keeps the settings it was sent with.
    Boolean(state.task?.queued_message);
  for (const id of [
    "picker-button",
    "effort-model",
    "effort-slider",
    "effort-reset",
  ])
    $(id).disabled = disabled;
  for (const option of $("model-options").children) option.disabled = disabled;
  if (disabled && !state.settingsBusy) closePicker();
  renderFast();
  renderChip();
  if (!$("effort-popover").hidden) renderEffort();
  if (!$("model-popover").hidden) renderModels();
}
/**
 * Store a model and reasoning selection for the open chat. A saved chat sends
 * it to the worker and keeps what the worker returns; a new chat only holds it
 * in state until its first message is sent. The open picker shows the choice
 * while it is sent, and the earlier one again if sending fails. Errors show
 * above the composer.
 */
async function saveSelection(settings) {
  const id = state.id;
  const previous = state.chatSettings.get(id);
  state.chatSettings.set(id, settings);
  state.settingsBusy = true;
  controls();
  showError(null);
  try {
    if (id) {
      const data = await api(`tasks/${encodeURIComponent(id)}/settings`, {
        chat_settings: settings,
      });
      settings = data.chat_settings;
      if (state.task) state.task.chat_settings = settings;
    }
    state.chatSettings.set(id, settings);
  } catch (error) {
    if (previous) state.chatSettings.set(id, previous);
    else state.chatSettings.delete(id);
    showError(error);
  } finally {
    state.settingsBusy = false;
    controls();
  }
}
/**
 * Put the open panel or model list right above the chip with its right edge
 * on the chip's right edge, as in the desktop app, moved in when that would
 * leave the window (on a phone the chip sits near the right edge).
 */
function placePicker() {
  const margin = 8;
  const chip = $("picker-button").getBoundingClientRect();
  for (const id of ["effort-popover", "model-popover"]) {
    const popover = $(id);
    if (popover.hidden || !popover.offsetParent) continue;
    const width = popover.offsetWidth;
    const left = Math.max(
      margin,
      Math.min(chip.right - width, innerWidth - width - margin),
    );
    popover.style.right = "auto";
    popover.style.left = `${left - popover.offsetParent.getBoundingClientRect().left}px`;
  }
}
window.addEventListener("resize", () => {
  if (pickerOpen()) placePicker();
});
/**
 * Close the other menus of the message box, so only the picker is open: the
 * add menu, the slash command list, the permissions pop-up and a chat's
 * actions menu.
 */
function closeOtherMenus() {
  $("plus-menu").hidden = true;
  $("attach").setAttribute("aria-expanded", "false");
  if (!$("slash-popover").hidden) {
    slash.dismissed = $("message").value;
    renderSlash();
  }
  if ($("permissions-dialog").open) $("permissions-dialog").close();
  closeChatMenu();
}
/**
 * Show the reasoning panel above the chip, filled for the open chat, with
 * the focus on its slider. The model list behind it is closed.
 */
function showEffortPanel() {
  closeOtherMenus();
  $("model-popover").hidden = true;
  $("effort-model").setAttribute("aria-expanded", "false");
  $("effort-popover").hidden = false;
  $("picker-button").setAttribute("aria-expanded", "true");
  renderEffort();
  renderChip();
  placePicker();
  if (!$("effort-slider").disabled) $("effort-slider").focus();
}
/**
 * Fill the model list with one button per model; the chat's model is checked.
 * A button saves the choice and goes back to the reasoning panel.
 */
function renderModels() {
  const options = $("model-options");
  const focused = options.contains(document.activeElement)
    ? document.activeElement.dataset.model
    : null;
  options.replaceChildren();
  for (const model of chatModels()) {
    const button = textNode("button", "", "model-option");
    button.type = "button";
    button.dataset.model = model.id;
    button.disabled = $("picker-button").disabled;
    const selected = model.id === selectedModel()?.id;
    button.setAttribute("aria-pressed", String(selected));
    const title = textNode("span", "", "model-option-title");
    title.append(
      textNode("span", model.label),
      textNode("span", selected ? "✓" : "", "model-check"),
    );
    button.append(title);
    button.onclick = async () => {
      // The add-on's own model is saved as no selection, so the chat keeps following the add-on setting.
      const settings = {
        ...selectedSettings(),
        model: model.id === state.catalog.default_model ? null : model.id,
      };
      if (
        settings.reasoning_effort &&
        !selectedEfforts(settings).includes(settings.reasoning_effort)
      )
        settings.reasoning_effort = null;
      // The panel comes back at once, showing the new model while it is saved.
      const saving = saveSelection(settings);
      showEffortPanel();
      await saving;
      if (!$("effort-popover").hidden) $("effort-slider").focus();
    };
    options.append(button);
  }
  if (focused)
    options.querySelector(`[data-model="${CSS.escape(focused)}"]`)?.focus();
}
/**
 * Show the model list ("Select model") in place of the reasoning panel, with
 * the focus on the checked model, or on the first one when the chat holds a
 * model that is no longer in the list.
 */
function showModelList() {
  closeOtherMenus();
  $("effort-popover").hidden = true;
  $("model-popover").hidden = false;
  $("picker-button").setAttribute("aria-expanded", "true");
  $("effort-model").setAttribute("aria-expanded", "true");
  renderModels();
  renderChip();
  placePicker();
  const options = $("model-options");
  (
    options.querySelector('[aria-pressed="true"]') || options.firstElementChild
  )?.focus();
}
/**
 * Open the picker, or close it when it is already open. "model" opens it on
 * the model list; anything else opens the reasoning panel.
 */
function openPicker(view = "effort") {
  if (pickerOpen()) {
    closePicker(true);
    return;
  }
  if ($("picker-button").disabled) return;
  if (view === "model") showModelList();
  else showEffortPanel();
}
$("picker-button").onclick = () => openPicker("effort");
$("effort-model").onclick = () => showModelList();
$("effort-slider").addEventListener("input", () =>
  renderEffort(selectedEfforts()[Number($("effort-slider").value)]),
);
$("effort-slider").addEventListener("change", async () => {
  const effort = selectedEfforts()[Number($("effort-slider").value)];
  await saveSelection({ ...selectedSettings(), reasoning_effort: effort });
  if (!$("effort-popover").hidden) $("effort-slider").focus();
});
$("effort-fast").onclick = async () => {
  await toggleFast();
  if (!$("effort-popover").hidden) $("effort-fast").focus();
};
// Reset goes back to the default model and its level, whatever the chat had selected, as in the ChatGPT apps.
$("effort-reset").onclick = async () => {
  await saveSelection({ ...selectedSettings(), model: null, reasoning_effort: null });
  if (!$("effort-popover").hidden) $("effort-reset").focus();
};
document.addEventListener("pointerdown", (event) => {
  // A tap on a model swaps the list for the panel, so the target can leave the page before this runs.
  if (event.target.isConnected && !$("chat-picker").contains(event.target))
    closePicker();
});
$("chat-picker").addEventListener("focusout", (event) => {
  if (event.relatedTarget && !$("chat-picker").contains(event.relatedTarget))
    closePicker();
});
// Ctrl+Shift+M opens and closes the picker from anywhere on the page, as in the desktop app.
document.addEventListener("keydown", (event) => {
  if (
    event.ctrlKey &&
    event.shiftKey &&
    !event.altKey &&
    !event.metaKey &&
    event.key.toLowerCase() === "m" &&
    (pickerOpen() || !$("picker-button").disabled) &&
    // A modal dialog keeps the keys; the permissions pop-up is not modal and closes instead.
    !document.querySelector("dialog:modal")
  ) {
    event.preventDefault();
    openPicker("effort");
  }
});
$("chat-picker").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    closePicker(true);
  }
  if (
    !$("model-popover").hidden &&
    ["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)
  ) {
    event.preventDefault();
    const options = [...$("model-options").children];
    const index = options.indexOf(document.activeElement);
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? options.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) %
            options.length;
    options[next].focus();
  }
});
let optionsRequest = 0;
/**
 * Load the models, defaults, and settings the UI needs into state.catalog and
 * update the controls. With quiet, a failed request shows no error.
 */
async function loadChatOptions(quiet = false) {
  // A slow answer must not replace the catalog of a request made after it.
  const request = ++optionsRequest;
  try {
    const catalog = await api("chat-options");
    if (request === optionsRequest) state.catalog = catalog;
  } catch (error) {
    if (!quiet) showError(error);
  }
  controls();
}
const welcome = $("messages").innerHTML;
/**
 * Open or close the sidebar, which slides over the chat on narrow screens.
 * There, whichever of the two is out of reach is made inert. Closing also
 * closes the chat actions menu; opening moves focus to New chat.
 */
function sidebar(open) {
  if (!open) closeChatMenu();
  document.body.classList.toggle("sidebar-open", open);
  $("scrim").hidden = !open;
  $("menu").setAttribute("aria-expanded", String(open));
  $("sidebar").inert = matchMedia("(max-width:700px)").matches && !open;
  document.querySelector("main").inert =
    matchMedia("(max-width:700px)").matches && open;
  if (open) $("new-chat").focus();
}
// When the message box is expanded, at least this much of the transcript stays in view above it.
const TRANSCRIPT_KEEP = 48;
/**
 * Return the height of the expanded message box: the transcript's room less a strip of it, and never past the
 * visible screen, which an on-screen keyboard makes shorter on a phone or iPad (visualViewport). The strip is at
 * least the transcript's padding and minimum height, below which it cannot shrink; with less, the composer would
 * overflow and each new line would add the difference again.
 */
function expandedHeight() {
  const message = $("message");
  const area = $("messages");
  const style = getComputedStyle(area);
  const floor = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
  const keep = Math.max(TRANSCRIPT_KEEP, floor, parseFloat(style.minHeight) || 0);
  // After the window shrinks, the composer may hang past the bottom of the chat column until it is resized.
  const overflow = Math.max(
    0,
    document.querySelector(".compose-area").getBoundingClientRect().bottom -
      document.querySelector("main").getBoundingClientRect().bottom,
  );
  const room = message.offsetHeight + area.clientHeight - keep - overflow;
  const rest = $("composer").offsetHeight - message.offsetHeight;
  const visible = (window.visualViewport?.height || window.innerHeight) - rest - keep;
  return Math.max(180, Math.floor(Math.min(room, visible)));
}
/** Fit the height of the message box to its text, up to 180 pixels, or give it the expanded height. */
function resizeInput() {
  const message = $("message");
  if ($("composer").classList.contains("expanded")) {
    message.style.height = expandedHeight() + "px";
    return;
  }
  message.style.height = "auto";
  message.style.height = Math.min(180, message.scrollHeight) + "px";
}
/**
 * Expand the message box to the chat's full height, or collapse it. The transcript stays where it was: at its
 * end if it was there, otherwise at the same line.
 */
function setComposerExpanded(expanded) {
  const area = $("messages");
  const atEnd = area.scrollHeight - area.scrollTop - area.clientHeight < 100;
  const top = area.scrollTop;
  const label = expanded ? "Collapse message box" : "Expand message box";
  $("composer").classList.toggle("expanded", expanded);
  $("expand-message").setAttribute("aria-expanded", String(expanded));
  $("expand-message").setAttribute("aria-label", label);
  $("expand-message").title = label;
  resizeInput();
  area.scrollTop = atEnd ? area.scrollHeight : top;
}
/** Say when a waiting message will start, from its place in the queue. */
function queueNote(entry) {
  const ahead = entry.position - 1;
  return ahead > 0
    ? `Starts on its own after the running chat and the ${ahead === 1 ? "message" : `${ahead} messages`} ahead of it in the queue.`
    : "Next in line. Starts on its own when the running chat finishes.";
}
/**
 * Update the controls around the open chat from state: whether Send, Stop
 * task, New chat, and Attach can be used, whether sending means queueing, the
 * notice above the composer, the hint below it, and the picker.
 */
function controls() {
  const task = state.task;
  const working =
    Boolean(task && ["queued", "running"].includes(task.status)) ||
    (Boolean(state.active) && state.active === state.id);
  const waiting = Boolean(task?.queued_message);
  // While another chat works, a message sent here waits in the queue instead.
  const queueing = Boolean(state.active) && state.active !== state.id;
  // What stops any message to this chat, whether typed or picked from choices.
  const blocked =
    !state.catalog ||
    state.settingsBusy ||
    state.busy ||
    state.loading ||
    working ||
    waiting ||
    (state.id !== null && !task?.can_continue);
  $("send").disabled =
    blocked || preparingCount(state.files) > 0 || !$("message").value.trim();
  for (const button of document.querySelectorAll("#messages .choice"))
    button.disabled = blocked;
  for (const button of document.querySelectorAll("#messages .permission-action"))
    button.disabled = state.busy || button.dataset.canAllow === "false";
  $("send").setAttribute(
    "aria-label",
    queueing ? "Add message to queue" : "Send message",
  );
  $("send").title = queueing
    ? "Add to the queue. It starts when the running chat finishes."
    : "";
  $("send").classList.toggle("queueing", queueing);
  // While this chat's task runs or waits to run, the send button is the stop
  // button, whatever the message box holds. Send cannot be used then anyway
  // (see blocked), and a draft must never take away the only way to stop.
  const running = Boolean(task && ["queued", "running"].includes(task.status));
  $("cancel").hidden = !running;
  $("cancel").disabled = state.busy;
  $("send").hidden = running;
  $("new-chat").disabled = state.busy || state.settingsBusy;
  $("message").readOnly = state.busy;
  $("attach").disabled =
    state.busy ||
    state.files.length + preparingCount(state.files) >= UPLOADS_PER_MESSAGE;
  let notice = "";
  if (waiting)
    notice =
      "This chat's message is waiting in the queue and has not been sent yet. You can edit or remove it above.";
  else if (task?.permission_required)
    notice = "Review the permission request above to continue, or stop the task.";
  else if (working)
    // A working chat shows no notice here; the in-chat "Working on your request" line says it.
    notice = "";
  else if (task && !task.can_continue)
    notice =
      "This chat has no saved session to continue. Start a new chat and include the context you need.";
  else if (queueing)
    notice = `Another chat is working. You can send this message now; it will wait in the queue and start on its own when the running chat ${
      state.queue.length
        ? `and the ${state.queue.length === 1 ? "message" : `${state.queue.length} messages`} already in the queue finish`
        : "finishes"
    }.`;
  $("notice").textContent = notice;
  $("notice").hidden = !notice;
  renderPicker();
  document.dispatchEvent(new Event("chat-view"));
}
/** Find a loaded sidebar chat by task id. */
function chatById(id) {
  return state.chats.find((chat) => chat.task_id === id);
}
/** Sort pinned chats first, then most recently updated, then by id for stability. */
function chatOrder(a, b) {
  return (
    Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)) ||
    (b.updated_at || b.created_at).localeCompare(
      a.updated_at || a.created_at,
    ) ||
    b.task_id.localeCompare(a.task_id)
  );
}
/**
 * Build one sidebar row with its title, summary, status, and actions button.
 * A row for a message waiting in the queue shows that message and its place
 * in line instead of the chat's last answer.
 */
function renderRow(chat, queued = null) {
  const title = chat.title || "Untitled chat";
  const row = textNode("div", "", "chat-row");
  row.classList.toggle("current", chat.task_id === state.id);
  row.classList.toggle("menu-open", chat.task_id === menu.id);
  row.classList.toggle("queued", Boolean(queued));
  row.classList.toggle("unread", Boolean(chat.unread) && !queued);
  const main = textNode("button", "", "row-main");
  main.type = "button";
  main.dataset.taskId = chat.task_id;
  main.setAttribute("aria-current", String(chat.task_id === state.id));
  const heading = textNode("span", "", "row-title");
  if (queued) heading.append(queueIcon());
  else if (chat.status === "running") heading.append(spinnerIcon());
  else if (chat.pinned) heading.append(pinIcon());
  else heading.append(chatIcon());
  heading.append(textNode("span", title));
  if (chat.unread && !queued) {
    // A scheduled run answered here since the owner last opened the chat.
    const dot = textNode("span", "", "unread-dot");
    dot.setAttribute("role", "img");
    dot.setAttribute("aria-label", "New scheduled run");
    heading.append(dot);
  }
  main.append(heading);
  main.append(
    textNode(
      "span",
      queued
        ? markdownPlain(queued.message) || queued.message
        : chat.permission_required ? "Waiting for your permission" : markdownPlain(chat.summary || chat.question) ||
            labels[chat.status] ||
            chat.status,
      "row-preview",
    ),
  );
  const meta = textNode("span", "", "row-meta");
  meta.append(
    textNode(
      "span",
      queued
        ? queued.position === 1
          ? "Next in queue"
          : `In queue · ${queued.position} of ${state.queue.length}`
        : chat.permission_required ? "Permission needed" : labels[chat.status] || chat.status,
      queued || chat.status === "waiting_for_input" || chat.permission_required ? "waiting" : "",
    ),
  );
  meta.append(
    textNode(
      "span",
      dateLabel(queued ? queued.created_at : chat.updated_at || chat.created_at),
    ),
  );
  main.append(meta);
  // One line for each chat: the last answer is the tooltip instead of a second line.
  main.title = `${title} - ${main.querySelector(".row-preview").textContent}`;
  // On a phone the side bar stays open, so the owner can look at several chats; its scrim or the close button shuts it.
  main.onclick = () => selectChat(chat.task_id, false, true);
  row.append(main);
  // A chat that is still waiting for its first message has nothing to pin or rename yet.
  if (!chatById(chat.task_id)) return row;
  const actions = textNode("button", "⋯", "row-menu");
  actions.type = "button";
  actions.dataset.menuFor = chat.task_id;
  actions.setAttribute("aria-label", `Chat actions for ${title}`);
  actions.setAttribute("aria-haspopup", "menu");
  actions.setAttribute("aria-expanded", String(chat.task_id === menu.id));
  actions.onclick = () =>
    chat.task_id === menu.id
      ? closeChatMenu(true)
      : openChatMenu(chat.task_id, actions);
  row.append(actions);
  return row;
}
/**
 * Redraw the sidebar chat list, under Pinned and Recent headings when any chat
 * is pinned. Does nothing when the list is unchanged; otherwise keeps the
 * focus and an open chat actions menu on the rows that replace the old ones.
 */
function renderList() {
  const signature = JSON.stringify([
    state.chats,
    state.id,
    menu.id,
    state.queue,
  ]);
  if (signature === state.listSignature) return;
  state.listSignature = signature;
  const list = $("chat-list");
  const focused = document.activeElement;
  const focusId = focused?.dataset.taskId || focused?.dataset.menuFor;
  const focusMenu = Boolean(focused?.dataset.menuFor);
  list.replaceChildren();
  // Chats with a waiting message are listed once, in the order they will start.
  const waiting = new Set(state.queue.map((entry) => entry.task_id));
  const chats = state.chats.filter((chat) => !waiting.has(chat.task_id));
  const pinned = chats.filter((chat) => chat.pinned);
  if (state.queue.length) list.append(textNode("p", "In queue", "list-group"));
  for (const entry of state.queue)
    list.append(
      renderRow(
        chatById(entry.task_id) || {
          task_id: entry.task_id,
          title: entry.title,
        },
        entry,
      ),
    );
  const groups =
    pinned.length || state.queue.length
      ? [
          ["Pinned", pinned],
          ["Recent", chats.filter((chat) => !chat.pinned)],
        ]
      : [["", chats]];
  for (const [label, rows] of groups) {
    if (label && rows.length) list.append(textNode("p", label, "list-group"));
    for (const chat of rows) list.append(renderRow(chat));
  }
  if (focusId)
    list
      .querySelector(
        focusMenu
          ? `[data-menu-for="${CSS.escape(focusId)}"]`
          : `[data-task-id="${CSS.escape(focusId)}"]`,
      )
      ?.focus();
  if (menu.id) {
    // Keep an open menu attached to the row that replaced its trigger.
    const trigger = list.querySelector(
      `[data-menu-for="${CSS.escape(menu.id)}"]`,
    );
    if (trigger) {
      menu.trigger = trigger;
      if (!$("chat-menu").classList.contains("sheet")) positionChatMenu();
    } else closeChatMenu();
  }
  $("list-empty").hidden = state.chats.length + state.queue.length > 0;
  $("load-more").hidden = state.next === null;
}
/**
 * Fetch the chat list from the worker: a refresh of the chats already loaded,
 * or the next 20 when older is set. Updates state.chats and state.active,
 * redraws the list, and opens a new chat when the open one is gone.
 */
async function loadList(older = false) {
  const count = older ? 20 : Math.max(20, Math.min(500, state.chats.length));
  const offset = older ? state.chats.length : 0;
  const data = await api(
    `tasks?summary=true&order=pinned_first&archived=${Boolean(state.archived)}&limit=${count}&offset=${offset}`,
  );
  // A full refresh covers every loaded chat, so drop entries the server no
  // longer returns, such as a chat deleted here or in another tab.
  const kept = older || state.chats.length > 500 ? state.chats : [];
  const hadSelected = state.chats.some((chat) => chat.task_id === state.id);
  const merged = new Map(kept.map((chat) => [chat.task_id, chat]));
  for (const chat of data.tasks) merged.set(chat.task_id, chat);
  state.chats = [...merged.values()].sort(chatOrder);
  state.next = state.chats.length < data.total ? state.chats.length : null;
  const activeChanged = state.active !== data.active_task_id;
  state.active = data.active_task_id;
  state.queue = Array.isArray(data.queue) ? data.queue : [];
  // The archived view lists other chats, so it leaves the approvals of the main list as they were.
  if (!state.archived)
    noteAttention("approvals", state.chats.filter((chat) => chat.permission_required).map((chat) => chat.task_id));
  if (hadSelected && !merged.has(state.id)) await selectChat(null);
  markRead(state.id);
  renderList();
  // Approval panels in the open chat depend on whether any chat is working.
  if (activeChanged && state.task) renderTask();
  controls();
}
/** Keep document ids subject to the same validation as image ids. */
function messageAttachments(list) {
  return (Array.isArray(list) ? list : []).filter(item =>
    item && ['image', 'file'].includes(item.kind) && /^[0-9a-f]{32}$/.test(item.attachment_id));
}
/**
 * The chat's permission mode for the next message. A Full access choice reads as
 * Default once the owner's switch is off (full_access.js sets the flag), so the
 * page shows and sends what the worker will run.
 */
function selectedPermissionMode() {
  const mode = state.permissionModes.get(state.id) || state.task?.permission_mode || 'default';
  return mode === 'full-access' && document.body.dataset.fullAccess !== 'on' ? 'default' : mode;
}
/** Return the image attachments in a list that carry a well-formed id. */
function imageAttachments(list) {
  return (Array.isArray(list) ? list : []).filter(
    (item) =>
      item &&
      item.kind === "image" &&
      typeof item.attachment_id === "string" &&
      /^[0-9a-f]{32}$/.test(item.attachment_id),
  );
}
/** Build a worker-relative attachment URL from ids only, never from server text. */
function attachmentUrl(taskId, attachment, download = false) {
  return (
    `tasks/${encodeURIComponent(taskId)}/attachments/` +
    encodeURIComponent(attachment.attachment_id) +
    (download ? "?download=1" : "")
  );
}
/** Format a byte count as KB or MB for the attachment caption. */
function sizeLabel(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  return bytes < 1024 * 1024
    ? `${Math.max(1, Math.round(bytes / 1024))} KB`
    : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
/** Build the card that downloads one file: its name, size and a download arrow. */
function fileCard(name, size, href) {
  const card = textNode("a", `${name} · ${sizeLabel(size)} ↓`, "document-attachment");
  card.href = href;
  card.download = name || "document";
  return card;
}
/**
 * Return what turns a link in an answer into a download of a file Codex saved
 * in this chat's files folder, or null when the chat has none. Only files the
 * worker listed are offered, and the worker checks the path again when asked.
 */
function chatFileLinks(task) {
  const listing = task?.chat_files;
  if (!listing?.folder || !listing.files?.length) return null;
  const sizes = new Map(listing.files.map((file) => [file.path, file.size]));
  const prefix = `${listing.folder.replace(/\/+$/, "")}/`;
  return (href) => {
    let path = String(href || "").replace(/^file:\/\//, "");
    try {
      // Per part, so that %23 and %3F in a file name become # and ? (decodeURI keeps them encoded).
      path = path.split("/").map(decodeURIComponent).join("/");
    } catch {
      return null;
    }
    const relative = path.startsWith(prefix) ? path.slice(prefix.length) : "";
    if (!sizes.has(relative)) return null;
    const parts = relative.split("/");
    const url = `tasks/${encodeURIComponent(task.task_id)}/files/${parts.map(encodeURIComponent).join("/")}`;
    return fileCard(parts.at(-1), sizes.get(relative), url);
  };
}
/** Render image attachments with a full-size link and a download button. */
function renderAttachments(
  taskId,
  attachments,
  alt = "Image generated by Codex",
  className = "attachments",
) {
  const list = textNode("div", "", className);
  for (const attachment of attachments) {
    if (attachment.kind === 'file') {
      list.append(fileCard(attachment.name, attachment.size, attachmentUrl(taskId, attachment, true)));
      continue;
    }
    const figure = textNode("figure", "", "attachment");
    const link = document.createElement("a");
    link.href = attachmentUrl(taskId, attachment);
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    link.title = "Open full size";
    const img = document.createElement("img");
    img.className = "attachment-image";
    img.src = link.href;
    img.alt = attachment.revised_prompt || alt;
    img.loading = "lazy";
    img.decoding = "async";
    img.onerror = async () => {
      // The worker answers 410 for an upload its daily cleanup has removed.
      let removed = false;
      try {
        removed = (await fetch(link.href)).status === 410;
      } catch {
        // Offline or blocked: show the general message.
      }
      figure.replaceChildren(
        textNode(
          "p",
          removed
            ? "Attachment removed after 7 days."
            : "This image is no longer available.",
          "attachment-missing",
        ),
      );
    };
    link.append(img);
    const caption = textNode("figcaption", "");
    caption.append(
      textNode(
        "span",
        [attachment.name, sizeLabel(attachment.size)]
          .filter(Boolean)
          .join(" · "),
      ),
    );
    const download = textNode("a", "Download");
    download.href = attachmentUrl(taskId, attachment, true);
    download.setAttribute("download", attachment.name || "image");
    caption.append(download);
    figure.append(link, caption);
    list.append(figure);
  }
  return list;
}
// Home Assistant's own configuration check runs after YAML changes; say how it went.
const checkLabels = {
  valid: "Home Assistant configuration check passed",
  invalid: "Home Assistant configuration check failed",
  unavailable: "Home Assistant could not check the configuration",
};
/**
 * Build the line that says how Home Assistant's configuration check went, with
 * any errors and warnings. Returns null when there is no known result.
 */
function renderConfigCheck(check) {
  if (!check || !checkLabels[check.result]) return null;
  const node = textNode("div", "", `check check-${check.result}`);
  node.append(
    textNode(
      "span",
      { valid: "✓", invalid: "✕", unavailable: "?" }[check.result],
      "check-mark",
    ),
    textNode("span", checkLabels[check.result]),
  );
  if (check.result !== "valid" && check.errors)
    node.append(textNode("pre", check.errors, "check-detail"));
  if (check.warnings)
    node.append(textNode("pre", `Warnings: ${check.warnings}`, "check-detail"));
  return node;
}
/** "12s", "1m 05s", or "1h 02m": how long the running exchange has taken so far. */
function elapsedLabel(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60);
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  return minutes ? `${minutes}m ${seconds.padStart(2, "0")}s` : `${seconds}s`;
}
/** Show the running time next to the waiting line and the step list heading. */
function tickElapsed() {
  const activity = state.activity;
  const running =
    activity &&
    activity.id === state.id &&
    activity.startedAt !== null &&
    (activity.running || taskActive());
  for (const node of document.querySelectorAll(".elapsed"))
    node.textContent = running
      ? ` · ${elapsedLabel(Date.now() - activity.startedAt)}`
      : "";
}
/** Check results that mean something did not work, or could not be confirmed. */
const BAD_CHECK_STATUSES = new Set(["failed", "issues", "unavailable"]);
/**
 * Build the collapsed checks block of an answer: one expandable row per check
 * with its result and details. It stays closed under the answer. The closed
 * line counts the bad results, so a failed check shows without opening the
 * block. Returns null when the turn has no checks.
 */
function renderVerification(turn) {
  const checks = turn.verification || [];
  if (!checks.length) return null;
  const section = textNode("details", "", "verification disclosure");
  const bad = checks.filter(check => BAD_CHECK_STATUSES.has(check.status)).length;
  const title = checks.length === 1 ? "Check and readback" : `Checks and readback (${checks.length})`;
  const summary = textNode("summary", bad ? `${title} · ${bad} failed` : title);
  if (bad) summary.classList.add("has-failed");
  section.append(summary);
  for (const check of checks) {
    const row = textNode("details", "", `verification-result disclosure is-${check.status}`);
    const label = {passed: "Passed", failed: "Failed", observed: "Readback"}[check.status] || "Unverified";
    row.append(textNode("summary", `${label} · ${check.entity_id || check.path || check.target || check.operation}`));
    if (check.message) row.append(textNode("pre", check.message));
    if (check.state !== undefined) row.append(textNode("p", `State: ${check.state}${check.expected_state !== null ? ` · Expected: ${check.expected_state}` : ""}`));
    if (check.attributes && Object.keys(check.attributes).length) row.append(textNode("pre", JSON.stringify(check.attributes, null, 2)));
    if (check.checked_at) row.append(textNode("small", dateLabel(check.checked_at, true)));
    section.append(row);
  }
  return section;
}
/**
 * Return "Worked for 1m 21s" from the exchange's start and end times, or ""
 * when the record lacks one of them (a chat saved by an older version).
 */
function workedLabel(turn) {
  const started = Date.parse(turn?.started_at || "");
  const completed = Date.parse(turn?.completed_at || "");
  if (!Number.isFinite(started) || !Number.isFinite(completed) || completed < started) return "";
  return `Worked for ${elapsedLabel(completed - started)}`;
}
/** Count the checks of an exchange that failed or could not be confirmed. */
function badCheckCount(turn) {
  return (turn.verification || []).filter(check => BAD_CHECK_STATUSES.has(check.status)).length;
}
// Only an exchange with a turn id of this form can have saved steps (see TURN_ID_RE in server.py).
const SAVED_TURN_ID = /^[0-9a-f]{32}$/;
/** Return the cache entry for an earlier answer's saved steps, made empty the first time. */
function earlierSteps(taskId, turnId) {
  const key = `${taskId}/${turnId}`;
  let entry = state.earlierSteps.get(key);
  if (!entry) {
    entry = {steps: null, loading: false, failed: false, open: new Set(), redraws: new Set()};
    state.earlierSteps.set(key, entry);
  }
  return entry;
}
/**
 * Fetch an earlier answer's saved steps once, then redraw its list. A chat
 * redrawn while the fetch runs adds its own list to the ones redrawn after it.
 * A failed fetch leaves the steps unknown, so the next opening tries again.
 */
async function loadEarlierSteps(taskId, turn, entry, redraw) {
  if (entry.steps) return;
  entry.redraws.add(redraw);
  if (entry.loading) return;
  entry.loading = true;
  try {
    const data = await api(
      `tasks/${encodeURIComponent(taskId)}/activity?turn=${encodeURIComponent(turn.turn_id)}`,
    );
    entry.steps = workLogEntries(data.steps, turn);
    entry.failed = false;
  } catch (error) {
    entry.failed = true;
  } finally {
    entry.loading = false;
  }
  const redraws = [...entry.redraws];
  entry.redraws.clear();
  for (const draw of redraws) draw();
}
/** Build the work log of an earlier answer, or one line that says why there is none. */
function earlierStepRows(entry, hasChecks, redraw) {
  if (entry.steps === null)
    return [textNode("p", entry.failed ? "Could not load the steps. Close and open this line to try again." : "Loading steps…", "muted")];
  if (!entry.steps.length)
    return hasChecks ? [] : [textNode("p", "No steps were saved for this answer.", "muted")];
  return entry.steps.map(step => workLogNode(step, entry.open, redraw));
}
/**
 * Build the "Worked for" line of an earlier answer: closed, like the latest
 * answer's line, and opening the same list of steps and then the checks. The
 * steps load from the worker the first time the line opens. An answer with no
 * saved steps possible (an old chat) and no checks keeps the plain line, and
 * with no times either it is left out.
 */
function renderWorked(turn, checks, taskId) {
  const label = workedLabel(turn);
  const saved = SAVED_TURN_ID.test(turn.turn_id || "");
  if (!checks && !(saved && label)) return label ? textNode("p", label, "worked-line") : null;
  const block = textNode("div", "", "activity worked");
  const toggle = textNode("button", "", "activity-toggle");
  toggle.type = "button";
  toggle.append(textNode("span", "", "activity-chevron"), textNode("span", label || "Checks and readback"));
  const list = textNode("div", "", "activity-steps");
  const entry = saved ? earlierSteps(taskId, turn.turn_id) : null;
  const fill = () => {
    const rows = entry ? earlierStepRows(entry, Boolean(checks), fill) : [];
    list.replaceChildren(...groupWorkLog([...rows, ...(checks ? [checks] : [])], entry?.open ?? new Set(), fill));
  };
  const show = open => {
    if (open) state.workedOpen.add(turn.turn_id);
    else state.workedOpen.delete(turn.turn_id);
    toggle.setAttribute("aria-expanded", String(open));
    list.hidden = !open;
    if (open && entry) loadEarlierSteps(taskId, turn, entry, fill);
  };
  fill();
  show(Boolean(checks?.open) || state.workedOpen.has(turn.turn_id));
  toggle.onclick = () => show(list.hidden);
  block.append(toggle, list);
  block.openChecks = () => {
    if (!checks) return;
    show(true);
    checks.open = true;
    checks.scrollIntoView({block: "nearest"});
  };
  return block;
}
/**
 * Build the line that stays under an answer when a check failed, so the owner
 * sees an accident without a tap. A click opens the checks.
 */
function failedChecksLine(count, openChecks) {
  const line = textNode("button", "", "check-failed-line");
  line.type = "button";
  line.append(
    textNode("span", count === 1 ? "1 check failed" : `${count} checks failed`),
    textNode("span", "", "activity-chevron"),
  );
  line.onclick = openChecks;
  return line;
}
/**
 * Whether the question needs its own line: from 0.3.3 the answer text already holds it (ask_owner),
 * while an older answer kept the question apart from its summary.
 */
function showsQuestionApart(turn) {
  return Boolean(turn.question) && !(turn.summary || "").includes(turn.question);
}
/** Say when the open chat will be deleted, in its last days before that; hide the line otherwise. */
function showRetentionWarning(warning) {
  const line = $("retention-warning");
  if (!line) return;
  const when = warning ? dateLabel(warning.deletes_at, true) : "";
  line.hidden = !when;
  line.textContent = when
    ? `This chat will be deleted on ${when} unless you send a message. Pin it to keep it.`
    : "";
}
/** Draw the open chat: every exchange with its answer, checks, saved copies, and steps. */
function renderTask(force = false) {
  const task = state.task;
  if (!task) return;
  const queued = task.queued_message || null;
  if (state.edit && state.edit.id !== queued?.queue_id) state.edit = null;
  $("chat-title").textContent = task.title || "Untitled chat";
  showRetentionWarning(task.deletion_warning);
  $("chat-status").textContent =
    task.status === "in_queue"
      ? "In queue · not sent yet"
      : `${task.permission_required ? "Permission needed" : labels[task.status] || task.status} · ${dateLabel(task.updated_at, true)}${queued ? " · next message in queue" : ""}`;
  const signature = JSON.stringify([
    task.turns,
    task.task_source,
    task.history_incomplete,
    task.status,
    queued && [queued.queue_id, queued.message, queued.prompt_attachments],
    state.edit?.id,
    state.catalog?.backup_retention_days,
    // Approval panels depend on whether any chat is working.
    state.active,
  ]);
  if (!force && signature === state.signature) {
    // Its place in line can change without the conversation being redrawn.
    if (queued && $("queued-note"))
      $("queued-note").textContent = queueNote(queued);
    controls();
    ensureActivity();
    return;
  }
  state.signature = signature;
  const area = $("messages");
  const nearBottom =
    area.scrollHeight - area.scrollTop - area.clientHeight < 100;
  const oldScroll = area.scrollTop;
  area.replaceChildren();
  if (task.history_incomplete)
    area.append(
      textNode(
        "p",
        "This chat predates conversation history. Available messages and the latest saved response are shown; some earlier responses may be missing.",
        "history-note",
      ),
    );
  const turns = task.turns || [];
  let newApproval = false;
  state.latestChecks = null;
  for (const turn of turns) {
    const latest = turn === turns[turns.length - 1];
    const exchange = textNode("section", "", "exchange");
    const source = turn.task_source || (turn === turns[0] ? task.task_source : null);
    // Chats the removed lead channel started keep their limits but show no lead label.
    if (source === "schedule") exchange.append(textNode("p", "Scheduled run", "muted task-source"));
    const sent = messageAttachments(turn.prompt_attachments);
    if (sent.length)
      exchange.append(
        renderAttachments(
          task.task_id,
          sent,
          "Image attached to your message",
          "attachments user-attachments",
        ),
      );
    if (turn.message)
      exchange.append(renderMarkdown(turn.message, "message user"));
    // A request still waiting for the owner goes last, below the working lines, so it is not missed.
    const active = latest && task.status === "running";
    const waiting = (turn.approvals || []).filter(approval => active && approval.status === "pending");
    for (const approval of turn.approvals || [])
      if (!waiting.includes(approval))
        exchange.append(renderApproval(approval, task.task_id, turn.turn_id, active));
    const attachments = imageAttachments(turn.attachments);
    const hasAnswer =
      turn.summary || turn.details || turn.question || attachments.length || turn.verification?.length;
    if (hasAnswer) {
      // The steps and checks sit closed behind one "Worked for" line above the answer, as in the desktop app.
      const evidence = renderVerification(turn);
      let openChecks = null;
      if (latest) {
        state.latestChecks = evidence;
        exchange.append(activityBlock(), activityText());
        openChecks = () => {
          if (!state.activity || !evidence) return;
          state.activity.expanded = true;
          evidence.open = true;
          renderActivity();
          evidence.scrollIntoView({block: "nearest"});
        };
      } else {
        const worked = renderWorked(turn, evidence, task.task_id);
        if (worked) exchange.append(worked);
        openChecks = () => worked?.openChecks?.();
      }
      const answer = textNode("div", "", "answer");
      // No visible "Codex" label above the reply; a screen reader still hears whose reply it is.
      answer.setAttribute("role", "group");
      answer.setAttribute("aria-label", "Codex reply");
      const fileLinks = chatFileLinks(task);
      if (turn.summary) answer.append(renderMarkdown(turn.summary, "message", fileLinks));
      if (turn.details && turn.details !== turn.summary)
        answer.append(renderMarkdown(turn.details, "message details", fileLinks));
      if (showsQuestionApart(turn)) answer.append(renderMarkdown(turn.question, "message question"));
      // Only the question still waiting can be answered with a choice.
      if (
        latest &&
        !queued &&
        task.status === "waiting_for_input" &&
        turn.choices?.length
      )
        answer.append(renderChoices(turn));
      const check = renderConfigCheck(turn.config_check);
      if (check) answer.append(check);
      const bad = badCheckCount(turn);
      if (bad) answer.append(failedChecksLine(bad, openChecks));
      if (attachments.length)
        answer.append(renderAttachments(task.task_id, attachments));
      answer.append(
        textNode(
          "div",
          [
            labels[turn.status],
            dateLabel(
              turn.completed_at || turn.updated_at || turn.created_at,
              true,
            ),
          ]
            .filter(Boolean)
            .join(" · "),
          "turn-meta",
        ),
      );
      exchange.append(answer);
    } else if (["queued", "running"].includes(turn.status)) {
      const pending = textNode(
        "p",
        turn.status === "queued"
          ? "Getting started…"
          : task.permission_required ? "Waiting for your permission…" : "Working on your request…",
        "pending",
      );
      if (latest) {
        pending.id = "pending";
        pending.append(textNode("span", "", "elapsed"));
      }
      exchange.append(pending);
      if (latest) exchange.append(activityBlock(), activityText());
    }
    for (const approval of waiting) {
      exchange.append(renderApproval(approval, task.task_id, turn.turn_id, active));
      // Bring each new request into view once, even if the owner scrolled up.
      if (!state.approvalsShown.has(approval.approval_id)) {
        state.approvalsShown.add(approval.approval_id);
        newApproval = true;
      }
    }
    area.append(exchange);
  }
  if (queued) renderQueued(area, task.task_id, queued);
  renderActivity();
  area.scrollTop = force || nearBottom || newApproval ? area.scrollHeight : oldScroll;
  controls();
  ensureActivity();
}
/** Return the first line of what a permission request asked for, for its closed line. */
function approvalHeadline(approval) {
  const plain = approval.confirmation || {};
  const first = [
    approval.command, approval.action, plain.what, approval.tool, approval.network,
    approval.changes?.[0]?.path, approval.scope,
  ].find(value => typeof value === "string" && value.trim());
  return first ? first.trim().split("\n")[0] : "";
}
/**
 * Show a native permission request as text, with its precise action and
 * scope. While it waits for the owner it is a full card with the two answers.
 * Once answered (or expired) it shrinks to one closed line, such as
 * "Allowed once: ls /homeassistant", that opens to the same details.
 */
function renderApproval(approval, taskId, turnId, active) {
  const pending = active && approval.status === "pending";
  const statuses = { allowed: "Allowed once", denied: "Denied", expired: "Expired" };
  const status = pending ? "Permission needed" : statuses[approval.status] || "Expired";
  const card = textNode(pending ? "section" : "details", "", pending ? "permission-card" : "permission-card disclosure is-answered");
  card.setAttribute("aria-label", "Permission request");
  if (pending) card.append(textNode("strong", status));
  else {
    const summary = textNode("summary", "");
    const headline = approvalHeadline(approval);
    summary.append(textNode("strong", status));
    if (headline) summary.append(textNode("span", `: ${headline}`, "permission-headline"));
    card.append(summary);
  }
  const plain = approval.confirmation || {};
  for (const [key, label] of [["what", "What changes"], ["where", "Where"], ["effect", "The effect"]]) {
    if (plain[key]) card.append(textNode("p", `${label}: ${plain[key]}`, "permission-plain"));
  }
  if (approval.reason) card.append(textNode("p", approval.reason));
  card.append(textNode("p", approval.scope || "", "permission-scope"));
  if (approval.cwd) card.append(textNode("p", `Working folder: ${approval.cwd}`, "permission-folder"));
  if (approval.network) card.append(textNode("p", `Network host: ${approval.network}`));
  if (approval.command) card.append(textNode("pre", approval.command, "permission-command"));
  // A tool request: the tool's name and every argument it will send, as the worker recorded them.
  if (approval.tool) card.append(textNode("p", `Tool: ${approval.tool}`, "permission-folder"));
  if (approval.arguments) card.append(textNode("pre", approval.arguments, "permission-command"));
  for (const change of approval.changes || []) {
    card.append(textNode("p", `${change.kind}: ${change.path}`, "permission-folder"));
    if (change.move_path) card.append(textNode("p", `Move to: ${change.move_path}`, "permission-folder"));
    card.append(textNode("pre", change.diff || "No diff available", "permission-diff"));
  }
  if (approval.note) card.append(textNode("p", approval.note, "permission-note"));
  if (pending) {
    const actions = textNode("div", "", "choices");
    actions.setAttribute("role", "group");
    actions.setAttribute("aria-label", "Permission decision");
    for (const [decision, label] of [["allow", "Allow once"], ["deny", "Deny"]]) {
      const button = textNode("button", label, "subtle permission-action");
      button.type = "button";
      button.dataset.canAllow = String(decision !== "allow" || approval.can_allow);
      button.disabled = state.busy || button.dataset.canAllow === "false";
      button.onclick = () => sendApproval(taskId, turnId, approval.approval_id, decision);
      actions.append(button);
    }
    card.append(actions);
  }
  return card;
}
/** Reply to the live request, preserving the draft and refreshing stale decisions. */
async function sendApproval(taskId, turnId, approvalId, decision) {
  if (state.busy || state.id !== taskId) return;
  state.busy = true;
  controls();
  showError(null);
  try {
    await api(`tasks/${encodeURIComponent(taskId)}/approvals/${encodeURIComponent(approvalId)}`, {
      turn_id: turnId, decision,
    });
  } catch (error) {
    showError(error);
  } finally {
    await Promise.allSettled([loadList(), fetchSelected(true)]);
    state.busy = false;
    controls();
  }
}
/**
 * Build a button for each answer Codex offered with its question. A click
 * sends that answer as the next message; the message box stays free for a
 * different one.
 */
function renderChoices(turn) {
  const group = textNode("div", "", "choices");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "Answers Codex offers");
  turn.choices.forEach((choice, index) => {
    const button = textNode(
      "button",
      markdownPlain(choice) || choice,
      "subtle choice",
    );
    button.type = "button";
    button.onclick = () => sendChoice(turn.turn_id, index);
    group.append(button);
  });
  return group;
}
/**
 * Answer the waiting question with one of the choices Codex offered. The turn
 * id lets the worker refuse the answer when the question was already answered
 * somewhere else. A draft in the message box stays as it is.
 */
async function sendChoice(turnId, index) {
  const id = state.id;
  if (!id || state.busy) return;
  state.busy = true;
  controls();
  showError(null);
  try {
    const result = await api(`tasks/${encodeURIComponent(id)}/continue`, {
      choice: index,
      turn_id: turnId,
      chat_settings: selectedSettings(),
      // If another chat is working, the worker holds this answer in its queue.
      queue: true,
    });
    state.chatSettings.delete(id);
    if (result.status !== "in_queue") state.active = result.task_id;
    state.busy = false;
    await selectChat(result.task_id);
    await loadList();
    if (result.status === "in_queue") $("chat-list").scrollTop = 0;
  } catch (error) {
    showError(error);
    // The question may have been answered elsewhere; show where the chat stands.
    await Promise.allSettled([loadList(), fetchSelected(true)]);
  } finally {
    state.busy = false;
    controls();
  }
}
/**
 * Show a message that waits in the queue: outlined rather than filled, so it
 * reads as not sent yet, with controls to edit or remove it.
 */
function renderQueued(area, taskId, entry) {
  const exchange = textNode("section", "", "exchange queued");
  const label = textNode("p", "", "queued-label");
  label.append(queueIcon(), textNode("span", "In queue · not sent yet"));
  exchange.append(label);
  const sent = messageAttachments(entry.prompt_attachments);
  if (sent.length) {
    const images = renderAttachments(
      taskId,
      sent,
      "Image attached to your queued message",
      "attachments user-attachments",
    );
    // A loading image pushes the edit and remove buttons down; keep them in view.
    for (const img of images.querySelectorAll("img"))
      img.addEventListener("load", () => {
        if (
          area.scrollHeight - area.scrollTop - area.clientHeight <
          img.height + 100
        )
          area.scrollTop = area.scrollHeight;
      });
    exchange.append(images);
  }
  const edit = state.edit?.id === entry.queue_id ? state.edit : null;
  const actions = textNode("div", "", "queued-actions");
  exchange.append(actions);
  area.append(exchange);
  const button = (text, className, onclick) => {
    const node = textNode("button", text, className);
    node.type = "button";
    node.disabled = Boolean(edit?.saving);
    node.onclick = onclick;
    actions.append(node);
    return node;
  };
  if (edit) {
    const input = document.createElement("textarea");
    input.id = "queued-edit";
    input.className = "queued-edit";
    input.rows = 2;
    input.value = edit.text;
    input.readOnly = Boolean(edit.saving);
    input.setAttribute("aria-label", "Edit queued message");
    const resize = () => {
      input.style.height = "auto";
      input.style.height = Math.min(320, input.scrollHeight + 2) + "px";
    };
    input.addEventListener("input", () => {
      edit.text = input.value;
      save.disabled = !input.value.trim();
      resize();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        cancelEdit();
      } else if (
        event.key === "Enter" &&
        !event.shiftKey &&
        !event.isComposing &&
        !matchMedia("(max-width:700px)").matches
      ) {
        event.preventDefault();
        saveEdit();
      }
    });
    exchange.insertBefore(input, actions);
    button("Cancel", "subtle", cancelEdit);
    const save = button("Save", "", saveEdit);
    save.id = "queued-save";
    save.disabled = Boolean(edit.saving) || !edit.text.trim();
    // Set the caret before the user can select or replace the text.
    resize();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  } else {
    exchange.insertBefore(renderMarkdown(entry.message, "message user queued"), actions);
    const note = textNode("span", queueNote(entry), "queued-note");
    note.id = "queued-note";
    actions.append(note);
    button("Edit", "subtle", () => startEdit(entry)).id = "queued-edit-button";
    button("Remove", "subtle danger", () => removeQueued(entry)).id =
      "queued-remove";
  }
}
/** Open the editor for a waiting message, starting from its current text. */
function startEdit(entry) {
  state.edit = { id: entry.queue_id, text: entry.message, saving: false };
  showError(null);
  renderTask(true);
}
/** Close the editor and keep the waiting message as it was. */
function cancelEdit() {
  state.edit = null;
  renderTask(true);
}
/** Refresh the open chat and the list after the queue changed. */
async function reloadQueue() {
  await Promise.allSettled([fetchSelected(true), loadList()]);
}
/** Save the edited text of a waiting message. */
async function saveEdit() {
  const edit = state.edit;
  const message = edit?.text.trim();
  if (!message || edit.saving) return;
  edit.saving = true;
  renderTask(true);
  showError(null);
  try {
    await api(`queue/${encodeURIComponent(edit.id)}`, { message });
    if (state.edit === edit) state.edit = null;
  } catch (error) {
    showError(error);
    edit.saving = false;
    // A 404 means it started while it was being edited, so there is nothing left to edit.
    if (error.status === 404 && state.edit === edit) state.edit = null;
    // Unlock the editor even if the refresh below cannot reach the worker.
    renderTask(true);
  }
  await reloadQueue();
}
let removing = false;
/** Take a waiting message out of the queue; a chat that never started goes with it. */
async function removeQueued(entry) {
  if (removing) return;
  removing = true;
  const id = state.id;
  showError(null);
  try {
    await api(
      `queue/${encodeURIComponent(entry.queue_id)}`,
      undefined,
      "DELETE",
    );
    if (state.id === id && state.task?.status === "in_queue")
      await selectChat(null);
  } catch (error) {
    showError(error);
  }
  await reloadQueue();
  removing = false;
}
// Steps Codex reports while it works on the latest message: reasoning
// headlines, progress notes, commands, file edits, searches, and tool calls.
const stepIcons = {
  reasoning: "✦",
  message: "❝",
  command: "›",
  file_change: "✎",
  web_search: "⌕",
  tool_call: "⚙",
  plan: "☰",
  error: "!",
  outcome: "■",
  notice: "…",
  // What the worker itself is doing, before and after Codex runs.
  phase: "◦",
  other: "•",
};
/** Return whether the open chat's task is queued or running. */
function taskActive() {
  return Boolean(
    state.task && ["queued", "running"].includes(state.task.status),
  );
}
/** Forget the steps of the previous chat and stop polling for them. */
function resetActivity(id) {
  polls.activity.stop();
  state.activity = id
    ? {
        id,
        turnId: null,
        seq: 0,
        steps: new Map(),
        running: false,
        startedAt: null,
        loaded: false,
        loading: false,
        expanded: null,
        open: new Set(),
      }
    : null;
}
/** Build the empty, hidden container that renderActivity fills with steps. */
function activityBlock() {
  const block = textNode("div", "", "activity");
  block.id = "activity";
  block.hidden = true;
  return block;
}
/** Build the container for the text messages of the running exchange, shown outside the closed step list. */
function activityText() {
  const box = textNode("div", "", "activity-text");
  box.id = "activity-text";
  return box;
}
/** Load steps once for an idle chat, and keep loading them while Codex works. */
function ensureActivity() {
  const activity = state.activity;
  if (!activity || activity.id !== state.id || polls.activity.waiting || activity.loading)
    return;
  // A chat that is still waiting in the queue has no steps to load yet.
  if (state.task?.status === "in_queue") return;
  if (!activity.loaded || activity.running || taskActive()) polls.activity.run();
}
/** Whether the open chat has a step list that no request is loading now. */
function activityIdle() {
  const activity = state.activity;
  return Boolean(activity && state.id && activity.id === state.id && !activity.loading);
}
/**
 * Fetch the steps added since the last poll. Returns the step list it read
 * and whether to read again from the start; null when there was nothing to
 * read.
 */
async function pollActivity() {
  if (!activityIdle()) return null;
  const activity = state.activity;
  const generation = state.generation;
  let restart = false;
  activity.loading = true;
  try {
    const data = await api(
      `tasks/${encodeURIComponent(state.id)}/activity?after=${activity.seq}`,
    );
    if (generation === state.generation && state.activity === activity)
      restart = mergeActivity(activity, data);
  } catch (error) {
    // The regular refresh reports worker problems; the steps just pause.
  } finally {
    activity.loading = false;
  }
  return { activity, restart };
}
/**
 * Merge one reply into the step list by position and draw it. Returns true
 * when the reply belongs to a new exchange: the list is then emptied and
 * must be read again from the start.
 */
function mergeActivity(activity, data) {
  if (activity.turnId !== null && data.turn_id !== activity.turnId) {
    // A new exchange started, so its steps count from the beginning again.
    activity.steps.clear();
    activity.open.clear();
    activity.expanded = null;
    activity.seq = 0;
    activity.turnId = data.turn_id;
    activity.startedAt = null;
    return true;
  }
  const wasRunning = activity.running;
  activity.turnId = data.turn_id;
  for (const step of data.steps) activity.steps.set(step.index, step);
  activity.seq = data.seq;
  activity.running = data.running;
  activity.loaded = true;
  if (data.running && typeof data.elapsed_ms === "number") {
    const startedAt = Date.now() - data.elapsed_ms;
    // Keep the first reading unless it is clearly off, so the seconds tick evenly.
    if (
      activity.startedAt === null ||
      Math.abs(startedAt - activity.startedAt) > 1500
    )
      activity.startedAt = startedAt;
  } else activity.startedAt = null;
  if (data.steps.length || wasRunning !== data.running) renderActivity();
  tickElapsed();
  if (!data.running && taskActive()) fetchSelected().catch(showError);
  return false;
}
/**
 * When to read the steps next: a second after a read while Codex works, at
 * once after a new exchange started, and a second later while the page is
 * hidden (polled is then undefined). null when the chat changed or is idle.
 */
function activityDelay(polled) {
  if (polled === undefined) return activityIdle() ? 1000 : null;
  if (!polled || state.activity !== polled.activity) return null;
  if (polled.restart) return 0;
  return polled.activity.running || taskActive() ? 1000 : null;
}
poll("activity", pollActivity, activityDelay);
/**
 * Draw the latest exchange's step list. While Codex works, the heading counts
 * the steps and shows the running time, and the list starts closed. A tap on
 * the heading opens it and holds until the turn ends. When the exchange is
 * over, the heading becomes the closed "Worked for" line above the answer, and
 * the list holds the steps with Codex's text messages between them, and then
 * the answer's checks.
 */
function renderActivity() {
  const block = $("activity");
  const activity = state.activity;
  if (!block || !activity || activity.id !== state.id) return;
  const all = [...activity.steps.values()].sort((a, b) => a.index - b.index);
  const running = activity.running || taskActive();
  const turn = state.task?.turns?.at(-1);
  // While Codex works, its text messages show outside the closed list, so the owner can follow along. Once the
  // exchange is over they move into the list, and the answer starts right under the "Worked for" line.
  const entries = running
    ? all.filter(step => step.kind !== "message")
    : workLogEntries(all, activity.turnId === turn?.turn_id ? turn : null);
  const texts = running ? all.filter(step => step.kind === "message" && step.text) : [];
  const steps = entries.filter(step => step.kind !== "message");
  const textBox = $("activity-text");
  if (textBox)
    textBox.replaceChildren(...texts.map(step => renderMarkdown(step.text, "message interim")));
  const checks = state.latestChecks;
  const worked = running ? "" : workedLabel(turn);
  block.hidden = !entries.length && !checks && !worked;
  block.classList.toggle("running", running);
  block.classList.toggle("worked", !running);
  // The step list takes the place of the waiting line once Codex reports steps.
  const pending = $("pending");
  if (pending) pending.hidden = !block.hidden;
  if (block.hidden) {
    block.replaceChildren();
    return;
  }
  if (!entries.length && !checks) {
    // Nothing to open: the total time alone.
    block.replaceChildren(textNode("p", worked, "worked-line"));
    return;
  }
  // While Codex works the steps start closed under the waiting line; a tap holds only until the turn ends.
  if (!running && activity.tappedWhileRunning) {
    activity.expanded = null;
    activity.tappedWhileRunning = false;
  }
  const expanded = activity.expanded ?? Boolean(checks?.open);
  const area = $("messages");
  const nearBottom =
    area.scrollHeight - area.scrollTop - area.clientHeight < 100;
  const count = steps.length === 1 ? "1 step" : `${steps.length} steps`;
  const toggle = textNode("button", "", "activity-toggle");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", String(expanded));
  toggle.setAttribute("aria-controls", "activity-steps");
  const label = textNode(
    "span",
    running
      ? `${state.task?.permission_required ? "Waiting for your permission…" : "Working on your request…"} · ${count}`
      : worked || idleLabel(steps.length, entries.length, count),
  );
  if (running) label.append(textNode("span", "", "elapsed"));
  toggle.append(textNode("span", "", "activity-chevron"), label);
  toggle.onclick = () => {
    activity.expanded = !expanded;
    activity.tappedWhileRunning = running;
    renderActivity();
  };
  const list = textNode("div", "", "activity-steps");
  list.id = "activity-steps";
  list.hidden = !expanded;
  const nodes = entries.map(entry => workLogNode(entry, activity.open, renderActivity));
  list.append(...groupWorkLog(checks ? [...nodes, checks] : nodes, activity.open, renderActivity, running));
  const latestReasoning = running
    ? [...steps].reverse().find(step => step.kind === "reasoning" &&
        typeof step.text === "string" && step.text.trim())
    : null;
  const thinking = latestReasoning
    ? textNode("div", `Thinking: ${latestReasoning.text.replace(/\s+/g, " ").trim()}`, "activity-thinking")
    : null;
  // Under the list, so with the list open the newest thought is next to the newest step.
  block.replaceChildren(...[toggle, list, thinking].filter(Boolean));
  tickElapsed();
  if (nearBottom) area.scrollTop = area.scrollHeight;
}
/** Name a finished list that has no "Worked for" time (a chat saved by an older version). */
function idleLabel(stepCount, entryCount, count) {
  if (stepCount) return `Activity (${count})`;
  return entryCount ? "Activity" : "Checks and readback";
}
/**
 * Return the work log of a finished exchange in order: its steps and the text
 * messages Codex wrote along the way. Saved steps can still hold a copy of the
 * final answer (`turn.summary`); it is left out, since the answer shows below
 * the log. Pass no turn when the steps belong to another exchange.
 */
function workLogEntries(steps, turn) {
  const entries = [...steps]
    .filter(step => step.kind !== "message" || step.text)
    .sort((a, b) => a.index - b.index);
  const lastText = entries.findLast(step => step.kind === "message");
  // Explicit commentary (a message with a phase) is never the answer, even when the words match.
  if (turn && lastText && !lastText.phase && lastText.text.trim() === turn.summary?.trim())
    entries.splice(entries.indexOf(lastText), 1);
  return entries;
}
/** Build one entry of a work log: a text message as a paragraph block, anything else as a step row. */
function workLogNode(entry, open, redraw) {
  if (entry.kind === "message") return renderMarkdown(entry.text, "message interim");
  return renderStep(entry, open, redraw);
}
/**
 * Put each run of step rows in an indented group, and leave the text messages
 * between the groups, so a message starts where the answer starts, as in the
 * desktop app. A group of two or more rows gets a heading that sums it up
 * ("Ran 3 commands, edited a file"); see stepGroup. Anything that is neither a
 * message nor a step row (the checks block) stands outside the groups, so a
 * check that asks for the owner is never folded away.
 */
function groupWorkLog(nodes, open, redraw, running = false) {
  const out = [];
  let rows = [];
  const flush = () => {
    if (rows.length) out.push(stepGroup(rows, open, redraw, running));
    rows = [];
  };
  for (const node of nodes) {
    if (node.classList.contains("step")) {
      rows.push(node);
      continue;
    }
    flush();
    out.push(node);
  }
  flush();
  return out;
}
// What a group heading says for each kind of step, for one and for several (CX-WORKLOG-GROUPS).
const STEP_GROUP_WORDS = {
  command: ["ran a command", n => `ran ${n} commands`],
  file_change: ["edited a file", n => `made ${n} file edits`],
  web_search: ["searched the web", n => `searched the web ${n} times`],
  tool_call: ["used a tool", n => `used ${n} tools`],
  reasoning: ["thought", () => "thought"],
  plan: ["updated the plan", () => "updated the plan"],
  error: ["hit an error", n => `hit ${n} errors`],
  phase: ["got ready", () => "got ready"],
};
/** Sum up a group's rows in one line, kinds in the order they first ran, and say how many failed. */
function stepGroupLabel(rows) {
  const counts = new Map();
  for (const row of rows) {
    const kind = STEP_GROUP_WORDS[row.dataset.kind] ? row.dataset.kind : "other";
    counts.set(kind, (counts.get(kind) || 0) + 1);
  }
  const parts = [...counts].map(([kind, n]) => {
    if (kind === "other") return n === 1 ? "1 other step" : `${n} other steps`;
    const [one, many] = STEP_GROUP_WORDS[kind];
    return n === 1 ? one : many(n);
  });
  const text = parts.join(", ");
  const failed = rows.filter(row => row.classList.contains("is-failed")).length;
  return text[0].toUpperCase() + text.slice(1) + (failed ? ` · ${failed} failed` : "");
}
/**
 * Build one group of step rows. One row needs no heading. Several rows get a
 * heading button: the group starts closed once the exchange is over and open
 * while Codex works, so the owner can follow along; a tap is remembered in
 * `open` under the group's first step index ("g+3" open, "g-3" closed), which
 * keeps it through the redraws while Codex works.
 */
function stepGroup(rows, open, redraw, running) {
  const group = textNode("div", "", "step-group");
  if (rows.length < 2) {
    group.append(...rows);
    return group;
  }
  const key = rows[0].dataset.index;
  const shown = open.has(`g+${key}`) || (running && !open.has(`g-${key}`));
  const head = textNode("button", "", "step-group-head");
  head.type = "button";
  head.setAttribute("aria-expanded", String(shown));
  head.append(textNode("span", "", "step-group-chevron"), textNode("span", stepGroupLabel(rows)));
  head.onclick = () => {
    open.delete(`g+${key}`);
    open.delete(`g-${key}`);
    open.add(`g${shown ? "-" : "+"}${key}`);
    redraw();
  };
  for (const row of rows) row.hidden = !shown;
  group.classList.add("has-head");
  group.append(head, ...rows);
  return group;
}
/**
 * Build one row of the step list: icon, text, and a line with a failed
 * command's exit code, the duration, and the running state. A step with
 * output gets a button that shows or hides it; `open` holds the indexes of
 * the steps whose output shows, and `redraw` draws the list again after a tap.
 */
function renderStep(step, open, redraw) {
  const row = textNode("div", "", `step step-${step.kind} is-${step.status}`);
  row.dataset.kind = step.kind;
  row.dataset.index = String(step.index);
  row.append(textNode("span", stepIcons[step.kind] || "•", "step-icon"));
  const body = textNode("div", "", "step-body");
  const text = textNode("div", step.text, "step-text");
  // A command shows on one line (CSS); the whole of it shows on hover and once its output is open.
  if (step.kind === "command") text.title = step.text;
  body.append(text);
  const meta = [];
  if (step.kind === "command" && step.exit_code)
    meta.push(`exit ${step.exit_code}`);
  if (step.duration_ms >= 1000)
    meta.push(
      `${(step.duration_ms / 1000).toFixed(step.duration_ms >= 10000 ? 0 : 1)} s`,
    );
  if (step.status === "running") meta.push("running");
  const footer = textNode("div", "", "step-meta");
  if (meta.length) footer.append(textNode("span", meta.join(" · ")));
  if (step.output) {
    const shown = open.has(step.index);
    const button = textNode(
      "button",
      shown
        ? "Hide output"
        : step.output_truncated
          ? "Show output (first part)"
          : "Show output",
      "step-output-toggle",
    );
    button.type = "button";
    button.onclick = () => {
      if (shown) open.delete(step.index);
      else open.add(step.index);
      redraw();
    };
    footer.append(button);
    if (shown) {
      row.classList.add("output-open");
      body.append(textNode("pre", step.output, "step-output"));
    }
  }
  if (footer.childElementCount) body.insertBefore(footer, body.children[1] || null);
  row.append(body);
  return row;
}
/**
 * Fetch the open chat from the worker into state.task and draw it; force
 * redraws it even when nothing changed. A response that arrives after another
 * chat was opened is dropped.
 */
async function fetchSelected(force = false) {
  const id = state.id;
  const generation = state.generation;
  if (!id) return;
  let data;
  try {
    data = await api(`tasks/${encodeURIComponent(id)}`);
  } catch (error) {
    // A chat that only existed as a queued message is gone once that message
    // is removed, here or in another tab.
    if (
      error.status === 404 &&
      state.task?.status === "in_queue" &&
      generation === state.generation &&
      id === state.id
    )
      return selectChat(null);
    throw error;
  }
  if (generation !== state.generation || id !== state.id) return;
  state.task = data.task;
  if (!state.chatSettings.has(id))
    state.chatSettings.set(
      id,
      data.task.chat_settings || NO_SELECTION,
    );
  state.loading = false;
  renderTask(force);
}
const LAST_CHAT_KEY = "codex-last-chat";
/** Remember the open chat, or forget it for a new chat, so a reload returns to it. */
function rememberChat(id) {
  try {
    if (id) localStorage.setItem(LAST_CHAT_KEY, id);
    else localStorage.removeItem(LAST_CHAT_KEY);
  } catch (_) {
    /* Storage may be disabled. */
  }
}
/** Return the remembered chat id when it looks like a task id, else null. */
function rememberedChat() {
  try {
    const id = localStorage.getItem(LAST_CHAT_KEY);
    return id && /^[A-Za-z0-9_-]{1,80}$/.test(id) ? id : null;
  } catch (_) {
    return null;
  }
}
/**
 * Open the remembered chat at once, before the list loads, so the welcome
 * screen is not shown first. Resolves when the chat is loaded or given up.
 */
function reopenLastChat() {
  const id = rememberedChat();
  return id ? selectChat(id, true) : Promise.resolve();
}
/**
 * Open a chat, or the welcome screen for null, keeping the draft of the one
 * left. When restoring a remembered chat, a 404 means it was deleted from
 * another tab or device: forget it and start fresh without an error.
 */
async function selectChat(id, restoring = false, keepSidebar = false) {
  if (state.busy || state.settingsBusy) return;
  closePicker();
  state.drafts.set(state.id, $("message").value);
  state.draftFiles.set(state.id, state.files);
  state.id = id;
  rememberChat(id);
  state.files = state.draftFiles.get(id) || [];
  renderPending();
  state.task = null;
  state.edit = null;
  state.generation++;
  state.signature = "";
  resetActivity(id);
  state.loading = Boolean(id);
  $("message").value = state.drafts.get(id) || "";
  resizeInput();
  showError(null);
  if (!keepSidebar) sidebar(false);
  markRead(id);
  renderList();
  controls();
  if (!id) {
    $("chat-title").textContent = "New chat";
    $("chat-status").textContent = "Your Home Assistant, with a little help.";
    $("messages").innerHTML = welcome; // Static, application-owned welcome content only.
    document.dispatchEvent(new Event("chat-view"));
    return;
  }
  $("chat-title").textContent =
    state.chats.find((chat) => chat.task_id === id)?.title || "Conversation";
  $("chat-status").textContent = "Loading…";
  $("messages").replaceChildren(
    textNode("p", "Loading conversation…", "muted"),
  );
  try {
    await fetchSelected(true);
  } catch (error) {
    if (state.id !== id) return;
    if (restoring && error.status === 404) {
      rememberChat(null);
      await selectChat(null);
    } else showError(error);
  }
}
const menu = {
  id: null,
  trigger: null,
  point: null,
  returnId: null,
  longPress: null,
  pressStart: null,
  suppressClick: false,
};
/** Show or clear the error line under the chat list. */
function sidebarError(error) {
  $("sidebar-error").textContent = error ? String(error.message || error) : "";
  $("sidebar-error").hidden = !error;
}
/** Show or clear the error line inside the named dialog. */
function dialogError(name, error) {
  $(name + "-error").textContent = error ? String(error.message || error) : "";
  $(name + "-error").hidden = !error;
}
/** Return the enabled, visible items of the chat actions menu. */
function menuItems() {
  return [...$("chat-menu").querySelectorAll('[role="menuitem"]')].filter(
    (item) => !item.disabled && item.offsetParent !== null,
  );
}
/** Place the desktop menu at the pointer or under its trigger, inside the viewport. */
function positionChatMenu() {
  const panel = $("chat-menu");
  panel.style.left = panel.style.top = "0px";
  const width = panel.offsetWidth;
  const height = panel.offsetHeight;
  const rect = menu.trigger?.getBoundingClientRect();
  let left = menu.point ? menu.point.x : rect ? rect.right - width : 8;
  let top = menu.point ? menu.point.y : rect ? rect.bottom + 4 : 8;
  if (!menu.point && rect && top + height > innerHeight - 8)
    top = rect.top - height - 4;
  left = Math.max(8, Math.min(left, innerWidth - width - 8));
  top = Math.max(8, Math.min(top, innerHeight - height - 8));
  panel.style.left = `${left}px`;
  panel.style.top = `${top}px`;
}
/** Hide the chat actions menu and optionally return focus to its trigger. */
function closeChatMenu(focus = false) {
  if ($("chat-menu").hidden) return;
  const trigger = menu.trigger;
  $("chat-menu").hidden = true;
  $("menu-scrim").hidden = true;
  menu.id = null;
  menu.trigger = null;
  menu.point = null;
  for (const button of $("chat-list").querySelectorAll(
    '.row-menu[aria-expanded="true"]',
  ))
    button.setAttribute("aria-expanded", "false");
  for (const row of $("chat-list").querySelectorAll(".chat-row.menu-open"))
    row.classList.remove("menu-open");
  trigger?.setAttribute("aria-expanded", "false");
  if (focus && trigger?.isConnected) trigger.focus();
}
/** Show the actions for one chat, anchored to its button or as a sheet on phones. */
function openChatMenu(id, trigger, point = null) {
  const chat = chatById(id) || (id === state.id ? state.task : null);
  if (id && !chat) return;
  if (id === menu.id && !$("chat-menu").hidden) return;
  closeChatMenu();
  closePicker();
  menu.id = id;
  menu.trigger =
    trigger ||
    $("chat-list").querySelector(`[data-menu-for="${CSS.escape(id || "")}"]`);
  menu.point = point;
  const panel = $("chat-menu");
  $("chat-menu-title").textContent = chat?.title || "New chat";
  panel.querySelector('[data-action="pin"]').textContent = chat?.pinned
    ? "Unpin chat"
    : "Pin chat";
  const remove = panel.querySelector('[data-action="delete"]');
  const working =
    ["queued", "running"].includes(chat?.status) || state.active === id;
  remove.disabled = working;
  remove.title = working ? "Stop this task before deleting it." : "";
  const sheet = matchMedia("(max-width:700px)").matches;
  panel.classList.toggle("sheet", sheet);
  panel.style.left = panel.style.top = "";
  panel.hidden = false;
  $("menu-scrim").hidden = !sheet;
  if (!sheet) positionChatMenu();
  menu.trigger?.setAttribute("aria-expanded", "true");
  menu.trigger?.closest(".chat-row")?.classList.add("menu-open");
  for (const action of ["pin", "rename", "share", "schedule", "delete", "archive"])
    panel.querySelector(`[data-action="${action}"]`)?.toggleAttribute("disabled", !id ||
      (["delete", "archive"].includes(action) && (working || Boolean(state.task?.queued_message))));
  panel.querySelector('[data-action="archive"]').textContent = chat?.archived ? "Restore chat" : "Archive";
  menuItems()[0]?.focus();
}
/** Stop a pending long-press timer and forget where the press started. */
function cancelLongPress() {
  clearTimeout(menu.longPress);
  menu.longPress = null;
  menu.pressStart = null;
}
// A long press opens the menu, so the click released afterwards must not
// select the row, tap the scrim, or hit whatever ends up under the finger.
document.addEventListener(
  "click",
  (event) => {
    if (!menu.suppressClick) return;
    menu.suppressClick = false;
    event.preventDefault();
    event.stopPropagation();
  },
  true,
);
// Some browsers fire no click after a long press, so every new press starts
// fresh instead of letting a stale flag swallow the next tap on the menu.
document.addEventListener(
  "pointerdown",
  () => {
    menu.suppressClick = false;
  },
  true,
);
$("chat-list").addEventListener("pointerdown", (event) => {
  cancelLongPress();
  const row = event.target.closest(".row-main");
  if (!row || !event.isPrimary || !["touch", "pen"].includes(event.pointerType))
    return;
  menu.pressStart = { x: event.clientX, y: event.clientY };
  menu.longPress = setTimeout(() => {
    menu.longPress = null;
    menu.suppressClick = true;
    if (navigator.vibrate) navigator.vibrate(10);
    openChatMenu(row.dataset.taskId);
  }, 500);
});
$("chat-list").addEventListener("pointermove", (event) => {
  if (
    menu.pressStart &&
    Math.hypot(
      event.clientX - menu.pressStart.x,
      event.clientY - menu.pressStart.y,
    ) > 10
  )
    cancelLongPress();
});
for (const type of ["pointerup", "pointercancel", "pointerleave"])
  $("chat-list").addEventListener(type, cancelLongPress);
$("chat-list").addEventListener("contextmenu", (event) => {
  const row = event.target.closest(".chat-row");
  if (!row) return;
  event.preventDefault();
  const id = row.querySelector(".row-main").dataset.taskId;
  // Android raises contextmenu for the same long press; keep one menu and drop the click.
  const touch = Boolean(menu.pressStart);
  if (touch) menu.suppressClick = true;
  cancelLongPress();
  const point =
    !touch && event.clientX && event.clientY
      ? { x: event.clientX, y: event.clientY }
      : null;
  openChatMenu(id, null, point);
});
$("chat-list").addEventListener("keydown", (event) => {
  const row = event.target.closest(".row-main");
  if (
    row &&
    (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10"))
  ) {
    event.preventDefault();
    openChatMenu(row.dataset.taskId);
  }
});
$("chat-list").addEventListener("scroll", () => {
  // Follow the row while it stays in view; close once it scrolls away.
  if ($("chat-menu").hidden || $("chat-menu").classList.contains("sheet"))
    return;
  const list = $("chat-list").getBoundingClientRect();
  const rect = menu.trigger?.getBoundingClientRect();
  if (!rect || rect.bottom < list.top || rect.top > list.bottom)
    closeChatMenu();
  else positionChatMenu();
});
window.addEventListener("resize", () => closeChatMenu());
$("menu-scrim").onclick = () => closeChatMenu();
document.addEventListener("pointerdown", (event) => {
  if (
    !$("chat-menu").hidden &&
    !$("chat-menu").contains(event.target) &&
    event.target !== menu.trigger
  )
    closeChatMenu();
});
$("chat-menu").addEventListener("keydown", (event) => {
  const items = menuItems();
  const index = items.indexOf(document.activeElement);
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closeChatMenu(true);
  } else if (event.key === "Tab") {
    closeChatMenu(true);
  } else if (["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? items.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) %
            items.length;
    items[next]?.focus();
  }
});
$("chat-menu").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const id = menu.id;
  const action = button.dataset.action;
  menu.returnId = id;
  closeChatMenu(action === "close" || action === "pin");
  if (action === "pin") await togglePin(id);
  else if (action === "rename") openRename(id);
  else if (action === "delete") openDelete(id);
  else if (action === "share") openShare(id);
  else document.dispatchEvent(new CustomEvent("thread-action", {detail: {action, id}}));
});
/**
 * Clear the unread mark a scheduled run left on the open chat, while the page is in view.
 * A failed request leaves nothing to undo: the next list refresh shows the mark again.
 */
function markRead(id) {
  const chat = id && chatById(id);
  if (!chat?.unread || document.hidden) return;
  chat.unread = false;
  api(`tasks/${encodeURIComponent(id)}/read`, {}).catch(() => {});
}
/** Pin or unpin a chat through the worker API and re-sort the list. */
async function togglePin(id) {
  const chat = chatById(id);
  if (!chat) return;
  sidebarError(null);
  try {
    const data = await api(`tasks/${encodeURIComponent(id)}/pin`, {
      pinned: !chat.pinned,
    });
    chat.pinned = data.pinned;
    state.chats.sort(chatOrder);
    renderList();
  } catch (error) {
    sidebarError(error);
  }
}
/** Open the rename dialog prefilled with the chat's current title. */
function openRename(id) {
  const chat = chatById(id);
  if (!chat) return;
  $("rename-dialog").dataset.chatId = id;
  $("rename-input").value = chat.title || "";
  $("rename-save").disabled = false;
  dialogError("rename", null);
  $("rename-dialog").showModal();
  $("rename-input").select();
}
$("rename-cancel").onclick = () => $("rename-dialog").close();
$("rename-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = $("rename-dialog").dataset.chatId;
  const title = $("rename-input").value.replace(/\s+/g, " ").trim();
  if (!title) {
    dialogError("rename", "Enter a title for this chat.");
    return;
  }
  await busyAction($("rename-save"), async () => {
    const data = await api(`tasks/${encodeURIComponent(id)}/title`, { title });
    const chat = chatById(id);
    if (chat) chat.title = data.title;
    if (state.task?.task_id === id) state.task.title = data.title;
    if (state.id === id) $("chat-title").textContent = data.title;
    renderList();
    $("rename-dialog").close();
  }, (error) => dialogError("rename", error));
});
/** Open the Share dialog for a chat: the whole chat, or its last lines. */
function openShare(id) {
  if (!chatById(id) && id !== state.id) return;
  $("share-dialog").dataset.chatId = id;
  $("share-download").disabled = false;
  dialogError("share", null);
  $("share-dialog").showModal();
  $("share-download").focus();
}
$("share-cancel").onclick = () => $("share-dialog").close();
$("share-lines").addEventListener("focus", () => {
  $("share-form").elements["share-range"].value = "last";
});
$("share-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = $("share-dialog").dataset.chatId;
  let query = "";
  if ($("share-form").elements["share-range"].value === "last") {
    const lines = Number($("share-lines").value);
    if (!Number.isInteger(lines) || lines < 1 || lines > 100000) {
      dialogError("share", "Enter a number of lines from 1 to 100000.");
      return;
    }
    query = `?lines=${lines}`;
  }
  await busyAction($("share-download"), async () => {
    const response = await pageFetch(`tasks/${encodeURIComponent(id)}/share${query}`, { cache: "no-store" });
    if (!response.ok) throw new Error("The chat could not be downloaded. Try again.");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = `codex-chat-${id}.md`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    $("share-dialog").close();
  }, (error) => dialogError("share", error));
});
/** Open the delete confirmation dialog for a chat. */
function openDelete(id) {
  const chat = chatById(id);
  if (!chat) return;
  $("delete-dialog").dataset.chatId = id;
  $("delete-text").textContent =
    `“${chat.title || "Untitled chat"}” and its saved history will be removed. This cannot be undone.`;
  $("delete-confirm").disabled = false;
  dialogError("delete", null);
  $("delete-dialog").showModal();
  $("delete-cancel").focus();
}
$("delete-cancel").onclick = () => $("delete-dialog").close();
for (const name of ["rename", "share", "delete"])
  $(name + "-dialog").addEventListener("close", () => {
    // A command in the message box opened the dialog: the browser has already
    // put focus back in the box. This event comes later than the close, so a
    // move of focus here takes it away while the person types.
    if (!menu.returnId) return;
    // The chat menu opened it: return focus to the row's menu button, or to
    // New chat when the row is gone.
    const trigger = $("chat-list").querySelector(
      `[data-menu-for="${CSS.escape(menu.returnId)}"]`,
    );
    menu.returnId = null;
    (trigger || $("new-chat")).focus();
  });
$("delete-confirm").onclick = () => {
  const id = $("delete-dialog").dataset.chatId;
  return busyAction($("delete-confirm"), async () => {
    await api(`tasks/${encodeURIComponent(id)}`, undefined, "DELETE");
    state.chats = state.chats.filter((chat) => chat.task_id !== id);
    state.chatSettings.delete(id);
    $("delete-dialog").close();
    if (state.id === id) await selectChat(null);
    state.drafts.delete(id);
    state.draftFiles.delete(id);
    renderList();
    await loadList();
  }, (error) => dialogError("delete", error));
};
$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  // A message that is exactly a command the page can run is run here, not sent.
  const command = !state.files.length && slashCommand($("message").value);
  if (command) return runSlash(command);
  if ($("send").disabled) return;
  const message = $("message").value.trim();
  const id = state.id;
  state.busy = true;
  controls();
  showError(null);
  try {
    const attachments = await Promise.all(
      state.files.map(async (upload) => ({
        name: upload.name,
        kind: upload.kind || "image",
        data: await encodeUpload(upload),
      })),
    );
    const result = await api(
      id ? `tasks/${encodeURIComponent(id)}/continue` : "tasks",
      {
        ...(id ? { message } : { prompt: message }),
        ...(attachments.length ? { attachments } : {}),
        chat_settings: selectedSettings(),
        permission_mode: selectedPermissionMode(),
        // If another chat is working, the worker holds this message in its queue.
        queue: true,
      },
    );
    state.drafts.delete(id);
    state.draftFiles.delete(id);
    state.chatSettings.delete(id);
    clearUploads();
    $("message").value = "";
    setComposerExpanded(false);
    if (result.status !== "in_queue") state.active = result.task_id;
    state.busy = false;
    await selectChat(result.task_id);
    await loadList();
    // The queue is listed first, so bring the new entry into view.
    if (result.status === "in_queue") $("chat-list").scrollTop = 0;
  } catch (error) {
    showError(error);
    // A failed request may have saved a terminal task; refresh status without retrying the message.
    await Promise.allSettled([loadList(), fetchSelected()]);
  } finally {
    state.busy = false;
    controls();
    resizeInput();
  }
});
/** Read a pending image as base64 for the JSON request body. */
function encodeUpload(upload) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () =>
      resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () =>
      reject(new Error(`Could not read ${upload.name}.`));
    reader.readAsDataURL(upload.blob);
  });
}
/** Shrink an image whose longest edge exceeds the limit; other images pass through. */
async function prepareUpload(file) {
  if (file.documentUpload) {
    if (!file.size) {
      const bytes = await file.arrayBuffer().catch(() => null);
      if (!bytes?.byteLength) throw new Error('The file is empty or could not be read from the picker.');
      file = new File([bytes], file.name || 'document.txt', {type:file.type});
    }
    if (file.size > UPLOAD_MAX_BYTES) throw new Error('Files must contain 1 byte to 10 MB.');
    if (!/\.(txt|md|ya?ml|json|csv|log|py|js|html|css|xml|pdf)$/i.test(file.name))
      throw new Error('Choose a text, source, configuration or PDF file.');
    return {id: crypto.randomUUID(), name:file.name, size:file.size, blob:file, kind:'file', url:''};
  }
  const type = file.type === "image/jpg" ? "image/jpeg" : file.type;
  // Drag-and-drop and clipboard sources can omit the type; the worker sniffs the bytes.
  if (type && !UPLOAD_TYPES.includes(type))
    throw new Error(
      `${file.name || "This file"} is not a PNG, JPEG, GIF, or WebP image.`,
    );
  if (!file.size) {
    // Some mobile pickers hand over a file whose size is unknown until read.
    const bytes = await file.arrayBuffer().catch(() => null);
    if (!bytes || !bytes.byteLength)
      throw new Error(
        `${file.name || "The selected file"} is empty or could not be read from the picker.`,
      );
    file = new File([bytes], file.name || "image", { type });
  }
  let blob = file;
  let name = file.name || "image";
  let bitmap = null;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    bitmap = null; // Undecodable here; the worker still verifies the bytes.
  }
  if (bitmap) {
    const scale = Math.min(
      1,
      UPLOAD_MAX_EDGE / Math.max(bitmap.width, bitmap.height),
    );
    if (scale < 1 || file.size > UPLOAD_MAX_BYTES) {
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      canvas
        .getContext("2d")
        .drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const output = type === "image/jpeg" ? "image/jpeg" : "image/png";
      const resized = await new Promise((resolve) =>
        canvas.toBlob(resolve, output, 0.9),
      );
      if (resized) {
        blob = resized;
        name =
          name.replace(/\.[^.]+$/, "") +
          (output === "image/jpeg" ? ".jpg" : ".png");
      }
    }
    bitmap.close();
  }
  if (blob.size > UPLOAD_MAX_BYTES)
    throw new Error(
      `${file.name || "This image"} is larger than ${UPLOAD_MAX_BYTES / (1024 * 1024)} MB.`,
    );
  return {
    id: globalThis.crypto?.randomUUID
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random()}`,
    name,
    size: blob.size,
    blob,
    url: URL.createObjectURL(blob),
  };
}
// Images still decoding or resizing, keyed by the pending list they belong to.
const preparing = new Map();
/** Count the images still being prepared for a pending list. */
function preparingCount(list) {
  return preparing.get(list) || 0;
}
/** Add chosen, dropped, or pasted files to the pending list after checks. */
async function addUploads(fileList) {
  const files = Array.from(fileList || []).filter(Boolean);
  if (!files.length) return;
  if (state.busy) {
    showError(new Error("Wait for the current message to finish sending, then attach the image."));
    return;
  }
  // The batch belongs to the draft it was picked in. Switching chats swaps
  // state.files for another list, so every step below works on this one.
  const target = state.files;
  /** Redraw the pending images and controls if this draft is still open. */
  const sync = () => {
    if (target !== state.files) return;
    renderPending();
    controls();
  };
  showError(null);
  for (const file of files) {
    if (target.length + preparingCount(target) >= UPLOADS_PER_MESSAGE) {
      showError(
        new Error(`Attach at most ${UPLOADS_PER_MESSAGE} images per message.`),
      );
      break;
    }
    preparing.set(target, preparingCount(target) + 1);
    sync();
    try {
      target.push(await prepareUpload(file));
    } catch (error) {
      showError(error);
    } finally {
      if (preparingCount(target) > 1)
        preparing.set(target, preparingCount(target) - 1);
      else preparing.delete(target);
      sync();
    }
  }
}
/** Drop one pending image and release its preview. */
function removeUpload(id) {
  const index = state.files.findIndex((item) => item.id === id);
  if (index < 0) return;
  URL.revokeObjectURL(state.files[index].url);
  // Mutate in place: a batch still decoding holds a reference to this list.
  state.files.splice(index, 1);
  renderPending();
  controls();
  $("attach").focus();
}
/** Forget every pending image after a successful send. */
function clearUploads() {
  for (const upload of state.files) URL.revokeObjectURL(upload.url);
  state.files = [];
  renderPending();
}
/** Show the pending images above the message box with remove buttons. */
function renderPending() {
  const strip = $("pending-files");
  strip.replaceChildren();
  for (const upload of state.files) {
    const item = textNode("div", "", "pending-file");
    const img = document.createElement(upload.kind === 'file' ? 'span' : 'img');
    if (upload.kind === 'file') img.textContent = '▤';
    else { img.src = upload.url; img.alt = ''; }
    const name = textNode("span", upload.name, "pending-name");
    name.title = `${upload.name} · ${sizeLabel(upload.size)}`;
    const remove = textNode("button", "×", "pending-remove");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${upload.name}`);
    remove.onclick = () => removeUpload(upload.id);
    item.append(img, name, remove);
    strip.append(item);
  }
  for (let count = preparingCount(state.files); count > 0; count--)
    strip.append(textNode("div", "Preparing image…", "pending-file preparing"));
  strip.hidden = !state.files.length && !preparingCount(state.files);
}
// Android WebViews, including the Home Assistant app, ask the system Photo
// Picker for a multi-select when the input allows several files, but their
// file-chooser result handling reads only a single URI, so the page receives
// nothing. Single selection returns a plain URI and works; pick again for more.
if (
  /Android/.test(navigator.userAgent) &&
  /; wv\)|Home Assistant/.test(navigator.userAgent)
)
  for (const id of ["file-input", "document-input"]) $(id).removeAttribute("multiple");
$("attach").onclick = () => {
  closePicker();
  const panel = $('plus-menu'); panel.hidden = !panel.hidden;
  $('attach').setAttribute('aria-expanded', String(!panel.hidden));
};
/** Feed picked, pasted, or dropped files to addUploads and report any surprise. */
async function acceptFiles(fileList) {
  try {
    await addUploads(fileList);
  } catch (error) {
    console.error("Attaching images failed", error);
    showError(
      new Error(
        `Could not attach the image: ${String(error?.message || error)}`,
      ),
    );
  }
}
$("file-input").addEventListener("change", async () => {
  const input = $("file-input");
  await acceptFiles(input.files);
  input.value = "";
});
$("message").addEventListener("paste", (event) => {
  const files = [...(event.clipboardData?.files || [])].filter(
    (file) => !file.type || file.type.startsWith("image/"),
  );
  if (!files.length) return;
  event.preventDefault();
  acceptFiles(files);
});
for (const type of ["dragenter", "dragover"])
  $("composer").addEventListener(type, (event) => {
    if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
    event.preventDefault();
    $("composer").classList.add("dropping");
  });
$("composer").addEventListener("dragleave", (event) => {
  if (!$("composer").contains(event.relatedTarget))
    $("composer").classList.remove("dropping");
});
$("composer").addEventListener("drop", (event) => {
  $("composer").classList.remove("dropping");
  if (!event.dataTransfer?.files?.length) return;
  event.preventDefault();
  acceptFiles(event.dataTransfer.files);
});
$("expand-message").onclick = () => {
  setComposerExpanded(!$("composer").classList.contains("expanded"));
  $("message").focus();
};
/** Fit the expanded message box to a new window or keyboard height; a collapsed box fits its text alone. */
function refitExpanded() {
  if ($("composer").classList.contains("expanded")) resizeInput();
}
window.addEventListener("resize", refitExpanded);
window.visualViewport?.addEventListener("resize", refitExpanded);
$("message").addEventListener("input", () => {
  resizeInput();
  renderSlash();
  controls();
});
$("message").addEventListener("keydown", (event) => {
  if (slashKeydown(event)) return;
  if (
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.isComposing &&
    !matchMedia("(max-width:700px)").matches
  ) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});
$("messages").addEventListener("click", (event) => {
  const button = event.target.closest("[data-prompt]");
  if (button) {
    $("message").value = button.dataset.prompt;
    resizeInput();
    controls();
    $("message").focus();
  }
});
$("new-chat").onclick = () => selectChat(null);
$("menu").onclick = () => sidebar(true);
$("close-sidebar").onclick = $("scrim").onclick = () => {
  sidebar(false);
  $("menu").focus();
};
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") sidebar(false);
});
matchMedia("(max-width:700px)").addEventListener("change", () =>
  sidebar(false),
);
$("refresh").onclick = async () => {
  polls.usage.run();
  try {
    await loadList();
    await fetchSelected();
    showError(null);
  } catch (error) {
    showError(error);
  }
};
$("load-more").onclick = () => busyAction($("load-more"), () => loadList(true), showError);
$("cancel").onclick = async () => {
  state.busy = true;
  controls();
  try {
    await api(`tasks/${encodeURIComponent(state.id)}/cancel`, {});
    await fetchSelected();
    await loadList();
  } catch (error) {
    showError(error);
  } finally {
    state.busy = false;
    controls();
  }
};
const SIDEBAR_MIN = 200;
const SIDEBAR_MAX = 480;
const SIDEBAR_DEFAULT = 260;
/**
 * Set the sidebar width in pixels, kept between 200 and 480 and leaving 360
 * for the chat when the window allows, and remember it for the next visit.
 */
function setWidth(value) {
  const width = Math.max(
    SIDEBAR_MIN,
    Math.min(SIDEBAR_MAX, Number(value) || SIDEBAR_DEFAULT, innerWidth - 360),
  );
  document.documentElement.style.setProperty("--sidebar-width", `${width}px`);
  $("divider").setAttribute("aria-valuenow", String(width));
  try {
    localStorage.setItem("codex-sidebar-width", String(width));
  } catch (_) {
    /* Storage may be disabled. */
  }
}
try {
  setWidth(localStorage.getItem("codex-sidebar-width") || SIDEBAR_DEFAULT);
} catch (_) {
  setWidth(SIDEBAR_DEFAULT);
}
$("divider").addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  $("divider").setPointerCapture(event.pointerId);
  document.body.classList.add("resizing");
});
$("divider").addEventListener("pointermove", (event) => {
  if ($("divider").hasPointerCapture(event.pointerId)) setWidth(event.clientX);
});
$("divider").addEventListener("pointerup", (event) => {
  $("divider").releasePointerCapture(event.pointerId);
});
$("divider").addEventListener("lostpointercapture", () =>
  document.body.classList.remove("resizing"),
);
// A double-click gives back the default width.
$("divider").addEventListener("dblclick", () => setWidth(SIDEBAR_DEFAULT));
$("divider").addEventListener("keydown", (event) => {
  if (["ArrowLeft", "ArrowRight"].includes(event.key)) {
    event.preventDefault();
    setWidth(
      Number($("divider").getAttribute("aria-valuenow")) +
        (event.key === "ArrowLeft" ? -20 : 20),
    );
  }
});
window.addEventListener("resize", () => {
  if (innerWidth > 700) setWidth($("divider").getAttribute("aria-valuenow"));
});
// Version 0.5 sends slash-prefixed messages through the regular composer.
const SLASH_COMMANDS_ENABLED = false;
if (!SLASH_COMMANDS_ENABLED) {
  const message = $("message");
  message.removeAttribute("aria-autocomplete");
  message.removeAttribute("aria-controls");
  message.removeAttribute("aria-expanded");
}

/**
 * Page shortcuts and their availability when slash commands are enabled.
 * /fast turns Fast mode on or off, as the lightning toggle in the picker
 * does; it is listed only while the chat's model has a Fast tier.
 */
const SLASH_COMMANDS = [
  {
    name: "new",
    icon: "＋",
    description: "Start a new chat",
    available: () => !$("new-chat").disabled,
    run: () => selectChat(null),
  },
  {
    name: "stop",
    icon: "■",
    description: "Stop the chat that is working",
    available: () => !$("cancel").hidden && !$("cancel").disabled,
    run: () => $("cancel").click(),
  },
  {
    name: "model",
    icon: "◇",
    description: "Choose the model for this chat",
    available: () => Boolean(state.catalog) && !$("picker-button").disabled,
    run: () => openPicker("model"),
  },
  {
    name: "reasoning",
    icon: "✦",
    description: "Choose how long Codex thinks before it answers",
    available: () => Boolean(state.catalog) && !$("picker-button").disabled,
    run: () => openPicker("effort"),
  },
  {
    name: "fast",
    icon: "⚡",
    description: "Turn Fast mode on or off for this chat",
    available: () =>
      Boolean(state.catalog) &&
      !$("picker-button").disabled &&
      effectiveFast() !== null,
    run: () => toggleFast(),
  },
  {
    name: "rename",
    icon: "✎",
    description: "Rename this chat",
    available: () => Boolean(state.id && chatById(state.id)),
    run: () => openRename(state.id),
  },
  {
    name: "usage",
    icon: "◔",
    aliases: ["status"],
    description: "Show how much of your ChatGPT plan is used",
    available: () => true,
    run: () => {
      polls.usage.run();
      if (matchMedia("(max-width:700px)").matches) sidebar(true);
      $("usage").scrollIntoView({ block: "nearest" });
    },
  },
  {
    name: "size",
    icon: "Aa",
    description: "Change the text size",
    available: () => true,
    run: () => openSettingsAt("text-size-section"),
  },
  {
    name: "connection",
    icon: "⇄",
    description: "Open the remote connection and its status",
    // Listed until Settings has shown that remote control cannot run here, and never while it is paused.
    available: () => !REMOTE_PAUSED && state.remoteAvailable !== false,
    run: () => openConnection(),
  },
  {
    name: "settings",
    icon: "⚙",
    description: "Open Settings",
    available: () => true,
    run: () => $("settings-button").click(),
  },
  {
    name: "pin",
    icon: "⚲",
    description: "Pin or unpin this chat",
    available: () => Boolean(state.id && chatById(state.id)),
    run: () => togglePin(state.id),
  },
  {
    name: "share",
    icon: "↗",
    description: "Download this chat as a file",
    available: () => Boolean(state.id && chatById(state.id)),
    run: () => openShare(state.id),
  },
  {
    name: "archive",
    icon: "▤",
    description: "Archive this chat, or restore it",
    available: () => chatIdle(),
    run: () =>
      document.dispatchEvent(
        new CustomEvent("thread-action", {
          detail: { action: "archive", id: state.id },
        }),
      ),
  },
  {
    name: "delete",
    icon: "⌫",
    description: "Delete this chat and its history",
    available: () => chatIdle(),
    run: () => openDelete(state.id),
  },
];
/** Open Settings with one section open and in view, as the GitHub banner does. */
async function openSettingsAt(sectionId) {
  await $("settings-button").onclick();
  openSettingsSection($(sectionId));
  $(sectionId).scrollIntoView({ block: "start" });
}
/**
 * True when a saved chat is open and nothing is running or queued in it: the
 * same rule the chat menu uses to enable Archive and Delete.
 */
function chatIdle() {
  const chat = state.id && chatById(state.id);
  return Boolean(
    chat &&
      !["queued", "running"].includes(chat.status) &&
      state.active !== state.id &&
      !state.task?.queued_message,
  );
}
const slash = { matches: [], index: 0, dismissed: null };
/**
 * Return the commands to list for a message box value: none unless the
 * whole value is "/" and one word, else every command the page can run now
 * whose name or alias starts with that word.
 */
function slashMatches(value) {
  const typed = /^\/(\S*)$/.exec(value);
  if (!typed) return [];
  const word = typed[1].toLowerCase();
  return SLASH_COMMANDS.filter(
    (command) =>
      [command.name, ...(command.aliases || [])].some((name) =>
        name.startsWith(word),
      ) && command.available(),
  );
}
/** Return the runnable command a whole message names exactly, if any. */
function slashCommand(value) {
  if (!SLASH_COMMANDS_ENABLED) return null;
  const typed = /^\/(\S+)$/.exec(value.trim());
  if (!typed) return null;
  const word = typed[1].toLowerCase();
  return (
    SLASH_COMMANDS.find(
      (command) =>
        [command.name, ...(command.aliases || [])].includes(word) &&
        command.available(),
    ) || null
  );
}
/** Build a command's icon; screen readers skip it and read the name. */
function slashIcon(glyph) {
  const icon = textNode("span", glyph, "slash-icon");
  icon.setAttribute("aria-hidden", "true");
  return icon;
}
/** Show or hide the command list for the message box's current text. */
function renderSlash() {
  if (!SLASH_COMMANDS_ENABLED) return;
  const value = $("message").value;
  if (slash.dismissed !== null && slash.dismissed !== value)
    slash.dismissed = null;
  const previous = slash.matches[slash.index]?.name;
  slash.matches = slash.dismissed === null ? slashMatches(value) : [];
  const keep = slash.matches.findIndex((command) => command.name === previous);
  slash.index = keep >= 0 ? keep : 0;
  const list = $("slash-popover");
  list.replaceChildren();
  slash.matches.forEach((command, index) => {
    const option = textNode("div", "", "slash-option");
    option.id = `slash-${command.name}`;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", String(index === slash.index));
    const typed = /^\/(\S*)$/.exec(value)[1];
    const name = textNode("span", "/", "slash-name");
    // Mark the letters the person typed; an alias match, such as /stat for
    // /usage, marks nothing.
    const marked = command.name.startsWith(typed.toLowerCase())
      ? typed.length
      : 0;
    name.append(
      textNode("span", command.name.slice(0, marked), "slash-match"),
      document.createTextNode(command.name.slice(marked)),
    );
    option.append(
      slashIcon(command.icon),
      name,
      textNode("span", command.description, "slash-description"),
    );
    // Keep the focus, and the phone keyboard, on the message box.
    option.addEventListener("pointerdown", (event) => event.preventDefault());
    option.addEventListener("click", () => runSlash(command));
    list.append(option);
  });
  const open = slash.matches.length > 0;
  // The command list and the model and reasoning picker are never open together.
  if (open) closePicker();
  list.hidden = !open;
  $("message").setAttribute("aria-expanded", String(open));
  if (open)
    $("message").setAttribute(
      "aria-activedescendant",
      `slash-${slash.matches[slash.index].name}`,
    );
  else $("message").removeAttribute("aria-activedescendant");
}
/** Clear the message box and run a command, if the page can still run it. */
function runSlash(command) {
  $("message").value = "";
  slash.dismissed = null;
  // A dialog this command opens returns focus to the message box, not to a
  // row that the chat menu used earlier.
  menu.returnId = null;
  resizeInput();
  renderSlash();
  controls();
  if (command.available()) command.run();
}
/**
 * Handle a key in the message box while the command list is open: the
 * arrows move the highlight, Enter or Tab runs the highlighted command and
 * Escape closes the list until the text changes. Returns true when it used
 * the key.
 */
function slashKeydown(event) {
  if ($("slash-popover").hidden || event.isComposing) return false;
  const count = slash.matches.length;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    slash.index =
      (slash.index + (event.key === "ArrowDown" ? 1 : -1) + count) % count;
  } else if ((event.key === "Enter" && !event.shiftKey) || event.key === "Tab") {
    event.preventDefault();
    runSlash(slash.matches[slash.index]);
    return true;
  } else if (event.key === "Escape") {
    slash.dismissed = $("message").value;
  } else return false;
  event.preventDefault();
  event.stopPropagation();
  renderSlash();
  $(`slash-${slash.matches[slash.index]?.name}`)?.scrollIntoView({
    block: "nearest",
  });
  return true;
}
$("message").addEventListener("blur", () => {
  if (!$("slash-popover").hidden) {
    slash.dismissed = $("message").value;
    renderSlash();
  }
});
// A press anywhere outside the message box and the list closes the list.
document.addEventListener("pointerdown", (event) => {
  if (
    !$("slash-popover").hidden &&
    !event.target.closest("#slash-popover, #message")
  ) {
    slash.dismissed = $("message").value;
    renderSlash();
  }
});
$("message").addEventListener("focus", () => {
  slash.dismissed = null;
  renderSlash();
});
let restore = Promise.resolve();
/**
 * Read the list and the open chat; the first pass waits for the restored
 * chat. polls.refresh repeats it: every three seconds while a chat works,
 * else every 12 seconds.
 */
async function refresh() {
  if (state.busy) return;
  try {
    if (!state.catalog) await loadChatOptions();
    await loadList();
    await restore;
    await fetchSelected();
  } catch (error) {
    showError(error);
  }
}
poll("refresh", refresh, () => (state.active ? 3000 : 12000));
sidebar(false);
controls();
restore = reopenLastChat();
polls.refresh.run();
polls.usage.run();
polls.login.run().catch(() => {});
polls.github.run().catch(() => {});
setInterval(tickElapsed, 1000);
