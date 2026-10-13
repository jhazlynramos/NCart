(() => {
  const root = document.documentElement;
  try {
    root.dataset.theme = window.localStorage.getItem("ncart-theme") === "dark" ? "dark" : "light";
  } catch (error) {
    console.error("Could not read the saved NCart theme preference.", error);
    root.dataset.theme = "light";
  }
  root.classList.add("ncart-render-pending");

  const renderGuard = document.createElement("style");
  renderGuard.textContent = "html.ncart-render-pending body{visibility:hidden!important}";
  document.head.append(renderGuard);

  document.addEventListener("DOMContentLoaded", () => {
    const reveal = () => {
      root.classList.remove("ncart-render-pending");
    };

    if (document.fonts?.ready) {
      document.fonts.ready.then(reveal, reveal);
    } else {
      reveal();
    }
  }, { once: true });
})();
