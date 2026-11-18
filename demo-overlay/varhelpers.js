// Pure helpers for the in-page Variables modal: friendly names, spreadsheet columns, typed cells, row edits.
// No DOM and no extension APIs here, so the same file is loaded by content.js (as DemoVarHelpers) and
// unit-tested with Node:  node --test demo-overlay/tests
(function (root) {
  'use strict';

  // "learners_json" -> "Learners", "moduleProgressMetrics_json" -> "Module progress metrics", "userID" -> "User ID"
  function prettyName(key) {
    let s = String(key == null ? '' : key).replace(/_json$/i, '');
    s = s.replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2');
    const words = s.split(/[\s_.\-]+/).filter(Boolean);
    if (!words.length) return String(key == null ? '' : key);
    const out = words.map((w) => (w.length > 1 && w === w.toUpperCase() && /[A-Z]/.test(w) ? w : w.toLowerCase()));
    out[0] = out[0].charAt(0).toUpperCase() + out[0].slice(1);
    return out.join(' ');
  }

  const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

  function getAt(obj, path) {
    let cur = obj;
    for (const step of path) {
      if (cur === null || cur === undefined) return undefined;
      cur = cur[step];
    }
    return cur;
  }

  // Sets a value at a path, creating the objects in between.
  function setAt(obj, path, value) {
    let cur = obj;
    for (let i = 0; i < path.length - 1; i++) {
      if (!isPlainObject(cur[path[i]])) cur[path[i]] = {};
      cur = cur[path[i]];
    }
    cur[path[path.length - 1]] = value;
  }

  function kindOf(v) {
    if (v === null || v === undefined) return 'null';
    if (Array.isArray(v)) return v.every((x) => x === null || typeof x !== 'object') ? 'list' : 'nested';
    if (typeof v === 'object') return 'object';
    return typeof v;                               // string | number | boolean
  }

  // The columns of a list of objects. One level of nested objects is spread into columns
  // ("Address › City"); lists of plain values become one comma-separated column; anything deeper
  // (lists of objects) stays one "nested" column that opens on its own.
  function columnsFor(rows, maxColumns) {
    const cols = [];
    const seen = new Set();
    const add = (path, label, values) => {
      const id = JSON.stringify(path);
      if (seen.has(id)) return;
      seen.add(id);
      const kinds = new Set(values.map(kindOf).filter((k) => k !== 'null'));
      let type = 'text';
      if (kinds.size === 1) {
        const k = [...kinds][0];
        type = k === 'number' || k === 'boolean' || k === 'list' || k === 'nested' ? k : 'text';
      } else if (kinds.size > 1) {
        type = kinds.has('nested') || kinds.has('object') ? 'nested' : 'text';
      }
      cols.push({ path, label, type });
    };
    const keys = [...new Set(rows.flatMap((r) => (isPlainObject(r) ? Object.keys(r) : [])))];
    for (const k of keys) {
      const vals = rows.map((r) => (isPlainObject(r) ? r[k] : undefined));
      if (vals.some(isPlainObject) && vals.every((v) => v === undefined || v === null || isPlainObject(v))) {
        const subKeys = [...new Set(vals.flatMap((v) => (isPlainObject(v) ? Object.keys(v) : [])))];
        for (const sk of subKeys) {
          add([k, sk], prettyName(k) + ' › ' + prettyName(sk), vals.map((v) => (isPlainObject(v) ? v[sk] : undefined)));
        }
      } else {
        add([k], prettyName(k), vals);
      }
    }
    return typeof maxColumns === 'number' ? cols.slice(0, maxColumns) : cols;
  }

  // What the cell shows as plain text.
  function cellText(value, type) {
    if (value === null || value === undefined) return '';
    if (type === 'list' && Array.isArray(value)) return value.map(String).join(', ');
    return String(value);
  }

  // Turns what the person typed into the value to store. Numbers and true/false stay typed,
  // so the page still gets a number where it expects one.
  function coerce(raw, type, original) {
    const text = String(raw == null ? '' : raw);
    const t = text.trim();
    if (type === 'number') {
      if (t === '') return { ok: true, value: null };
      const n = Number(t.replace(/,/g, ''));
      return Number.isFinite(n) ? { ok: true, value: n } : { ok: false, error: 'Enter a number' };
    }
    if (type === 'boolean') {
      if (/^(true|yes)$/i.test(t)) return { ok: true, value: true };
      if (/^(false|no)$/i.test(t)) return { ok: true, value: false };
      return { ok: false, error: 'Choose yes or no' };
    }
    if (type === 'list') {
      const parts = t === '' ? [] : t.split(',').map((x) => x.trim()).filter((x) => x !== '');
      const numeric = Array.isArray(original) && original.length > 0 && original.every((x) => typeof x === 'number');
      if (numeric) {
        const nums = parts.map((x) => Number(x));
        if (nums.some((n) => !Number.isFinite(n))) return { ok: false, error: 'Enter numbers separated by commas' };
        return { ok: true, value: nums };
      }
      return { ok: true, value: parts };
    }
    return { ok: true, value: text };
  }

  // A new empty row shaped like the others.
  function blankRow(columns) {
    const row = {};
    for (const c of columns) {
      const empty = c.type === 'number' ? 0 : c.type === 'boolean' ? false : c.type === 'list' || c.type === 'nested' ? [] : '';
      setAt(row, c.path, empty);
    }
    return row;
  }

  const cloneRow = (row) => JSON.parse(JSON.stringify(row));

  function insertAfter(rows, index, row) {
    const out = rows.slice();
    out.splice(index + 1, 0, row);
    return out;
  }

  function removeAt(rows, index) {
    return rows.filter((_, i) => i !== index);
  }

  // One-line description of a collapsed variable, with no JSON braces.
  function friendlySummary(v) {
    if (Array.isArray(v)) {
      if (!v.length) return 'Empty list — click to add';
      if (v.every(isPlainObject)) {
        const names = columnsFor(v, 5).map((c) => c.label);
        return v.length + (v.length === 1 ? ' row' : ' rows') + ' · ' + names.join(', ') + (columnsFor(v).length > 5 ? ', …' : '');
      }
      const shown = v.slice(0, 4).map((x) => (x !== null && typeof x === 'object' ? '…' : String(x)));
      return v.length + (v.length === 1 ? ' value' : ' values') + ' · ' + shown.join(', ') + (v.length > 4 ? ', …' : '');
    }
    const keys = Object.keys(v);
    return keys.length + (keys.length === 1 ? ' field' : ' fields') + ' · ' +
      keys.slice(0, 4).map(prettyName).join(', ') + (keys.length > 4 ? ', …' : '');
  }

  // Does an api rule's URL pattern ("*/api/students*", optionally "GET */api/x*") match a call the page made?
  // Same rules as page-hook.js: * matches anything; tried against the full URL, path+query and host+path+query.
  function urlPatternMatches(pattern, method, url, base) {
    let pat = String(pattern || '').trim();
    let want = '';
    const m = /^(GET|POST|PUT|PATCH|DELETE)\s+/i.exec(pat);
    if (m) { want = m[1].toUpperCase(); pat = pat.slice(m[0].length); }
    if (!pat) return false;
    if (want && String(method || 'GET').toUpperCase() !== want) return false;
    let u;
    try { u = new URL(url, base || 'http://localhost/'); } catch (e) { return false; }
    const re = new RegExp('^' + pat.split('*').map((x) => x.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$');
    return [u.href, u.pathname + u.search, u.host + u.pathname + u.search].some((f) => re.test(f));
  }

  const api = { urlPatternMatches, prettyName, getAt, setAt, kindOf, columnsFor, cellText, coerce, blankRow, cloneRow, insertAfter, removeAt, friendlySummary };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.DemoVarHelpers = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
