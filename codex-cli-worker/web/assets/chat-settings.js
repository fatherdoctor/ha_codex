"use strict";
// The Settings window and the account lines: usage, attention notes, the integration line, the sections,
// text size, the ChatGPT sign-in, GitHub, remote control, the app configuration and pairing. Loads after chat-core.js.
let usageLoading = false;
// The last sign-in and status reads behind the ChatGPT details in Settings; see renderChatgptDetails.
const chatgptAccount = { auth: null, status: null };
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
/** The plan type the worker reports, capitalized ("Pro"), or "" when it reports none. */
function planName(planType) {
  const plan = typeof planType === "string" ? planType.trim() : "";
  return plan && `${plan[0].toUpperCase()}${plan.slice(1)}`;
}
/**
 * The plan line under the quota bars, for example "Pro plan · Weekly resets
 * Mon 9:00 AM · 5h resets 2:30 PM". The plan is information only; a reset time
 * shows only for a window the read reported.
 */
function planLine(usage, planType) {
  const plan = planName(planType);
  const parts = [plan ? `${plan} plan` : "Plan unknown"];
  for (const [key, name] of [["weekly", "Weekly"], ["five_hour", "5h"]]) {
    const reset = shortResetLabel(usage[`${key}_reset_at`]) || usage[`${key}_reset`];
    if (usage[`${key}_percent`] !== undefined && usage[`${key}_percent`] !== "" && reset)
      parts.push(`${name} resets ${reset}`);
  }
  return parts.join(" · ");
}
/*
 * The three rows at the foot of the side bar: Codex (the ChatGPT sign-in),
 * GitHub and Remote (the ChatGPT app connection). Each shows one word or two
 * and a dot: ready, starting, needs-owner, error or off.
 */
function setFootRow(id, dot, text) {
  $(id).dataset.state = dot;
  $(id.replace("-row", "-summary")).textContent = text;
}
/*
 * The warning on the Settings icon. While something needs the owner, the cog
 * becomes a warning symbol: yellow for an approval or a schedule proposal that
 * waits, red for a sign-out. On a phone the menu buttons show the same symbol.
 * Red wins over yellow.
 *
 * Each part of the page reports what it last read with noteAttention, from
 * the polls it already runs; nothing here asks the worker for more. GitHub
 * and Remote count only a sign-out the owner did not ask for: GitHub reports
 * "disconnected" after Sign out and "expired" or "revoked" when the stored
 * sign-in stops working, and Remote reports "error" only while the owner's
 * switch is on. ChatGPT not signed in, or its status unreadable, is the one
 * red the owner cannot dismiss: no chat can run without it.
 *
 * Dismiss hides the warnings shown at that moment. A warning comes back when
 * something new happens: a new approval or proposal, or a sign-out after the
 * account was working again. Dismissed keys live in this page only.
 */
const attention = {
  chatgpt: "",
  github: "",
  remote: false,
  approvals: [],
  proposals: [],
  dismissed: new Set(),
};
/** Every warning in the reported state, each with a key that changes when something new happens. */
function attentionWarnings(parts) {
  const warnings = [];
  if (parts.chatgpt === "signed-out")
    warnings.push({ key: "chatgpt", level: "red", label: "ChatGPT signed out", dismissible: false });
  if (parts.chatgpt === "unavailable")
    warnings.push({ key: "chatgpt", level: "red", label: "ChatGPT unavailable", dismissible: false });
  if (["expired", "revoked"].includes(parts.github))
    warnings.push({ key: `github:${parts.github}`, level: "red", label: "GitHub signed out", dismissible: true });
  if (parts.remote)
    warnings.push({ key: "remote", level: "red", label: "Remote disconnected", dismissible: true });
  for (const id of parts.approvals)
    warnings.push({ key: `approval:${id}`, level: "yellow", label: "Approval waiting", dismissible: true });
  for (const id of parts.proposals)
    warnings.push({ key: `proposal:${id}`, level: "yellow", label: "Schedule proposal waiting", dismissible: true });
  return warnings;
}
/** The warnings to show: those not dismissed, plus the red that cannot be dismissed. */
function shownWarnings(warnings, dismissed) {
  return warnings.filter((warning) => !warning.dismissible || !dismissed.has(warning.key));
}
/** Draw the warning from the state each part last reported. */
function showAttention() {
  const warnings = attentionWarnings(attention);
  // A key that is gone is no longer dismissed, so the same thing coming back counts as new.
  const current = new Set(warnings.map((warning) => warning.key));
  for (const key of attention.dismissed) if (!current.has(key)) attention.dismissed.delete(key);
  const shown = shownWarnings(warnings, attention.dismissed);
  const level = shown.some((warning) => warning.level === "red") ? "red" : shown.length ? "yellow" : "";
  const label = [...new Set(shown.map((warning) => warning.label))].join(", ");
  if (level) document.body.dataset.attention = level;
  else delete document.body.dataset.attention;
  $("attention-label").textContent = label;
  for (const id of ["settings-button", "menu", "phone-history"]) $(id).title = label;
  $("attention-dismiss").hidden = !shown.some((warning) => warning.dismissible);
}
/** Record what one part of the page read (chatgpt, github, remote, approvals or proposals), then redraw. */
function noteAttention(part, value) {
  attention[part] = value;
  showAttention();
}
$("attention-dismiss").onclick = () => {
  for (const warning of shownWarnings(attentionWarnings(attention), attention.dismissed))
    if (warning.dismissible) attention.dismissed.add(warning.key);
  showAttention();
};
/** Open Settings at one part of Accounts and connections, as a foot row or a banner asks. */
async function openAccountItem(sectionId) {
  await $("settings-button").onclick();
  openSettingsSection($(sectionId));
  $(sectionId).scrollIntoView({ block: "start" });
}
for (const id of ["codex-status-row", "github-status-row", "remote-status-row"])
  $(id).onclick = () => openAccountItem($(id).dataset.section);
/** The ChatGPT account part says whether a turn has confirmed Codex access with this sign-in. */
function renderCodexAccess(access) {
  $("codex-access").textContent = access?.message || "";
  $("codex-access").hidden = !access?.message;
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
 * integration, and reload the chat options. polls.usage runs it every minute
 * while the page is shown. A failed check shows the quota as unavailable and
 * leaves the integration line as it is.
 */
async function loadUsage() {
  if (usageLoading) return;
  usageLoading = true;
  try {
    const data = await api("status");
    document.dispatchEvent(new CustomEvent("worker-status", { detail: data }));
    renderUsage(data.codex_usage, data.account_access?.plan_type);
    renderIntegration(data.integration);
    renderCodexAccess(data.account_access);
    chatgptAccount.status = data;
    renderChatgptDetails();
    // The quota check also tells the worker which model Codex picks when the add-on names none.
    // Not awaited: the quota shown must not depend on it.
    if (state.catalog) loadChatOptions(true);
  } catch (_) {
    renderUsage();
    renderCodexAccess({ message: "Codex access could not be checked." });
    // A failed read drops the plan and quota rows rather than keep stale ones.
    chatgptAccount.status = null;
    renderChatgptDetails();
  } finally {
    usageLoading = false;
  }
}
poll("usage", loadUsage, () => 60000);
/*
 * Settings sections start collapsed (add-on 0.3.2). Each section's first
 * heading becomes a button with the title and a one-line status; the rest of
 * the section opens under it on tap. A section that needs the owner says so
 * in its header, so nothing important hides in a closed section. One
 * top-level section is open at a time: opening one closes the others.
 */
function collapseSettingsSections() {
  // Sections marked data-own-dialog move to a dialog of their own (studio.js) and stay open there.
  // The parts of Accounts and connections collapse too, inside it, each under its own header.
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
/**
 * Open or close one settings section. Opening a part, such as GitHub, opens the section that holds it too.
 * Opening a top-level section closes the other top-level sections, so one is open at a time.
 */
function openSettingsSection(section, open = true) {
  const toggle = section.querySelector(":scope > :is(h3, h4) > .settings-toggle");
  const body = section.querySelector(":scope > .settings-body");
  if (!toggle || !body) return;
  toggle.setAttribute("aria-expanded", String(open));
  body.hidden = !open;
  const parent = section.parentElement?.closest("#settings section");
  if (open && parent) openSettingsSection(parent);
  if (open && !parent) closeOtherSettingsSections(section);
}
/** Close every top-level settings section except the one given. Parts inside a closed section keep their state. */
function closeOtherSettingsSections(keep) {
  for (const other of document.querySelectorAll("#settings > section")) {
    if (other !== keep) openSettingsSection(other, false);
  }
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
 * with a wrong clock still shows the right time. One poll, codeTimes, serves
 * every countdown on the page and ends when the last card has left it.
 */
const codeDeadlines = new Map();
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
  button.onclick = () => busyAction(button, getNewCode);
  const entry = { deadline: performance.now() + secondsLeft * 1000, button, kind };
  codeDeadlines.set(line, entry);
  showCodeTime(line, entry);
  if (!polls.codeTimes.waiting) polls.codeTimes.after(1000);
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
}
poll("codeTimes", showCodeTimes, () => (codeDeadlines.size ? 1000 : null), { whileHidden: true });
/** Replace a waiting ChatGPT sign-in with a new one, for a code that expired. */
async function newChatgptCode() {
  try {
    await api("auth/cancel", {});
    await api("auth/start", {});
    await polls.login.run();
  } catch (error) {
    if ($("settings").open) settingsError(error);
    else showError(error);
  }
}
// What the sign-in card shows now; the card is rebuilt only when this changes,
// so a code being read or a QR being scanned never jumps under the user's thumb.
let shownLogin = null;
/**
 * Build the sign-in card: the code in large letters that stay put until the
 * sign-in ends or is canceled, the link to the sign-in page, and the QR code
 * of the same page served by the worker. The link comes before the QR code, so
 * a phone shows it without a scroll. A phone cannot scan its own screen, so on
 * a phone-sized window the QR code starts folded behind "Show QR code".
 */
function signInCard(auth) {
  const card = document.createElement("div");
  card.className = "signin-card";
  // The sign-in link comes first, as the main button; then the code, its timer and the folded QR code.
  if (auth.verification_url) {
    const url = new URL(auth.verification_url, location.href);
    if (url.protocol === "https:") {
      const link = textNode("a", "Open the ChatGPT sign-in page", "signin-link");
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noreferrer";
      card.append(link);
    }
  }
  if (auth.user_code) {
    const code = textNode("p", auth.user_code, "signin-code");
    code.setAttribute(
      "aria-label",
      `Sign-in code ${auth.user_code.split("").join(" ")}`,
    );
    card.append(code, ...codeExpiry(auth.expires_in, newChatgptCode, "chatgpt"));
  } else {
    card.append(textNode("p", "Waiting for the sign-in code…", "muted"));
  }
  if (auth.qr_path) card.append(signInQr(auth.qr_path, "QR code that opens the ChatGPT sign-in page"));
  return card;
}
/**
 * The QR code of a sign-in page, served by the worker, in a fold. A phone
 * cannot scan its own screen, so on a phone-sized window the fold starts closed.
 */
function signInQr(src, alt) {
  const img = document.createElement("img");
  img.src = src;
  img.alt = alt;
  img.className = "signin-qr";
  img.width = img.height = 220;
  const fold = document.createElement("details");
  fold.className = "disclosure signin-qr-fold";
  fold.open = !matchMedia("(max-width:700px)").matches;
  fold.append(textNode("summary", "Show QR code"), img);
  return fold;
}
/**
 * Show the ChatGPT sign-in state in the settings dialog and, while the
 * assistant is not signed in, in the banner above the chat. polls.login
 * checks again every three seconds while a sign-in waits, every 30 seconds
 * while signed out, and every five minutes while signed in, so an expired
 * sign-in shows.
 */
async function loadLogin() {
  let auth;
  try {
    ({ auth } = await api("auth/status"));
  } catch (error) {
    setFootRow("codex-status-row", "error", "Unavailable");
    noteAttention("chatgpt", "unavailable");
    throw error;
  }
  renderLogin(auth);
  return auth;
}
/*
 * The ChatGPT sign-in details under its status line, like GitHub's: the sign-in
 * method the Codex CLI reports, the plan, OpenAI's last answer and the weekly
 * quota left. Two reads feed it (auth/status and status), so each keeps its last
 * answer here. A row shows only what the worker reported; no account name or
 * email is shown, because the worker does not send one.
 */
/** The sign-in method in the Codex CLI's "login status" line, or "" when it names none. */
function chatgptSignInMethod(message) {
  if (/\bChatGPT\b/i.test(message || "")) return "ChatGPT account";
  if (/\bAPI key\b/i.test(message || "")) return "API key";
  return "";
}
/** The rows of the ChatGPT details list while signed in; none while signed out. */
function chatgptDetailRows(auth, status) {
  if (!auth) return [];
  const rows = [];
  const method = chatgptSignInMethod(auth.codex_login?.message);
  if (method) rows.push(["Sign-in", method]);
  const plan = planName(status?.account_access?.plan_type);
  if (plan) rows.push(["Plan", plan]);
  // While signed in, OpenAI's last answer can only be "ok": a refusal signs the page out.
  const checked = auth.codex_login?.server_check === "ok" && dateLabel(auth.codex_login.server_checked_at, true);
  rows.push(["Last check", checked ? `${checked}: OpenAI accepted the sign-in` : "Not checked yet"]);
  // The worker sends the quota left as a number or a numeric string; any other value shows no row.
  const raw = status?.codex_usage?.weekly_percent;
  const weekly = typeof raw === "number" || (typeof raw === "string" && raw.trim()) ? Number(raw) : NaN;
  if (Number.isFinite(weekly) && weekly >= 0 && weekly <= 100) rows.push(["Weekly quota", `${weekly}% left`]);
  return rows;
}
/** Put the ChatGPT details rows on the Settings page. */
function renderChatgptDetails() {
  const rows = chatgptDetailRows(chatgptAccount.auth, chatgptAccount.status);
  const details = $("chatgpt-details");
  details.replaceChildren(...rows.flatMap(([term, value]) => [textNode("dt", term), textNode("dd", value)]));
  details.hidden = !rows.length;
}
poll(
  "login",
  loadLogin,
  (auth) => (auth?.pending ? 3000 : auth && !auth.codex_login?.status_ok ? 30000 : 300000),
  { whileHidden: true },
);
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
  noteAttention("chatgpt", signedIn ? "signed-in" : "signed-out");
  const sectionStatus = chatgptSectionStatus({ signedIn, pending, failed, lapsed });
  setSectionStatus("chatgpt-section", sectionStatus, !signedIn);
  setAccountPart("chatgpt-section", sectionStatus.toLowerCase(), !signedIn);
  setFootRow(
    "codex-status-row",
    signedIn ? "ready" : failed ? "error" : "needs-owner",
    signedIn ? "Ready" : pending ? "Signing in" : sectionStatus,
  );
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
  // Sign in shows only while signed out with no code waiting; Sign out only while a sign-in, even a lapsed one, is saved.
  for (const id of ["login", "banner-login"]) $(id).hidden = pending || signedIn;
  $("logout").hidden = !signedIn && !auth.codex_login?.has_auth_file;
  chatgptAccount.auth = signedIn ? auth : null;
  renderChatgptDetails();
  // While a sign-in waits, the section shows only the card and Cancel (CSS hides the rest).
  $("chatgpt-section").classList.toggle("signing-in", pending);
  for (const id of ["login-cancel", "banner-cancel"]) $(id).hidden = !pending;
  $("signin-banner").hidden = signedIn;
}
/*
 * GitHub sign-in (add-on 0.3). The worker holds the sign-in; this page only
 * shows its status and sends button presses. The device code is shown on the
 * page while a sign-in waits and nowhere else: it is never logged, never kept
 * in browser storage, and the pasted token is cleared from its field at once.
 */
let githubStatus = null;
let githubRevokeUrl = "";
const GITHUB_BAD_STATES = ["expired", "revoked", "access_changed", "failed"];
const GITHUB_STATE_TEXT = {
  disconnected: "Not connected to GitHub.",
  connecting: "Connecting. Enter the code below on GitHub.",
  ok: "Connected to GitHub.",
  near_expiry: "GitHub still works, but this sign-in expires within 7 days. Choose Sign in before then.",
  expired: "The GitHub sign-in expired. Choose Sign in.",
  revoked: "GitHub no longer accepts this sign-in. Choose Sign in. If needed, restore the App's repository access on GitHub.",
  access_changed: "The GitHub sign-in reaches different repositories or access than you connected.",
  failed: "The GitHub sign-in cannot be checked.",
};
// Sign in starts a GitHub App sign-in, so a token connection is sent to the token form instead.
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
/** The device-sign-in card: the code in large letters, GitHub's own page and its QR code. */
function githubFlowCard(flow) {
  const card = document.createElement("div");
  card.className = "signin-card github-flow-card";
  // The same order as the ChatGPT card: the link as the main button, then the code and its timer.
  const link = githubLink(flow.verification_uri || "https://github.com/login/device", "Open github.com/login/device");
  if (link) {
    link.className = "signin-link";
    card.append(link);
  }
  if (flow.user_code) {
    const code = textNode("p", flow.user_code, "signin-code");
    code.setAttribute("aria-label", `GitHub code ${flow.user_code.split("").join(" ")}`);
    card.append(code);
  } else {
    card.append(textNode("p", "Waiting for the GitHub code…", "muted"));
  }
  if (flow.user_code) {
    card.append(...codeExpiry(flow.expires_in, newGithubCode, "github"));
    // The query names this code's expiry, so a new code never shows the QR image of the last one.
    const qr = `github/qr.svg?flow=${encodeURIComponent(flow.expires_at || "")}`;
    card.append(signInQr(qr, "QR code that opens the GitHub sign-in page"));
  }
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
    polls.github.run().catch(() => {});
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
/** The GitHub row at the foot of the side bar: its dot and its words. */
function githubFootRow({ waiting, connected, bad, retry }) {
  if (connected) return bad ? ["needs-owner", "Needs attention"] : ["ready", "Ready"];
  if (waiting) return ["needs-owner", "Enter the code"];
  if (retry) return ["error", "Sign-in did not finish"];
  return ["off", "Not connected"];
}
/** Put the GitHub status on the card and the banner. Only the contract's fields are read. */
function renderGithub(github) {
  githubStatus = github;
  const state = String(github.state || "disconnected");
  noteAttention("github", state);
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
  // The App's Configure page on GitHub, where the owner adds repositories; a token reaches one repository by design.
  const addUrl = connected && kind === "app" ? String(github.add_repositories_url || "") : "";
  const addable = addUrl.startsWith("https://github.com/");
  $("github-add-repositories-link").href = addable ? addUrl : "#";
  $("github-add-repositories").hidden = !addable;
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
    // A failed new sign-in also shows its reason, and says that the stored sign-in did not change.
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
  $("github-connect").textContent = connected ? "Sign in" : retry ? "Get a new code" : "Connect GitHub";
  $("github-connect").hidden = waiting;
  // While a sign-in waits, the section shows only the card and Cancel (CSS hides the rest).
  $("github-section").classList.toggle("signing-in", waiting);
  $("github-cancel").hidden = !waiting;
  $("github-disconnect").hidden = !connected;
  $("github-accept").hidden = !(state === "access_changed" && !github.permission_problem);
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
  setFootRow("github-status-row", ...githubFootRow({ waiting, connected, bad, retry }));
  $("github-banner-text").textContent = `The add-on's GitHub sign-in needs attention: ${githubStateText(state, kind).replace(/\.$/, "")}.`;
  $("github-banner").hidden = !bad;
}
/** Show that this worker has no GitHub sign-in (an add-on before 0.3). */
function githubUnavailable() {
  githubStatus = null;
  noteAttention("github", "");
  $("github-status").textContent = "Not available in this version of the add-on.";
  for (const id of ["github-connect", "github-cancel", "github-accept", "github-disconnect"])
    $(id).hidden = true;
  $("github-details").hidden = $("github-repositories").hidden = $("github-local").hidden = true;
  $("github-add-repositories").hidden = true;
  $("github-token").hidden = true;
  $("github-banner").hidden = true;
  setSectionStatus("github-section", "Not available in this version");
  setAccountPart("github-section", "not available");
  setFootRow("github-status-row", "off", "Not available");
}
/**
 * Read the GitHub status and return whether a sign-in code waits. polls.github
 * reads it every 5 seconds while a code waits, else every 5 minutes, so a lost
 * sign-in shows.
 */
async function loadGithub() {
  try {
    const data = await api("github");
    if (!data.github) throw workerError("The worker returned no GitHub status.");
    $("github-token").hidden = false;
    renderGithub(data.github);
    return data.github.flow?.status === "waiting_for_user";
  } catch (error) {
    // A worker without the GitHub feature answers 404.
    if (error.status === 404) return githubUnavailable();
    setFootRow("github-status-row", "error", "Unavailable");
    throw error;
  }
}
poll("github", loadGithub, (waiting) => (waiting ? 5000 : 300000), { whileHidden: true });
function githubResult(text, isError) {
  const result = $("github-result");
  result.textContent = text || "";
  result.classList.toggle("error", Boolean(isError));
}
/** Run one GitHub button: disable it while the worker works, then show the outcome on the card. */
async function githubAction(button, path, body, done) {
  githubResult("");
  await busyAction(button, async () => {
    const data = await githubApi(path, body);
    if (done) done(data);
  }, (error) => githubResult(String(error.message || error), true));
  polls.github.run().catch(() => {});
}
$("github-connect").onclick = () => githubAction($("github-connect"), "github/connect", {});
$("github-cancel").onclick = () => githubAction($("github-cancel"), "github/cancel", {});
$("github-accept").onclick = () => githubAction($("github-accept"), "github/accept", {}, () =>
  githubResult("The current repositories are now the ones the check expects."));
$("github-disconnect").onclick = () => {
  const revoke = githubRevokeUrl || githubStatus?.revoke_url || "";
  if (!window.confirm("Sign out of GitHub? The assistant can no longer fetch, pull or push until you sign in again.")) return;
  githubAction($("github-disconnect"), "github/disconnect", {}, () => {
    const result = $("github-result");
    result.classList.remove("error");
    result.replaceChildren(textNode("span", "Sign out forgets the sign-in on this box. To revoke it on GitHub, open this link: "));
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
$("github-banner-open").onclick = () => openAccountItem("github-section");
let remoteCardKey = "";
/**
 * Show the remote-control state in the settings dialog: on or off, the
 * machine name, the pairing code as large text and a QR code, and the recent
 * messages. polls.remote checks again while the dialog is open: every five
 * seconds while remote control is on, else every 15 seconds. A failed check
 * ends the repeats.
 */
async function loadRemoteControl() {
  if (REMOTE_PAUSED) return;
  const { remote_control: rc } = await api("remote-control");
  renderRemoteControl(rc);
  return rc;
}
/** When to read the remote-control state next; null after a failed read or once the dialog is closed. */
function remoteDelay(rc) {
  if (!rc || !$("settings").open) return null;
  return rc.enabled ? 5000 : 15000;
}
poll("remote", loadRemoteControl, remoteDelay, { whileHidden: true, onError: settingsError });
/** Draw one remote-control state; the buttons follow what can be done now. */
function renderRemoteControl(rc) {
  state.remoteAvailable = Boolean(rc.available);
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
  $("remote-control-log").textContent = (rc.log || []).join("\n");
  $("remote-control-log-box").hidden = !(rc.log || []).length;
  document.dispatchEvent(new CustomEvent("remote-view", {detail: rc}));
}
const REMOTE_NAME_LOCKED = "Turn remote control off to change the name.";
/**
 * Show the configured machine name in its field. Codex reads the name when remote control starts, so the field
 * is locked while it is on and then shows the name in use. Polling never replaces a name the person is still
 * typing while it is off. The locked note goes when remote control is off; the result of a save stays.
 */
function renderRemoteControlName(rc) {
  const input = $("remote-control-name");
  const note = $("remote-control-name-note");
  if (rc.enabled) delete input.dataset.edited;
  if (!input.dataset.edited) input.value = rc.configured_name || "";
  input.disabled = Boolean(rc.enabled);
  $("remote-control-name-save").disabled = Boolean(rc.enabled);
  if (rc.enabled) note.textContent = REMOTE_NAME_LOCKED;
  else if (note.textContent === REMOTE_NAME_LOCKED) note.textContent = "";
}
$("remote-control-name").oninput = () => {
  $("remote-control-name").dataset.edited = "1";
};
/**
 * Save the name in the field to the one add-on option (Remote control machine name) the Configuration tab also
 * sets. True when it is saved; a failed save shows its error under the field.
 */
async function saveRemoteControlName() {
  const input = $("remote-control-name");
  $("remote-control-name-save").disabled = true;
  try {
    const data = await api("remote-control/name", { name: input.value });
    delete input.dataset.edited;
    renderRemoteControl(data.remote_control);
    $("remote-control-name-note").textContent = data.notice
      || `Saved. The ChatGPT app will show “${data.remote_control.configured_name}”.`;
    return true;
  } catch (error) {
    $("remote-control-name-note").textContent = error.message;
    $("remote-control-name-save").disabled = false;
    return false;
  }
}
$("remote-control-name-form").onsubmit = (event) => {
  event.preventDefault();
  saveRemoteControlName();
};
/** Turn on saves a typed name first, since Codex reads the name when it starts; a failed save keeps it off. */
async function remoteNameReadyForStart() {
  return !$("remote-control-name").dataset.edited || saveRemoteControlName();
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
  if (typeof data.url === "string" && data.url.startsWith("/config/app/")) {
    row.href = data.url;
    description.textContent = description.dataset.usual;
  } else {
    row.removeAttribute("href");
    description.textContent = ADDON_CONFIGURATION_UNKNOWN;
  }
}
for (const [id, action, prepare] of [
  ["remote-control-start", "start", remoteNameReadyForStart],
  ["remote-control-stop", "stop"],
  ["remote-control-pair", "pair"],
]) {
  $(id).onclick = async () => {
    await busyAction($(id), async () => {
      if (prepare && !(await prepare())) return;
      const data = await api(`remote-control/${action}`, {});
      renderRemoteControl(data.remote_control);
      $("settings-result").textContent = "";
    }, settingsError);
    polls.remote.run().catch(settingsError);
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
$("settings-button").onclick = async () => {
  sidebar(false);
  haTheme();
  $("settings").showModal();
  $("settings-result").textContent = "";
  $("agents").disabled = true;
  $("save-agents").disabled = true;
  const results = await Promise.allSettled([
    polls.login.run(),
    polls.github.run(),
    polls.remote.run(),
    loadAddonConfiguration(),
  ]);
  for (const result of results)
    if (result.status === "rejected") settingsError(result.reason);
};
$("close-settings").onclick = () => $("settings").close();
$("settings").addEventListener("close", () => {
  polls.login.stop();
  polls.remote.stop();
  $("github-result").textContent = "";
});
for (const [id, path] of [
  ["login", "auth/start"],
  ["banner-login", "auth/start"],
  ["login-cancel", "auth/cancel"],
  ["banner-cancel", "auth/cancel"],
  ["logout", "auth/logout"],
]) {
  $(id).onclick = () => busyAction($(id), async () => {
    await api(path, {});
    await polls.login.run();
    $("settings-result").textContent = "";
  }, (error) => {
    if ($("settings").open) settingsError(error);
    else showError(error);
  });
}
