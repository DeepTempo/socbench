// SOCBench results: interactivity layer.
// - Highlights the active nav link as the reader scrolls
// - Renders Chart.js charts from window.SOCBenchData
// - Wires up split tabs (combined / benign / malicious / mixed) to re-render
//   the headline chart + main table without leaving the page.

(function () {
  const data = window.SOCBenchData;
  const fmtF1   = (v) => v == null ? "-" : v.toFixed(3);
  const fmtPct  = (v) => v == null ? "-" : (v * 100).toFixed(1) + "%";
  const fmtUsd  = (v) => v == null ? "-" : "$" + v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const fmtSec  = (ms) => ms == null ? "-" : (ms / 1000).toFixed(1) + "s";

  // Provider colors mirrored from --anthropic / --openai / --gemini in CSS.
  const providerColor = {
    anthropic: "#d97706", // amber-600
    openai:    "#059669", // emerald-600
    gemini:    "#7c3aed", // violet-600
  };

  // ------------- nav active-section highlight on scroll -------------
  function initNavScrollspy() {
    const links = Array.from(document.querySelectorAll(".nav-links a[href^='#']"));
    const sections = links
      .map(a => document.querySelector(a.getAttribute("href")))
      .filter(Boolean);
    if (!sections.length) return;

    const observer = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        const id = "#" + e.target.id;
        links.forEach(a => a.classList.toggle("active", a.getAttribute("href") === id));
      });
    }, { rootMargin: "-30% 0px -60% 0px", threshold: 0 });
    sections.forEach(s => observer.observe(s));
  }

  // ------------- shared chart helpers -------------
  function commonOpts({ yMax = 1, yFmt = (v) => v.toFixed(2), tooltipFmt } = {}) {
    return {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: { label: tooltipFmt || ((c) => `${c.label}: ${c.parsed.y == null ? "n/a" : yFmt(c.parsed.y)}`) },
        },
      },
      scales: {
        x: { ticks: { color: "#111827", font: { family: "ui-monospace, Menlo, monospace" } }, grid: { color: "#e5e7eb" } },
        y: { beginAtZero: true, max: yMax, ticks: { color: "#6b7280", callback: yFmt }, grid: { color: "#e5e7eb" } },
      },
    };
  }
  function providerBarData(rows, valueKey) {
    return {
      labels: rows.map(r => r.provider),
      datasets: [{
        data: rows.map(r => r[valueKey] == null ? null : r[valueKey]),
        backgroundColor: rows.map(r => providerColor[r.provider] + "cc"),
        borderColor: rows.map(r => providerColor[r.provider]),
        borderWidth: 1, borderRadius: 6,
      }],
    };
  }

  // ------------- single-metric bar charts (overall: combined split only) -------------
  // These charts do NOT respond to the split tabs. They use the combined-split
  // rollup so the reader sees one consistent number per provider.
  const SHARED_UNITS = data.scope?.sharedUnits ?? 1205;

  function renderCostPerAlertBars() {
    const ctx = document.getElementById("cost-bars");
    if (!ctx) return;
    const PERSONAS_PER_UNIT = 4;
    const rows = data.meanPerProvider.combined.map(r => ({
      provider: r.provider,
      value: r.costUsd / (SHARED_UNITS * PERSONAS_PER_UNIT),   // $ / alert (one persona per verdict)
    }));
    const maxV = Math.max(...rows.map(r => r.value));
    new Chart(ctx.getContext("2d"), {
      type: "bar",
      data: {
        labels: rows.map(r => r.provider),
        datasets: [{
          data: rows.map(r => r.value),
          backgroundColor: rows.map(r => providerColor[r.provider] + "cc"),
          borderColor:     rows.map(r => providerColor[r.provider]),
          borderWidth: 1, borderRadius: 6,
        }],
      },
      options: commonOpts({
        yMax: Math.ceil(maxV * 1.15 * 10) / 10 || 1,
        yFmt: (v) => "$" + v.toFixed(2),
        tooltipFmt: (c) => `${c.label}: $${c.parsed.y.toFixed(3)} / alert`,
      }),
    });
  }

  function renderFprBars() {
    const ctx = document.getElementById("fpr-bars");
    if (!ctx) return;
    const rows = data.meanPerProvider.combined;
    new Chart(ctx.getContext("2d"), {
      type: "bar",
      data: providerBarData(rows, "fpr"),
      options: commonOpts({
        yMax: 1,
        yFmt: (v) => (v * 100).toFixed(0) + "%",
        tooltipFmt: (c) => `${c.label}: ${(c.parsed.y * 100).toFixed(1)}%`,
      }),
    });
  }

  function renderCompletionBars() {
    const ctx = document.getElementById("completion-bars");
    if (!ctx) return;
    const rows = data.meanPerProvider.combined;
    new Chart(ctx.getContext("2d"), {
      type: "bar",
      data: providerBarData(rows, "fpv"),
      options: commonOpts({
        yMax: 1,
        yFmt: (v) => (v * 100).toFixed(0) + "%",
        tooltipFmt: (c) => `${c.label}: ${(c.parsed.y * 100).toFixed(1)}%`,
      }),
    });
  }

  // ------------- headline F1 bar chart -------------
  let headlineChart = null;
  function renderHeadlineChart(split) {
    const ctx = document.getElementById("headline-chart");
    if (!ctx) return;
    const rows = data.meanPerProvider[split];

    const cfg = {
      type: "bar",
      data: {
        labels: rows.map(r => r.provider),
        datasets: [
          {
            label: "per-flow F1 (macro)",
            data: rows.map(r => r.flowF1),
            backgroundColor: rows.map(r => providerColor[r.provider] + "cc"),
            borderColor: rows.map(r => providerColor[r.provider]),
            borderWidth: 1, borderRadius: 6,
          },
          {
            label: "verdict F1",
            data: rows.map(r => r.verdictF1),
            backgroundColor: rows.map(r => providerColor[r.provider] + "55"),
            borderColor: rows.map(r => providerColor[r.provider]),
            borderWidth: 1, borderRadius: 6,
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: "#6b7280", font: { family: "ui-monospace, Menlo, monospace", size: 12 } } },
          tooltip: {
            callbacks: {
              label: (c) => `${c.dataset.label}: ${c.parsed.y == null ? "n/a" : c.parsed.y.toFixed(3)}`,
            },
          },
        },
        scales: {
          x: { ticks: { color: "#111827", font: { family: "ui-monospace, Menlo, monospace" } }, grid: { color: "#e5e7eb" } },
          y: { beginAtZero: true, max: 1, ticks: { color: "#6b7280" }, grid: { color: "#e5e7eb" } },
        },
      },
    };

    if (headlineChart) {
      headlineChart.data = cfg.data;
      headlineChart.update();
    } else {
      headlineChart = new Chart(ctx.getContext("2d"), cfg);
    }
    renderHeadlineTable(split);
  }

  // ------------- headline table (re-rendered per split) -------------
  function renderHeadlineTable(split) {
    const host = document.getElementById("headline-table");
    if (!host) return;
    const rows = data.meanPerProvider[split];
    host.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Provider</th>
            <th class="num">Completion</th>
            <th class="num">Verdict acc</th>
            <th class="num">Verdict F1</th>
            <th class="num">per-flow F1</th>
            <th class="num">per-pair F1</th>
            <th class="num">per-host F1</th>
            <th class="num">Total cost</th>
            <th class="num">Mean wall</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(r => `
            <tr>
              <td class="provider-cell">${r.provider}</td>
              <td class="num">${fmtPct(r.fpv)}</td>
              <td class="num">${fmtF1(r.verdictAcc)}</td>
              <td class="num">${fmtF1(r.verdictF1)}</td>
              <td class="num"><strong>${fmtF1(r.flowF1)}</strong></td>
              <td class="num">${fmtF1(r.pairF1)}</td>
              <td class="num">${fmtF1(r.hostF1)}</td>
              <td class="num">${fmtUsd(r.costUsd)}</td>
              <td class="num">${fmtSec(r.wallMs)}</td>
            </tr>`).join("")}
        </tbody>
      </table>`;
  }

  // ------------- scatter metric toggle (verdict F1 vs per-flow F1) -------------
  function initMetricToggle() {
    const buttons = document.querySelectorAll(".tabs--compact .tab[data-metric]");
    buttons.forEach(b => b.addEventListener("click", () => {
      buttons.forEach(x => x.classList.toggle("active", x === b));
      renderCostScatter(b.dataset.metric);
    }));
  }

  // ------------- split tabs (only the F1 chart + headline table respond) -------------
  function initSplitTabs() {
    const tabs = document.querySelectorAll(".tabs .tab");
    const descEl = document.getElementById("tab-desc");
    tabs.forEach(t => t.addEventListener("click", () => {
      tabs.forEach(x => x.classList.toggle("active", x === t));
      renderHeadlineChart(t.dataset.split);  // also re-renders the table
      if (descEl && t.dataset.desc) descEl.textContent = t.dataset.desc;
    }));
  }

  // ------------- F1-vs-cost scatter -------------
  // Color = provider; shape = persona. Both axes encode separate dimensions so
  // the reader can see "openai always at the bottom" / "soc_analyst always at
  // the top" patterns at a glance.
  const PERSONA_SHAPE = {
    soc_analyst:        "circle",
    threat_analyst:     "triangle",
    adversary_hunter:   "rectRot",   // diamond
    detection_engineer: "rect",      // square
  };
  // The detection_engineer "rect" shape is a touch smaller than circles at the
  // same radius; bump it slightly so all four read as the same visual weight.
  const PERSONA_RADIUS_BUMP = { rect: 1, rectRot: 0.5 };

  let costScatterChart = null;
  function renderCostScatter(metric = "verdict") {
    const ctx = document.getElementById("cost-chart");
    if (!ctx) return;
    if (costScatterChart) { costScatterChart.destroy(); costScatterChart = null; }
    const field   = metric === "flow" ? "flowF1" : "verdictF1";
    const yLabel  = metric === "flow" ? "per-flow F1" : "verdict F1";
    const yRange  = metric === "flow" ? { beginAtZero: true, max: 0.8 } : { min: 0.7, max: 1.0 };
    const points = data.perPersona.map(r => ({
      x: r.costUsd / SHARED_UNITS, y: r[field], provider: r.provider, persona: r.persona,
    }));
    costScatterChart = new Chart(ctx.getContext("2d"), {
      type: "scatter",
      data: {
        datasets: ["anthropic", "openai", "gemini"].map(p => {
          const pts = points.filter(d => d.provider === p);
          const styles = pts.map(d => PERSONA_SHAPE[d.persona] || "circle");
          const radii  = pts.map(d => 7 + (PERSONA_RADIUS_BUMP[PERSONA_SHAPE[d.persona]] || 0));
          return {
            label: p,
            data: pts,
            backgroundColor: providerColor[p] + "cc",
            borderColor: providerColor[p],
            borderWidth: 1.5,
            pointStyle: styles,
            pointRadius: radii,
            pointHoverRadius: radii.map(r => r + 3),
          };
        }),
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        layout: { padding: { top: 0 } },   // pull the legend tight to the top edge
        plugins: {
          legend: {
            align: "center",
            // Tighten the legend block itself (default takes a lot of vertical
            // room). Spacing between items is now handled by `generateLabels`
            // wrapping each label in extra whitespace.
            labels: {
              color: "#6b7280",
              font: { family: "ui-monospace, Menlo, monospace" },
              usePointStyle: true,
              pointStyle: "circle",
              padding: 8,
              boxWidth: 10,
              boxHeight: 10,
              generateLabels: function(chart) {
                const defaults = Chart.defaults.plugins.legend.labels.generateLabels(chart);
                // Pad each label text with spaces so legend items render
                // farther apart while the legend block itself stays compact.
                return defaults.map(l => ({ ...l, text: "    " + l.text + "    " }));
              },
            },
          },
          tooltip: {
            callbacks: {
              label: (c) => `${c.raw.provider} / ${c.raw.persona}: ${yLabel}=${c.raw.y.toFixed(3)}, $${c.raw.x.toFixed(3)} / alert`,
            },
          },
        },
        scales: {
          x: {
            title: { text: "cost / alert (USD)", color: "#6b7280", display: true },
            ticks: { color: "#6b7280", callback: (v) => "$" + Number(v).toFixed(2) },
            grid: { color: "#e5e7eb" },
            beginAtZero: true,
          },
          y: { title: { text: yLabel, color: "#6b7280", display: true }, ...yRange, ticks: { color: "#6b7280" }, grid: { color: "#e5e7eb" } },
        },
      },
    });
  }

  // ------------- per-persona detail table -------------
  function renderPerPersonaTable() {
    const host = document.getElementById("per-persona-table");
    if (!host) return;
    host.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Provider</th><th>Persona</th>
            <th class="num">Completion</th>
            <th class="num">Verdict acc</th>
            <th class="num">Verdict F1</th>
            <th class="num">per-flow F1</th>
            <th class="num">per-pair F1</th>
            <th class="num">per-host F1</th>
            <th class="num">Mean conf</th>
            <th class="num">Cost (USD)</th>
            <th class="num">p50 wall</th>
            <th class="num">p95 wall</th>
          </tr>
        </thead>
        <tbody>
          ${data.perPersona.map(r => `
            <tr>
              <td class="provider-cell">${r.provider}</td>
              <td class="persona-cell">${r.persona}</td>
              <td class="num">${fmtPct(r.fpv)}</td>
              <td class="num">${fmtF1(r.verdictAcc)}</td>
              <td class="num">${fmtF1(r.verdictF1)}</td>
              <td class="num">${fmtF1(r.flowF1)}</td>
              <td class="num">${fmtF1(r.pairF1)}</td>
              <td class="num">${fmtF1(r.hostF1)}</td>
              <td class="num">${fmtF1(r.conf)}</td>
              <td class="num">${fmtUsd(r.costUsd)}</td>
              <td class="num">${fmtSec(r.p50)}</td>
              <td class="num">${fmtSec(r.p95)}</td>
            </tr>`).join("")}
        </tbody>
      </table>`;
  }

  // ------------- GitHub star count -------------
  // Public REST API; no auth required (unauth rate limit = 60/hr per IP).
  // 5-minute localStorage cache to avoid re-hitting on every page view.
  function loadStarCount() {
    const el = document.getElementById("repo-star-count");
    if (!el) return;
    const REPO = "DeepTempo/socbench";
    const CACHE_KEY = `gh-stars-${REPO}`;
    const TTL_MS = 5 * 60 * 1000;

    const cached = (() => {
      try {
        const raw = localStorage.getItem(CACHE_KEY);
        if (!raw) return null;
        const { count, at } = JSON.parse(raw);
        if (Date.now() - at > TTL_MS) return null;
        return count;
      } catch { return null; }
    })();
    if (cached != null) {
      el.textContent = formatCount(cached);
      return;
    }
    fetch(`https://api.github.com/repos/${REPO}`)
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(d => {
        const c = d.stargazers_count;
        el.textContent = formatCount(c);
        try { localStorage.setItem(CACHE_KEY, JSON.stringify({ count: c, at: Date.now() })); } catch {}
      })
      .catch(() => { /* leave the badge empty if the API fails */ });
  }
  function formatCount(n) {
    if (n == null) return "";
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    return String(n);
  }

  // ------------- bootstrap -------------
  document.addEventListener("DOMContentLoaded", () => {
    initNavScrollspy();
    initSplitTabs();
    // overall (combined-split) charts: render once, never change
    renderCostPerAlertBars();
    renderFprBars();
    renderCompletionBars();
    // tab-driven F1 chart: default to benign
    renderHeadlineChart("benign");
    renderCostScatter("verdict");
    initMetricToggle();
    renderPerPersonaTable();
    loadStarCount();
  });
})();
