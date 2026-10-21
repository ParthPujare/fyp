// Harness console chrome — presentation only (clock, health dot, card spotlight,
// login tagline, boot sequence). Touches no auth state and sends no user data;
// the only request is GET /health.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- UTC clock in the top bar ----
  const clock = $("clock");
  const tick = () => { clock.textContent = new Date().toISOString().slice(11, 19) + " UTC"; };
  tick();
  setInterval(tick, 1000);

  // ---- live status dot, backed by the dashboard's real /health endpoint ----
  const sys = $("sys");
  function setSys(ok) {
    sys.classList.toggle("online", ok);
    sys.classList.toggle("offline", !ok);
    sys.querySelector("span").textContent = ok ? "dashboard online" : "dashboard unreachable";
  }
  async function ping() {
    try {
      const r = await fetch("/health", { credentials: "same-origin", cache: "no-store" });
      setSys(r.ok);
    } catch { setSys(false); }
  }
  ping();
  setInterval(ping, 30000);

  // ---- Mk21: collapsible navigation rail (desktop; CSS ignores the class on phones) ----
  const NAV_KEY = "harness_nav_collapsed";
  const toggle = $("nav-toggle");
  function setCollapsed(collapsed) {
    document.body.classList.toggle("nav-collapsed", collapsed);
    const label = collapsed ? "Expand navigation" : "Collapse navigation";
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
  }
  let startCollapsed = false;
  try { startCollapsed = localStorage.getItem(NAV_KEY) === "1"; } catch { /* storage blocked */ }
  setCollapsed(startCollapsed);
  toggle.addEventListener("click", () => {
    const collapsed = !document.body.classList.contains("nav-collapsed");
    setCollapsed(collapsed);
    try { localStorage.setItem(NAV_KEY, collapsed ? "1" : "0"); } catch { /* per-viewer nicety only */ }
  });

  // ---- spotlight: cards glow where the pointer is ----
  document.addEventListener("pointermove", (e) => {
    const card = e.target instanceof Element ? e.target.closest(".card") : null;
    if (!card) return;
    const r = card.getBoundingClientRect();
    card.style.setProperty("--mx", (e.clientX - r.left) + "px");
    card.style.setProperty("--my", (e.clientY - r.top) + "px");
  }, { passive: true });

  // ---- login tagline typewriter ----
  const LINES = [
    "every tool call is policy-enforced by role",
    "memory is scoped by role, not by prompt",
    "high-impact actions wait for a human",
    "append-only, integrity-hashed audit trail",
    "PII sanitized before the model sees it",
  ];
  const typed = $("typed");
  if (reduceMotion) {
    typed.textContent = LINES[0];
  } else {
    let line = 0, pos = 0, deleting = false;
    (function step() {
      const text = LINES[line];
      if (!deleting) {
        pos++;
        typed.textContent = text.slice(0, pos);
        if (pos === text.length) { deleting = true; return setTimeout(step, 2200); }
        return setTimeout(step, 38);
      }
      pos--;
      typed.textContent = text.slice(0, pos);
      if (pos === 0) { deleting = false; line = (line + 1) % LINES.length; return setTimeout(step, 300); }
      setTimeout(step, 16);
    })();
  }

  // ---- boot sequence: once per browser session, click or any key skips ----
  let seen = true;
  try {
    seen = sessionStorage.getItem("harness_boot") === "1";
    sessionStorage.setItem("harness_boot", "1");
  } catch { /* storage blocked: skip the intro */ }
  if (seen || reduceMotion) return;

  const boot = $("boot"), log = $("boot-log");
  const STEPS = [
    ["dim", "harness console // zero-trust gateway"],
    ["", "init  casbin rbac enforcer ........ "],
    ["", "init  pii sanitizer ............... "],
    ["", "init  hitl approval gate .......... "],
    ["", "init  audit trail ................. "],
    ["", "init  3-tier memory scopes ........ "],
    ["dim", "> handing off to console_"],
  ];
  boot.classList.add("on");
  let i = 0, timer = null, finished = false;
  function finish() {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    boot.classList.add("done");
    setTimeout(() => boot.classList.remove("on"), 600);
    document.removeEventListener("keydown", finish);
  }
  boot.addEventListener("click", finish);
  document.addEventListener("keydown", finish);
  (function next() {
    if (i >= STEPS.length) { timer = setTimeout(finish, 450); return; }
    const [cls, text] = STEPS[i];
    const row = document.createElement("div");
    if (cls) row.className = cls;
    row.textContent = text;
    if (!cls) {
      const ok = document.createElement("span");
      ok.className = "ok";
      ok.textContent = "[ ok ]";
      row.appendChild(ok);
    }
    log.appendChild(row);
    i++;
    timer = setTimeout(next, i === 1 ? 260 : 150);
  })();
})();
