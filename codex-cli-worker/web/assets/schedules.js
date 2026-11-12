"use strict";
/** Manage owner-reviewed schedules and local messages without rendering stored HTML. */
(() => {
  const el = (id) => document.getElementById(id);
  const dialog = el("settings");
  let timer;
  let refreshing = false;
  function message(error) {
    el("schedule-result").textContent = String(error.message || error);
  }
  function node(tag, text, className) {
    const value = document.createElement(tag);
    value.textContent = text;
    if (className) value.className = className;
    return value;
  }
  function when(seconds) {
    return seconds == null ? "No next run" : new Date(seconds * 1000).toISOString().replace("T", " ").replace(".000Z", " UTC");
  }
  function button(label, action) {
    const value = node("button", label, "subtle");
    value.type = "button";
    value.onclick = async () => {
      value.disabled = true;
      try { await action(); await refresh(); }
      catch (error) { message(error); }
      finally { value.disabled = false; }
    };
    return value;
  }
  function describe(spec) {
    if (spec.kind === "interval") return `Every ${spec.seconds / 60} minutes`;
    if (spec.kind === "cron") return `Cron: ${spec.expression} (UTC)`;
    return `Once: ${when(spec.at)}`;
  }
  function reset() { el("schedule-form").reset(); el("schedule-id").value = ""; format(); }
  function edit(schedule) {
    el("schedule-id").value = schedule.id;
    el("schedule-name").value = schedule.name;
    el("schedule-prompt").value = schedule.prompt;
    el("schedule-mode").value = schedule.mode;
    const spec = schedule.spec;
    el("schedule-kind").value = spec.kind === "interval" ? "minutes" : spec.kind;
    el("schedule-value").value = spec.kind === "interval" ? spec.seconds / 60 : spec.kind === "cron" ? spec.expression : new Date(spec.at * 1000).toISOString().slice(0, 16);
    format(); el("schedule-name").focus();
  }
  function format() {
    const kind = el("schedule-kind").value;
    el("schedule-value-label").textContent = { minutes: "Minutes", hours: "Hours", cron: "Five cron fields", once: "Date and time in UTC" }[kind];
    el("schedule-value").type = kind === "once" ? "datetime-local" : "text";
    el("schedule-format-note").textContent = kind === "cron" ? "Numeric minute hour day month weekday. Lists, ranges and steps are supported; Sunday is 0 or 7." : kind === "once" ? "This date and time are UTC, not your device's local time." : "Use a whole number, at least 1.";
  }
  async function refresh() {
    if (refreshing || !dialog.open) return;
    refreshing = true;
    try {
      const data = await api("schedules");
      const list = el("schedule-list"); list.replaceChildren();
      for (const schedule of data.schedules) {
        const item = node("div", "");
        item.append(node("strong", schedule.name), node("p", `${schedule.approved ? schedule.enabled ? "On" : "Paused" : "Needs your approval"} · ${describe(schedule.spec)} · ${schedule.mode === "read-only" ? "Read only" : "Default"} · ${when(schedule.next_due)}`, "muted"));
        const prompt = node("details", ""); prompt.append(node("summary", "Review prompt"), node("pre", schedule.prompt)); item.append(prompt);
        const actions = node("div", "", "settings-actions");
        actions.append(button("Edit", () => edit(schedule)));
        const action = schedule.approved ? schedule.enabled ? "pause" : "resume" : "approve";
        actions.append(button({pause: "Pause", resume: "Resume", approve: "Approve this proposal"}[action], () => api(`schedules/${schedule.id}/${action}`, {revision: schedule.revision})));
        actions.append(button("Delete", () => api(`schedules/${schedule.id}/delete`, {})));
        item.append(actions); list.append(item);
      }
      if (!data.schedules.length) list.append(node("p", "No schedules saved.", "muted"));
      const runs = el("schedule-runs"); runs.replaceChildren();
      for (const run of data.runs) {
        const item = node("p", `${when(run.created)} · ${run.status.replaceAll("_", " ")}${run.error ? ` · ${run.error}` : ""} · `);
        const link = node("a", "Open chat"); link.href = "#"; link.onclick = async (event) => { event.preventDefault(); dialog.close(); await selectChat(run.task_id); }; item.append(link); runs.append(item);
      }
      if (!data.runs.length) runs.append(node("p", "No runs yet.", "muted"));
      el("lead-token-status").textContent = data.lead_token_configured ? "A lead token is configured." : "No lead token is configured.";
      const mailbox = el("schedule-mailbox"); mailbox.replaceChildren();
      for (const mail of data.mailbox) {
        const item = node("div", "");
        item.append(node("strong", `${mail.author} · ${when(mail.created)}${mail.parent ? " · reply" : ""}`), node("pre", mail.body));
        item.append(button("Reply", () => { el("schedule-mail-parent").value = mail.parent || mail.id; el("schedule-mail-target").textContent = "Reply to selected thread"; el("schedule-mail-body").focus(); })); mailbox.append(item);
      }
      if (!data.mailbox.length) mailbox.append(node("p", "No local messages.", "muted"));
    } catch (error) { message(error); }
    finally { refreshing = false; }
  }
  el("schedule-form").onsubmit = async (event) => {
    event.preventDefault();
    const kind = el("schedule-kind").value, value = el("schedule-value").value;
    let spec;
    if (kind === "minutes" || kind === "hours") {
      if (!Number.isInteger(Number(value)) || Number(value) < 1) { message("Use a positive whole number of minutes or hours."); return; }
      spec = {kind: "interval", seconds: Number(value) * (kind === "hours" ? 3600 : 60)};
    }
    else if (kind === "cron") spec = {kind, expression: value};
    else spec = {kind: "once", at: `${value}:00Z`};
    const data = {name: el("schedule-name").value, prompt: el("schedule-prompt").value, mode: el("schedule-mode").value, spec};
    const id = el("schedule-id").value;
    try { await api(id ? `schedules/${id}` : "schedules", data, id ? "PATCH" : "POST"); reset(); message("Schedule saved and approved."); await refresh(); }
    catch (error) { message(error); }
  };
  el("schedule-kind").onchange = format;
  el("schedule-reset").onclick = reset;
  el("lead-token-create").onclick = async () => {
    try {
      const data = await api("lead-token", {confirm: true});
      el("lead-token-value").value = data.token; el("lead-token-result").hidden = false;
      message("Copy this token now. It is cleared when you close settings."); await refresh();
    } catch (error) { message(error); }
  };
  function hideToken() { el("lead-token-value").value = ""; el("lead-token-result").hidden = true; }
  el("lead-token-hide").onclick = hideToken;
  el("lead-token-copy").onclick = async () => {
    try { await navigator.clipboard.writeText(el("lead-token-value").value); message("Token copied. Store it only in the other agent's private configuration."); }
    catch (error) { message(error); }
  };
  el("schedule-mail-reset").onclick = () => { el("schedule-mail-parent").value = ""; el("schedule-mail-target").textContent = "New message"; };
  el("schedule-mail-form").onsubmit = async (event) => {
    event.preventDefault();
    const parent = el("schedule-mail-parent").value;
    try { await api(parent ? `schedule-mailbox/${parent}/reply` : "schedule-mailbox", {body: el("schedule-mail-body").value}); el("schedule-mail-body").value = ""; message("Message saved locally."); await refresh(); }
    catch (error) { message(error); }
  };
  new MutationObserver(() => {
    clearInterval(timer);
    if (dialog.open) { refresh(); timer = setInterval(refresh, 15000); }
    else hideToken();
  }).observe(dialog, {attributes: true, attributeFilter: ["open"]});
  dialog.addEventListener("close", () => { clearInterval(timer); hideToken(); });
  format();
})();
