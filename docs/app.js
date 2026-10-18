/*
 * MacTrack dashboard: fetch data/prices.csv, compute stats, render with DOM APIs.
 * CSV-derived text is only ever assigned via textContent (never innerHTML).
 */
(function () {
  "use strict";

  const CSV_URL = "data/prices.csv";
  const RECENT_ROWS = 30;
  const ERROR_PREVIEW_LEN = 80;
  const RANGES = [
    { label: "30 days", days: 30 },
    { label: "90 days", days: 90 },
    { label: "All", days: null },
  ];
  const LINE_ITEM_LABELS = { device: "Device", applecare: "AppleCare+" };
  const DAY_MS = 24 * 60 * 60 * 1000;
  const TARGET_X_TICKS = 5;

  const S = window.MacTrackStats;
  const charts = [];
  let currentRange = null;
  let itemStatsList = [];

  // ------------------------------------------------------------------ helpers

  function el(tag, opts, children) {
    const node = document.createElement(tag);
    const o = opts || {};
    if (o.className) node.className = o.className;
    if (o.text !== undefined) node.textContent = o.text;
    if (o.title) node.title = o.title;
    (children || []).forEach(function (c) {
      if (c) node.appendChild(c);
    });
    return node;
  }

  function safeHttpsUrl(url) {
    try {
      const parsed = new URL(url);
      return parsed.protocol === "https:" ? parsed.href : null;
    } catch (e) {
      return null;
    }
  }

  function showMessage(kind, text) {
    const box = document.getElementById("messages");
    box.appendChild(el("div", { className: "banner banner-" + kind, text: text }));
  }

  function statRow(label, value, extraClass) {
    return el("div", { className: "stat" }, [
      el("dt", { text: label }),
      el("dd", { text: value, className: extraClass || "" }),
    ]);
  }

  function changeClass(change) {
    if (!change || change.abs === 0) return "";
    return change.abs < 0 ? "down" : "up";
  }

  // ------------------------------------------------------------------- header

  function renderHeader(rows) {
    const lastOk = S.lastOkTime(rows);
    document.getElementById("last-ok").textContent =
      "Last successful check: " + (lastOk === null ? "never" : S.formatDateTimeIST(lastOk));
    if (S.isStale(lastOk, Date.now())) {
      showMessage(
        "warn",
        lastOk === null
          ? "Tracker may be down: no successful check recorded yet."
          : "Tracker may be down: last successful check is more than " + S.STALE_HOURS + " hours old."
      );
    }
  }

  // -------------------------------------------------------------------- cards

  function asOfLine(stats) {
    const fresh = S.freshness(stats, Date.now());
    const line = el("p", {
      className: "card-asof",
      text: fresh.asOf === null ? "No successful check yet" : "as of " + S.formatDateTimeIST(fresh.asOf),
    });
    if (fresh.stale) {
      line.appendChild(document.createTextNode(" "));
      line.appendChild(
        el("span", {
          className: "badge badge-stale",
          text: "Stale",
          title: "This item's latest successful check is more than " + S.STALE_HOURS + " hours old.",
        })
      );
    }
    return line;
  }

  function itemCard(stats) {
    const kind = LINE_ITEM_LABELS[stats.lineItem] || stats.lineItem || "Item";
    const card = el("article", { className: "card" }, [
      el("p", { className: "card-kind", text: kind }),
      el("h2", { className: "card-title", text: stats.itemId }),
      el("p", {
        className: "price",
        text: stats.latest ? S.formatInr(stats.latest.price) : "No price yet",
      }),
      asOfLine(stats),
    ]);
    const dl = el("dl", { className: "stats" }, [
      statRow("Change vs previous", S.formatChange(stats.change), changeClass(stats.change)),
      statRow(
        "Lowest ever",
        stats.lowest ? S.formatInr(stats.lowest.price) + " on " + S.formatDateIST(stats.lowest.time) : "—"
      ),
      statRow("Highest ever", stats.highest ? S.formatInr(stats.highest.price) : "—"),
      statRow("OK checks", stats.okCount + " of " + stats.totalCount),
    ]);
    card.appendChild(dl);

    const href = safeHttpsUrl(stats.url);
    if (href) {
      const link = el("a", { className: "card-link", text: "Education price page ↗" });
      link.href = href;
      link.rel = "noopener noreferrer";
      link.target = "_blank";
      card.appendChild(link);
    }
    return card;
  }

  function totalCard(pair) {
    return el("article", { className: "card card-total" }, [
      el("p", { className: "card-kind", text: "Derived sum" }),
      el("h2", { className: "card-title", text: "Device + AppleCare+" }),
      el("p", { className: "price", text: S.formatInr(pair.total) }),
      el("dl", { className: "stats" }, [
        statRow(LINE_ITEM_LABELS.device, S.formatInr(pair.device.latest.price)),
        statRow(LINE_ITEM_LABELS.applecare, S.formatInr(pair.applecare.latest.price)),
      ]),
      el("p", {
        className: "note",
        text:
          "Sum of the latest successful prices above, not a price quoted by Apple. " +
          "Choosing the 96W adapter adds about INR 3,100 (per Apple's page).",
      }),
    ]);
  }

  function renderCards(statsList) {
    const container = document.getElementById("cards");
    container.replaceChildren();
    statsList.forEach(function (s) {
      container.appendChild(itemCard(s));
    });
    S.combinedTotals(statsList).forEach(function (pair) {
      container.appendChild(totalCard(pair));
    });
  }

  // ------------------------------------------------------------------- charts

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function destroyCharts() {
    while (charts.length) charts.pop().destroy();
  }

  function renderCharts() {
    const container = document.getElementById("charts");
    destroyCharts();
    container.replaceChildren();
    if (typeof window.Chart === "undefined") {
      container.appendChild(
        el("p", { className: "empty", text: "Charts unavailable: the Chart.js library failed to load." })
      );
      return;
    }
    const now = Date.now();
    itemStatsList.forEach(function (stats) {
      const points = S.filterRange(stats.points, currentRange, now);
      const figure = el("figure", { className: "chart-card" }, [
        el("figcaption", {
          text: stats.itemId + " (" + (LINE_ITEM_LABELS[stats.lineItem] || stats.lineItem) + ")",
        }),
      ]);
      container.appendChild(figure);
      if (points.length === 0) {
        figure.appendChild(el("p", { className: "empty", text: "No successful checks in this range." }));
        return;
      }
      const wrap = el("div", { className: "chart-wrap" });
      const canvas = document.createElement("canvas");
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", "Price history for " + stats.itemId);
      wrap.appendChild(canvas);
      figure.appendChild(wrap);
      charts.push(makeChart(canvas, points));
    });
  }

  function makeChart(canvas, points) {
    const line = cssVar("--accent");
    const grid = cssVar("--grid");
    const text = cssVar("--muted");
    const single = points.length === 1;
    const xs = points.map(function (p) {
      return p.time;
    });
    const minX = Math.min.apply(null, xs);
    const maxX = Math.max.apply(null, xs);
    const pad = single ? DAY_MS : 0; // give a lone point some horizontal room
    const spanDays = Math.max(1, (maxX - minX + 2 * pad) / DAY_MS);
    const stepMs = Math.max(1, Math.ceil(spanDays / TARGET_X_TICKS)) * DAY_MS;

    return new window.Chart(canvas, {
      type: "line",
      data: {
        datasets: [
          {
            data: points.map(function (p) {
              return { x: p.time, y: p.price };
            }),
            stepped: "before",
            borderColor: line,
            backgroundColor: line,
            borderWidth: 2,
            pointRadius: single || points.length < 15 ? 3 : 0,
            pointHoverRadius: 5,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        parsing: false,
        interaction: { mode: "nearest", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: function (items) {
                return S.formatDateTimeIST(items[0].parsed.x);
              },
              label: function (item) {
                return S.formatInr(item.parsed.y);
              },
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            min: minX - pad,
            max: maxX + pad,
            grid: { color: grid },
            // Whole-day steps, then drop ticks whose IST date label repeats.
            afterBuildTicks: function (axis) {
              const seen = new Set();
              axis.ticks = axis.ticks.filter(function (t) {
                const label = S.formatDateIST(t.value);
                if (seen.has(label)) return false;
                seen.add(label);
                return true;
              });
            },
            ticks: {
              color: text,
              stepSize: stepMs,
              callback: function (value) {
                return S.formatDateIST(value);
              },
            },
          },
          y: {
            beginAtZero: false,
            grace: single ? "2%" : "5%",
            grid: { color: grid },
            ticks: {
              color: text,
              callback: function (value) {
                return S.formatInr(value);
              },
            },
          },
        },
      },
    });
  }

  function renderRangeButtons() {
    const group = document.getElementById("range");
    group.replaceChildren();
    RANGES.forEach(function (r) {
      const btn = el("button", { text: r.label });
      btn.type = "button";
      btn.setAttribute("aria-pressed", String(r.days === currentRange));
      btn.addEventListener("click", function () {
        currentRange = r.days;
        renderRangeButtons();
        renderCharts();
      });
      group.appendChild(btn);
    });
  }

  // -------------------------------------------------------------------- table

  function statusBadge(status) {
    return el("span", { className: "badge badge-" + S.statusClass(status), text: status || "?" });
  }

  function renderRecent(rows) {
    const tbody = document.getElementById("recent-body");
    tbody.replaceChildren();
    const recent = rows.slice(-RECENT_ROWS).reverse();
    if (recent.length === 0) {
      const td = el("td", { className: "empty", text: "No checks recorded yet." });
      td.colSpan = 5;
      tbody.appendChild(el("tr", {}, [td]));
      return;
    }
    recent.forEach(function (row) {
      const error = (row.error || "").trim();
      const shortError =
        error.length > ERROR_PREVIEW_LEN ? error.slice(0, ERROR_PREVIEW_LEN - 1) + "…" : error;
      const price = S.parsePrice(row.price_inr || "");
      tbody.appendChild(
        el("tr", {}, [
          el("td", { className: "nowrap", text: S.formatDateTimeIST(S.parseTimestamp(row.timestamp_ist)) }),
          el("td", { text: row.item_id || "" }),
          el("td", {}, [statusBadge(row.status)]),
          el("td", { className: "num", text: price === null ? "—" : S.formatInr(price) }),
          el("td", { className: "error-cell", text: shortError, title: error }),
        ])
      );
    });
  }

  // --------------------------------------------------------------------- main

  async function load() {
    let text;
    try {
      const response = await fetch(CSV_URL + "?t=" + Date.now(), { cache: "no-store" });
      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }
      text = await response.text();
    } catch (err) {
      const hint =
        location.protocol === "file:"
          ? " Open the page through a web server (see README); file:// cannot fetch the CSV."
          : "";
      showMessage("error", "Could not load " + CSV_URL + " (" + err.message + ")." + hint);
      document.getElementById("last-ok").textContent = "Last successful check: unknown";
      return;
    }

    let parsed;
    try {
      parsed = window.MacTrackCSV.parseCSVObjects(text);
    } catch (err) {
      showMessage("error", "Could not parse " + CSV_URL + ": " + err.message);
      return;
    }

    const rows = parsed.rows;
    renderHeader(rows);
    if (rows.length === 0) {
      showMessage("info", "No price checks recorded yet. The first scheduled run will add data.");
    }
    itemStatsList = S.allItemStats(rows);
    renderCards(itemStatsList);
    renderRangeButtons();
    renderCharts();
    renderRecent(rows);

    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", renderCharts);
    }
  }

  document.addEventListener("DOMContentLoaded", load);
})();
