"use strict";
/** Production controls for the Quiet Studio layout. No sample account data. */
const permissionNames = {default: "Ask for approval", "auto-review": "Approve for me", "read-only": "Read only", "full-access": "Full access"};
function updateStudio() {
  document.body.dataset.newChat = String(!state.id);
  const hour = new Date().getHours();
  const greeting = $("greeting");
  if (greeting) greeting.textContent = hour < 12 ? "Good morning" : hour < 18 ? "Nice to see you again" : "Good evening";
  $("phone-title").textContent = state.task?.title || "New chat";
  const permissionLabel = permissionNames[selectedPermissionMode()];
  $("permission-label").textContent = permissionLabel;
  $("permission-button").setAttribute('aria-label', permissionLabel);
  $("permission-button").title = permissionLabel;
  $("permission-button").classList.toggle("full-access", selectedPermissionMode() === "full-access");
  $("permission-button").disabled = state.busy || state.loading || state.settingsBusy ||
    Boolean(state.task && ["queued", "running"].includes(state.task.status)) || Boolean(state.task?.queued_message);
  for (const choice of document.querySelectorAll("[data-mode]"))
    choice.setAttribute("aria-pressed", String(choice.dataset.mode === selectedPermissionMode()));
}
document.addEventListener("chat-view", updateStudio);

for (const button of document.querySelectorAll("[data-close]"))
  button.onclick = () => $(button.dataset.close).close();
$("phone-history").onclick = () => sidebar(true);
/** Put the permissions pop-up directly above its button, inside the viewport, on a phone too. */
function placePermissions() {
  const popup = $("permissions-dialog");
  const rect = $("permission-button").getBoundingClientRect();
  const width = Math.min(300, innerWidth - 16);
  popup.style.width = `${width}px`;
  popup.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - width - 8))}px`;
  popup.style.bottom = `${Math.max(8, innerHeight - rect.top + 8)}px`;
}
$("permission-button").onclick = () => {
  if ($("permissions-dialog").open) {
    $("permissions-dialog").close();
    return;
  }
  $("permission-result").textContent = "";
  // One menu at a time: the model and reasoning picker closes (chat.js).
  closePicker();
  updateStudio();
  placePermissions();
  $("permissions-dialog").show();
};
window.addEventListener("resize", () => {
  if ($("permissions-dialog").open) placePermissions();
});
for (const button of document.querySelectorAll("[data-mode]")) {
  button.onclick = async () => {
    if (state.settingsBusy) return;
    state.settingsBusy = true;
    for (const choice of document.querySelectorAll('[data-mode]')) choice.disabled = true;
    controls();
    const id = state.id;
    try {
      let mode = button.dataset.mode;
      if (id) {
        const data = await api(`tasks/${encodeURIComponent(id)}/settings`, {
          chat_settings: selectedSettings(), permission_mode: mode,
        });
        mode = data.permission_mode;
        if (id === state.id && state.task) state.task.permission_mode = mode;
      }
      state.permissionModes.set(id, mode);
      $("permissions-dialog").close();
      updateStudio();
    } catch (error) { $("permission-result").textContent = error.message; }
    finally {
      state.settingsBusy = false;
      // Full access stays unavailable while the owner's switch is off (full_access.js keeps the flag).
      for (const choice of document.querySelectorAll('[data-mode]'))
        choice.disabled = choice.dataset.mode === "full-access" && document.body.dataset.fullAccess !== "on";
      controls();
    }
  };
}
$("upload-photo").onclick = () => { $("plus-menu").hidden = true; $("file-input").click(); };
$("upload-document").onclick = () => { $("plus-menu").hidden = true; $("document-input").click(); };
$("document-input").onchange = async event => {
  const input = event.target;
  const files = [...input.files];
  for (const file of files) file.documentUpload = true;
  await acceptFiles(files);
  input.value = "";
};
document.addEventListener("pointerdown", event => {
  if (!$("plus-menu").contains(event.target) && !$("attach").contains(event.target)) $("plus-menu").hidden = true;
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") $("plus-menu").hidden = true;
  // The permissions pop-up is not modal, so the browser does not close it on Escape.
  if (event.key === "Escape" && $("permissions-dialog").open) {
    $("permissions-dialog").close();
    $("permission-button").focus();
  }
});
// The permissions pop-up closes on a press outside it (its button toggles it).
document.addEventListener("pointerdown", event => {
  const popup = $("permissions-dialog");
  if (popup.open && !popup.contains(event.target) && !$("permission-button").contains(event.target)) popup.close();
});

/** Dialogs where the person typed or changed a field since they opened; an outside click must not throw that away. */
const dirtyDialogs = new WeakSet();
for (const type of ["input", "change"])
  document.addEventListener(type, event => {
    if (event.isTrusted) event.target.closest?.("dialog") && dirtyDialogs.add(event.target.closest("dialog"));
  }, true);
document.addEventListener("close", event => dirtyDialogs.delete(event.target), true);
const dialogHasInput = dialog => dirtyDialogs.has(dialog);
// A click or tap on the dim area behind a modal dialog closes it, like Escape. A dialog whose fields were
// changed stays open, so nothing typed is lost. A press that starts inside the dialog (a text selection
// that ends outside it) never closes it.
let dialogPressStart = null;
document.addEventListener("pointerdown", event => {
  dialogPressStart = event.target;
}, true);
document.addEventListener("click", event => {
  const dialog = event.target;
  if (!(dialog instanceof HTMLDialogElement) || !dialog.open || dialog.id === "permissions-dialog") return;
  if (dialogPressStart !== dialog) return;
  const box = dialog.getBoundingClientRect();
  const inside = event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
  if (inside || dialogHasInput(dialog)) return;
  if (dialog.dispatchEvent(new Event("cancel", {cancelable: true}))) dialog.close();
});

// Keep code/QR and the remote actions on a separate page, not inside Settings.
$("connection-content").append($("remote-control-section"));
const retryConnection = textNode("button", "Retry connection", "subtle");
retryConnection.type = "button";
retryConnection.hidden = true;
retryConnection.onclick = async () => {
  retryConnection.disabled = true;
  try {
    await api("remote-control/stop", {});
    const data = await api("remote-control/start", {});
    renderRemoteControl(data.remote_control);
  } catch (error) { $("connection-result").textContent = error.message; }
  finally { retryConnection.disabled = false; }
};
$("connection-content").append(retryConnection);
let connectionTimer;
let lastRemoteState = null;
function connectionLabel(rc) {
  if (!rc.available) return {state: "error", text: rc.blocked_reason || "Unavailable"};
  if (!rc.enabled) return {state: "off", text: "Off"};
  if (rc.state === "starting") return {state: "starting", text: "Starting"};
  if (["ready", "pairing"].includes(rc.state))
    return {state: "ready", text: `Ready as ${rc.machine_name || rc.configured_name || "Home Assistant"}`};
  if (rc.state === "reconnecting") return {state: "reconnecting", text: "Reconnecting"};
  return {state: "error", text: rc.message || "Connection error"};
}
/** The Remote row's dot and words. A version that cannot turn remote control on shows a gray dot. */
function remoteFootRow(rc, label) {
  if (!rc.available) return ["off", "Unavailable"];
  if (label.state === "ready") return ["ready", "Ready"];
  if (label.state === "reconnecting") return ["needs-owner", "Reconnecting"];
  if (label.state === "error") return ["error", "Connection error"];
  return [label.state, label.text];
}
function renderStudioRemote(rc) {
  lastRemoteState = rc;
  const label = connectionLabel(rc);
  $("remote-control-pair").disabled = label.state !== "ready";
  retryConnection.hidden = label.state !== "error" || !rc.enabled || !rc.available;
  // The Remote row at the foot of the side bar keeps to a word or two; its tooltip and the window's title say more.
  setFootRow("remote-status-row", ...remoteFootRow(rc, label));
  // Only an error while the owner's switch is on counts as a sign-out; Off is the owner's choice.
  noteAttention("remote", Boolean(rc.available && rc.enabled && label.state === "error"));
  $("remote-status-row").title = `Remote · ${label.text}`;
  document.body.dataset.connection = label.state;
  $("connection-title").textContent = `Remote connection · ${label.text}`;
  const card = $("remote-control-card");
  let pairingURL = null;
  try {
    const url = new URL(rc.pairing_url);
    if (url.protocol === "https:" && url.hostname === "chatgpt.com" &&
        url.pathname === "/codex/pair" && url.searchParams.get("pairing_code")) pairingURL = url.href;
  } catch (_) { /* Missing opaque pairing code: do not invent an app link. */ }
  if (rc.pairing_code && !card.querySelector("#copy-pairing-code")) {
    if (pairingURL) {
      const link = textNode("a", "Continue in ChatGPT", "pairing-continue");
      link.href = pairingURL;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      card.prepend(link);
    } else {
      card.querySelector("img")?.remove();
      card.append(textNode("p", "Use the manual code, or request a new code for a QR and app link.", "muted"));
    }
    const copy = textNode("button", "Copy code", "subtle");
    copy.type = "button";
    copy.id = "copy-pairing-code";
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(rc.pairing_code); copy.textContent = "Copied"; }
      catch (_) {
        const code = card.querySelector(".pairing-code");
        const range = document.createRange();
        range.selectNodeContents(code);
        const selection = window.getSelection();
        selection.removeAllRanges(); selection.addRange(range);
        copy.textContent = "Code selected — use Copy";
      }
    };
    card.append(copy);
  }
};
document.addEventListener("remote-view", event => renderStudioRemote(event.detail));
async function pollConnection() {
  clearTimeout(connectionTimer);
  if (REMOTE_PAUSED) return;
  try { await loadRemoteControl(); $("connection-result").textContent = ""; }
  catch (error) {
    $("connection-result").textContent = error.message;
    setFootRow("remote-status-row", "error", "Unavailable");
  }
  connectionTimer = setTimeout(pollConnection, $("connection-dialog").open ? 5000 : 15000);
}
function openConnection() {
  if (REMOTE_PAUSED) return;
  if ($("settings").open) $("settings").close();
  if (!$("connection-dialog").open) $("connection-dialog").showModal();
  pollConnection();
}
// The ChatGPT app pairing part of Accounts and connections in Settings opens the same window.
$("remote-connection-open").onclick = openConnection;
// Back returns to Settings, where the window was opened from; the X closes the window alone.
$("connection-back").onclick = () => {
  $("connection-dialog").close();
  $("settings-button").click();
};
$("app-pairing-new").onclick = () => {
  openConnection();
  $("remote-control-pair").click();
};
$("connection-dialog").addEventListener("close", () => polls.remote.stop());

// Workspace rules are loaded only when Workspace instructions in Settings opens their dedicated editor.
$("workspace-open").onclick = async () => {
  $("settings").close();
  $("workspace-dialog").showModal();
  $("agents").disabled = $("save-agents").disabled = true;
  $("workspace-result").textContent = "Loading…";
  try {
    const data = await api("agents");
    $("agents").value = data.content || "";
    $("agents").disabled = $("save-agents").disabled = false;
    $("workspace-result").textContent = "";
  } catch (error) { $("workspace-result").textContent = error.message; }
};
$("save-agents").onclick = async () => {
  $("save-agents").disabled = true;
  try {
    await api("agents", {content: $("agents").value});
    $("workspace-result").textContent = "Instructions saved.";
  } catch (error) { $("workspace-result").textContent = error.message; }
  finally { $("save-agents").disabled = false; }
};

// A missing AGENTS.md is a setup hint above the chat, shown until it is dismissed in this browser.
const AGENTS_HINT_KEY = "codex-agents-hint-dismissed";
function agentsHintDismissed() {
  try { return Boolean(localStorage.getItem(AGENTS_HINT_KEY)); } catch (_error) { return false; }
}
async function loadAgentsHint() {
  if (agentsHintDismissed()) return;
  try { $("agents-hint").hidden = (await api("agents")).exists !== false; }
  catch (_error) { $("agents-hint").hidden = true; }
}
$("agents-hint-dismiss").onclick = () => {
  $("agents-hint").hidden = true;
  try { localStorage.setItem(AGENTS_HINT_KEY, "1"); } catch (_error) { /* The hint returns on the next visit. */ }
};
$("agents-hint-open").onclick = () => {
  $("agents-hint").hidden = true;
  $("workspace-open").click();
};
loadAgentsHint();

document.addEventListener("thread-action", async event => {
  const {action, id} = event.detail;
  if (action === "archive") {
    try {
      const chat = chatById(id) || (id === state.id ? state.task : null);
      await api(`tasks/${encodeURIComponent(id)}/archive`, {archived: !chat?.archived});
      if (id === state.id) await selectChat(null);
      await loadList();
    } catch (error) { showError(error); }
  }
});
$("archive-list").onclick = async () => {
  state.archived = !state.archived;
  state.chats = []; state.next = null;
  $("archive-list").textContent = state.archived ? "Recent chats" : "Archived chats";
  try { await loadList(); } catch (error) { sidebarError(error); }
};

/** Follow Home Assistant's theme now and whenever it changes (haTheme() is in chat-core.js). */
haTheme();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", haTheme);
try {
  if (parent !== window) {
    const observer = new MutationObserver(haTheme);
    observer.observe(parent.document.documentElement, {attributes:true, attributeFilter:["style", "class"]});
    const host = parent.document.querySelector("home-assistant");
    if (host) observer.observe(host, {attributes:true, attributeFilter:["style", "class"]});
  }
} catch (_) { /* Same fallback as initial theme selection. */ }
window.addEventListener("resize", updateStudio);
updateStudio();
pollConnection();
