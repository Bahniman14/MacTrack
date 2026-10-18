// Run: node --test tests/js/csv.test.mjs tests/js/stats.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const S = require("../../docs/stats.js");

const URL_A = "https://www.apple.com/in-edu/shop/buy-mac/a";
const HOUR = 60 * 60 * 1000;

function row(day, price, extra = {}) {
  const d = String(day).padStart(2, "0");
  return {
    timestamp_ist: `2026-10-${d}T12:00:00+05:30`,
    item_id: "dev",
    line_item: "device",
    url: URL_A,
    price_inr: price === null ? "" : String(price),
    status: price === null ? "failed" : "ok",
    error: "",
    ...extra,
  };
}

test("single data point", () => {
  const s = S.itemStats("dev", [row(1, 251900)]);
  assert.equal(s.latest.price, 251900);
  assert.equal(s.previous, null);
  assert.equal(s.change, null);
  assert.equal(s.lowest.price, 251900);
  assert.equal(s.highest.price, 251900);
  assert.equal(s.okCount, 1);
  assert.equal(S.formatChange(s.change), "—");
});

test("equal prices: no change, lowest keeps the earliest date", () => {
  const s = S.itemStats("dev", [row(1, 251900), row(2, 251900)]);
  assert.deepEqual(s.change, { abs: 0, pct: 0 });
  assert.equal(S.formatChange(s.change), "No change");
  assert.equal(s.lowest.time, Date.parse("2026-10-01T12:00:00+05:30"));
});

test("a drop", () => {
  const s = S.itemStats("dev", [row(1, 260000), row(2, 255000)]);
  assert.equal(s.change.abs, -5000);
  assert.ok(Math.abs(s.change.pct - -1.923) < 0.01);
  assert.equal(S.formatChange(s.change), "−₹5,000 (−1.9%)");
  assert.equal(s.lowest.price, 255000);
  assert.equal(s.highest.price, 260000);
});

test("a rise", () => {
  const s = S.itemStats("dev", [row(1, 251900), row(2, 255000)]);
  assert.equal(s.change.abs, 3100);
  assert.equal(S.formatChange(s.change), "+₹3,100 (+1.2%)");
});

test("missing values and non-ok rows are ignored", () => {
  const rows = [
    row(1, 260000),
    row(2, null, { status: "blocked", error: "HTTP 403" }),
    row(3, null, { status: "ok" }), // ok but no price
    row(4, 999999, { status: "no_match" }), // price on a non-ok row never counts
    row(5, 258000, { timestamp_ist: "not a date" }),
    row(6, 255000),
  ];
  const s = S.itemStats("dev", rows);
  assert.deepEqual(
    s.points.map((p) => p.price),
    [260000, 255000]
  );
  assert.equal(s.okCount, 2);
  assert.equal(s.totalCount, 6);
  assert.equal(s.change.abs, -5000);
  assert.equal(S.parsePrice(""), null);
  assert.equal(S.parsePrice("12.5"), null);
  assert.equal(S.parsePrice("abc"), null);
});

test("no ok rows at all", () => {
  const s = S.itemStats("dev", [row(1, null)]);
  assert.equal(s.latest, null);
  assert.equal(s.lowest, null);
  assert.equal(S.lastOkTime([row(1, null)]), null);
});

test("last ok time and stale detection with injected now", () => {
  const rows = [row(1, 251900), row(2, null), row(3, 251900, { item_id: "ac" })];
  const lastOk = S.lastOkTime(rows);
  assert.equal(lastOk, Date.parse("2026-10-03T12:00:00+05:30"));
  assert.equal(S.isStale(lastOk, lastOk + 36 * HOUR), false);
  assert.equal(S.isStale(lastOk, lastOk + 36 * HOUR + 1), true);
  assert.equal(S.isStale(lastOk, lastOk + 2 * HOUR), false);
  assert.equal(S.isStale(null, lastOk), true);
});

test("groups dynamically by item_id and pairs totals by line_item + url", () => {
  const rows = [
    row(1, 251900),
    row(1, 26900, { item_id: "ac", line_item: "applecare" }),
    row(1, 300000, { item_id: "other", url: "https://www.apple.com/in-edu/other" }),
    row(2, 250000),
  ];
  const stats = S.allItemStats(rows);
  assert.deepEqual(
    stats.map((s) => s.itemId),
    ["dev", "ac", "other"]
  );
  const totals = S.combinedTotals(stats);
  assert.equal(totals.length, 1);
  assert.equal(totals[0].total, 250000 + 26900);
  assert.equal(totals[0].device.itemId, "dev");
});

test("per-item freshness: each item is judged by its own latest ok row", () => {
  const rows = [
    row(1, 251900), // device last ok on day 1
    row(2, null), // device fails afterwards
    row(1, 26900, { item_id: "ac", line_item: "applecare" }),
    row(3, 26900, { item_id: "ac", line_item: "applecare" }),
  ];
  const [dev, ac] = S.allItemStats(rows);
  const now = Date.parse("2026-10-03T13:00:00+05:30"); // 49h after day 1, 1h after day 3

  assert.deepEqual(S.freshness(dev, now), { asOf: Date.parse("2026-10-01T12:00:00+05:30"), stale: true });
  assert.deepEqual(S.freshness(ac, now), { asOf: Date.parse("2026-10-03T12:00:00+05:30"), stale: false });
  assert.equal(S.isStale(S.lastOkTime(rows), now), false); // global banner stays quiet
});

test("freshness of an item with no ok rows", () => {
  const s = S.itemStats("dev", [row(1, null)]);
  assert.deepEqual(S.freshness(s, Date.now()), { asOf: null, stale: true });
});

test("status classes: only ok is green; suspect is red", () => {
  assert.equal(S.statusClass("ok"), "ok");
  for (const status of ["failed", "blocked", "no_match", "suspect", "", undefined]) {
    assert.equal(S.statusClass(status), "bad");
  }
});

test("suspect rows are never used as prices", () => {
  const s = S.itemStats("dev", [row(1, 251900), row(2, 1, { status: "suspect" })]);
  assert.equal(s.latest.price, 251900);
  assert.equal(s.lowest.price, 251900);
  assert.equal(s.okCount, 1);
});

test("range filter", () => {
  const points = S.okPoints([row(1, 1), row(10, 2), row(20, 3)]);
  const now = Date.parse("2026-10-21T00:00:00+05:30");
  assert.equal(S.filterRange(points, null, now).length, 3);
  assert.deepEqual(
    S.filterRange(points, 15, now).map((p) => p.price),
    [2, 3]
  );
});

test("formatting: Indian grouping and IST", () => {
  assert.equal(S.formatInr(251900), "₹2,51,900");
  assert.equal(S.formatInr(10000000), "₹1,00,00,000");
  assert.equal(S.formatInr(null), "—");
  const ms = S.parseTimestamp("2026-10-19T00:30:00+05:30"); // = 18 Oct 19:00 UTC
  assert.match(S.formatDateTimeIST(ms), /19 Oct 2026.*00:30 IST/);
  assert.match(S.formatDateIST(ms), /19 Oct 2026/);
});
