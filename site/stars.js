// Keeps the fixed bar transparent while it sits over the dark hero or the
// closing band, and solid over the light sheet. No dependencies.
(() => {
  // The bar is transparent only while it sits over a night scene: before the
  // sheet has slid up under it, and once the closing band has reached it.
  const bar = document.querySelector(".bar");
  const sheet = document.querySelector(".sheet");
  const closing = document.querySelector(".final-cta");
  if (bar && sheet) {
    const update = () => {
      const edge = bar.offsetHeight;
      const night = sheet.getBoundingClientRect().top > edge
        || (closing && closing.getBoundingClientRect().top < edge
            && sheet.getBoundingClientRect().bottom < edge);
      bar.toggleAttribute("data-over-scene", Boolean(night));
    };
    addEventListener("scroll", update, { passive: true });
    addEventListener("resize", update, { passive: true });
    update();
  }

  // Review tabs.
  const tabs = [...document.querySelectorAll(".car-tab")];
  const cards = [...document.querySelectorAll(".gh-card")];
  tabs.forEach((tab, i) => tab.addEventListener("click", () => {
    tabs.forEach((t) => t.classList.toggle("active", t === tab));
    cards.forEach((c, k) => c.classList.toggle("active", k === i));
  }));

  // Theme toggle, remembered per browser.
  const root = document.documentElement;
  try { const saved = localStorage.getItem("ada-theme"); if (saved) root.dataset.theme = saved; } catch {}
  document.getElementById("theme")?.addEventListener("click", () => {
    const dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("ada-theme", root.dataset.theme); } catch {}
  });
})();
