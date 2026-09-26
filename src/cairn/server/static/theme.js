// a classic script, so the saved theme and density apply before the first paint
try {
  const ui = JSON.parse(localStorage.getItem("cairn.ui") || "{}");
  const theme = ui.theme === "auto" || !ui.theme
    ? (matchMedia("(prefers-contrast: more)").matches ? "contrast" : null) : ui.theme;
  if (["light", "dark", "contrast"].includes(theme)) document.documentElement.dataset.theme = theme;
  if (ui.density === "compact") document.documentElement.dataset.density = "compact";
} catch {
  // unreadable storage leaves the default theme; app.js reads it again
}
