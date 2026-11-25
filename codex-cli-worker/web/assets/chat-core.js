"use strict";
// The page's shared base, loaded first: the element helper, labels, state, the session and fetch helpers,
// poll() and busyAction(), text and date helpers, the icons and the theme. It uses no name of the later files.
/** Return the page element with the given id. */
const $ = (id) => document.getElementById(id);
// Remote Control is paused in this version: the worker marks the page root (server.mark_remote_paused), the page
// hides every part of it and sends no remote-control request.
const REMOTE_PAUSED = document.documentElement.hasAttribute("data-remote-paused");
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
  // Earlier answers whose "Worked for" line the owner opened. The latest answer keeps this in state.activity.
  workedOpen: new Set(),
  // The saved steps of earlier answers, by "<chat id>/<turn id>", loaded the first time their line opens.
  earlierSteps: new Map(),
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
/** Every repeating check of the page, by the name given to poll(). */
const polls = {};
/**
 * Register a check that repeats. run() calls fn now, unless the page is
 * hidden, and then waits delayFn(result) milliseconds before the next run;
 * null from delayFn ends the repeats until the next run() or after(). result
 * is what fn returned, or undefined when the page was hidden or fn failed.
 * run() returns what fn returns, and a failure of fn reaches its caller; a
 * failure in a run that the timer started goes to onError. whileHidden is for
 * a check that must also run on a hidden page.
 */
function poll(name, fn, delayFn, { whileHidden = false, onError = () => {} } = {}) {
  let timer = 0;
  const entry = {
    /** Whether a later run is scheduled. */
    get waiting() {
      return timer !== 0;
    },
    stop() {
      clearTimeout(timer);
      timer = 0;
    },
    /** Run after this many milliseconds, in place of any run that was scheduled. */
    after(delay) {
      entry.stop();
      timer = setTimeout(() => {
        timer = 0;
        entry.run().catch(onError);
      }, delay);
    },
    async run() {
      entry.stop();
      let result;
      try {
        if (whileHidden || !document.hidden) result = await fn();
        return result;
      } finally {
        const delay = delayFn(result);
        if (delay !== null) entry.after(delay);
      }
    },
  };
  polls[name] = entry;
  return entry;
}
/**
 * Run one button's action: the button is disabled while fn works and enabled
 * again when it ends. A failure goes to onError; without onError it reaches
 * the caller.
 */
async function busyAction(button, fn, onError) {
  button.disabled = true;
  try {
    await fn();
  } catch (error) {
    if (!onError) throw error;
    onError(error);
  } finally {
    button.disabled = false;
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
