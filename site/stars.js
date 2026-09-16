// Night fields behind the hero and the closing band: faint stars, a few
// constellations drawn as hollow nodes on hairlines, a slow twinkle. Pauses
// off-screen and under prefers-reduced-motion. No dependencies.
(() => {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const rand = (a, b) => a + Math.random() * (b - a);

  function field(canvas) {
    const ctx = canvas.getContext("2d");
    let W = 0, H = 0, stars = [], figures = [], running = false;
    const t0 = performance.now();
    function build() {
      stars = Array.from({ length: Math.round((W * H) / 26000) }, () => ({
        x: rand(0, W), y: rand(0, H), r: rand(.6, 1.4), p: rand(0, 6.28),
      }));
      figures = [];
      const count = Math.max(3, Math.round(W / 220));
      for (let i = 0; i < count; i++) {
        const n = 3 + Math.floor(rand(0, 5));
        let x = rand(W * .04, W * .96), y = rand(H * .06, H * .62);
        const pts = [];
        for (let k = 0; k < n; k++) { pts.push({ x, y }); x += rand(-70, 70); y += rand(-55, 55); }
        figures.push(pts);
      }
    }
    function resize() {
      const dpr = Math.min(devicePixelRatio || 1, 2);
      W = canvas.clientWidth; H = canvas.clientHeight;
      canvas.width = W * dpr; canvas.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      build();
    }
    function draw(now) {
      if (!running) return;
      const t = (now - t0) / 1000;
      ctx.clearRect(0, 0, W, H);
      for (const s of stars) {
        ctx.fillStyle = `rgba(255,255,255,${.25 + .35 * (.5 + .5 * Math.sin(t * .5 + s.p))})`;
        ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 6.28); ctx.fill();
      }
      ctx.lineWidth = 1;
      for (const f of figures) {
        ctx.strokeStyle = "rgba(255,255,255,.22)";
        for (let i = 1; i < f.length; i++) {
          // Hairlines stop short of the nodes, as on a star chart.
          const a = f[i - 1], b = f[i], d = Math.hypot(b.x - a.x, b.y - a.y) || 1;
          const ux = (b.x - a.x) / d, uy = (b.y - a.y) / d;
          ctx.beginPath(); ctx.moveTo(a.x + ux * 6, a.y + uy * 6); ctx.lineTo(b.x - ux * 6, b.y - uy * 6); ctx.stroke();
        }
        ctx.strokeStyle = "rgba(255,255,255,.55)";
        for (const p of f) { ctx.beginPath(); ctx.arc(p.x, p.y, 3, 0, 6.28); ctx.stroke(); }
      }
      if (!reduce) requestAnimationFrame(draw);
    }
    new IntersectionObserver((entries) => {
      const on = entries.some((e) => e.isIntersecting);
      if (on && !running) { running = true; requestAnimationFrame(draw); }
      else if (!on) running = false;
    }).observe(canvas);
    addEventListener("resize", resize, { passive: true });
    resize();
    if (reduce) { running = true; draw(performance.now()); running = false; }
  }
  document.querySelectorAll("canvas.field").forEach(field);

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
