/*
 * Minimal RFC 4180 CSV parser (no dependencies).
 * Handles quoted fields, embedded commas/newlines, "" escapes, CRLF/LF/CR line endings,
 * a UTF-8 BOM, and a trailing newline. Works as a browser global (MacTrackCSV) and in Node.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.MacTrackCSV = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  /** Parse CSV text into an array of records (arrays of strings). Blank lines are skipped. */
  function parseCSV(text) {
    if (typeof text !== "string") {
      throw new TypeError("parseCSV expects a string");
    }
    if (text.charCodeAt(0) === 0xfeff) {
      text = text.slice(1);
    }

    const records = [];
    let record = [];
    let field = "";
    let inQuotes = false;
    let fieldStarted = false; // distinguishes an empty line from a line with one empty field
    let i = 0;
    const n = text.length;

    function endField() {
      record.push(field);
      field = "";
    }
    function endRecord() {
      endField();
      const isBlank = record.length === 1 && record[0] === "" && !fieldStarted;
      if (!isBlank) {
        records.push(record);
      }
      record = [];
      fieldStarted = false;
    }

    while (i < n) {
      const ch = text[i];
      if (inQuotes) {
        if (ch === '"') {
          if (text[i + 1] === '"') {
            field += '"';
            i += 2;
            continue;
          }
          inQuotes = false;
          i += 1;
          continue;
        }
        field += ch;
        i += 1;
        continue;
      }
      if (ch === '"') {
        inQuotes = true;
        fieldStarted = true;
        i += 1;
      } else if (ch === ",") {
        fieldStarted = true;
        endField();
        i += 1;
      } else if (ch === "\r" || ch === "\n") {
        endRecord();
        i += ch === "\r" && text[i + 1] === "\n" ? 2 : 1;
      } else {
        fieldStarted = true;
        field += ch;
        i += 1;
      }
    }
    if (inQuotes) {
      throw new Error("Malformed CSV: unterminated quoted field");
    }
    if (fieldStarted || field !== "" || record.length > 0) {
      endRecord();
    }
    return records;
  }

  /**
   * Parse CSV with a header row into { header: string[], rows: object[] }.
   * Missing trailing fields become "" so every row has every header key.
   */
  function parseCSVObjects(text) {
    const records = parseCSV(text);
    if (records.length === 0) {
      return { header: [], rows: [] };
    }
    const header = records[0].map(function (h) {
      return h.trim();
    });
    const rows = records.slice(1).map(function (rec) {
      const obj = {};
      header.forEach(function (key, idx) {
        obj[key] = idx < rec.length ? rec[idx] : "";
      });
      return obj;
    });
    return { header: header, rows: rows };
  }

  return { parseCSV: parseCSV, parseCSVObjects: parseCSVObjects };
});
