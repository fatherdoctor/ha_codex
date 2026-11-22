"use strict";
/**
 * Two banners above the chat, from the worker's /update-status:
 * - "Version X is installed. Restart Home Assistant to finish." while Home Assistant runs another version of the
 *   integration than the one on disk (or none, after the first install);
 * - "Update available: version X" while Supervisor offers a newer app, with a link to the app's Info page.
 * The app never updates or restarts anything itself. Dismiss hides a banner for that version only, in this
 * browser; a newer version shows it again.
 */
const UPDATE_DISMISSED_KEY = "codex-update-banners-dismissed";

/** Return the dismissed version of each banner, as {restart: "x", update: "y"}; empty when storage is blocked. */
function dismissedUpdateBanners() {
  try {
    const saved = JSON.parse(localStorage.getItem(UPDATE_DISMISSED_KEY) || "{}");
    return saved && typeof saved === "object" ? saved : {};
  } catch {
    return {};
  }
}

/** Remember that the owner dismissed this banner for this version; without storage it stays hidden until reload. */
function rememberDismissedUpdateBanner(kind, version) {
  try {
    localStorage.setItem(UPDATE_DISMISSED_KEY, JSON.stringify({ ...dismissedUpdateBanners(), [kind]: version }));
  } catch {
    // Storage is blocked (a private window): the banner comes back on the next visit.
  }
}

/** Show one banner for a version, unless the owner dismissed it for that version. */
function showUpdateBanner(kind, version, text) {
  const banner = $(`${kind}-banner`);
  banner.hidden = !version || dismissedUpdateBanners()[kind] === version;
  if (banner.hidden) return;
  $(`${kind}-banner-text`).textContent = text;
  $(`${kind}-banner-dismiss`).onclick = () => {
    banner.hidden = true;
    rememberDismissedUpdateBanner(kind, version);
  };
}

/** Read the worker's update status once, when the page opens. */
async function loadUpdateBanners() {
  const data = await api("update-status").catch(() => null);
  if (!data) return;
  showUpdateBanner("restart", data.restart_version,
    `Version ${data.restart_version} is installed. Restart Home Assistant to finish.`);
  showUpdateBanner("update", data.update_version, `Update available: version ${data.update_version}`);
  const link = $("update-banner-open");
  const linked = typeof data.info_url === "string" && data.info_url.startsWith("/config/app/");
  link.hidden = !linked;
  if (linked) link.href = data.info_url;
  else link.removeAttribute("href");
}

loadUpdateBanners();
