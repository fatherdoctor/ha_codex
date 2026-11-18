"use strict";
/** Return the page element with the given id. */
const $ = (id) => document.getElementById(id);
const labels = {
  queued: "Starting",
  in_queue: "In queue",
  running: "Working",
  waiting_for_input: "Needs your reply",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Stopped",
};
// Mirrors the worker's limits for images attached to a message.
const UPLOAD_MAX_BYTES = 10 * 1024 * 1024;
const UPLOADS_PER_MESSAGE = 6;
const UPLOAD_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const UPLOAD_MAX_EDGE = 2048;
const state = {
  id: null,
  task: null,
  chats: [],
  queue: [],
  edit: null,
  files: [],
  draftFiles: new Map(),
  next: null,
  active: null,
  busy: false,
  loading: false,
  generation: 0,
  drafts: new Map(),
  signature: "",
  approvalsShown: new Set(),
  listSignature: "",
  catalog: null,
  chatSettings: new Map(),
  settingsBusy: false,
  activity: null,
  permissionModes: new Map(),
  // Checks whose retry the owner started on this page. Their result shows open; a reload forgets them.
  retriedChecks: new Set(),
  // Earlier answers whose "Worked for" line the owner opened. The latest answer keeps this in state.activity.
  workedOpen: new Set(),
  // The checks block of the latest answer. renderActivity puts it inside that answer's "Worked for" line.
  latestChecks: null,
};
const effortLabels = {
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra High",
  max: "Max",
  ultra: "Ultra",
  minimal: "Minimal",
};
const effortDescriptions = {
  low: "Fast responses with lighter reasoning.",
  medium: "Balances speed and reasoning depth.",
  high: "More reasoning for complex problems.",
  xhigh: "Extra reasoning for demanding tasks.",
  max: "Maximum reasoning for the hardest problems.",
  ultra: "Maximum reasoning with automatic task delegation.",
};
/**
 * Return the model and reasoning level selected for the open chat. A null
 * value means the chat follows the add-on setting.
 */
function selectedSettings() {
  return (
    state.chatSettings.get(state.id) || { model: null, reasoning_effort: null }
  );
}
/** The models a chat can run on, with the add-on's own model when it is not one of the usual choices. */
function chatModels() {
  const { models = [], default_model: id, default_efforts: efforts } =
    state.catalog || {};
  return !id || models.some((model) => model.id === id)
    ? models
    : [{ id, label: id, efforts }, ...models];
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
 * else the add-on default. Falls back to medium when neither is known or the
 * chat's model does not offer that level.
 */
function effectiveEffort(settings = selectedSettings()) {
  const effort =
    settings.reasoning_effort ||
    state.catalog?.defaults.reasoning_effort ||
    "medium";
  return selectedModel(settings) && !selectedEfforts(settings).includes(effort)
    ? "medium"
    : effort;
}
/**
 * Close the model and reasoning popovers. With focus, the button of the one
 * that was open gets the focus back.
 */
function closePicker(focus = false) {
  for (const name of ["model", "effort"]) {
    if (focus && !$(name + "-popover").hidden) $(name + "-button").focus();
    $(name + "-popover").hidden = true;
    $(name + "-button").setAttribute("aria-expanded", "false");
  }
}
/**
 * Fill the reasoning popover for a level, by default the one that applies to
 * the open chat: its name and description, the model name, and the slider
 * with one dot per level.
 */
function renderEffort(effort = effectiveEffort()) {
  const efforts = selectedEfforts();
  const index = Math.max(0, efforts.indexOf(effort));
  $("effort-title").textContent = effortLabels[effort] || effort;
  $("effort-model").textContent = modelLabel();
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
}
/** The name of the model a chat runs on, whether it selected one or follows the add-on setting. */
function modelLabel(settings = selectedSettings()) {
  return selectedModel(settings)?.label || settings.model || "Model";
}
/**
 * Show the open chat's model and reasoning level on the picker buttons. The
 * picker is disabled and closed until the chat options are loaded, while the
 * chat loads or its task is queued or running, and while a message, a stop
 * request, or a selection is being sent.
 */
function renderPicker() {
  const settings = selectedSettings();
  const effort = effectiveEffort(settings);
  $("model-button").textContent = modelLabel(settings);
  $("effort-label").textContent = effortLabels[effort] || effort;
  $("effort-button").title = settings.reasoning_effort
    ? "Select reasoning level"
    : "Using add-on default reasoning";
  const disabled =
    !state.catalog ||
    state.loading ||
    state.busy ||
    state.settingsBusy ||
    Boolean(state.task && ["queued", "running"].includes(state.task.status)) ||
    // A waiting message keeps the settings it was sent with.
    Boolean(state.task?.queued_message);
  for (const id of [
    "model-button",
    "effort-button",
    "effort-slider",
    "effort-reset",
  ])
    $(id).disabled = disabled;
  if (disabled) closePicker();
  if (!$("effort-popover").hidden) renderEffort();
}
/**
 * Store a model and reasoning selection for the open chat. A saved chat sends
 * it to the worker and keeps what the worker returns; a new chat only holds it
 * in state until its first message is sent. Errors show above the composer.
 */
async function saveSelection(settings) {
  const id = state.id;
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
    showError(error);
  } finally {
    state.settingsBusy = false;
    controls();
  }
}
/**
 * Open the model or reasoning popover, or close it when it is already open.
 * The model popover gets one button per model that saves the choice; the
 * reasoning popover is filled and its slider focused.
 */
function openPicker(name) {
  const wasOpen = !$(name + "-popover").hidden;
  closePicker();
  if (wasOpen) return;
  $(name + "-popover").hidden = false;
  $(name + "-button").setAttribute("aria-expanded", "true");
  if (name === "model") {
    const options = $("model-options");
    options.replaceChildren();
    for (const model of chatModels()) {
      const button = textNode("button", "", "model-option");
      button.type = "button";
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
        closePicker(true);
        await saveSelection(settings);
        $("model-button").focus();
      };
      options.append(button);
    }
    // A chat can hold a model that is no longer in the list; then none is marked.
    (
      options.querySelector('[aria-pressed="true"]') ||
      options.firstElementChild
    )?.focus();
  } else {
    renderEffort();
    $("effort-slider").focus();
  }
}
$("model-button").onclick = () => openPicker("model");
$("effort-button").onclick = () => openPicker("effort");
$("effort-slider").addEventListener("input", () =>
  renderEffort(selectedEfforts()[Number($("effort-slider").value)]),
);
$("effort-slider").addEventListener("change", async () => {
  const effort = selectedEfforts()[Number($("effort-slider").value)];
  closePicker(true);
  await saveSelection({ ...selectedSettings(), reasoning_effort: effort });
  $("effort-button").focus();
});
$("effort-reset").onclick = async () => {
  closePicker(true);
  await saveSelection({ ...selectedSettings(), reasoning_effort: null });
  $("effort-button").focus();
};
document.addEventListener("pointerdown", (event) => {
  if (!$("chat-picker").contains(event.target)) closePicker();
});
$("chat-picker").addEventListener("focusout", (event) => {
  if (event.relatedTarget && !$("chat-picker").contains(event.relatedTarget))
    closePicker();
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
let refreshTimer;
let activityTimer = null;
let usageTimer;
let usageLoading = false;
/**
 * Show the quota left for the five-hour and weekly limits as a figure and a
 * bar, with the reset time as a tooltip. A limit the account does not report
 * reads Unknown, one with a bad value reads Unavailable, and a note says when
 * the quota is stale or missing. The line under the bars names the plan and
 * the reset times in plain text.
 */
function renderUsage(usage, planType) {
  // An account can lack either limit, and the worker may have no quota data at all.
  usage = usage || {};
  // The worker's percentages already represent quota left, not quota used.
  for (const [id, key] of [
    ["usage-five-hour", "five_hour"],
    ["usage-weekly", "weekly"],
  ]) {
    const raw = usage[`${key}_percent`];
    const percent =
      typeof raw === "number" || (typeof raw === "string" && raw.trim())
        ? Number(raw)
        : NaN;
    const valid = Number.isFinite(percent) && percent >= 0 && percent <= 100;
    // An empty field in a good read means the account has no such window.
    const unreported = usage.status === "ok" && (raw === undefined || raw === null || raw === "");
    $(id).textContent = valid ? `${percent}% left` : unreported ? "Unknown" : "Unavailable";
    // The bar is as long as the quota left, turns red below 5%, and is an outline without a value.
    const bar = $(`${id}-bar`);
    bar.style.width = valid ? `${percent}%` : "0";
    bar.parentElement.classList.toggle("low", valid && percent < 5);
    bar.parentElement.classList.toggle("unknown", !valid);
    const reset =
      dateLabel(usage[`${key}_reset_at`], true) || usage[`${key}_reset`];
    $(id).title = bar.parentElement.title =
      valid && reset ? `Resets ${reset}` : "";
  }
  $("usage-note").textContent =
    usage.status === "deferred"
      ? "Last known quota; refreshes after the task finishes."
      : usage.status === "ok"
        ? ""
        : "Quota is currently unavailable.";
  $("usage-note").hidden = !$("usage-note").textContent;
  $("usage-plan").textContent = planLine(usage, planType);
}
/** A reset time short enough for the side bar: the weekday and the time. */
function shortResetLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
}
/**
 * The plan line under the quota bars, for example "Pro plan · Weekly resets
 * Mon 9:00 AM · 5h resets 2:30 PM". The plan is information only; a reset time
 * shows only for a window the read reported.
 */
function planLine(usage, planType) {
  const plan = typeof planType === "string" && planType.trim()
    ? `${planType.trim()[0].toUpperCase()}${planType.trim().slice(1)} plan`
    : "Plan unknown";
  const parts = [plan];
  for (const [key, name] of [["weekly", "Weekly"], ["five_hour", "5h"]]) {
    const reset = shortResetLabel(usage[`${key}_reset_at`]) || usage[`${key}_reset`];
    if (usage[`${key}_percent`] !== undefined && usage[`${key}_percent`] !== "" && reset)
      parts.push(`${name} resets ${reset}`);
  }
  return parts.join(" · ");
}
// The Home Assistant row at the foot of the side bar: the state of the Codex integration.
const HOME_ASSISTANT_STATES = {
  ok: ["ready", "Ready"],
  starting: ["starting", "Starting"],
  not_installed: ["needs-owner", "Integration not installed"],
  outdated: ["needs-owner", "Integration out of date"],
  not_connected: ["needs-owner", "Integration not connected"],
};
/** Show the integration state in the side bar row; a failed status read shows Unavailable. */
function renderHomeAssistantStatus(integration) {
  const [dot, text] = integration === undefined
    ? ["error", "Unavailable"]
    : HOME_ASSISTANT_STATES[integration?.state] || ["error", "Unavailable"];
  $("ha-status-row").dataset.state = dot;
  $("ha-status-summary").textContent = text;
}
// Where the line at the top of the chat sends someone to install the integration.
const INTEGRATION_INSTALL_URL =
  "https://github.com/fatherdoctor/ha_codex#installation";
/**
 * Show a line at the top of the chat while the Codex integration is not
 * installed, is older than the worker needs, or is not in touch with the
 * worker. Any other state hides the line.
 */
function renderIntegration(integration) {
  const notice = $("integration-notice");
  const state = integration?.state;
  const minimum = integration?.minimum_version;
  let reason = "";
  if (state === "not_installed") reason = "is not installed.";
  else if (state === "outdated")
    reason = `is out of date. Update it${
      minimum ? ` to ${minimum} or newer` : ""
    }, then restart Home Assistant.`;
  else if (state === "not_connected")
    reason =
      "is installed but not connected. Restart Home Assistant, then add Codex under Settings > Devices & services.";
  // The status is read every minute. An unchanged line is left alone, so that
  // a screen reader does not announce it again.
  const signature = reason ? `${state} ${minimum || ""}` : "";
  if (notice.dataset.shown !== signature) {
    notice.dataset.shown = signature;
    notice.replaceChildren();
    if (reason) {
      notice.append(
        `Some features are not available because the Codex integration ${reason}`,
      );
      if (state === "not_installed") {
        const link = textNode("a", "How to install it");
        link.href = INTEGRATION_INSTALL_URL;
        link.target = "_blank";
        link.rel = "noreferrer noopener";
        notice.append(" ", link);
      }
    }
  }
  notice.hidden = !reason;
}
/**
 * Fetch the worker status, show its quota and the state of the Codex
 * integration, and reload the chat options, then schedule the next check in a
 * minute. Nothing is fetched while the page is hidden, and a failed check
 * shows the quota as unavailable and leaves the integration line as it is.
 */
async function loadUsage() {
  if (usageLoading) return;
  usageLoading = true;
  clearTimeout(usageTimer);
  try {
    if (!document.hidden) {
      const data = await api("status");
      renderUsage(data.codex_usage, data.account_access?.plan_type);
      renderIntegration(data.integration);
      renderHomeAssistantStatus(data.integration || null);
      // The quota check also tells the worker which model Codex picks when the add-on names none.
      // Not awaited: the quota shown must not depend on it.
      if (state.catalog) loadChatOptions(true);
    }
  } catch (_) {
    renderUsage();
    renderHomeAssistantStatus(undefined);
  } finally {
    usageLoading = false;
    usageTimer = setTimeout(loadUsage, 60000);
  }
}
/*
 * Home Assistant lets this page reach the worker through an ingress session,
 * a cookie that the Home Assistant tab renews. When the tab sleeps, the
 * session can lapse and every request gets HTTP 401 before it reaches the
 * worker. The page then asks Home Assistant, through the parent window, for a
 * new session and sends the request once more. If that fails too, a banner
 * asks the owner to reload; the HTTP code is never shown.
 */
const SESSION_EXPIRED = "Your Home Assistant session expired. Reload the page to continue.";
let ingressRenewal = null;
/** The app's slug from the Home Assistant address of this panel, or "" when the address does not show it. */
function ingressSlug(parentPath) {
  const match = /^\/(?:app|hassio\/ingress)\/([a-z0-9_]+)\/?$/.exec(parentPath || "");
  return match ? match[1] : "";
}
/**
 * Ask Home Assistant for a new ingress session and store it as the frontend
 * does. Newer Home Assistant has a command for this; older releases pass the
 * request to the Supervisor. True when a new session cookie was set.
 */
async function renewIngressSession() {
  let hass = null;
  let parentPath = "";
  try {
    if (window.parent === window) return false;
    hass = window.parent.document.querySelector("home-assistant")?.hass;
    parentPath = window.parent.location.pathname;
  } catch (_) {
    return false; // Not inside the Home Assistant frontend.
  }
  if (typeof hass?.callWS !== "function") return false;
  const slug = ingressSlug(parentPath);
  const requests = [];
  if (slug) requests.push({ type: "supervisor/ingress/session", slug });
  requests.push({ type: "supervisor/api", endpoint: "/ingress/session", method: "post" });
  for (const request of requests) {
    try {
      const reply = await hass.callWS(request);
      const session = reply?.session ?? reply?.data?.session;
      if (typeof session !== "string" || !session) continue;
      const secure = location.protocol === "https:" ? ";Secure" : "";
      document.cookie = `ingress_session=${session};path=/api/hassio_ingress/;SameSite=Strict${secure}`;
      return true;
    } catch (_) {
      // Try the older request next.
    }
  }
  return false;
}
/** Show or hide the banner that asks the owner to reload the page. */
function showSessionExpired(expired) {
  $("session-banner").hidden = !expired;
}
// Reload the Home Assistant tab, which starts a new session; this page alone when it is not inside one.
$("session-reload").onclick = () => {
  try {
    if (window.parent !== window) return window.parent.location.reload();
  } catch (_) {
    // A parent on another origin: reload this page only.
  }
  location.reload();
};
let sessionGeneration = 0;
/**
 * fetch() for the worker: on HTTP 401 renew the ingress session and send the
 * request once more. Requests that fail together share one renewal, and a
 * request sent before a renewal finished is sent again without a new one.
 */
async function pageFetch(url, options) {
  const generation = sessionGeneration;
  const response = await fetch(url, options);
  if (response.status !== 401) {
    showSessionExpired(false); // The session works again, for example after a sign-in in another tab.
    return response;
  }
  let renewed = generation !== sessionGeneration;
  if (!renewed) {
    ingressRenewal ||= renewIngressSession()
      .then((ok) => {
        if (ok) sessionGeneration += 1;
        return ok;
      })
      .finally(() => {
        ingressRenewal = null;
      });
    renewed = await ingressRenewal;
  }
  const retry = renewed ? await fetch(url, options) : response;
  showSessionExpired(retry.status === 401);
  if (retry.status === 401) throw workerError(SESSION_EXPIRED, 401);
  return retry;
}
/** Send a JSON request to the worker API and return the parsed response. */
async function api(path, body, method) {
  const response = await pageFetch(path.replace(/^\//, ""), {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json().catch(() => null);
  if (!data)
    throw workerError(
      `The worker returned HTTP ${response.status}. Try refreshing.`,
      response.status,
    );
  if (!response.ok || !data.ok)
    throw workerError(
      data.error || `The worker returned HTTP ${response.status}.`,
      response.status,
    );
  return data;
}

document.getElementById("export-interaction-logs").addEventListener("click", async () => {
  const button = document.getElementById("export-interaction-logs");
  const status = document.getElementById("export-logs-status");
  button.disabled = true;
  try {
    const response = await pageFetch("diagnostics/export", { cache: "no-store" });
    if (!response.ok) throw new Error("The export could not be downloaded. Try again.");
    const data = await response.json();
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "codex-interactions.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status.textContent = "Downloaded. Open the file to review it before attaching it to a bug report. Nothing was sent.";
  } catch (_error) {
    status.textContent = "The export could not be downloaded. Try again.";
  } finally {
    button.disabled = false;
  }
});

/** Build an error that keeps the HTTP status so callers can tell 404 apart. */
function workerError(message, status) {
  const error = new Error(message);
  error.status = status;
  return error;
}
/** Create an element with the given text and optional class name. */
function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text || "";
  if (className) node.className = className;
  return node;
}
/** Show or clear the error line above the composer. */
function showError(error) {
  $("error").textContent = error ? String(error.message || error) : "";
  $("error").hidden = !error;
}
/**
 * Format a timestamp in the browser's locale as a day and month, or as a full
 * date and time when full is set. Returns "" for a value that is not a date.
 */
function dateLabel(value, full = false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return full
    ? date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}
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
/** Fit the height of the message box to its text, up to 180 pixels. */
function resizeInput() {
  $("message").style.height = "auto";
  $("message").style.height = Math.min(180, $("message").scrollHeight) + "px";
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
  $("compose-hint").textContent = state.id
    ? task?.status === "in_queue"
      ? "This chat starts when its message leaves the queue."
      : "Continue this conversation with its saved context."
    : "A new chat starts a separate conversation.";
  let notice = "";
  if (waiting)
    notice =
      "This chat's message is waiting in the queue and has not been sent yet. You can edit or remove it above.";
  else if (task?.permission_required)
    notice = "Review the permission request above to continue, or stop the task.";
  else if (working && task?.browser_retry)
    notice = "Retrying the dashboard inspection. You can continue this chat when it finishes.";
  else if (working)
    notice =
      "Codex is working. Your response will appear here when it finishes.";
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
/** Build the small pin icon shown beside pinned chat titles. */
/** One 24 px Material Design icon, the set Home Assistant uses, filled with the text color. */
function mdiIcon(path, className) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("row-icon", className);
  const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");
  shape.setAttribute("d", path);
  svg.append(shape);
  return svg;
}
/** mdiPin: a pinned chat. */
function pinIcon() {
  return mdiIcon("M16,12V4H17V2H7V4H8V12L6,14V16H11.2V22H12.8V16H18V14L16,12Z", "pin-icon");
}
/** mdiClockOutline: a message that waits in the queue. */
function queueIcon() {
  return mdiIcon(
    "M12,20A8,8 0 0,0 20,12A8,8 0 0,0 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20M12,2A10,10 0 0,1 22,12A10,10 0 0,1 12,22C6.47,22 2,17.5 2,12A10,10 0 0,1 12,2M12.5,7V12.25L17,14.92L16.25,16.15L11,13V7H12.5Z",
    "queue-icon",
  );
}
/** mdiMessageOutline: an ordinary chat. */
function chatIcon() {
  return mdiIcon("M20,2H4A2,2 0 0,0 2,4V22L6,18H20A2,2 0 0,0 22,16V4A2,2 0 0,0 20,2M20,16H5.17L4,17.17V4H20V16Z", "chat-icon");
}
/** A small spinner for a chat that is working now. */
function spinnerIcon() {
  const spinner = textNode("span", "", "row-icon row-spinner");
  spinner.setAttribute("aria-hidden", "true");
  return spinner;
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
  main.onclick = () => selectChat(chat.task_id);
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
  if (hadSelected && !merged.has(state.id)) await selectChat(null);
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
function selectedPermissionMode() {
  return state.permissionModes.get(state.id) || state.task?.permission_mode || 'default';
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
      const file = textNode('a', `${attachment.name} · ${sizeLabel(attachment.size)} ↓`, 'document-attachment');
      file.href = attachmentUrl(taskId, attachment, true);
      file.download = attachment.name || 'document';
      list.append(file);
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
    img.onerror = () =>
      figure.replaceChildren(
        textNode(
          "p",
          "This image is no longer available.",
          "attachment-missing",
        ),
      );
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
/**
 * Say which changed files have a copy of their previous version and which have
 * none. Copies are deleted after the retention period set in the app options.
 */
function renderBackups(turn) {
  const entries = Array.isArray(turn.backups) ? turn.backups : [];
  const saved = entries.filter((entry) => entry.status === "saved");
  const unsaved = entries.filter((entry) =>
    ["missing", "unverified"].includes(entry.status),
  );
  /** Word a number of files: "1 file" or "3 files". */
  const count = (list) =>
    list.length === 1 ? "1 file" : `${list.length} files`;
  const nodes = [];
  if (saved.length) {
    const node = textNode("div", "", "check check-valid backups-saved");
    const days = state.catalog?.backup_retention_days;
    node.append(
      textNode("span", "✓", "check-mark"),
      textNode(
        "span",
        turn.backups_removed
          ? `Previous version saved for ${count(saved)}; the copies have since been removed`
          : `Previous version saved for ${count(saved)}${days ? `, kept for ${days === 1 ? "1 day" : `${days} days`}` : ""}`,
      ),
    );
    if (!turn.backups_removed)
      node.append(
        textNode(
          "pre",
          saved.map((entry) => `${entry.path} → ${entry.copy}`).join("\n"),
          "check-detail",
        ),
      );
    nodes.push(node);
  }
  if (unsaved.length) {
    const node = textNode("div", "", "check backups-missing");
    node.append(
      textNode("span", "!", "check-mark"),
      textNode("span", `No previous version saved for ${count(unsaved)}`),
      textNode(
        "pre",
        unsaved.map((entry) => entry.path).join("\n"),
        "check-detail",
      ),
    );
    nodes.push(node);
  }
  return nodes;
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
 * with its result, details, and findings, then the screenshots that have not
 * expired. It stays closed under the answer, except while a batch of blocked
 * browser requests waits for the owner, a retry runs, or a retry the owner
 * started on this page has its result. The closed line counts
 * the bad results, so a failed check shows without opening the block. Returns
 * null when the turn has no checks.
 */
function renderVerification(turn, taskId) {
  const checks = turn.verification || [];
  if (!checks.length) return null;
  const section = textNode("details", "", "verification disclosure");
  const bad = checks.filter(check => BAD_CHECK_STATUSES.has(check.status)).length;
  const title = checks.length === 1 ? "Check and readback" : `Checks and readback (${checks.length})`;
  const summary = textNode("summary", bad ? `${title} · ${bad} failed` : title);
  if (bad) summary.classList.add("has-failed");
  section.append(summary);
  if (state.task?.browser_retry?.turn_id === turn.turn_id) {
    section.open = true;
    const progress = textNode("p", "Retrying dashboard inspection…", "browser-permission-progress");
    progress.setAttribute("role", "status");
    section.append(progress);
  }
  for (const check of checks) {
    const row = textNode("details", "", `verification-result disclosure is-${check.status}`);
    // A retry result opens its own row. The block opens too when the owner started that retry on this page,
    // so the result shows where they clicked; a saved chat opened later starts closed.
    if (check.retry_of && check === checks[checks.length - 1]) {
      row.open = true;
      if (state.retriedChecks.has(check.retry_of)) section.open = true;
    }
    const label = {
      passed: "Passed", failed: "Failed", observed: "Readback", captured: "Screenshots captured",
      issues: "Needs review", disabled: "Disabled", unavailable: "Unverified",
    }[check.status] || "Unverified";
    row.append(textNode("summary", `${label} · ${check.entity_id || check.path || check.target || check.operation}`));
    if (check.message) row.append(textNode("pre", check.message));
    if (check.state !== undefined) row.append(textNode("p", `State: ${check.state}${check.expected_state !== null ? ` · Expected: ${check.expected_state}` : ""}`));
    if (check.attributes && Object.keys(check.attributes).length) row.append(textNode("pre", JSON.stringify(check.attributes, null, 2)));
    if (Array.isArray(check.findings) && check.findings.length) {
      const groups = {
        resource: "Resources that could not load", home_assistant: "Home Assistant errors",
        dashboard: "Dashboard errors", policy: "Blocked actions and requests",
        diagnostic: "Blocked diagnostic logging and notifications",
      };
      for (const [kind, title] of Object.entries(groups)) {
        const findings = check.findings.filter(finding => finding.kind === kind);
        if (!findings.length) continue;
        row.append(textNode("h4", title));
        for (const finding of findings) {
          row.append(textNode("p", finding.message));
          const views = Array.isArray(finding.viewports) ? finding.viewports.join(", ") : "";
          row.append(textNode("small", `${finding.count > 1 ? `${finding.count} occurrences` : "1 occurrence"}${views ? ` · ${views}` : ""}`));
        }
      }
      if (check.findings_omitted) row.append(textNode("p", "Additional findings were omitted because the evidence limit was reached."));
    } else {
      for (const error of [...(Array.isArray(check.errors) ? check.errors : []), ...(Array.isArray(check.blocked) ? check.blocked : [])]) row.append(textNode("p", error));
    }
    if (check.checked_at) row.append(textNode("small", dateLabel(check.checked_at, true)));
    if (check.permission_decision) {
      const names = (check.permission_decision.approved || []).map(item => item.label).join(", ");
      row.append(textNode("p", `${check.permission_decision.remember ? "Remembered for this dashboard" : "Allowed for this retry"}: ${names}`));
      const kept = (check.permission_decision.carried || []).map(item => item.label).join(", ");
      if (kept) row.append(textNode("p", `Still allowed from earlier in this exchange: ${kept}`));
    }
    if (check.permissions_used?.length)
      row.append(textNode("p", `${check.permissions_used.length} user-approved browser permission(s) used.`));
    if (check.permission_review_unavailable) row.append(textNode("p", check.permission_review_unavailable));
    if (check.permission_store_error) row.append(textNode("p", check.permission_store_error));
    if (check.permission_candidates_omitted)
      row.append(textNode("p", "Some blocked requests can't be offered for approval: they were incomplete, looked like credentials, or exceeded the review limit."));
    const review = renderBrowserPermissions(check, turn, taskId);
    if (review) row.append(review);
    if (review?.classList.contains("browser-permission-review")) section.open = true;
    section.append(row);
  }
  const images = imageAttachments(turn.verification_attachments).filter(image => image.expires_at * 1000 > Date.now());
  if (images.length) section.append(renderAttachments(taskId, images, "Dashboard verification screenshot"));
  section.append(textNode("p", "Screenshots are evidence for visual review. State and configuration checks do not prove automation behavior.", "verification-note"));
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
/**
 * Build the "Worked for" line of an earlier answer. Only the latest exchange
 * has its steps on the page, so here the line opens the checks block alone.
 * With no checks it is plain text, and with no times either it is left out.
 * The line opens by itself when the checks block wants attention (a browser
 * permission review or a retry result).
 */
function renderWorked(turn, checks) {
  const label = workedLabel(turn);
  if (!checks) return label ? textNode("p", label, "worked-line") : null;
  const block = textNode("div", "", "activity worked");
  const toggle = textNode("button", "", "activity-toggle");
  toggle.type = "button";
  toggle.append(textNode("span", "", "activity-chevron"), textNode("span", label || "Checks and readback"));
  const list = textNode("div", "", "activity-steps");
  list.append(checks);
  const show = open => {
    if (open) state.workedOpen.add(turn.turn_id);
    else state.workedOpen.delete(turn.turn_id);
    toggle.setAttribute("aria-expanded", String(open));
    list.hidden = !open;
  };
  show(checks.open || state.workedOpen.has(turn.turn_id));
  toggle.onclick = () => show(list.hidden);
  block.append(toggle, list);
  block.openChecks = () => {
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
/** Let the owner review a batch of complete requests without interrupting capture. */
function renderBrowserPermissions(check, turn, taskId) {
  const candidates = check.permission_candidates || [];
  if (!candidates.length || !check.check_id) return null;
  if ((turn.verification || []).some(item => item.retry_of === check.check_id))
    return textNode("p", "This batch was reviewed in a later inspection.", "muted");
  if (check.permission_review) {
    const retrying = state.task?.browser_retry?.source_check_id === check.check_id;
    return textNode("p", retrying ? "Retrying with the approved requests…"
      : "This batch was already approved. If no retry result is available, start a new inspection.", "muted");
  }
  if (turn.turn_id !== state.task?.current_turn_id) return null;
  if (state.task?.cancellation_requested)
    return textNode("p", "This exchange was stopped, so its requests can't be approved. Start a new inspection to review them.", "muted");
  const panel = textNode("div", "", "browser-permission-review");
  panel.append(textNode("h4", "Review blocked browser requests"));
  panel.append(textNode("p", "Select requests you trust. Unreviewed handlers may have side effects. Known device controls, writes, sign-in changes, URL signing and camera signaling stay blocked."));
  const fields = document.createElement("fieldset");
  fields.append(textNode("legend", `Additional access for ${check.path}`));
  const boxes = [];
  const selectLabel = textNode("label", "", "browser-permission-choice");
  const selectAll = document.createElement("input");
  selectAll.type = "checkbox";
  selectLabel.append(selectAll, textNode("span", "Select all requests"));
  fields.append(selectLabel);
  for (const candidate of candidates) {
    const item = textNode("div", "", "browser-permission-request");
    const label = textNode("label", "", "browser-permission-choice");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = candidate.id;
    boxes.push(box);
    label.append(box, textNode("span", candidate.label));
    const details = textNode("details", "", "browser-permission-details disclosure");
    details.append(textNode("summary", "Request details"), textNode("pre", candidate.description));
    item.append(label, details);
    fields.append(item);
  }
  const rememberLabel = textNode("label", "", "browser-permission-choice browser-permission-remember");
  const remember = document.createElement("input");
  remember.type = "checkbox";
  rememberLabel.append(remember, textNode("span", "Remember for this dashboard"));
  fields.append(rememberLabel);
  const actions = textNode("div", "", "browser-permission-actions");
  const allow = textNode("button", "Allow selected and retry");
  allow.type = "button";
  const keep = textNode("button", "Keep blocked", "subtle");
  keep.type = "button";
  keep.onclick = () => {
    const result = panel.closest(".verification-result");
    result.open = false;
    result.querySelector("summary").focus();
  };
  actions.append(allow, keep);
  const feedback = textNode("p", "", "browser-permission-feedback");
  feedback.setAttribute("role", "status");
  // The worker accepts a batch only while no task or inspection runs in any chat.
  const busy = Boolean(state.task?.browser_retry) || Boolean(state.active) ||
    ["queued", "running", "in_queue"].includes(state.task?.status);
  fields.disabled = busy;
  const update = () => {
    const selected = boxes.filter(box => box.checked).length;
    selectAll.checked = selected === boxes.length;
    selectAll.indeterminate = selected > 0 && selected < boxes.length;
    allow.disabled = busy || !selected;
  };
  selectAll.onchange = () => { for (const box of boxes) box.checked = selectAll.checked; update(); };
  for (const box of boxes) box.onchange = update;
  update();
  allow.onclick = async () => {
    const generation = state.generation;
    fields.disabled = allow.disabled = keep.disabled = true;
    feedback.textContent = "Starting a fresh inspection…";
    state.retriedChecks.add(check.check_id);
    try {
      await api(`tasks/${encodeURIComponent(taskId)}/browser-permissions`, {
        turn_id: turn.turn_id, check_id: check.check_id,
        candidate_ids: boxes.filter(box => box.checked).map(box => box.value), remember: remember.checked,
      });
      if (state.id === taskId && state.generation === generation) {
        await fetchSelected(true);
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(refresh, 1000);
      }
    } catch (error) {
      if (!panel.isConnected) return;
      feedback.textContent = String(error.message || error);
      fields.disabled = busy;
      keep.disabled = false;
      update();
    }
  };
  panel.append(fields, actions, feedback);
  if (busy) feedback.textContent = "Wait for running tasks and inspections to finish before approving this batch.";
  return panel;
}

let browserPermissionLoad = 0;
/** List saved exceptions in Settings, with an explicit revoke action for each. */
async function loadBrowserPermissions() {
  const load = ++browserPermissionLoad;
  const area = $("browser-permissions");
  area.textContent = "Loading saved permissions…";
  try {
    const { permissions } = await api("browser-permissions");
    if (load !== browserPermissionLoad) return;
    area.replaceChildren();
    if (!permissions.length) area.append(textNode("p", "No browser permissions remembered.", "muted"));
    setSectionStatus("browser-permissions-section", permissions.length ? `${permissions.length} remembered` : "None remembered");
    for (const permission of permissions) {
      const row = textNode("div", "", "saved-browser-permission");
      const info = textNode("div", "", "saved-browser-permission-info");
      info.append(textNode("strong", permission.label), textNode("p", permission.path));
      const details = textNode("details", "", "browser-permission-details disclosure");
      details.append(textNode("summary", "Request details"), textNode("pre", permission.description));
      info.append(details);
      const revoke = textNode("button", "Revoke", "subtle");
      revoke.type = "button";
      revoke.setAttribute("aria-label", `Revoke ${permission.label} for ${permission.path}`);
      revoke.onclick = async () => {
        revoke.disabled = true;
        try {
          await api(`browser-permissions/${encodeURIComponent(permission.id)}`, undefined, "DELETE");
          await loadBrowserPermissions();
          const next = area.querySelector("button");
          if (next) next.focus();
          else { area.tabIndex = -1; area.focus(); }
          $("settings-result").textContent = "Browser permission revoked.";
        } catch (error) { revoke.disabled = false; settingsError(error); }
      };
      row.append(info, revoke);
      area.append(row);
    }
  } catch (error) {
    if (load !== browserPermissionLoad) return;
    area.replaceChildren(textNode("p", String(error.message || error)));
    setSectionStatus("browser-permissions-section", "Could not be read", true);
    // An unreadable permissions file can only be replaced, never edited here.
    if (error.status === 503) {
      const clear = textNode("button", "Remove all saved permissions", "subtle");
      clear.type = "button";
      clear.onclick = async () => {
        clear.disabled = true;
        try {
          await api("browser-permissions", undefined, "DELETE");
          await loadBrowserPermissions();
          area.tabIndex = -1;
          area.focus();
          $("settings-result").textContent = "Saved browser permissions removed.";
        } catch (failure) { clear.disabled = false; settingsError(failure); }
      };
      area.append(clear);
    }
  }
}
/**
 * Whether the question needs its own line: from 0.3.3 the answer text already holds it (ask_owner),
 * while an older answer kept the question apart from its summary.
 */
function showsQuestionApart(turn) {
  return Boolean(turn.question) && !(turn.summary || "").includes(turn.question);
}
/** Try the browser clipboard, then the selection-based copy used on local HTTP. */
async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) {
    // Ingress, browser permissions, or a WebView can deny the modern API.
  }
  const focus = document.activeElement;
  const selection = window.getSelection();
  const ranges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) =>
        selection.getRangeAt(index).cloneRange(),
      )
    : [];
  const buffer = textNode("textarea", "", "copy-buffer");
  buffer.value = text;
  buffer.readOnly = true;
  buffer.tabIndex = -1;
  document.body.append(buffer);
  try {
    buffer.focus({ preventScroll: true });
    buffer.select();
    return document.execCommand("copy");
  } catch (_) {
    return false;
  } finally {
    buffer.remove();
    if (focus?.isConnected) focus.focus({ preventScroll: true });
    if (selection) {
      selection.removeAllRanges();
      for (const range of ranges) selection.addRange(range);
    }
  }
}
/** Draw the open chat: every exchange with its answer, checks, saved copies, and steps. */
function renderTask(force = false) {
  const task = state.task;
  if (!task) return;
  const queued = task.queued_message || null;
  if (state.edit && state.edit.id !== queued?.queue_id) state.edit = null;
  $("chat-title").textContent = task.title || "Untitled chat";
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
    task.browser_retry,
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
    if (source === "schedule" || source === "lead")
      exchange.append(textNode("p", source === "schedule" ? "Scheduled run" : "Started by lead", "muted task-source"));
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
      const evidence = renderVerification(turn, task.task_id);
      let openChecks = null;
      if (latest) {
        state.latestChecks = evidence;
        exchange.append(activityBlock());
        openChecks = () => {
          if (!state.activity || !evidence) return;
          state.activity.expanded = true;
          evidence.open = true;
          renderActivity();
          evidence.scrollIntoView({block: "nearest"});
        };
      } else {
        const worked = renderWorked(turn, evidence);
        if (worked) exchange.append(worked);
        openChecks = () => worked?.openChecks?.();
      }
      const answer = textNode("div", "", "answer");
      const heading = textNode("div", "", "answer-heading");
      heading.append(textNode("span", "⌘", "mark"), textNode("span", "Codex"));
      answer.append(heading);
      if (turn.summary) answer.append(renderMarkdown(turn.summary, "message"));
      if (turn.details && turn.details !== turn.summary)
        answer.append(renderMarkdown(turn.details, "message details"));
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
      answer.append(...renderBackups(turn));
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
      if (latest) exchange.append(activityBlock());
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
  if (queued) area.append(renderQueued(task.task_id, queued));
  renderActivity();
  area.scrollTop = force || nearBottom || newApproval ? area.scrollHeight : oldScroll;
  controls();
  ensureActivity();
}
/** Return the first line of what a permission request asked for, for its closed line. */
function approvalHeadline(approval) {
  const plain = approval.confirmation || {};
  const first = [
    approval.command, approval.action, plain.what, approval.network,
    approval.changes?.[0]?.path, approval.scope,
  ].find(value => typeof value === "string" && value.trim());
  return first ? first.trim().split("\n")[0] : "";
}
/**
 * Show a native permission request as text, with its precise action and
 * scope. While it waits for the owner it is a full card with the two answers.
 * Once answered (or expired) it shrinks to one closed line, such as
 * "Allowed once: ls /config", that opens to the same details.
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
function renderQueued(taskId, entry) {
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
        const area = $("messages");
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
    exchange.append(input);
    button("Cancel", "subtle", cancelEdit);
    const save = button("Save", "", saveEdit);
    save.id = "queued-save";
    save.disabled = Boolean(edit.saving) || !edit.text.trim();
    // Size and focus the box once it is in the page.
    requestAnimationFrame(() => {
      if (!input.isConnected) return;
      resize();
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    });
  } else {
    exchange.append(renderMarkdown(entry.message, "message user queued"));
    const note = textNode("span", queueNote(entry), "queued-note");
    note.id = "queued-note";
    actions.append(note);
    button("Edit", "subtle", () => startEdit(entry)).id = "queued-edit-button";
    button("Remove", "subtle danger", () => removeQueued(entry)).id =
      "queued-remove";
  }
  exchange.append(actions);
  return exchange;
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
  clearTimeout(activityTimer);
  activityTimer = null;
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
/** Load steps once for an idle chat, and keep loading them while Codex works. */
function ensureActivity() {
  const activity = state.activity;
  if (!activity || activity.id !== state.id || activityTimer || activity.loading)
    return;
  // A chat that is still waiting in the queue has no steps to load yet.
  if (state.task?.status === "in_queue") return;
  if (!activity.loaded || activity.running || taskActive()) pollActivity();
}
/** Fetch the steps added since the last poll and merge them by position. */
async function pollActivity() {
  clearTimeout(activityTimer);
  activityTimer = null;
  const activity = state.activity;
  const id = state.id;
  if (!activity || !id || activity.id !== id || activity.loading) return;
  if (document.hidden) {
    activityTimer = setTimeout(pollActivity, 1000);
    return;
  }
  activity.loading = true;
  const generation = state.generation;
  let restart = false;
  try {
    const data = await api(
      `tasks/${encodeURIComponent(id)}/activity?after=${activity.seq}`,
    );
    if (generation !== state.generation || state.activity !== activity) return;
    if (activity.turnId !== null && data.turn_id !== activity.turnId) {
      // A new exchange started, so its steps count from the beginning again.
      activity.steps.clear();
      activity.open.clear();
      activity.expanded = null;
      activity.seq = 0;
      activity.turnId = data.turn_id;
      activity.startedAt = null;
      restart = true;
      return;
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
  } catch (error) {
    // The regular refresh reports worker problems; the steps just pause.
  } finally {
    activity.loading = false;
    if (state.activity === activity) {
      if (restart) pollActivity();
      else if (activity.running || taskActive())
        activityTimer = setTimeout(pollActivity, 1000);
    }
  }
}
/**
 * Draw the latest exchange's step list. While Codex works, the heading counts
 * the steps and shows the running time, and the list stays open. When the
 * exchange is over, the heading becomes the closed "Worked for" line above the
 * answer, and the list holds the steps and then the answer's checks.
 */
function renderActivity() {
  const block = $("activity");
  const activity = state.activity;
  if (!block || !activity || activity.id !== state.id) return;
  const steps = [...activity.steps.values()].sort((a, b) => a.index - b.index);
  const running = activity.running || taskActive();
  const checks = state.latestChecks;
  const worked = running ? "" : workedLabel(state.task?.turns?.at(-1));
  block.hidden = !steps.length && !checks && !worked;
  block.classList.toggle("running", running);
  block.classList.toggle("worked", !running);
  // The step list takes the place of the waiting line once Codex reports steps.
  const pending = $("pending");
  if (pending) pending.hidden = !block.hidden;
  if (block.hidden) {
    block.replaceChildren();
    return;
  }
  if (!steps.length && !checks) {
    // Nothing to open: the total time alone.
    block.replaceChildren(textNode("p", worked, "worked-line"));
    return;
  }
  // A checks block that asks for the owner (a browser permission review, a retry result) opens the line.
  const expanded = activity.expanded ?? (running || Boolean(checks?.open));
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
      : worked || (steps.length ? `Activity (${count})` : "Checks and readback"),
  );
  if (running) label.append(textNode("span", "", "elapsed"));
  toggle.append(textNode("span", "", "activity-chevron"), label);
  toggle.onclick = () => {
    activity.expanded = !expanded;
    renderActivity();
  };
  const list = textNode("div", "", "activity-steps");
  list.id = "activity-steps";
  list.hidden = !expanded;
  for (const step of steps) list.append(renderStep(step, activity));
  if (checks) list.append(checks);
  block.replaceChildren(toggle, list);
  tickElapsed();
  if (nearBottom) area.scrollTop = area.scrollHeight;
}
/**
 * Build one row of the step list: icon, text, and a line with a failed
 * command's exit code, the duration, and the running state. A step with
 * output gets a button that shows or hides it.
 */
function renderStep(step, activity) {
  const row = textNode("div", "", `step step-${step.kind} is-${step.status}`);
  row.append(textNode("span", stepIcons[step.kind] || "•", "step-icon"));
  const body = textNode("div", "", "step-body");
  body.append(textNode("div", step.text, "step-text"));
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
    const open = activity.open.has(step.index);
    const button = textNode(
      "button",
      open
        ? "Hide output"
        : step.output_truncated
          ? "Show output (first part)"
          : "Show output",
      "step-output-toggle",
    );
    button.type = "button";
    button.onclick = () => {
      if (open) activity.open.delete(step.index);
      else activity.open.add(step.index);
      renderActivity();
    };
    footer.append(button);
    if (open) body.append(textNode("pre", step.output, "step-output"));
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
      data.task.chat_settings || { model: null, reasoning_effort: null },
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
async function selectChat(id, restoring = false) {
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
  sidebar(false);
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
  for (const action of ["pin", "rename", "delete", "copy-id", "archive"])
    panel.querySelector(`[data-action="${action}"]`).disabled = !id ||
      (["delete", "archive"].includes(action) && (working || Boolean(state.task?.queued_message)));
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
  else document.dispatchEvent(new CustomEvent("thread-action", {detail: {action, id}}));
});
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
  $("rename-save").disabled = true;
  try {
    const data = await api(`tasks/${encodeURIComponent(id)}/title`, { title });
    const chat = chatById(id);
    if (chat) chat.title = data.title;
    if (state.task?.task_id === id) state.task.title = data.title;
    if (state.id === id) $("chat-title").textContent = data.title;
    renderList();
    $("rename-dialog").close();
  } catch (error) {
    dialogError("rename", error);
  } finally {
    $("rename-save").disabled = false;
  }
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
for (const name of ["rename", "delete"])
  $(name + "-dialog").addEventListener("close", () => {
    // Return focus to the row's menu button, or to New chat when the row is gone.
    const trigger = menu.returnId
      ? $("chat-list").querySelector(
          `[data-menu-for="${CSS.escape(menu.returnId)}"]`,
        )
      : null;
    menu.returnId = null;
    (trigger || $("new-chat")).focus();
  });
$("delete-confirm").onclick = async () => {
  const id = $("delete-dialog").dataset.chatId;
  $("delete-confirm").disabled = true;
  try {
    await api(`tasks/${encodeURIComponent(id)}`, undefined, "DELETE");
    state.chats = state.chats.filter((chat) => chat.task_id !== id);
    state.chatSettings.delete(id);
    $("delete-dialog").close();
    if (state.id === id) await selectChat(null);
    state.drafts.delete(id);
    state.draftFiles.delete(id);
    renderList();
    await loadList();
  } catch (error) {
    dialogError("delete", error);
  } finally {
    $("delete-confirm").disabled = false;
  }
};
$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
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
$("message").addEventListener("input", () => {
  resizeInput();
  controls();
});
$("message").addEventListener("keydown", (event) => {
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
  loadUsage();
  try {
    await loadList();
    await fetchSelected();
    showError(null);
  } catch (error) {
    showError(error);
  }
};
$("load-more").onclick = async () => {
  $("load-more").disabled = true;
  try {
    await loadList(true);
  } catch (error) {
    showError(error);
  } finally {
    $("load-more").disabled = false;
  }
};
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
/*
 * Settings sections start collapsed (add-on 0.3.2). Each section's first
 * heading becomes a button with the title and a one-line status; the rest of
 * the section opens under it on tap. A section that needs the owner says so
 * in its header, so nothing important hides in a closed section.
 */
function collapseSettingsSections() {
  // Sections marked data-own-dialog move to a dialog of their own (studio.js) and stay open there.
  // The three parts of Accounts and connections collapse too, inside it, each under its own header.
  const sections = document.querySelectorAll(
    "#settings > section:not([data-own-dialog]), #accounts-section > section",
  );
  sections.forEach((section, index) => {
    const heading = section.querySelector(":scope > h3, :scope > h4");
    if (!heading || heading.querySelector(".settings-toggle")) return;
    const body = document.createElement("div");
    body.className = "settings-body";
    body.id = `${section.id || `settings-section-${index}`}-body`;
    body.hidden = true;
    while (heading.nextSibling) body.append(heading.nextSibling);
    section.append(body);
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "settings-toggle";
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-controls", body.id);
    toggle.append(textNode("span", heading.textContent.trim(), "settings-title"), textNode("span", "", "settings-status"));
    toggle.onclick = () => openSettingsSection(section, toggle.getAttribute("aria-expanded") !== "true");
    heading.replaceChildren(toggle);
  });
}
/** Open or close one settings section; opening a part of Accounts and connections opens that section too. */
function openSettingsSection(section, open = true) {
  const toggle = section.querySelector(":scope > :is(h3, h4) > .settings-toggle");
  const body = section.querySelector(":scope > .settings-body");
  if (!toggle || !body) return;
  toggle.setAttribute("aria-expanded", String(open));
  body.hidden = !open;
  const parent = section.parentElement?.closest("#settings section");
  if (open && parent) openSettingsSection(parent);
}
/** Put a one-line status in a section's header; attention marks one that needs the owner. */
function setSectionStatus(sectionId, text, attention = false) {
  const section = $(sectionId);
  const status = section?.querySelector(":scope > :is(h3, h4) > .settings-toggle .settings-status");
  if (!status) return;
  status.textContent = text;
  section.classList.toggle("needs-attention", Boolean(attention));
}
/*
 * Accounts and connections holds three parts. Its header joins a few words
 * for each part, for example "ChatGPT signed in · GitHub not connected · app
 * off". When a part needs the owner, the header is marked and names only the
 * parts that need the owner.
 */
const accountParts = {
  "chatgpt-section": { label: "ChatGPT", word: "", attention: false },
  "github-section": { label: "GitHub", word: "", attention: false },
  "app-pairing-section": { label: "app", word: "", attention: false },
};
/** Record one part's short state and redraw the Accounts and connections header. */
function setAccountPart(sectionId, word, attention = false) {
  accountParts[sectionId].word = word;
  accountParts[sectionId].attention = Boolean(attention);
  const parts = Object.values(accountParts).filter((part) => part.word);
  // When a part needs the owner, the header names only those parts, so it stays short and plain.
  const waiting = parts.filter((part) => part.attention);
  setSectionStatus(
    "accounts-section",
    (waiting.length ? waiting : parts).map((part) => `${part.label} ${part.word}`).join(" · "),
    waiting.length > 0,
  );
}
collapseSettingsSections();
setSectionStatus("workspace-section", "AGENTS.md");
const TEXT_SIZES = ["normal", "larger", "largest"];
const TEXT_SIZE_KEY = "codex-text-size";
/**
 * Show the page at one of the three text sizes and tick that choice in
 * Settings. The stylesheet multiplies every font size by --text-scale,
 * which the data-text-size attribute on the root sets.
 * With save, remember the choice on this device for the next visit.
 */
function setTextSize(value, save = false) {
  const size = TEXT_SIZES.includes(value) ? value : "normal";
  if (size === "normal") delete document.documentElement.dataset.textSize;
  else document.documentElement.dataset.textSize = size;
  for (const input of document.querySelectorAll("input[data-text-size-choice]"))
    input.checked = input.value === size;
  setSectionStatus("text-size-section", size[0].toUpperCase() + size.slice(1));
  if (!save) return;
  try {
    if (size === "normal") localStorage.removeItem(TEXT_SIZE_KEY);
    else localStorage.setItem(TEXT_SIZE_KEY, size);
  } catch (_) {
    /* Storage may be disabled: the size lasts until the page is reloaded. */
  }
}
setTextSize(document.documentElement.dataset.textSize);
for (const input of document.querySelectorAll("input[data-text-size-choice]"))
  input.addEventListener("change", () => setTextSize(input.value, true));
/*
 * The time left on a sign-in code. The worker sends the seconds left, by its
 * own clock; the page counts down from when that reply arrived, so a phone
 * with a wrong clock still shows the right time. One timer serves every
 * countdown on the page.
 */
const codeDeadlines = new Map();
let codeCountdownTimer = 0;
/**
 * Build the expiry line and the hidden "Get a new code" button for a card;
 * nothing when the time is unknown. kind ("chatgpt" or "github") lets a later
 * status reply set the time again; see followCodeTime.
 */
function codeExpiry(secondsLeft, getNewCode, kind) {
  if (typeof secondsLeft !== "number" || !Number.isFinite(secondsLeft)) return [];
  const line = textNode("p", "", "muted signin-expiry");
  const button = textNode("button", "Get a new code");
  button.type = "button";
  button.hidden = true;
  button.onclick = async () => {
    button.disabled = true;
    try {
      await getNewCode();
    } finally {
      button.disabled = false;
    }
  };
  const entry = { deadline: performance.now() + secondsLeft * 1000, button, kind };
  codeDeadlines.set(line, entry);
  showCodeTime(line, entry);
  if (!codeCountdownTimer) codeCountdownTimer = setInterval(showCodeTimes, 1000);
  return [line, button];
}
/**
 * Set the time left on the shown codes of one kind from the worker's latest
 * reply. The card is built once per code, and the page's own count can stop
 * while a phone sleeps, so each status reply corrects it.
 */
function followCodeTime(kind, secondsLeft) {
  if (typeof secondsLeft !== "number" || !Number.isFinite(secondsLeft)) return;
  for (const [line, entry] of codeDeadlines) {
    if (entry.kind !== kind) continue;
    entry.deadline = performance.now() + secondsLeft * 1000;
    showCodeTime(line, entry);
  }
}
/** Show the time left on one code; an expired code says so and offers a new one. */
function showCodeTime(line, { deadline, button }) {
  const left = Math.max(0, Math.round((deadline - performance.now()) / 1000));
  const expired = left === 0;
  const minutes = Math.floor(left / 60);
  const seconds = String(left % 60).padStart(2, "0");
  line.textContent = expired ? "This code has expired. Get a new code to sign in." : `The code expires in ${minutes}:${seconds}.`;
  line.classList.toggle("signin-expired", expired);
  button.hidden = !expired;
}
/** Update every countdown on the page; a card that left the page is forgotten. */
function showCodeTimes() {
  for (const [line, entry] of codeDeadlines) {
    if (line.isConnected) showCodeTime(line, entry);
    else codeDeadlines.delete(line);
  }
  if (!codeDeadlines.size) {
    clearInterval(codeCountdownTimer);
    codeCountdownTimer = 0;
  }
}
/** Replace a waiting ChatGPT sign-in with a new one, for a code that expired. */
async function newChatgptCode() {
  try {
    await api("auth/cancel", {});
    await api("auth/start", {});
    await loadLogin();
  } catch (error) {
    if ($("settings").open) settingsError(error);
    else showError(error);
  }
}
let loginTimer;
// What the sign-in card shows now; the card is rebuilt only when this changes,
// so a code being read or a QR being scanned never jumps under the user's thumb.
let shownLogin = null;
/**
 * Build the sign-in card: the code in large letters that stay put until the
 * sign-in ends or is canceled, the QR code of the sign-in page served by the
 * worker, and the link to the same page.
 */
function signInCard(auth) {
  const card = document.createElement("div");
  const requirement = textNode("p", "A ChatGPT account with Codex access is required. As of November 13, 2026, this includes Free and Go. ", "muted");
  const availability = textNode("a", "Check OpenAI’s current availability");
  availability.href = "https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan";
  availability.target = "_blank";
  availability.rel = "noopener noreferrer";
  requirement.append(availability);
  card.append(requirement);
  card.className = "signin-card";
  if (auth.user_code) {
    card.append(
      textNode("p", "Enter this code on the ChatGPT sign-in page:", "muted"),
    );
    const code = textNode("p", auth.user_code, "signin-code");
    code.setAttribute(
      "aria-label",
      `Sign-in code ${auth.user_code.split("").join(" ")}`,
    );
    card.append(code, ...codeExpiry(auth.expires_in, newChatgptCode, "chatgpt"));
  } else {
    card.append(textNode("p", "Waiting for the sign-in code…", "muted"));
  }
  if (auth.qr_path) {
    const img = document.createElement("img");
    img.src = auth.qr_path;
    img.alt = "QR code that opens the ChatGPT sign-in page";
    img.className = "signin-qr";
    img.width = img.height = 220;
    card.append(img);
  }
  if (auth.verification_url) {
    const url = new URL(auth.verification_url, location.href);
    if (url.protocol === "https:") {
      const link = textNode("a", "Open the ChatGPT sign-in page");
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noreferrer";
      card.append(link);
    }
  }
  return card;
}
/**
 * Show the ChatGPT sign-in state in the settings dialog and, while the
 * assistant is not signed in, in the banner above the chat. Checks again
 * every three seconds while a sign-in waits, every 30 seconds while signed
 * out, and every five minutes while signed in, so an expired sign-in shows.
 */
async function loadLogin() {
  clearTimeout(loginTimer);
  let auth;
  try {
    ({ auth } = await api("auth/status"));
  } finally {
    loginTimer = setTimeout(
      () => loadLogin().catch(() => {}),
      auth?.pending ? 3000 : auth && !auth.codex_login?.status_ok ? 30000 : 300000,
    );
  }
  renderLogin(auth);
}
/** The ChatGPT account header in a few words. */
function chatgptSectionStatus({ signedIn, pending, failed, lapsed }) {
  if (signedIn) return "Signed in";
  if (pending) return "Signing in: enter the code";
  if (failed) return "Sign-in failed";
  if (lapsed) return "Sign-in expired";
  return "Not signed in";
}
/** Put the sign-in state on the page; see loadLogin. */
function renderLogin(auth) {
  const signedIn = Boolean(auth.codex_login?.status_ok);
  const pending = Boolean(auth.pending);
  // The worker reports "rejected" when OpenAI refused the saved sign-in (expired or revoked),
  // although the local sign-in file is still there.
  const lapsed = !signedIn && auth.codex_login?.server_check === "rejected";
  // The last sign-in ended without signing in; the worker says why when it can.
  const failed = !signedIn && !pending && auth.status === "failed";
  const failure = `The last sign-in did not finish. ${auth.error || "Get a new code to try again."}`;
  $("login-status").textContent = signedIn
    ? auth.codex_login.message || "Signed in to ChatGPT."
    : pending
      ? "Signing in. Enter the code below on the ChatGPT sign-in page."
      : failed
        ? failure
        : lapsed
          ? "The ChatGPT sign-in expired or was revoked. Sign in again."
          : "Not signed in to ChatGPT.";
  $("signin-banner-text").textContent = failed
    ? failure
    : lapsed
      ? "The assistant's ChatGPT sign-in expired or was revoked. Sign in again."
      : "The assistant is not signed in to ChatGPT.";
  const sectionStatus = chatgptSectionStatus({ signedIn, pending, failed, lapsed });
  setSectionStatus("chatgpt-section", sectionStatus, !signedIn);
  setAccountPart("chatgpt-section", sectionStatus.toLowerCase(), !signedIn);
  for (const id of ["login", "banner-login"]) $(id).textContent = failed ? "Get a new code" : "Sign in";
  const key = JSON.stringify([
    signedIn,
    lapsed,
    pending,
    auth.login_id,
    auth.user_code,
    auth.verification_url,
  ]);
  if (key !== shownLogin) {
    shownLogin = key;
    $("login-card").replaceChildren(...(pending ? [signInCard(auth)] : []));
    $("signin-banner-card").replaceChildren(
      ...(pending ? [signInCard(auth)] : []),
    );
  } else if (pending) {
    followCodeTime("chatgpt", auth.expires_in);
  }
  for (const id of ["login", "banner-login"]) $(id).hidden = pending;
  // The sign-in card repeats the account requirement, so the section shows it once.
  $("login-requirement").hidden = pending;
  for (const id of ["login-cancel", "banner-cancel"]) $(id).hidden = !pending;
  $("signin-banner").hidden = signedIn;
}
/*
 * GitHub sign-in (add-on 0.3). The worker holds the sign-in; this page only
 * shows its status and sends button presses. The device code is shown on the
 * page while a sign-in waits and nowhere else: it is never logged, never kept
 * in browser storage, and the pasted token is cleared from its field at once.
 */
let githubTimer;
let githubStatus = null;
let githubRevokeUrl = "";
const GITHUB_BAD_STATES = ["expired", "revoked", "access_changed", "failed"];
const GITHUB_STATE_TEXT = {
  disconnected: "Not connected to GitHub.",
  connecting: "Connecting. Enter the code below on GitHub.",
  ok: "Connected to GitHub.",
  near_expiry: "GitHub still works, but this sign-in expires within 7 days. Choose Connect again before then.",
  expired: "The GitHub sign-in expired. Choose Connect again.",
  revoked: "GitHub no longer accepts this sign-in. Choose Connect again. If needed, restore the App's repository access on GitHub.",
  access_changed: "The GitHub sign-in reaches different repositories or access than you connected.",
  failed: "The GitHub sign-in cannot be checked.",
};
// Connect again starts a GitHub App sign-in, so a token connection is sent to the token form instead.
const GITHUB_TOKEN_STATE_TEXT = {
  near_expiry: "GitHub still works, but this token expires within 7 days. Create a new token on GitHub, then choose Use a token instead.",
  expired: "This GitHub token expired. Create a new token on GitHub, then choose Use a token instead.",
  revoked: "GitHub no longer accepts this token. Create a new token on GitHub, then choose Use a token instead.",
};
/** The sentence for a GitHub state, with the recovery step that fits the kind of sign-in. */
function githubStateText(state, kind) {
  return (kind === "token" && GITHUB_TOKEN_STATE_TEXT[state]) || GITHUB_STATE_TEXT[state] || state;
}
/** Send a GitHub request; every reply carries the status, also a refusal's, so the card stays true. */
async function githubApi(path, body) {
  const response = await pageFetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json().catch(() => null);
  if (data && data.github && typeof data.github === "object") renderGithub(data.github);
  if (!data) throw workerError(`The worker returned HTTP ${response.status}.`, response.status);
  if (!response.ok || !data.ok)
    throw workerError(
      data.error || data.text || `The worker returned HTTP ${response.status}.`,
      response.status,
    );
  return data;
}
/** An https link on github.com only; anything else is not shown as a link. */
function githubLink(href, text) {
  try {
    const url = new URL(href);
    if (url.protocol !== "https:" || url.hostname !== "github.com") return null;
    const link = textNode("a", text);
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  } catch (_) {
    return null;
  }
}
function githubDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}
function githubExpiry(github) {
  if (!github.expires_at)
    return github.kind === "app" ? "Does not expire; revoke it on GitHub." : "No expiry reported.";
  const days = Math.floor((new Date(github.expires_at).getTime() - Date.now()) / 86400000);
  if (Number.isNaN(days)) return "";
  return `${githubDate(github.expires_at)} (${days < 0 ? "expired" : days === 0 ? "today" : `in ${days} day${days === 1 ? "" : "s"}`})`;
}
/** The device-sign-in card: the code in large letters and GitHub's own page. */
function githubFlowCard(flow) {
  const card = document.createElement("div");
  card.className = "signin-card github-flow-card";
  if (flow.user_code) {
    card.append(textNode("p", "Enter this code on GitHub's device sign-in page, then approve Codex for Home Assistant:", "muted"));
    const code = textNode("p", flow.user_code, "signin-code");
    code.setAttribute("aria-label", `GitHub code ${flow.user_code.split("").join(" ")}`);
    card.append(code);
  } else {
    card.append(textNode("p", "Waiting for the GitHub code…", "muted"));
  }
  const link = githubLink(flow.verification_uri || "https://github.com/login/device", "Open github.com/login/device");
  if (link) card.append(link);
  if (flow.user_code) card.append(...codeExpiry(flow.expires_in, newGithubCode, "github"));
  return card;
}
/** Replace a waiting GitHub sign-in with a new one, for a code that expired. */
async function newGithubCode() {
  githubResult("");
  try {
    await githubApi("github/cancel", {});
    await githubApi("github/connect", {});
  } catch (error) {
    githubResult(String(error.message || error), true);
  } finally {
    loadGithub().catch(() => {});
  }
}
/** The GitHub header in a few words, for example "Connected as owner, 1 repository". */
function githubSectionStatus({ waiting, connected, bad, state, kind, login, repositoryCount, retry }) {
  if (waiting) return "Connecting: enter the code";
  if (connected) {
    const condition = bad ? githubStateText(state, kind).replace(/\.$/, "") : "Connected";
    const account = login ? ` as ${login}` : "";
    const repositories = repositoryCount === 1 ? "1 repository" : `${repositoryCount} repositories`;
    return `${condition}${account}, ${repositories}`;
  }
  if (retry) return "Sign-in did not finish";
  return "Not connected";
}
/** The GitHub part of the Accounts and connections header, in one or two words. */
function githubAccountWord({ waiting, connected, bad, retry }) {
  if (waiting) return "waiting for the code";
  if (connected) return bad ? "needs attention" : "connected";
  if (retry) return "sign-in did not finish";
  return "not connected";
}
/** Put the GitHub status on the card and the banner. Only the contract's fields are read. */
function renderGithub(github) {
  githubStatus = github;
  const state = String(github.state || "disconnected");
  const flow = github.flow && typeof github.flow === "object" ? github.flow : { status: "idle" };
  const waiting = flow.status === "waiting_for_user";
  const connected = !["disconnected", "connecting"].includes(state);
  const login = github.account?.login || "";
  const kind = github.kind === "token" ? "token" : "app";
  if (connected && github.revoke_url) githubRevokeUrl = github.revoke_url;
  $("github-status").textContent =
    connected && login ? `${githubStateText(state, kind)} Signed in as ${login}.` : githubStateText(state, kind);
  const details = $("github-details");
  const rows = [];
  if (connected) {
    rows.push(["Sign-in", kind === "token" ? "Fine-grained token" : "GitHub App"]);
    if (github.connected_at) rows.push(["Connected", githubDate(github.connected_at)]);
    rows.push(["Expiry", githubExpiry(github)]);
    rows.push(["Access", github.permission_problem
      ? String(github.permission_problem)
      : github.permissions ? "Contents read and write, Metadata read" : "Unknown"]);
    if (github.last_check?.at)
      rows.push(["Last check", `${githubDate(github.last_check.at)}${github.last_check.reason ? `: ${github.last_check.reason}` : ""}`]);
  }
  details.replaceChildren(...rows.flatMap(([term, value]) => [textNode("dt", term), textNode("dd", value)]));
  details.hidden = !rows.length;
  const list = $("github-repositories");
  const items = [];
  if (connected) {
    for (const repo of github.repositories || []) {
      if (!repo || typeof repo.full_name !== "string") continue;
      const extra = (github.extra || []).includes(repo.full_name);
      const item = textNode("li", `${repo.private ? "🔒 " : ""}${repo.full_name} · ${repo.push ? "read and write" : "read only"}${extra ? " · new, not accepted" : ""}`);
      if (extra) item.className = "github-extra";
      items.push(item);
    }
    for (const name of github.missing || [])
      if (typeof name === "string") items.push(textNode("li", `${name} · no longer reached`, "github-missing"));
  }
  list.replaceChildren(...items);
  list.hidden = !items.length;
  const flowArea = $("github-flow");
  if (waiting) {
    const key = JSON.stringify([flow.user_code, flow.verification_uri, flow.expires_at]);
    if (flowArea.dataset.key !== key) {
      flowArea.dataset.key = key;
      flowArea.replaceChildren(githubFlowCard(flow));
    } else {
      followCodeTime("github", flow.expires_in);
    }
  } else {
    flowArea.dataset.key = "";
    // A failed Connect again also shows its reason, and says that the stored sign-in did not change.
    const ended = connected
      ? { expired: "The new GitHub code expired before it was entered.",
        canceled: "The new GitHub sign-in was canceled.", failed: "The new GitHub sign-in failed." }[flow.status]
      : { expired: "The GitHub code expired before it was entered. Get a new code to sign in.",
        canceled: "The GitHub sign-in was canceled.", failed: "The GitHub sign-in failed." }[flow.status];
    const why = flow.status === "failed" && flow.error ? ` ${flow.error}` : "";
    const kept = connected ? " Your previous sign-in has not changed." : "";
    flowArea.replaceChildren(...(ended ? [textNode("p", `${ended}${why}${kept}`, "muted")] : []));
  }
  const retry = !connected && ["expired", "failed"].includes(flow.status);
  $("github-connect").textContent = connected ? "Connect again" : retry ? "Get a new code" : "Connect GitHub";
  $("github-connect").hidden = waiting;
  $("github-cancel").hidden = !waiting;
  for (const id of ["github-test", "github-check", "github-disconnect"]) $(id).hidden = !connected;
  $("github-accept").hidden = !(state === "access_changed" && !github.permission_problem);
  if (!connected) $("github-test-result").hidden = true;
  else if (github.last_test?.text) {
    $("github-test-result").textContent = github.last_test.text;
    $("github-test-result").hidden = false;
  }
  const usable = ["ok", "near_expiry"].includes(state);
  const names = usable ? (github.repositories || []).filter((repo) => repo?.push && typeof repo.full_name === "string").map((repo) => repo.full_name) : [];
  for (const id of ["github-clone-repository", "github-link-repository"]) {
    const select = $(id);
    const chosen = select.value;
    select.replaceChildren(...names.map((name) => { const option = textNode("option", name); option.value = name; return option; }));
    if (names.includes(chosen)) select.value = chosen;
  }
  $("github-local").hidden = !names.length;
  githubDefaultFolder();
  const bad = GITHUB_BAD_STATES.includes(state);
  const repositoryCount = (github.repositories || []).length;
  setSectionStatus(
    "github-section",
    githubSectionStatus({ waiting, connected, bad, state, kind, login, repositoryCount, retry }),
    bad || waiting || retry,
  );
  setAccountPart("github-section", githubAccountWord({ waiting, connected, bad, retry }), bad || waiting || retry);
  $("github-banner-text").textContent = `The add-on's GitHub sign-in needs attention: ${githubStateText(state, kind).replace(/\.$/, "")}.`;
  $("github-banner").hidden = !bad;
}
/** Show that this worker has no GitHub sign-in (an add-on before 0.3). */
function githubUnavailable() {
  githubStatus = null;
  $("github-status").textContent = "Not available in this version of the add-on.";
  for (const id of ["github-connect", "github-cancel", "github-test", "github-check", "github-accept", "github-disconnect"])
    $(id).hidden = true;
  $("github-details").hidden = $("github-repositories").hidden = $("github-local").hidden = true;
  $("github-token").hidden = true;
  $("github-banner").hidden = true;
  setSectionStatus("github-section", "Not available in this version");
  setAccountPart("github-section", "not available");
}
/** Read the GitHub status. Every 5 seconds while a code waits, else every 5 minutes, so a lost sign-in shows. */
async function loadGithub() {
  clearTimeout(githubTimer);
  let waiting = false;
  try {
    const response = await pageFetch("github");
    if (response.status === 404) return githubUnavailable();
    const data = await response.json().catch(() => null);
    if (!data?.ok || !data.github) throw workerError(data?.error || `The worker returned HTTP ${response.status}.`, response.status);
    $("github-token").hidden = false;
    renderGithub(data.github);
    waiting = data.github.flow?.status === "waiting_for_user";
  } finally {
    githubTimer = setTimeout(() => loadGithub().catch(() => {}), waiting ? 5000 : 300000);
  }
}
function githubResult(text, isError) {
  const result = $("github-result");
  result.textContent = text || "";
  result.classList.toggle("error", Boolean(isError));
}
/** Run one GitHub button: disable it while the worker works, then show the outcome on the card. */
async function githubAction(button, path, body, done) {
  button.disabled = true;
  githubResult("");
  try {
    const data = await githubApi(path, body);
    if (done) done(data);
  } catch (error) {
    githubResult(String(error.message || error), true);
  } finally {
    button.disabled = false;
    loadGithub().catch(() => {});
  }
}
$("github-connect").onclick = () => githubAction($("github-connect"), "github/connect", {});
$("github-cancel").onclick = () => githubAction($("github-cancel"), "github/cancel", {});
$("github-test").onclick = () => githubAction($("github-test"), "github/test", {});
$("github-check").onclick = () => githubAction($("github-check"), "github/check", {}, (data) =>
  githubResult(`Check finished: ${data.github?.state ? githubStateText(data.github.state, data.github.kind) : "done"}`));
$("github-accept").onclick = () => githubAction($("github-accept"), "github/accept", {}, () =>
  githubResult("The current repositories are now the ones the check expects."));
$("github-disconnect").onclick = () => {
  const revoke = githubRevokeUrl || githubStatus?.revoke_url || "";
  if (!window.confirm("Disconnect GitHub? The assistant can no longer fetch, pull or push until you connect again.")) return;
  githubAction($("github-disconnect"), "github/disconnect", {}, () => {
    const result = $("github-result");
    result.classList.remove("error");
    result.replaceChildren(textNode("span", "Disconnect forgets the sign-in on this box. To revoke it on GitHub, open this link: "));
    const link = githubLink(revoke, revoke.replace(/^https:\/\//, ""));
    if (link) result.append(link);
  });
};
$("github-token-form").onsubmit = (event) => {
  event.preventDefault();
  const field = $("github-token-value");
  const token = field.value;
  // The token leaves the page once, in this request body, and is not kept anywhere on the page.
  field.value = "";
  githubAction($("github-token-save"), "github/token",
    { token, repository: $("github-token-repository").value.trim() },
    () => { $("github-token").open = false; githubResult("Token accepted."); });
};
/** Clone goes to /share/<repository name> unless the owner typed another folder name. */
function githubDefaultFolder() {
  const field = $("github-clone-folder");
  if (field.dataset.edited) return;
  const name = ($("github-clone-repository").value || "").split("/")[1] || "";
  field.value = name.replace(/[^A-Za-z0-9_-]/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 100);
}
$("github-clone-repository").onchange = githubDefaultFolder;
$("github-clone-folder").oninput = () => { $("github-clone-folder").dataset.edited = "1"; };
$("github-clone-form").onsubmit = (event) => {
  event.preventDefault();
  githubAction($("github-clone"), "github/repos/clone",
    { repository: $("github-clone-repository").value, folder: $("github-clone-folder").value.trim() },
    (data) => githubResult(data.path ? `Cloned into ${data.path}.` : data.text || "Cloned."));
};
$("github-link-form").onsubmit = (event) => {
  event.preventDefault();
  githubAction($("github-link"), "github/repos/link",
    { repository: $("github-link-repository").value, path: $("github-link-path").value.trim() },
    (data) => githubResult(data.text || "Linked."));
};
$("github-banner-open").onclick = async () => {
  await $("settings-button").onclick();
  openSettingsSection($("github-section"));
  $("github-section").scrollIntoView({ block: "start" });
};
/** Show whether the assistant is paused, in the settings dialog and above the chat. */
function renderPause(pause) {
  const paused = Boolean(pause?.paused);
  $("pause-status").textContent = paused
    ? "Paused. New messages are refused and queued ones wait."
    : "Running.";
  $("pause").textContent = paused ? "Resume the assistant" : "Pause the assistant";
  $("pause").dataset.paused = paused ? "1" : "";
  $("paused-banner").hidden = !paused;
  setSectionStatus("assistant-section", paused ? "Paused" : "Running", paused);
}
/** Read the pause state from the worker and show it. */
async function loadPause() {
  renderPause(await api("pause"));
}
let remoteTimer;
let remoteCardKey = "";
/**
 * Show the remote-control state in the settings dialog: on or off, the
 * machine name, the pairing code as large text and a QR code, and the recent
 * messages. Checks again every five seconds while the dialog is open.
 */
async function loadRemoteControl() {
  clearTimeout(remoteTimer);
  const { remote_control: rc } = await api("remote-control");
  renderRemoteControl(rc);
  if ($("settings").open)
    remoteTimer = setTimeout(
      () => loadRemoteControl().catch(settingsError),
      rc.enabled ? 5000 : 15000,
    );
}
/** Draw one remote-control state; the buttons follow what can be done now. */
function renderRemoteControl(rc) {
  const name = rc.machine_name || rc.configured_name;
  let status = rc.message || rc.state || "Unknown";
  if (!rc.available) status = rc.blocked_reason;
  else if (!rc.enabled) status = `Off. When on, this Home Assistant shows in the ChatGPT app as “${rc.configured_name}”.`;
  $("remote-control-status").textContent = status;
  const address = rc.core_address;
  $("remote-core-address").textContent = address ? address.url ? `Core address (${address.source}): ${address.url}` : address.error || "Core address unavailable." : "";
  $("remote-read-tools").textContent = rc.read_tools || "";
  for (const [id, text] of [
    ["remote-control-name-warning", rc.name_warning],
    ["remote-control-policy-warning", rc.policy_warning],
  ]) {
    $(id).textContent = text || "";
    $(id).hidden = !text;
  }
  const card = $("remote-control-card");
  const key = JSON.stringify([name, rc.enabled, rc.pairing_code, rc.pairing_qr, rc.pairing_url]);
  // Polling must not replace the code while a person copies it or scans its QR.
  if (key !== remoteCardKey) {
    remoteCardKey = key;
    card.replaceChildren();
  if (rc.enabled && rc.pairing_code) {
    card.append(
      textNode("p", `Pairing code for “${name}”:`),
      textNode("p", rc.pairing_code, "pairing-code"),
    );
    if (rc.pairing_qr && rc.pairing_qr.startsWith("data:image/svg+xml;base64,")) {
      const img = document.createElement("img");
      img.src = rc.pairing_qr;
      img.alt = `QR code for pairing code ${rc.pairing_code}`;
      img.className = "pairing-qr";
      card.append(img);
    }
  }
  }
  renderAppPairing(rc, status);
  renderRemoteControlName(rc);
  $("remote-control-start").hidden = rc.enabled;
  $("remote-control-start").disabled = !rc.available;
  $("remote-control-stop").hidden = !rc.enabled;
  $("remote-control-pair").hidden = !rc.enabled;
  $("remote-control-note").hidden = !rc.available;
  const findings = rc.config_findings || [];
  const list = $("remote-control-findings-list");
  list.replaceChildren(
    ...findings.map((item) =>
      textNode("li", `${item.file || "config"}${item.line ? `, line ${item.line}` : ""}: ${item.reason}${item.undone ? ` — ${item.undone}` : item.undo_note ? ` — ${item.undo_note}` : ""}${item.recovery_copy ? ` — replaced version saved at ${item.recovery_copy}` : ""}${item.text ? ` (${item.text})` : ""}`),
    ),
  );
  if ((rc.config_findings_count || 0) > findings.length) {
    list.append(textNode("li", `… and ${rc.config_findings_count - findings.length} more`));
  }
  $("remote-control-findings").hidden = !findings.length;
  $("remote-control-log").textContent = (rc.log || []).join("\n");
  $("remote-control-log-box").hidden = !(rc.log || []).length;
  document.dispatchEvent(new CustomEvent("remote-view", {detail: rc}));
}
/** Show the machine name the ChatGPT app lists; it is set on the Configuration tab (0.3.5). */
function renderRemoteControlName(rc) {
  $("remote-control-name").textContent = rc.configured_name || "";
}
const ADDON_CONFIGURATION_UNKNOWN =
  "Home Assistant did not give this app's address. Open the app's Configuration tab from the Home Assistant settings.";
/**
 * Point the Add-on configuration row at this app's Configuration tab. The slug differs per install, so the
 * worker asks Supervisor. The row has no link until Supervisor answers, and it says where to go when it cannot.
 */
async function loadAddonConfiguration() {
  const row = $("addon-configuration");
  const description = $("addon-configuration-description");
  description.dataset.usual ??= description.textContent;
  const data = await api("addon-configuration").catch(() => ({}));
  if (typeof data.url === "string" && data.url.startsWith("/hassio/addon/")) {
    row.href = data.url;
    description.textContent = description.dataset.usual;
  } else {
    row.removeAttribute("href");
    description.textContent = ADDON_CONFIGURATION_UNKNOWN;
  }
}
for (const [id, action] of [
  ["remote-control-start", "start"],
  ["remote-control-stop", "stop"],
  ["remote-control-pair", "pair"],
  ["remote-control-review", "review"],
]) {
  $(id).onclick = async () => {
    $(id).disabled = true;
    try {
      const data = await api(`remote-control/${action}`, {});
      renderRemoteControl(data.remote_control);
      $("settings-result").textContent = "";
    } catch (error) {
      settingsError(error);
    } finally {
      $(id).disabled = false;
      loadRemoteControl().catch(settingsError);
    }
  };
}
/**
 * The ChatGPT app pairing part of Accounts and connections: the same state as
 * the remote connection window, a button for a new pairing code while
 * remote control is on, and Turn off.
 */
function renderAppPairing(rc, status) {
  const ready = rc.enabled && ["ready", "pairing"].includes(rc.state);
  const failed = rc.enabled && rc.available && !ready && !["starting", "reconnecting"].includes(rc.state);
  let word = "on";
  if (!rc.available) word = "unavailable";
  else if (!rc.enabled) word = "off";
  else if (rc.state === "starting" || rc.state === "reconnecting") word = rc.state;
  else if (failed) word = "connection error";
  $("app-pairing-status").textContent = status;
  $("app-pairing-new").hidden = !ready;
  $("app-pairing-off").hidden = !rc.enabled;
  // The part's own header shows the word too, capitalized as the other headers are.
  setSectionStatus("app-pairing-section", word[0].toUpperCase() + word.slice(1), failed);
  setAccountPart("app-pairing-section", word, failed);
}
$("app-pairing-off").onclick = () => $("remote-control-stop").click();
/** Show an error in the result line of the settings dialog. */
function settingsError(error) {
  const result = $("connection-dialog").open ? $("connection-result") : $("settings-result");
  result.textContent = String(error.message || error);
}
/**
 * The Home Assistant theme variables the page reads, by name without the leading "--". Home Assistant sets them on
 * its own document; the page runs in an iframe and does not inherit them, so haTheme() copies them to this page's
 * root. Each use in chat.css and studio.css has a fallback, so a name a frontend version lacks changes nothing.
 */
const haThemeTokens = [
  // Colors: studio.css maps these onto the page's own --bg, --sidebar, --surface, --text, --muted, --line and --accent.
  "primary-color", "primary-background-color", "secondary-background-color", "card-background-color",
  "primary-text-color", "secondary-text-color", "divider-color", "disabled-text-color", "error-color", "warning-color",
  "text-light-primary-color",
  // Filled buttons and filled fields, as on the add-on's Configuration tab.
  "ha-color-fill-primary-loud-resting", "ha-color-on-primary-loud",
  "input-fill-color", "input-ink-color", "input-label-ink-color", "input-idle-line-color",
  // Font and type scale.
  "ha-font-family-body", "ha-font-size-s", "ha-font-size-m",
];
/**
 * Follow Home Assistant's theme: copy its variables to this page's root and set data-theme to light or dark. A
 * variable the theme does not set is removed, so a theme change never leaves an old color behind. With no parent
 * window (the standalone preview) or one the page may not read, the page keeps its own palette and the system's
 * light or dark setting.
 */
function haTheme() {
  let dark = matchMedia("(prefers-color-scheme: dark)").matches;
  try {
    if (window.parent !== window) {
      const host = window.parent.document.querySelector("home-assistant") || window.parent.document.documentElement;
      const style = window.parent.getComputedStyle(host);
      if (typeof host.hass?.themes?.darkMode === "boolean") dark = host.hass.themes.darkMode;
      else if (style.colorScheme === "dark") dark = true;
      else if (style.colorScheme === "light") dark = false;
      for (const token of haThemeTokens) {
        const value = style.getPropertyValue("--" + token).trim();
        if (value) document.documentElement.style.setProperty("--" + token, value);
        else document.documentElement.style.removeProperty("--" + token);
      }
    }
  } catch (_error) { /* A cross-origin parent: keep the page's own palette. */ }
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}
$("settings-button").onclick = async () => {
  try {
    const data = await api("diagnostics/settings");
    setSectionStatus("interaction-logs-section", `Keeps ${data.capacity}`);
  } catch (_error) {
    setSectionStatus("interaction-logs-section", "Administrators only");
  }
  sidebar(false);
  haTheme();
  $("settings").showModal();
  $("settings-result").textContent = "";
  $("agents").disabled = true;
  $("save-agents").disabled = true;
  const results = await Promise.allSettled([
    loadLogin(),
    loadGithub(),
    loadRemoteControl(),
    loadPause(),
    loadBrowserPermissions(),
    loadAddonConfiguration(),
  ]);
  for (const result of results)
    if (result.status === "rejected") settingsError(result.reason);
};
$("close-settings").onclick = () => $("settings").close();
$("settings").addEventListener("close", () => {
  clearTimeout(loginTimer);
  clearTimeout(remoteTimer);
  $("github-result").textContent = "";
});
for (const [id, path] of [
  ["login", "auth/start"],
  ["banner-login", "auth/start"],
  ["login-cancel", "auth/cancel"],
  ["banner-cancel", "auth/cancel"],
  ["logout", "auth/logout"],
]) {
  $(id).onclick = async () => {
    $(id).disabled = true;
    try {
      await api(path, {});
      await loadLogin();
      $("settings-result").textContent = "";
    } catch (error) {
      if ($("settings").open) settingsError(error);
      else showError(error);
    } finally {
      $(id).disabled = false;
    }
  };
}
for (const id of ["pause", "resume"]) {
  $(id).onclick = async () => {
    $(id).disabled = true;
    try {
      const paused = id === "pause" && !$("pause").dataset.paused;
      renderPause(await api("pause", { paused }));
    } catch (error) {
      if ($("settings").open) settingsError(error);
      else showError(error);
    } finally {
      $(id).disabled = false;
    }
  };
}
let restore = Promise.resolve();
/** Poll the list and the open chat; the first pass waits for the restored chat. */
async function refresh() {
  clearTimeout(refreshTimer);
  if (!document.hidden && !state.busy) {
    try {
      if (!state.catalog) await loadChatOptions();
      await loadList();
      await restore;
      await fetchSelected();
    } catch (error) {
      showError(error);
    }
  }
  refreshTimer = setTimeout(refresh, state.task?.browser_retry ? 1000 : state.active ? 3000 : 12000);
}
sidebar(false);
controls();
restore = reopenLastChat();
refresh();
loadUsage();
loadLogin().catch(() => {});
loadGithub().catch(() => {});
loadPause().catch(() => {});
setInterval(tickElapsed, 1000);
