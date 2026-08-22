// Telegram.WebApp.HapticFeedback wrapper (spec section 3.4). No-ops
// gracefully outside Telegram (e.g. testing in a plain browser) so nothing
// else needs to guard every call site.

function api() {
  return window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.HapticFeedback;
}

export function lightTap() {
  const h = api();
  if (h) h.impactOccurred("light");
}

export function mediumTap() {
  const h = api();
  if (h) h.impactOccurred("medium");
}

export function success() {
  const h = api();
  if (h) h.notificationOccurred("success");
}

export function warning() {
  const h = api();
  if (h) h.notificationOccurred("warning");
}
