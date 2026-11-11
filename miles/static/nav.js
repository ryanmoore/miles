// Shared chrome for every static page: header nav + ECharts theme reader.
// Include with a single <script src="/nav.js"></script> right after <body>.

// Flat entries are `{ href, label }`; a group entry is `{ label, children }`
// where children are themselves `{ href, label }` flat entries.
const NAV_LINKS = [
  { href: "/plan.html", label: "Plan" },
  { href: "/activities.html", label: "Activities" },
  { href: "/training.html", label: "Training" },
  { href: "/routes.html", label: "Routes", sheetOnPhone: true },
  { href: "/doc.html", label: "Notebook", sheetOnPhone: true },  // the phone tab bar fits four tabs and More
  {
    label: "History",
    children: [
      { href: "/builds.html", label: "Builds" },
      { href: "/compare.html", label: "Compare" },
      { href: "/races.html", label: "Races" },
      { href: "/years.html", label: "Years" },
      { href: "/terrain.html", label: "Terrain" },
    ],
  },
];

// "/" and "/index.html" land on Plan (index.html redirects there).
function isLinkActive(href, path) {
  if (href === "/doc.html") return path === href && !location.search.includes("id=");  // plan documents share the page
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
  // Two buttons, one state: the header's (desktop) and the phone tab bar's,
  // whose narrow cell gets a short label with the full text as its title.
  const syncBtn = document.createElement("button");
  syncBtn.className = "sync-btn";
  const tabSyncBtn = document.createElement("button");
  tabSyncBtn.type = "button";
  tabSyncBtn.className = "tabbar-item tabbar-sync";
  const syncBtns = [syncBtn, tabSyncBtn];

  function setSync(full, short, { disabled = true, title = null } = {}) {
    syncBtn.textContent = full;
    tabSyncBtn.textContent = short;
    syncBtn.title = title ?? "Sync from Strava";
    tabSyncBtn.title = title ?? (disabled ? full : "Sync from Strava");
    tabSyncBtn.setAttribute("aria-label", tabSyncBtn.title);
    for (const b of syncBtns) b.disabled = disabled;
  }

  // Button text while running is driven entirely by /api/sync/status's phase/
  // done/total, so a page-load poll (below) can resume mid-run with the same text
  // a click would have produced.
  function formatPhase(data) {
    if (data.phase === "waiting" && data.until) return `↻ waiting until ${data.until}`;
    if (!data.phase) return "↻ Syncing…";
    if (data.total != null && data.done != null) return `↻ ${data.phase} ${data.done}/${data.total}`;
    if (data.done != null) return `↻ ${data.phase} ${data.done}`;
    return `↻ ${data.phase}`;
  }

  function formatPhaseShort(data) {
    if (data.phase === "waiting") return "↻ Wait";
    if (data.total != null && data.done != null) return `↻ ${data.done}/${data.total}`;
    if (data.done != null) return `↻ ${data.done}`;
    return "↻ …";
  }

  function showRunning(data) {
    setSync(formatPhase(data), formatPhaseShort(data));
  }

  function showError(message) {
    setSync("✗ Error", "✗ Error", { title: message || "Sync failed" });
    setTimeout(resetSync, 8000);
  }

  function resetSync() {
    setSync("↻ Sync", "↻ Sync", { disabled: false });
  }

  async function startSync() {
    setSync("↻ Syncing…", "↻ …");
    try {
      const res = await fetch("/api/sync", { method: "POST" });
      const data = await res.json();
      if (data.status === "running" || data.status === "started") {
        pollSync();
      } else {
        resetSync();
      }
    } catch {
      showError("Network error starting sync");
    }
  }

  async function pollSync() {
    try {
      const res = await fetch("/api/sync/status");
      const data = await res.json();
      if (data.status === "running") {
        showRunning(data);
        setTimeout(pollSync, 1000);
      } else if (data.status === "error") {
        showError(data.error);
      } else if (data.status === "done") {
        const n = data.new_activities || 0;
        setSync(n > 0 ? `✓ ${n} new` : "✓ up to date", n > 0 ? `✓ ${n} new` : "✓");
        setTimeout(() => { location.reload(); }, 2000);
      } else {
        resetSync();
      }
    } catch {
      showError("Network error checking sync status");
    }
  }

  resetSync();
  for (const b of syncBtns) b.addEventListener("click", startSync);
  header.insertBefore(syncBtn, nav);

  // Resume the polling state on a fresh page load if a sync kicked off from
  // another tab/page is still running — otherwise navigating away loses the
  // indicator until the next click.
  (async function resumeSyncStatus() {
    try {
      const res = await fetch("/api/sync/status");
      const data = await res.json();
      if (data.status === "running") {
        showRunning(data);
        setTimeout(pollSync, 1000);
      }
    } catch {
      // Leave the button at its idle label; the next click will surface any error.
    }
  })();
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

  // --- build/version info ---
  const versionEl = document.createElement("span");
  versionEl.className = "build-version";
  header.appendChild(versionEl);
  fetch("/api/version")
    .then((res) => res.json())
    .then((data) => {
      versionEl.textContent = `${data.build_time} · ${data.git_hash}`;
    })
    .catch(() => {});
  // --- end build/version info ---

  document.body.insertBefore(header, document.body.firstChild);
  document.body.appendChild(renderTabbar(path, tabSyncBtn));
}

// Phone bottom bar (theme.css hides it from 640px, and hides the header
// below it): Sync, the flat NAV_LINKS as tabs, then "More" opening a sheet
// with the grouped links and Workbook.
function renderTabbar(path, syncBtn) {
  const frag = document.createDocumentFragment();
  const bar = document.createElement("nav");
  bar.className = "tabbar";
  bar.setAttribute("aria-label", "Primary");
  bar.appendChild(syncBtn);

  const sheetLinks = [{ href: "/workbook.html", label: "Workbook" }];
  for (const entry of NAV_LINKS) {
    if (entry.children) {
      sheetLinks.unshift(...entry.children);
      continue;
    }
    if (entry.sheetOnPhone) {
      sheetLinks.unshift(entry);
      continue;
    }
    const a = document.createElement("a");
    a.href = entry.href;
    a.className = "tabbar-item";
    a.textContent = entry.label;
    if (isLinkActive(entry.href, path)) a.classList.add("active");
    bar.appendChild(a);
  }

  const backdrop = document.createElement("div");
  backdrop.className = "tabbar-sheet-backdrop";
  backdrop.hidden = true;
  const sheet = document.createElement("div");
  sheet.className = "tabbar-sheet";
  sheet.id = "tabbar-sheet";
  sheet.hidden = true;
  for (const link of sheetLinks) {
    const a = document.createElement("a");
    a.href = link.href;
    a.textContent = link.label;
    if (isLinkActive(link.href, path)) a.classList.add("active");
    sheet.appendChild(a);
  }

  const more = document.createElement("button");
  more.type = "button";
  more.className = "tabbar-item";
  more.textContent = "More";
  more.setAttribute("aria-haspopup", "true");
  more.setAttribute("aria-expanded", "false");
  more.setAttribute("aria-controls", sheet.id);
  if (sheetLinks.some((link) => isLinkActive(link.href, path))) more.classList.add("active");
  bar.appendChild(more);

  function setOpen(open) {
    sheet.hidden = !open;
    backdrop.hidden = !open;
    more.setAttribute("aria-expanded", String(open));
  }
  more.addEventListener("click", () => setOpen(sheet.hidden));
  backdrop.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setOpen(false);
  });

  frag.append(backdrop, sheet, bar);
  return frag;
}

// Reads theme.css custom properties so ECharts options can follow light/dark
// mode without hardcoding hex values in each page's chart setup.
function chartTheme() {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  return {
    isDark: window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false,
    // Phone layout (theme.css's 640px chrome breakpoint): charts tighten
    // their grid and thin their axis labels.
    compact: window.matchMedia?.("(max-width: 639px)").matches ?? false,
    text: v("--text"),
    textSecondary: v("--text-secondary"),
    muted: v("--muted"),
    baseline: v("--baseline"),
    gridline: v("--gridline"),
    surface: v("--surface"),
    accent: v("--accent"),
    series: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => v(`--series-${i}`)),
    route: { run: v("--route-run"), grind: v("--route-grind"), hike: v("--route-hike"), neutral: v("--route-neutral") },
  };
}

// hex "#rrggbb" -> "rgba(r,g,b,a)"; ECharts markArea/tooltip need real rgba.
function withAlpha(hex, alpha) {
  const n = parseInt(hex.replace("#", ""), 16);
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

renderNav();
