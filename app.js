(() => {
  "use strict";
  const config = window.PLAY_MONITOR_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  const state = { current: null, history: { scans: [], changes: [] } };
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const fmtDate = (value) => {
    if (!value) return "Not available";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return esc(value);
    return new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "2-digit", timeZone: "UTC" }).format(date);
  };
  const fmtDateTime = (value) => {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? esc(value) : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" }).format(date) + " UTC";
  };
  const setStatus = (text, mode = "ok") => {
    const el = $("scan-status");
    el.className = `status-pill ${mode}`;
    el.innerHTML = `<i></i>${esc(text)}`;
  };
  async function fetchJson(path) {
    const url = new URL(path, window.location.href);
    url.searchParams.set("_", String(Date.now())); // Avoid stale GitHub Pages/browser cache.
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
    return response.json();
  }
  async function loadData() {
    setStatus("Refreshing…", "loading");
    try {
      const [current, history] = await Promise.all([
        fetchJson(config.currentDataPath || "data/current.json"),
        fetchJson(config.historyDataPath || "data/history.json").catch(() => ({ scans: [], changes: [] }))
      ]);
      if (!current || !Array.isArray(current.apps)) throw new Error("Inventory JSON has an unexpected format");
      state.current = current;
      state.history = { scans: Array.isArray(history.scans) ? history.scans : [], changes: Array.isArray(history.changes) ? history.changes : [] };
      render();
      setStatus(current.last_scan ? `Scanned ${fmtDate(current.last_scan)}` : "Awaiting first scan", current.last_scan ? "ok" : "warning");
      $("inventory-note").textContent = current.last_scan ? `Last scan: ${fmtDateTime(current.last_scan)} · Discovery: ${current.discovery_method || "unknown"}` : "No successful scan has been recorded yet. Run a manual scan to populate the inventory.";
    } catch (error) {
      console.error("Unable to load monitor data", error);
      setStatus("Data unavailable", "error");
      $("apps-body").innerHTML = `<tr><td colspan="6" class="empty-state">Could not load inventory. Check that GitHub Pages is deployed and data/current.json exists.</td></tr>`;
      $("history-body").innerHTML = `<tr><td colspan="5" class="empty-state">Scan history could not be loaded.</td></tr>`;
    }
  }
  function render() {
    const apps = state.current?.apps || [];
    const changes = state.history.changes;
    $("metric-apps").textContent = apps.length;
    $("metric-new").textContent = changes.filter((item) => item.type === "new_app").length;
    $("metric-updates").textContent = changes.filter((item) => item.type === "version_changed").length;
    $("metric-scan").textContent = state.current?.last_scan ? fmtDate(state.current.last_scan) : "Never";
    $("metric-method").textContent = state.current?.discovery_method || "Waiting for first scan";
    $("history-count").textContent = `${state.history.scans.length} scan${state.history.scans.length === 1 ? "" : "s"}`;
    const query = $("search").value.trim().toLowerCase();
    const filter = $("version-filter").value;
    const sort = $("sort-by").value;
    const filtered = apps.filter((app) => {
      const text = `${app.name || ""} ${app.package_id || ""}`.toLowerCase();
      if (query && !text.includes(query)) return false;
      if (filter === "known" && !app.version) return false;
      if (filter === "missing" && app.version) return false;
      if (filter === "partial" && app.metadata_status !== "partial") return false;
      return true;
    }).sort((a, b) => {
      if (sort === "updated") return String(b.last_updated || "").localeCompare(String(a.last_updated || ""));
      if (sort === "version") return String(b.version || "").localeCompare(String(a.version || ""), undefined, { numeric: true });
      return String(a.name || a.package_id || "").localeCompare(String(b.name || b.package_id || ""));
    });
    $("result-count").textContent = `${filtered.length} of ${apps.length} apps`;
    $("empty-filter").hidden = filtered.length !== 0;
    $("apps-body").innerHTML = filtered.length ? filtered.map((app) => {
      const status = app.metadata_status === "partial" || !app.version ? "Partial" : "Available";
      return `<tr>
        <td><span class="app-name">${esc(app.name || "Name unavailable")}</span><span class="app-sub">${app.not_seen_in_latest_scan ? "Not seen in latest scan; retained" : `First seen ${esc(fmtDate(app.first_seen))}`}</span></td>
        <td><code>${esc(app.package_id)}</code></td><td><span class="version-chip ${app.version ? "" : "unknown"}">${esc(app.version || "Unavailable")}</span></td>
        <td>${esc(fmtDate(app.last_updated))}</td><td><span class="metadata ${status === "Available" ? "complete" : "partial"}">${status}</span></td>
        <td><a class="store-link" href="${esc(app.play_store_url || `https://play.google.com/store/apps/details?id=${encodeURIComponent(app.package_id || "")}`)}" target="_blank" rel="noopener noreferrer">Open listing ↗</a></td></tr>`;
    }).join("") : `<tr><td colspan="6" class="empty-state">No apps match these filters.</td></tr>`;
    const scans = [...state.history.scans].reverse();
    $("history-body").innerHTML = scans.length ? scans.map((scan) => `<tr><td>${esc(fmtDateTime(scan.scanned_at))}</td><td>${Number(scan.discovered_count) || 0}</td><td>${Number(scan.inventory_count) || 0}</td><td>${Number(scan.new_apps) || 0}</td><td>${Number(scan.version_changes) || 0}</td></tr>`).join("") : `<tr><td colspan="5" class="empty-state">No scan history yet. History will appear after the first successful manual scan.</td></tr>`;
  }
  $("scan-now").addEventListener("click", () => {
    $("scan-notice").hidden = false;
    const owner = config.githubOwner;
    const repo = config.githubRepo;
    if (!owner || owner.startsWith("YOUR_") || !repo) {
      alert("Configure githubOwner in config.js after uploading this project to GitHub. The dashboard cannot securely trigger Actions directly from static JavaScript.");
      return;
    }
    const url = `https://github.com/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/actions/workflows/${encodeURIComponent(config.workflowFile || "manual-scan.yml")}`;
    window.open(url, "_blank", "noopener,noreferrer");
  });
  $("dismiss-notice").addEventListener("click", () => { $("scan-notice").hidden = true; });
  $("refresh").addEventListener("click", loadData);
  $("search").addEventListener("input", render);
  $("version-filter").addEventListener("change", render);
  $("sort-by").addEventListener("change", render);
  loadData();
})();
