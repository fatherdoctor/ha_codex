"use strict";
/** Production controls for the Quiet Studio layout. No sample account data. */
const permissionNames = {default: "Default permissions", "read-only": "Read only"};
function updateStudio() {
  document.body.dataset.newChat = String(!state.id);
  const hour = new Date().getHours();
  const greeting = $("greeting");
  if (greeting) greeting.textContent = hour < 12 ? "Good morning" : hour < 18 ? "Nice to see you again" : "Good evening";
  $("phone-title").textContent = state.task?.title || "New chat";
  const permissionLabel = permissionNames[selectedPermissionMode()];
  $("permission-button").textContent = (innerWidth <= 700 && selectedPermissionMode() === 'default'
    ? 'Default' : permissionLabel) + '⌄';
  $("permission-button").setAttribute('aria-label', permissionLabel);
  $("permission-button").title = permissionLabel;
  $("phone-new").disabled = state.busy || state.settingsBusy;
  $("permission-button").disabled = state.busy || state.loading || state.settingsBusy ||
    Boolean(state.task && ["queued", "running"].includes(state.task.status)) || Boolean(state.task?.queued_message);
  for (const choice of document.querySelectorAll("[data-mode]"))
    choice.setAttribute("aria-pressed", String(choice.dataset.mode === selectedPermissionMode()));
}
document.addEventListener("chat-view", updateStudio);

for (const button of document.querySelectorAll("[data-close]"))
  button.onclick = () => $(button.dataset.close).close();
$("header-settings").onclick = () => $("settings-button").click();
$("phone-history").onclick = () => sidebar(true);
$("phone-new").onclick = () => selectChat(null);
$("permission-button").onclick = () => {
  $("permission-result").textContent = "";
  updateStudio();
  $("permissions-dialog").showModal();
};
for (const button of document.querySelectorAll("[data-mode]")) {
  button.onclick = async () => {
    button.disabled = true;
    const id = state.id;
    try {
      let mode = button.dataset.mode;
      if (id) {
        const data = await api(`tasks/${encodeURIComponent(id)}/settings`, {
          chat_settings: selectedSettings(), permission_mode: mode,
        });
        mode = data.permission_mode;
        if (state.task) state.task.permission_mode = mode;
      }
      state.permissionModes.set(id, mode);
      $("permissions-dialog").close();
      updateStudio();
    } catch (error) { $("permission-result").textContent = error.message; }
    finally { button.disabled = false; }
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
const remoteLink = textNode("button", "Manage remote connection", "subtle");
remoteLink.type = "button";
$("settings").querySelector("section").after(remoteLink);
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
function renderStudioRemote(rc) {
  lastRemoteState = rc;
  const label = connectionLabel(rc);
  $("remote-control-pair").disabled = label.state !== "ready";
  retryConnection.hidden = label.state !== "error" || !rc.enabled || !rc.available;
  $("connection-summary").textContent = label.text;
  $("connection-chip").textContent = `Home Assistant · ${label.text}`;
  document.body.dataset.connection = label.state;
  $("strip-pair").hidden = label.state !== "ready";
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
  try { await loadRemoteControl(); $("connection-result").textContent = ""; }
  catch (error) { $("connection-result").textContent = error.message; }
  connectionTimer = setTimeout(pollConnection, $("connection-dialog").open ? 5000 : 15000);
}
function openConnection() {
  if ($("settings").open) $("settings").close();
  if (!$("connection-dialog").open) $("connection-dialog").showModal();
  pollConnection();
}
for (const id of ["connection-chip", "connection-manage"])
  $(id).onclick = openConnection;
$("strip-pair").onclick = () => {
  openConnection();
  $("remote-control-pair").click();
};
remoteLink.onclick = openConnection;
$("connection-dialog").addEventListener("close", () => clearTimeout(remoteTimer));

// Workspace rules are loaded only when Advanced opens their dedicated editor.
$("workspace-content").append($("workspace-section"));
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

function showStudioStatus() {
  const details = $("status-details");
  details.replaceChildren();
  for (const [name, value] of [
    ["Thread", state.task?.session_id || "Unavailable"], ["Workspace", "/config"],
    ["Model", modelLabel()], ["Permissions", permissionNames[selectedPermissionMode()]],
    ["Context", "Unavailable"], ["Usage and reset", "Unavailable"],
    ["Connection", lastRemoteState ? connectionLabel(lastRemoteState).text : "Unavailable"],
  ]) details.append(textNode("dt", name), textNode("dd", value));
  $("status-dialog").showModal();
}
for (const id of ["thread-actions", "phone-thread-actions"])
  $(id).onclick = event => openChatMenu(state.id, event.currentTarget);
document.addEventListener("thread-action", async event => {
  const {action, id} = event.detail;
  if (action === "status") showStudioStatus();
  else if (action === "history") sidebar(true);
  else if (action === "new-chat") await selectChat(null);
  else if (action === "copy-id") {
    try {
      const task = id === state.id ? state.task : (await api(`tasks/${encodeURIComponent(id)}`)).task;
      if (!task?.session_id) throw new Error("This conversation has no native thread ID yet.");
      await copyResponseText(task.session_id);
    } catch (error) { showError(error); }
  } else if (action === "archive") {
    try {
      await api(`tasks/${encodeURIComponent(id)}/archive`, {archived: !chatById(id)?.archived});
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

/** Copy HA's exposed theme tokens across the iframe boundary when permitted. */
const themeTokens = ["primary-color", "primary-background-color", "secondary-background-color", "card-background-color",
  "primary-text-color", "secondary-text-color", "divider-color", "error-color", "text-light-primary-color"];
function followTheme() {
  let dark = matchMedia("(prefers-color-scheme: dark)").matches;
  try {
    if (parent !== window) {
      const host = parent.document.querySelector("home-assistant") || parent.document.documentElement;
      const style = parent.getComputedStyle(host);
      if (typeof host.hass?.themes?.darkMode === "boolean") dark = host.hass.themes.darkMode;
      else if (style.colorScheme === "dark") dark = true;
      else if (style.colorScheme === "light") dark = false;
      for (const token of themeTokens) {
        const value = style.getPropertyValue("--" + token).trim();
        if (value) document.documentElement.style.setProperty("--" + token, value);
        else document.documentElement.style.removeProperty("--" + token);
      }
    }
  } catch (_) { /* Cross-origin hosts use standalone system-theme fallbacks. */ }
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}
followTheme();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", followTheme);
try {
  if (parent !== window) {
    const observer = new MutationObserver(followTheme);
    observer.observe(parent.document.documentElement, {attributes:true, attributeFilter:["style", "class"]});
    const host = parent.document.querySelector("home-assistant");
    if (host) observer.observe(host, {attributes:true, attributeFilter:["style", "class"]});
  }
} catch (_) { /* Same fallback as initial theme selection. */ }
window.addEventListener("resize", updateStudio);
updateStudio();
pollConnection();
