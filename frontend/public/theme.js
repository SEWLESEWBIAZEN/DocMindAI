// Applies the saved theme before the app renders, so a dark page never flashes light.
// A file rather than an inline script: the CSP only allows scripts from our origin.
try {
  var t = window.localStorage.getItem('documind.theme')
  if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t)
} catch {
  // Storage blocked: follow the system setting.
}
