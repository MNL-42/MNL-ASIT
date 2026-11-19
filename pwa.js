// Enregistre le service worker (fichier séparé : la CSP n'autorise que script-src 'self').
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  });
}
