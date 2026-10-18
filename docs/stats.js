/*
 * Pure statistics + formatting helpers for the MacTrack dashboard.
 * No DOM access; works as a browser global (MacTrackStats) and in Node.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.MacTrackStats = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const STALE_HOURS = 36;
  const HOUR_MS = 60 * 60 * 1000;
  const DAY_MS = 24 * HOUR_MS;
  const IST = "Asia/Kolkata";

  const inrFormatter = new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  });
  const dateTimeFormatter = new Intl.DateTimeFormat("en-IN", {
    timeZone: IST,
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const dateFormatter = new Intl.DateTimeFormat("en-IN", {
    timeZone: IST,
    year: "numeric",
    month: "short",
    day: "2-digit",
  });

  /** ISO-8601 timestamp (e.g. 2026-10-19T12:00:05+05:30) -> epoch ms, or null. */
  function parseTimestamp(value) {
    if (typeof value !== "string" || value.trim() === "") {
      return null;
    }
    const ms = Date.parse(value.trim());
    return Number.isFinite(ms) ? ms : null;
  }

  /** "251900" -> 251900; anything that is not a whole positive number -> null. */
  function parsePrice(value) {
    if (typeof value !== "string" || !/^\d+$/.test(value.trim())) {
      return null;
    }
    const n = Number(value.trim());
    return Number.isSafeInteger(n) && n > 0 ? n : null;
  }

  /** Rows with status ok, a valid timestamp and a valid price, as sorted {time, price} points. */
  function okPoints(rows) {
    const points = [];
    rows.forEach(function (row, index) {
      if (row.status !== "ok") {
        return;
      }
      const time = parseTimestamp(row.timestamp_ist);
      const price = parsePrice(row.price_inr);
      if (time !== null && price !== null) {
        points.push({ time: time, price: price, index: index });
      }
    });
    points.sort(function (a, b) {
      return a.time - b.time || a.index - b.index;
    });
    return points.map(function (p) {
      return { time: p.time, price: p.price };
    });
  }

  /** Map of item_id -> rows (file order), preserving first-seen item order. */
  function groupByItem(rows) {
    const groups = new Map();
    rows.forEach(function (row) {
      const id = (row.item_id || "").trim();
      if (!id) {
        return;
      }
      if (!groups.has(id)) {
        groups.set(id, []);
      }
      groups.get(id).push(row);
    });
    return groups;
  }

  function lastValue(rows, key) {
    for (let i = rows.length - 1; i >= 0; i -= 1) {
      const v = (rows[i][key] || "").trim();
      if (v) {
        return v;
      }
    }
    return "";
  }

  /** Summary numbers for one item's rows. Fields are null when not derivable. */
  function itemStats(itemId, rows) {
    const points = okPoints(rows);
    const latest = points.length ? points[points.length - 1] : null;
    const previous = points.length > 1 ? points[points.length - 2] : null;
    let lowest = null;
    let highest = null;
    points.forEach(function (p) {
      if (lowest === null || p.price < lowest.price) {
        lowest = p; // earliest date at which the low was first reached
      }
      if (highest === null || p.price > highest.price) {
        highest = p;
      }
    });
    return {
      itemId: itemId,
      lineItem: lastValue(rows, "line_item"),
      url: lastValue(rows, "url"),
      points: points,
      latest: latest,
      previous: previous,
      change: changeBetween(previous, latest),
      lowest: lowest,
      highest: highest,
      okCount: points.length,
      totalCount: rows.length,
    };
  }

  function changeBetween(previous, latest) {
    if (!previous || !latest) {
      return null;
    }
    const abs = latest.price - previous.price;
    return { abs: abs, pct: previous.price ? (abs / previous.price) * 100 : null };
  }

  function allItemStats(rows) {
    const result = [];
    groupByItem(rows).forEach(function (itemRows, itemId) {
      result.push(itemStats(itemId, itemRows));
    });
    return result;
  }

  /** Newest ok-row time (epoch ms) across all items, or null. */
  function lastOkTime(rows) {
    let newest = null;
    rows.forEach(function (row) {
      if (row.status !== "ok") {
        return;
      }
      const t = parseTimestamp(row.timestamp_ist);
      if (t !== null && (newest === null || t > newest)) {
        newest = t;
      }
    });
    return newest;
  }

  /** True when there has never been an ok check, or the last one is older than `hours`. */
  function isStale(lastOkMs, nowMs, hours) {
    const limit = (hours === undefined ? STALE_HOURS : hours) * HOUR_MS;
    return lastOkMs === null || nowMs - lastOkMs > limit;
  }

  /** Per-item freshness: when its latest ok price was seen, and whether that is too old. */
  function freshness(stats, nowMs, hours) {
    const asOf = stats && stats.latest ? stats.latest.time : null;
    return { asOf: asOf, stale: isStale(asOf, nowMs, hours) };
  }

  /** Badge colour class for a row status: only "ok" is green; failed/blocked/no_match/suspect are red. */
  function statusClass(status) {
    return status === "ok" ? "ok" : "bad";
  }

  /**
   * Device + AppleCare+ totals, pairing items by line_item and shared page url.
   * Returns [{device, applecare, total}] only where both have a latest ok price.
   */
  function combinedTotals(statsList) {
    const devices = statsList.filter(function (s) {
      return s.lineItem === "device" && s.latest;
    });
    const plans = statsList.filter(function (s) {
      return s.lineItem === "applecare" && s.latest;
    });
    const pairs = [];
    devices.forEach(function (device) {
      const plan = plans.find(function (p) {
        return p.url && p.url === device.url;
      });
      if (plan) {
        pairs.push({ device: device, applecare: plan, total: device.latest.price + plan.latest.price });
      }
    });
    return pairs;
  }

  /** Points within the last `days` days of `nowMs`; days === null means all. */
  function filterRange(points, days, nowMs) {
    if (days === null || days === undefined) {
      return points.slice();
    }
    const cutoff = nowMs - days * DAY_MS;
    return points.filter(function (p) {
      return p.time >= cutoff;
    });
  }

  function formatInr(amount) {
    return amount === null || amount === undefined ? "—" : inrFormatter.format(amount);
  }

  function formatChange(change) {
    if (!change) {
      return "—";
    }
    if (change.abs === 0) {
      return "No change";
    }
    const sign = change.abs > 0 ? "+" : "−";
    const pct = change.pct === null ? "" : " (" + sign + Math.abs(change.pct).toFixed(1) + "%)";
    return sign + inrFormatter.format(Math.abs(change.abs)) + pct;
  }

  function formatDateTimeIST(ms) {
    return ms === null || ms === undefined ? "—" : dateTimeFormatter.format(new Date(ms)) + " IST";
  }

  function formatDateIST(ms) {
    return ms === null || ms === undefined ? "—" : dateFormatter.format(new Date(ms));
  }

  return {
    STALE_HOURS: STALE_HOURS,
    parseTimestamp: parseTimestamp,
    parsePrice: parsePrice,
    okPoints: okPoints,
    groupByItem: groupByItem,
    itemStats: itemStats,
    allItemStats: allItemStats,
    lastOkTime: lastOkTime,
    isStale: isStale,
    freshness: freshness,
    statusClass: statusClass,
    combinedTotals: combinedTotals,
    filterRange: filterRange,
    formatInr: formatInr,
    formatChange: formatChange,
    formatDateTimeIST: formatDateTimeIST,
    formatDateIST: formatDateIST,
  };
});
