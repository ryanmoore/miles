// Shared chrome for every static page: header nav + ECharts theme reader.
// Include with a single <script src="/nav.js"></script> right after <body>.

const NAV_LINKS = [
  { href: "/races.html", label: "Races" },
  { href: "/builds.html", label: "Builds" },
  { href: "/compare.html", label: "Compare" },
  { href: "/training.html", label: "Training" },
  { href: "/years.html", label: "Years" },
  { href: "/workbook.html", label: "Workbook" },
];

function renderNav() {
  const header = document.createElement("header");
  header.className = "site-header";

  const brand = document.createElement("a");
  brand.href = "/";
  brand.className = "brand";
  brand.textContent = "miles";
  header.appendChild(brand);

  const nav = document.createElement("nav");
  const path = location.pathname;
  for (const { href, label } of NAV_LINKS) {
    const a = document.createElement("a");
    a.href = href;
    a.textContent = label;
    const isActive = href === "/races.html"
      ? (path === "/races.html" || path === "/" || path === "/index.html")
      : path === href;
    if (isActive) a.classList.add("active");
    nav.appendChild(a);
  }
  header.appendChild(nav);

  // --- sync button ---
  const syncBtn = document.createElement("button");
  syncBtn.className = "sync-btn";
  syncBtn.title = "Sync from Strava";
  syncBtn.textContent = "↻ Sync";

  async function startSync() {
    syncBtn.disabled = true;
    syncBtn.textContent = "↻ Syncing…";
    try {
      const res = await fetch("/api/sync", { method: "POST" });
      const data = await res.json();
      if (data.status === "running" || data.status === "started") {
        pollSync();
      } else {
        resetSync("↻ Sync");
      }
    } catch {
      syncBtn.textContent = "✗ Error";
      setTimeout(() => resetSync("↻ Sync"), 3000);
    }
  }

  async function pollSync() {
    try {
      const res = await fetch("/api/sync/status");
      const data = await res.json();
      if (data.status === "running") {
        setTimeout(pollSync, 3000);
      } else if (data.returncode === 0) {
        syncBtn.textContent = "✓ Done";
        setTimeout(() => { location.reload(); }, 2000);
      } else {
        syncBtn.textContent = "✗ Error";
        setTimeout(() => resetSync("↻ Sync"), 3000);
      }
    } catch {
      syncBtn.textContent = "✗ Error";
      setTimeout(() => resetSync("↻ Sync"), 3000);
    }
  }

  function resetSync(label) {
    syncBtn.disabled = false;
    syncBtn.textContent = label;
  }

  syncBtn.addEventListener("click", startSync);
  header.insertBefore(syncBtn, nav);
  // --- end sync button ---

  document.body.insertBefore(header, document.body.firstChild);
}

// Reads theme.css custom properties so ECharts options can follow light/dark
// mode without hardcoding hex values in each page's chart setup.
function chartTheme() {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  return {
    isDark: window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false,
    text: v("--text"),
    textSecondary: v("--text-secondary"),
    muted: v("--muted"),
    baseline: v("--baseline"),
    gridline: v("--gridline"),
    surface: v("--surface"),
    accent: v("--accent"),
    series: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => v(`--series-${i}`)),
  };
}

// hex "#rrggbb" -> "rgba(r,g,b,a)"; ECharts markArea/tooltip need real rgba.
function withAlpha(hex, alpha) {
  const n = parseInt(hex.replace("#", ""), 16);
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

renderNav();
