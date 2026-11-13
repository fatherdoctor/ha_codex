"use strict";
/** Manage owner-reviewed schedules and local messages without rendering stored HTML. */
(() => {
  const el = (id) => document.getElementById(id);
  const dialog = el("settings");
  let timer;
  let zone = null;
  let refreshing = false;
  let clearConfirmation = false;
  let focusClearCancel = false;
  function message(error) {
    el("schedule-result").textContent = String(error.message || error);
  }
  function node(tag, text, className) {
    const value = document.createElement(tag);
    value.textContent = text;
    if (className) value.className = className;
    return value;
  }
  function localParts(seconds) {
    const parts = new Intl.DateTimeFormat("en-CA", {timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).formatToParts(new Date(seconds * 1000));
    const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}`;
  }
  function when(seconds) {
    return seconds == null ? "No next run" : zone ? `${localParts(seconds).replace("T", " ")} (${zone})` : "Home Assistant time zone unavailable";
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
  function cancelMailClear() { clearConfirmation = false; focusClearCancel = false; }
  function describe(spec) {
    if (spec.kind === "interval") return `Every ${spec.seconds / 60} minutes`;
    if (spec.kind === "cron") return `Cron: ${spec.expression} (${zone || "time zone unavailable"})`;
    return `Once: ${when(spec.at)}`;
  }
  function reset() { el("schedule-form").reset(); el("schedule-id").value = ""; format(); }
  function edit(schedule) {
    if (schedule.prompt_redacted || !zone) { message("This prompt is masked or the time zone is unavailable. Create a new schedule with a complete prompt."); return; }
    el("schedule-id").value = schedule.id;
    el("schedule-name").value = schedule.name;
    el("schedule-prompt").value = schedule.prompt;
    el("schedule-mode").value = schedule.mode;
    const spec = schedule.spec;
    el("schedule-kind").value = spec.kind === "interval" ? "minutes" : spec.kind;
    el("schedule-value").value = spec.kind === "interval" ? spec.seconds / 60 : spec.kind === "cron" ? spec.expression : localParts(spec.at);
    format(); el("schedule-name").focus();
  }
  function format() {
    const kind = el("schedule-kind").value;
    el("schedule-value-label").textContent = { minutes: "Minutes", hours: "Hours", cron: "Five cron fields", once: `Date and time in ${zone || "Home Assistant"}` }[kind];
    el("schedule-value").type = kind === "once" ? "datetime-local" : "text";
    el("schedule-format-note").textContent = kind === "cron" ? `Numeric minute hour day month weekday in ${zone || "Home Assistant time"}. Missing clock-change minutes are skipped; repeated minutes run once, at the first occurrence.` : kind === "once" ? `Use ${zone || "Home Assistant time"}. A repeated time uses its first occurrence; a nonexistent time is refused.` : "Use a whole number, at least 1.";
  }
  async function refresh() {
    if (refreshing || !dialog.open) return;
    refreshing = true;
    try {
      const data = await api("schedules");
      zone = data.timezone || null; format();
      el("schedule-timezone").textContent = zone ? `Home Assistant time zone: ${zone}` : data.timezone_error;
      const list = el("schedule-list"); list.replaceChildren();
      for (const schedule of data.schedules) {
        const item = node("div", "");
        item.append(node("strong", schedule.name), node("p", `${schedule.approved ? schedule.enabled ? "On" : "Paused" : "Needs your approval"} · ${describe(schedule.spec)} · ${schedule.mode === "read-only" ? "Read only" : "Default"} · ${when(schedule.next_due)}`, "muted"));
        const prompt = node("details", ""); prompt.append(node("summary", "Review prompt"), node("pre", schedule.prompt)); item.append(prompt);
        const actions = node("div", "", "settings-actions");
        const editButton = button("Edit", () => edit(schedule));
        editButton.disabled = Boolean(schedule.prompt_redacted || !zone);
        if (schedule.prompt_redacted) item.append(node("p", "Prompt contains masked credentials. Create a replacement with a complete prompt; editing is disabled.", "muted"));
        actions.append(editButton);
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
      if (data.mailbox.length) {
        if (clearConfirmation) {
          const confirmation = node("div", "");
          confirmation.id = "schedule-mail-clear-confirmation";
          confirmation.setAttribute("role", "group");
          confirmation.setAttribute("aria-label", "Clear mailbox confirmation");
          confirmation.append(node("p", "Delete all mailbox threads and replies? This cannot be undone."));
          const actions = node("div", "", "settings-actions");
          const cancel = button("Cancel", cancelMailClear);
          actions.append(cancel, button("Clear all messages", async () => {
            await api("schedule-mailbox", undefined, "DELETE");
            cancelMailClear();
            el("schedule-mail-parent").value = "";
            el("schedule-mail-target").textContent = "New message";
          }));
          confirmation.append(actions); mailbox.append(confirmation);
          if (focusClearCancel) { cancel.focus(); focusClearCancel = false; }
        } else {
          mailbox.append(button("Clear mailbox", () => { clearConfirmation = true; focusClearCancel = true; }));
        }
      } else cancelMailClear();
      for (const mail of data.mailbox) {
        const item = node("div", "");
        item.append(node("strong", `${mail.author} · ${when(mail.created)}${mail.parent ? " · reply" : ""}`), node("pre", mail.body));
        item.append(button("Reply", () => { el("schedule-mail-parent").value = mail.parent || mail.id; el("schedule-mail-target").textContent = "Reply to selected thread"; el("schedule-mail-body").focus(); }));
        if (!mail.parent) item.append(button("Delete thread", () => api(`schedule-mailbox/${mail.id}`, undefined, "DELETE")));
        mailbox.append(item);
      }
      if (!data.mailbox.length) mailbox.append(node("p", "No local messages.", "muted"));
      if (dialog.open && data.mailbox.length) {
        await api("schedule-mailbox/read", {ids: data.mailbox.map(mail => mail.id)});
      }
    } catch (error) { message(error); }
    finally { refreshing = false; }
  }
  el("schedule-form").onsubmit = async (event) => {
    event.preventDefault();
    if (!zone) { message("Home Assistant time zone unavailable; schedule entry is paused."); return; }
    const kind = el("schedule-kind").value, value = el("schedule-value").value;
    let spec;
    if (kind === "minutes" || kind === "hours") {
      if (!Number.isInteger(Number(value)) || Number(value) < 1) { message("Use a positive whole number of minutes or hours."); return; }
      spec = {kind: "interval", seconds: Number(value) * (kind === "hours" ? 3600 : 60)};
    }
    else if (kind === "cron") spec = {kind, expression: value};
    else spec = {kind: "once", at: `${value}:00`};
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
    else { hideToken(); cancelMailClear(); }
  }).observe(dialog, {attributes: true, attributeFilter: ["open"]});
  dialog.addEventListener("close", () => { clearInterval(timer); hideToken(); cancelMailClear(); });
  format();
})();
