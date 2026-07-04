// Shared chrome for every static page: header nav + ECharts theme reader.
// Include with a single <script src="/nav.js"></script> right after <body>.

const NAV_LINKS = [
  { href: "/", label: "Marathons" },
  { href: "/races.html", label: "Races" },
  { href: "/training.html", label: "Training" },
  { href: "/years.html", label: "Years" },
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
    const isActive = path === href || (href === "/" && path === "/index.html");
    if (isActive) a.classList.add("active");
    nav.appendChild(a);
  }
  header.appendChild(nav);

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
