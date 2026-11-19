"use strict";
/**
 * Schedules and local messages, without rendering stored HTML.
 *
 * A schedule is made and edited in one window, the schedule editor, opened by
 * Schedule in a chat's menu. Settings keeps the list of every schedule, where
 * an agent's proposal is approved, and the local mailbox.
 */
(() => {
  const el = (id) => document.getElementById(id);
  const dialog = el("settings");
  const editor = el("schedule-dialog");
  /** False while scheduled runs are switched off: the page then has no schedule markup at all. */
  const scheduleUi = Boolean(editor);
  /** The newest answer of the schedules API: every schedule and the recent runs. */
  let latest = { schedules: [], runs: [] };
  let timer;
  let reader = "owner";
  const selector = node("select", ""); selector.id = "schedule-mail-reader";
  selector.setAttribute("aria-label", "Mailbox");
  for (const role of ["owner", "lead", "codex"]) {
    const option = node("option", role); option.value = role; selector.append(option);
  }
  selector.onchange = () => { reader = selector.value; cancelMailClear(); refresh(); };
  el("schedule-mailbox").before(selector);
  const recipient = node("select", ""); recipient.id = "schedule-mail-to";
  recipient.setAttribute("aria-label", "To");
  for (const role of ["all", "owner", "lead", "codex"]) {
    const option = node("option", role); option.value = role; recipient.append(option);
  }
  const due = node("input", ""); due.id = "schedule-mail-due"; due.type = "text";
  due.placeholder = "Optional ISO 8601 due time, including zone";
  due.setAttribute("aria-label", "Due (optional, include time zone)");
  el("schedule-mail-body").before(node("label", "To"), recipient, node("label", "Due (optional)"), due);
  const mailboxPath = path => `${path}?mailbox=${reader}`;
  function mailWhen(value) {
    return `${new Intl.DateTimeFormat(undefined, {timeZone: zone || "UTC", dateStyle: "medium", timeStyle: "medium"}).format(new Date(value))} (${zone || "UTC"})`;
  }
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
  const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  function describe(spec) {
    const shown = triggerOf(spec), place = zone || "time zone unavailable";
    if (shown.trigger === "hourly") return "Every hour";
    if (shown.trigger === "daily") return `Every day at ${shown.time} (${place})`;
    if (shown.trigger === "weekly") return `Every ${WEEKDAYS[shown.weekday]} at ${shown.time} (${place})`;
    if (spec.kind === "interval") return `Every ${spec.seconds / 60} minutes`;
    if (spec.kind === "cron") return `Cron: ${spec.expression} (${place})`;
    return `Once: ${when(spec.at)}`;
  }
  /** Read every schedule and the recent runs, and learn Home Assistant's time zone. */
  async function load() {
    latest = await api(mailboxPath("schedules"));
    zone = latest.timezone || null;
    return latest;
  }
  /** The schedule a chat belongs to: the one made from it, or else the one that started it. */
  function scheduleOfChat(id) {
    const made = latest.schedules.find((schedule) => schedule.chat_id === id);
    if (made) return made;
    const run = latest.runs.find((item) => item.task_id === id && item.schedule_id);
    return run ? latest.schedules.find((schedule) => schedule.id === run.schedule_id) : undefined;
  }
  function editorError(error) {
    el("schedule-error").textContent = error ? String(error.message || error) : "";
    el("schedule-error").hidden = !error;
  }
  /**
   * How the editor shows a saved timing. Hourly, Daily and Weekly are names
   * for an interval of one hour and for two simple cron lines; any other
   * timing is Custom and keeps its own fields.
   */
  function triggerOf(spec) {
    const shown = { trigger: "custom", weekday: "1", time: "09:00", kind: "minutes", value: "60" };
    if (spec.kind === "interval" && spec.seconds === 3600) return { ...shown, trigger: "hourly" };
    if (spec.kind === "interval") {
      const hours = spec.seconds % 3600 === 0;
      return { ...shown, kind: hours ? "hours" : "minutes", value: String(spec.seconds / (hours ? 3600 : 60)) };
    }
    if (spec.kind === "once") return { ...shown, kind: "once", value: localParts(spec.at) };
    const clock = /^(\d{1,2}) (\d{1,2}) \* \* (\*|[0-7])$/.exec(spec.expression);
    if (!clock) return { ...shown, kind: "cron", value: spec.expression };
    const time = `${clock[2].padStart(2, "0")}:${clock[1].padStart(2, "0")}`;
    if (clock[3] === "*") return { ...shown, trigger: "daily", time };
    return { ...shown, trigger: "weekly", time, weekday: String(Number(clock[3]) % 7) };
  }
  /** The timing the editor's fields describe, as the schedules API takes it. */
  function specOfForm() {
    const trigger = el("schedule-trigger").value;
    if (trigger === "hourly") return { kind: "interval", seconds: 3600 };
    if (trigger === "daily" || trigger === "weekly") {
      const clock = /^(\d\d):(\d\d)$/.exec(el("schedule-time").value);
      if (!clock) throw new Error("Choose a time of day.");
      const weekday = trigger === "weekly" ? el("schedule-weekday").value : "*";
      return { kind: "cron", expression: `${Number(clock[2])} ${Number(clock[1])} * * ${weekday}` };
    }
    const kind = el("schedule-kind").value, value = el("schedule-value").value.trim();
    if (kind === "cron") return { kind, expression: value };
    if (kind === "once") {
      if (!value) throw new Error("Choose a date and time.");
      return { kind, at: `${value}:00` };
    }
    if (!Number.isInteger(Number(value)) || Number(value) < 1) throw new Error("Use a positive whole number of minutes or hours.");
    return { kind: "interval", seconds: Number(value) * (kind === "hours" ? 3600 : 60) };
  }
  /** Show only the fields of the chosen trigger, and one line that says when it runs. */
  function showTrigger() {
    const trigger = el("schedule-trigger").value, kind = el("schedule-kind").value;
    const place = zone || "Home Assistant time";
    el("schedule-weekday-field").hidden = trigger !== "weekly";
    el("schedule-time-field").hidden = trigger !== "daily" && trigger !== "weekly";
    el("schedule-custom").hidden = trigger !== "custom";
    el("schedule-value-label").textContent = { minutes: "Minutes", hours: "Hours", cron: "Five cron fields", once: `Date and time in ${zone || "Home Assistant"}` }[kind];
    el("schedule-value").type = kind === "once" ? "datetime-local" : "text";
    const customNote = kind === "cron" ? `Numeric minute hour day month weekday in ${place}. Missing clock-change minutes are skipped; repeated minutes run once, at the first occurrence.` : kind === "once" ? `Use ${place}. A repeated time uses its first occurrence; a nonexistent time is refused.` : "Use a whole number, at least 1.";
    el("schedule-format-note").textContent = {
      hourly: "Runs every hour, counted from the time you save.",
      daily: `Runs every day at this time in ${place}.`,
      weekly: `Runs every week on this day and time in ${place}.`,
      custom: customNote,
    }[trigger];
  }
  /** List the add-on's own model and the models the chat page offers; keep a saved model that is no longer offered. */
  function showModels(selected) {
    const list = el("schedule-model");
    list.replaceChildren();
    const models = [{ id: "", label: "Add-on default" }, ...(state.catalog?.models || [])];
    if (selected && !models.some((model) => model.id === selected)) models.push({ id: selected, label: selected });
    for (const model of models) {
      const option = node("option", model.label); option.value = model.id; list.append(option);
    }
    list.value = selected || "";
  }
  /** The part of Advanced settings a saved schedule has: its state, pause or approval, delete, and its runs. */
  function showSaved(schedule) {
    el("schedule-saved").hidden = !schedule;
    if (!schedule) return;
    const action = schedule.approved ? schedule.enabled ? "pause" : "resume" : "approve";
    el("schedule-state").textContent = `${schedule.approved ? schedule.enabled ? "On" : "Paused" : "Needs your approval"} · ${describe(schedule.spec)} · ${when(schedule.next_due)}`;
    el("schedule-toggle").textContent = { pause: "Pause", resume: "Resume", approve: "Approve this proposal" }[action];
    el("schedule-toggle").dataset.action = action;
    const runs = el("schedule-own-runs"); runs.replaceChildren();
    for (const run of latest.runs.filter((item) => item.schedule_id === schedule.id).slice(0, 10)) runs.append(runLine(run, editor));
    if (!runs.children.length) runs.append(node("p", "No runs yet.", "muted"));
  }
  /** One line of a run log: when, how it ended, and a link that closes the window and opens the run's chat. */
  function runLine(run, windowToClose) {
    const item = node("p", `${when(run.created)} · ${run.status.replaceAll("_", " ")}${run.error ? ` · ${run.error}` : ""} · `);
    const link = node("a", "Open chat"); link.href = "#";
    link.onclick = async (event) => { event.preventDefault(); windowToClose.close(); if (dialog.open) dialog.close(); await selectChat(run.task_id); };
    item.append(link);
    return item;
  }
  /**
   * Open the editor on a saved schedule, or on a new one for a chat.
   * A new schedule starts from the chat's title and its first message.
   */
  function openEditor(schedule, start = {}) {
    const masked = Boolean(schedule?.prompt_redacted);
    el("schedule-form").reset();
    el("schedule-title").textContent = schedule ? "Edit schedule" : "Schedule this chat";
    el("schedule-id").value = schedule?.id || "";
    el("schedule-chat").value = schedule ? schedule.chat_id || "" : start.chat_id || "";
    el("schedule-name").value = schedule?.name || start.name || "";
    el("schedule-prompt").value = schedule?.prompt || start.prompt || "";
    el("schedule-mode").value = schedule?.mode || "default";
    showModels(schedule?.model || "");
    const shown = triggerOf(schedule?.spec || { kind: "interval", seconds: 3600 });
    el("schedule-trigger").value = shown.trigger;
    el("schedule-weekday").value = shown.weekday;
    el("schedule-time").value = shown.time;
    el("schedule-kind").value = shown.kind;
    showTrigger();  // Sets the kind of the value field before a date goes into it.
    el("schedule-value").value = shown.value;
    // A masked prompt is not the real one: saving it would replace the prompt with the mask.
    el("schedule-masked").hidden = !masked;
    for (const field of el("schedule-form").querySelectorAll("input, select, textarea")) field.disabled = masked;
    el("schedule-save").disabled = masked || !zone;
    el("schedule-save").textContent = !schedule ? "Create schedule" : schedule.approved ? "Save changes" : "Save and approve";
    el("schedule-advanced").open = masked;
    showSaved(schedule);
    editorError(zone ? null : latest.timezone_error || "Home Assistant time zone unavailable; schedule entry is paused.");
    if (!editor.open) editor.showModal();
    if (!masked) el("schedule-name").focus();
  }
  /** Schedule in a chat's menu: edit the chat's schedule, or start a new one from the chat. */
  async function openForChat(id) {
    try {
      await load();
      const schedule = scheduleOfChat(id);
      if (schedule) { openEditor(schedule); return; }
      const task = (await api(`tasks/${encodeURIComponent(id)}`)).task;
      const first = (task.turns || [])[0];
      openEditor(null, { chat_id: id, name: (task.title || "").slice(0, 80), prompt: first?.message || "" });
    } catch (error) { showError(error); }
  }
  document.addEventListener("thread-action", (event) => {
    if (scheduleUi && event.detail.action === "schedule") openForChat(event.detail.id);
  });
  /**
   * The Scheduled runs header: how many schedules there are. It needs the owner
   * when the time zone is unknown (no schedule runs) or a proposal waits for
   * approval.
   */
  function showScheduleSectionStatus(schedules) {
    const waiting = schedules.filter((schedule) => !schedule.approved).length;
    let text = schedules.length === 1 ? "1 schedule" : `${schedules.length} schedules`;
    if (waiting) text = `${waiting} waiting for your approval`;
    if (!zone) text = "Paused: time zone unavailable";
    setSectionStatus("scheduled-work", text, Boolean(waiting) || !zone);
  }
  async function refresh() {
    if (refreshing || !dialog.open) return;
    refreshing = true;
    try {
      const data = await load();
      if (scheduleUi) {
      el("schedule-timezone").textContent = zone ? `Home Assistant time zone: ${zone}` : data.timezone_error;
      const list = el("schedule-list"); list.replaceChildren();
      for (const schedule of data.schedules) {
        const item = node("div", "");
        item.append(node("strong", schedule.name), node("p", `${schedule.approved ? schedule.enabled ? "On" : "Paused" : "Needs your approval"} · ${describe(schedule.spec)} · ${schedule.mode === "read-only" ? "Read only" : "Default"} · ${when(schedule.next_due)}`, "muted"));
        const prompt = node("details", "", "disclosure"); prompt.append(node("summary", "Review prompt"), node("pre", schedule.prompt)); item.append(prompt);
        const actions = node("div", "", "settings-actions");
        const editButton = button("Edit", () => openEditor(schedule));
        editButton.disabled = Boolean(schedule.prompt_redacted || !zone);
        if (schedule.prompt_redacted) item.append(node("p", "Prompt contains masked credentials. Create a replacement with a complete prompt; editing is disabled.", "muted"));
        actions.append(editButton);
        const action = schedule.approved ? schedule.enabled ? "pause" : "resume" : "approve";
        actions.append(button({pause: "Pause", resume: "Resume", approve: "Approve this proposal"}[action], () => api(`schedules/${schedule.id}/${action}`, {revision: schedule.revision})));
        actions.append(button("Delete", () => api(`schedules/${schedule.id}/delete`, {})));
        item.append(actions); list.append(item);
      }
      if (!data.schedules.length) list.append(node("p", "No schedules saved.", "muted"));
      showScheduleSectionStatus(data.schedules);
      const runs = el("schedule-runs"); runs.replaceChildren();
      for (const run of data.runs) runs.append(runLine(run, dialog));
      if (!data.runs.length) runs.append(node("p", "No runs yet.", "muted"));
      }
      el("lead-token-status").textContent = data.lead_token_configured ? "A lead token is configured." : "No lead token is configured.";
      if (!scheduleUi) setSectionStatus("scheduled-work", data.lead_token_configured ? "Lead token set" : "No lead token", false);
      const mailbox = el("schedule-mailbox"); mailbox.replaceChildren();
      const mailboxStatus = data.mailbox_status;
      if (mailboxStatus?.full[reader]) mailbox.append(node("p", "Mailbox full of unread messages. Read or clear it before sending more.", "muted"));
      if (data.mailbox.length) {
        if (clearConfirmation) {
          const confirmation = node("div", "");
          confirmation.id = "schedule-mail-clear-confirmation";
          confirmation.setAttribute("role", "group");
          confirmation.setAttribute("aria-label", "Clear mailbox confirmation");
          confirmation.append(node("p", "Delete every message in this mailbox? Other mailbox copies remain. This cannot be undone."));
          const actions = node("div", "", "settings-actions");
          const cancel = button("Cancel", cancelMailClear);
          actions.append(cancel, button("Clear all messages", async () => {
            await api(mailboxPath("schedule-mailbox"), {confirm: true}, "DELETE");
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
        const envelope = mail.envelope;
        if (mail.origin) item.append(node("strong", mail.origin === "schedule" ? "Scheduled run" : "Started by lead"));
        for (const key of ["to", "from", "sent", "last_modified", "due", "message"]) {
          if (envelope[key] == null) continue;
          const label = {to: "To", from: "From", sent: "Sent", last_modified: "Last modified", due: "Due", message: "Message"}[key];
          const value = ["sent", "last_modified", "due"].includes(key) ? mailWhen(envelope[key]) : envelope[key];
          item.append(node("strong", label), node(key === "message" ? "pre" : "p", value));
        }
        if (envelope.due && Date.parse(envelope.due) <= Date.now()) item.append(node("p", "Due / overdue", "muted"));
        const plain = node("details", "", "disclosure"); plain.append(node("summary", "Plain text"), node("pre", mail.text)); item.append(plain);
        item.append(button("Reply", () => { el("schedule-mail-parent").value = mail.parent || mail.id; el("schedule-mail-target").textContent = "Reply to selected thread"; el("schedule-mail-body").focus(); }));
        if (!mail.parent) item.append(button("Delete thread", () => api(mailboxPath(`schedule-mailbox/${mail.id}`), undefined, "DELETE")));
        mailbox.append(item);
      }
      if (!data.mailbox.length) mailbox.append(node("p", "No local messages.", "muted"));
      if (dialog.open && reader === "owner" && data.mailbox.length) {
        await api(mailboxPath("schedule-mailbox/read"), {ids: data.mailbox.map(mail => mail.id)});
      }
    } catch (error) { message(error); }
    finally { refreshing = false; }
  }
  if (scheduleUi) {
  el("schedule-form").onsubmit = async (event) => {
    event.preventDefault();
    if (!zone) { editorError("Home Assistant time zone unavailable; schedule entry is paused."); return; }
    const id = el("schedule-id").value;
    el("schedule-save").disabled = true;
    try {
      const data = {
        name: el("schedule-name").value, prompt: el("schedule-prompt").value, mode: el("schedule-mode").value,
        spec: specOfForm(), model: el("schedule-model").value || null, chat_id: el("schedule-chat").value,
      };
      await api(id ? `schedules/${id}` : "schedules", data, id ? "PATCH" : "POST");
      editor.close();
      message("Schedule saved and approved.");
      await refresh();
    } catch (error) { editorError(error); }
    finally { el("schedule-save").disabled = false; }
  };
  /** Pause, resume or approve the schedule in the editor, then show its new state. */
  el("schedule-toggle").onclick = async () => {
    const id = el("schedule-id").value;
    const schedule = latest.schedules.find((item) => item.id === id);
    try {
      await api(`schedules/${id}/${el("schedule-toggle").dataset.action}`, { revision: schedule?.revision });
      await load();
      const changed = latest.schedules.find((item) => item.id === id);
      if (changed) { openEditor(changed); el("schedule-advanced").open = true; }
      else editor.close();
      await refresh();
    } catch (error) { editorError(error); }
  };
  el("schedule-delete").onclick = async () => {
    try {
      await api(`schedules/${el("schedule-id").value}/delete`, {});
      editor.close();
      await refresh();
    } catch (error) { editorError(error); }
  };
  el("schedule-trigger").onchange = showTrigger;
  el("schedule-kind").onchange = showTrigger;
  }
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
    try { await api(parent ? `schedule-mailbox/${parent}/reply` : "schedule-mailbox", {message: el("schedule-mail-body").value, to: recipient.value, due: due.value.trim() || null}); el("schedule-mail-body").value = ""; message("Message saved locally."); await refresh(); }
    catch (error) { message(error); }
  };
  new MutationObserver(() => {
    clearInterval(timer);
    if (dialog.open) { refresh(); timer = setInterval(refresh, 15000); }
    else { hideToken(); cancelMailClear(); }
  }).observe(dialog, {attributes: true, attributeFilter: ["open"]});
  dialog.addEventListener("close", () => { clearInterval(timer); hideToken(); cancelMailClear(); });
  if (scheduleUi) showTrigger();
})();
