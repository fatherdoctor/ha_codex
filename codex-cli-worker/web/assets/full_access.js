"use strict";
/**
 * The owner's Full access setting: the banner above every chat, the Full
 * access choice in a chat's permissions, and the Settings section that turns
 * it on (after the warning is confirmed) or off. The worker checks again that
 * the change comes from an administrator and that the warning was confirmed.
 */
let fullAccess = null;

/** Show the setting everywhere the page marks it. */
function renderFullAccess(setting) {
  if (!setting) return;
  // A new "on" time means the setting went off and on again, perhaps while this tab was asleep:
  // a choice made before that is not a choice for now. The worker checks the same (server.py).
  if (fullAccess && fullAccess.changed_at !== setting.changed_at) forgetFullAccessChoices();
  fullAccess = setting;
  const on = setting.enabled === true;
  document.body.dataset.fullAccess = on ? "on" : "off";
  $("full-access-banner").hidden = !on;
  $("full-access-state").textContent = on ? "On. Chats set to Full access run outside the sandbox." : "Off.";
  $("full-access-toggle").textContent = on ? "Turn off Full access" : "Turn on Full access";
  $("full-access-toggle").disabled = false;
  const option = document.querySelector('[data-mode="full-access"]');
  option.disabled = !on;
  $("full-access-option-text").textContent = on
    ? "Commands run outside the sandbox, with the app's full access."
    : "Off. An administrator can turn it on in Settings.";
  setSectionStatus("full-access-section", on ? "On" : "Off", on);
  if (!on) forgetFullAccessChoices();
  updateStudio();
}

/** The time the setting was turned on, as this page last saw it; null while it is off. */
function fullAccessSince() {
  return fullAccess?.enabled === true ? fullAccess.changed_at : null;
}

/**
 * Turning Full access off returns each chat set to it to Default; the worker does the
 * same for the saved chats. So turning it on again starts every chat at Default, and
 * the owner chooses Full access again in the chat's Permissions.
 */
function forgetFullAccessChoices() {
  for (const [id, mode] of state.permissionModes) {
    if (mode === "full-access") state.permissionModes.delete(id);
  }
  if (state.task?.permission_mode === "full-access") state.task.permission_mode = "default";
  state.newChatFullAccessSince = null;
}

/** Save the setting; turning it on sends the id of the warning the owner confirmed. */
async function saveFullAccess(enabled) {
  const result = $("full-access-result");
  $("full-access-toggle").disabled = true;
  try {
    const body = enabled ? { enabled, confirm: fullAccess.warning_id } : { enabled };
    const data = await api("full-access", body);
    renderFullAccess(data.full_access);
    result.textContent = enabled ? "Full access is on." : "Full access is off.";
  } catch (error) {
    result.textContent = error.message;
    $("full-access-toggle").disabled = false;
  }
}

$("full-access-toggle").onclick = () => {
  if (!fullAccess) return;
  if (fullAccess.enabled) {
    saveFullAccess(false);
    return;
  }
  $("full-access-warning").textContent = fullAccess.warning;
  $("full-access-dialog").showModal();
};
$("full-access-cancel").onclick = () => $("full-access-dialog").close();
$("full-access-confirm").onclick = () => {
  $("full-access-dialog").close();
  saveFullAccess(true);
};
document.addEventListener("worker-status", event => renderFullAccess(event.detail.full_access));
api("full-access").then(data => renderFullAccess(data.full_access)).catch(() => {});
