// Used only by page-hook.js (the script that rewrites the app's JSON). No DOM, no extension APIs, so Node can test it:
//   node --test demo-overlay/tests
//
// An injected list replaces the app's own list. If the invented items leave out a field the real items had, the
// app reads it as undefined (for example a course's version, used to build the next URL: ".../undefined/sections").
// fillMissing returns a copy of `injected` where every field the real item it stands for had, and the injected
// item lacks, is added IF it is a number, true/false, null or an empty list (ids, versions, flags). Text and
// filled lists/objects are never copied from the real data, so no real names or emails reach a demo.
(function (root) {
  'use strict';

  const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

  // Which real item does an injected item stand for? The one it shares an identifying value with (a course id
  // that was copied over, say); values that are unique across the real items count most. If nothing is shared,
  // the real item at the same position (cycling).
  function pickTemplate(item, orig, i) {
    if (!isPlainObject(item) || !orig.every(isPlainObject)) return orig[i % orig.length];
    const counts = {};
    for (const o of orig) {
      for (const k of Object.keys(o)) {
        const v = o[k];
        if (typeof v === 'string' || typeof v === 'number') {
          const key = JSON.stringify([k, v]);
          counts[key] = (counts[key] || 0) + 1;
        }
      }
    }
    let best = -1;
    let bestScore = 0;
    orig.forEach((o, j) => {
      let score = 0;
      for (const k of Object.keys(item)) {
        const v = o[k];
        if ((typeof v === 'string' || typeof v === 'number') && item[k] === v) score += counts[JSON.stringify([k, v])] === 1 ? 10 : 1;
      }
      if (score > bestScore) { bestScore = score; best = j; }
    });
    return best >= 0 ? orig[best] : orig[i % orig.length];
  }

  function complete(inj, orig) {
    if (Array.isArray(inj) && Array.isArray(orig) && orig.length) {
      inj.forEach((item, i) => complete(item, pickTemplate(item, orig, i)));
    } else if (isPlainObject(inj) && isPlainObject(orig)) {
      for (const k of Object.keys(orig)) {
        const ov = orig[k];
        if (!has(inj, k)) {
          if (ov === null || typeof ov === 'number' || typeof ov === 'boolean') inj[k] = ov;
          else if (Array.isArray(ov) && ov.length === 0) inj[k] = [];
        } else {
          complete(inj[k], ov);
        }
      }
    }
  }

  function fillMissing(injected, original) {
    const copy = JSON.parse(JSON.stringify(injected));
    complete(copy, original);
    return copy;
  }

  const api = { fillMissing };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.DemoInjectShape = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
