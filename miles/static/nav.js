// Shared chrome for every static page: header nav + ECharts theme reader.
// Include with a single <script src="/nav.js"></script> right after <body>.

// Flat entries are `{ href, label }`; a group entry is `{ label, children }`
// where children are themselves `{ href, label }` flat entries.
const NAV_LINKS = [
  { href: "/plan.html", label: "Plan" },
  { href: "/activities.html", label: "Activities" },
  { href: "/races.html", label: "Races" },
  {
    label: "History",
    children: [
      { href: "/builds.html", label: "Builds" },
      { href: "/compare.html", label: "Compare" },
      { href: "/training.html", label: "Training" },
      { href: "/years.html", label: "Years" },
    ],
  },
];

// "/" and "/index.html" land on Plan (index.html redirects there).
function isLinkActive(href, path) {
  return href === "/plan.html"
    ? (path === "/plan.html" || path === "/" || path === "/index.html")
    : path === href;
}

// A grouped nav entry: a toggle button plus a menu of plain links. Click
// (not hover) opens it so it works on touch; outside clicks and Escape
// close it.
function renderNavGroup(entry, path) {
  const wrap = document.createElement("div");
  wrap.className = "nav-group";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "nav-group-toggle";
  button.textContent = `${entry.label} ▾`;
  button.setAttribute("aria-haspopup", "true");
  button.setAttribute("aria-expanded", "false");

  const menu = document.createElement("div");
  menu.className = "nav-group-menu";

  const isChildActive = entry.children.some((child) => isLinkActive(child.href, path));
  if (isChildActive) button.classList.add("active");

  for (const child of entry.children) {
    const a = document.createElement("a");
    a.href = child.href;
    a.textContent = child.label;
    if (isLinkActive(child.href, path)) a.classList.add("active");
    menu.appendChild(a);
  }

  function closeMenu() {
    wrap.classList.remove("open");
    button.setAttribute("aria-expanded", "false");
    document.removeEventListener("click", handleOutsideClick);
    document.removeEventListener("keydown", handleKeydown);
  }

  function openMenu() {
    wrap.classList.add("open");
    button.setAttribute("aria-expanded", "true");
    document.addEventListener("click", handleOutsideClick);
    document.addEventListener("keydown", handleKeydown);
  }

  function handleOutsideClick(event) {
    if (!wrap.contains(event.target)) closeMenu();
  }

  function handleKeydown(event) {
    if (event.key === "Escape") closeMenu();
  }

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    if (wrap.classList.contains("open")) {
      closeMenu();
    } else {
      openMenu();
    }
  });

  wrap.appendChild(button);
  wrap.appendChild(menu);
  return wrap;
}

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
  for (const entry of NAV_LINKS) {
    if (entry.children) {
      nav.appendChild(renderNavGroup(entry, path));
      continue;
    }
    const a = document.createElement("a");
    a.href = entry.href;
    a.textContent = entry.label;
    if (isLinkActive(entry.href, path)) a.classList.add("active");
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

  // Workbook is a utility page (collected one-off analyses), not a primary
  // view, so it gets an icon beside Sync rather than a text nav link.
  const workbookLink = document.createElement("a");
  workbookLink.href = "/workbook.html";
  workbookLink.className = "workbook-link";
  workbookLink.title = "Workbook";
  workbookLink.setAttribute("aria-label", "Workbook");
  workbookLink.textContent = "⌗";
  if (path === "/workbook.html") workbookLink.classList.add("active");
  header.insertBefore(workbookLink, nav);

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
