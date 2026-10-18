// Run: node --test tests/js/csv.test.mjs tests/js/stats.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { parseCSV, parseCSVObjects } = require("../../docs/csv.js");

const HEADER =
  "timestamp_ist,item_id,line_item,seller,price_type,url,part_number_matched," +
  "mrp_inr,price_inr,in_stock,offers_raw,status,error";

test("quoted field with embedded commas", () => {
  assert.deepEqual(parseCSV('a,"b, c, d",e\n'), [["a", "b, c, d", "e"]]);
});

test("quoted field with embedded newline (LF and CRLF)", () => {
  assert.deepEqual(parseCSV('a,"line1\nline2",c\r\nx,"p\r\nq",z'), [
    ["a", "line1\nline2", "c"],
    ["x", "p\r\nq", "z"],
  ]);
});

test("escaped quotes", () => {
  assert.deepEqual(parseCSV('"say ""hi""",""""\n'), [['say "hi"', '"']]);
});

test("empty input", () => {
  assert.deepEqual(parseCSV(""), []);
  assert.deepEqual(parseCSVObjects(""), { header: [], rows: [] });
});

test("header only (with and without trailing newline)", () => {
  for (const text of [HEADER, HEADER + "\n", HEADER + "\r\n"]) {
    const parsed = parseCSVObjects(text);
    assert.equal(parsed.header.length, 13);
    assert.deepEqual(parsed.rows, []);
  }
});

test("CRLF line endings", () => {
  assert.deepEqual(parseCSV("a,b\r\nc,d\r\n"), [
    ["a", "b"],
    ["c", "d"],
  ]);
});

test("trailing newline does not create an empty record; blank lines skipped", () => {
  assert.deepEqual(parseCSV("a,b\nc,d\n"), [
    ["a", "b"],
    ["c", "d"],
  ]);
  assert.deepEqual(parseCSV("a,b\n\n\nc,d\n\n"), [
    ["a", "b"],
    ["c", "d"],
  ]);
});

test("empty fields are preserved", () => {
  assert.deepEqual(parseCSV(",,\n"), [["", "", ""]]);
  assert.deepEqual(parseCSV('""\n'), [[""]]);
  assert.deepEqual(parseCSV("a,\n"), [["a", ""]]);
});

test("UTF-8 BOM is stripped and non-ASCII text kept", () => {
  assert.deepEqual(parseCSV("﻿name,price\nMRP ₹,1\n"), [
    ["name", "price"],
    ["MRP ₹", "1"],
  ]);
});

test("short rows are padded to the header", () => {
  const { rows } = parseCSVObjects("a,b,c\n1,2\n");
  assert.deepEqual(rows, [{ a: "1", b: "2", c: "" }]);
});

test("unterminated quote throws a clear error", () => {
  assert.throws(() => parseCSV('a,"oops\n'), /unterminated/);
});

test("row shaped like the tracker's offers_raw/error columns", () => {
  const line =
    '2026-10-19T12:00:00+05:30,dev,device,apple_in_edu,education,https://x,Z1KM,,251900,,' +
    '"Get 3 months free. | Says ""hi"", then\nmore",ok,"err, with comma"';
  const { rows } = parseCSVObjects(HEADER + "\r\n" + line + "\r\n");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].offers_raw, 'Get 3 months free. | Says "hi", then\nmore');
  assert.equal(rows[0].error, "err, with comma");
  assert.equal(rows[0].price_inr, "251900");
});

test("parses the real docs/data/prices.csv", () => {
  const path = fileURLToPath(new URL("../../docs/data/prices.csv", import.meta.url));
  const { header, rows } = parseCSVObjects(readFileSync(path, "utf8"));
  assert.deepEqual(header, HEADER.split(","));
  for (const row of rows) {
    assert.equal(Object.keys(row).length, 13);
    assert.match(row.timestamp_ist, /\+05:30$/);
    assert.ok(["ok", "failed", "blocked", "no_match", "suspect"].includes(row.status), row.status);
  }
});
