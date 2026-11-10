// Demo Overlay - page hook. Runs in the PAGE's own JavaScript world ("world": "MAIN"),
// so it can wrap fetch and XMLHttpRequest and edit JSON API responses before the app sees them.
// It cannot use chrome.* APIs; content.js (isolated world) sends it the rules by window.postMessage.

(() => {
  if (window.__demoOverlayHook) return;
  window.__demoOverlayHook = true;

  const TAG = '__demoOverlay';
  const MAX_CAPTURES = 40;
  const MAX_TEXT = 300000;

  let rules = [];            // compiled: [{ method, re, edits: [{ tokens, value }] }]
  let ready = false;         // true once content.js has sent the rules (or after a safety timeout)
  let markReady;
  const readyPromise = new Promise((r) => { markReady = r; });
  setTimeout(() => { ready = true; markReady(); }, 1500);   // never hold the page for long

  let shadowSeen = false;    // the app has created at least one shadow root

  const captures = [];       // last JSON responses the page received (originals), for the popup
  let captureSeq = 0;

  // ---------- messages from content.js ----------
  const send = (msg) => window.postMessage({ [TAG]: 'from-page', ...msg }, '*');

  window.addEventListener('message', (e) => {
    const m = e.data;
    if (e.source !== window || !m || m[TAG] !== 'to-page') return;
    if (m.type === 'rules') {
      rules = compile(m.rules || []);
      ready = true;
      markReady();
      if (shadowSeen) send({ type: 'shadow' });
    } else if (m.type === 'captures') {
      send({ type: 'reply', reqId: m.reqId, data: captures.map(({ id, method, url, status, size }) => ({ id, method, url, status, size })).reverse() });
    } else if (m.type === 'capture') {
      const c = captures.find((x) => x.id === m.id);
      send({ type: 'reply', reqId: m.reqId, data: c ? { id: c.id, method: c.method, url: c.url, text: c.text } : null });
    }
  });
  send({ type: 'hello' });   // in case content.js is already listening and holds rules

  // ---------- shadow DOM ----------
  // Closed shadow roots hide their contents from every extension. Opening them lets rules reach inside.
  const origAttach = window.Element && Element.prototype.attachShadow;
  if (typeof origAttach === 'function') {
    Element.prototype.attachShadow = function (init) {
      if (!shadowSeen) { shadowSeen = true; send({ type: 'shadow' }); }
      return origAttach.call(this, init && init.mode === 'closed' ? Object.assign({}, init, { mode: 'open' }) : init);
    };
  }

  // ---------- rule compiling and matching ----------
  function globToRegExp(glob) {
    return new RegExp('^' + glob.split('*').map((s) => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$');
  }

  function parsePath(path) {
    const tokens = [];
    const re = /([^.\[\]]+)|\[(\d+|\*)\]/g;
    let m;
    while ((m = re.exec(path)) !== null) {
      tokens.push(m[1] !== undefined ? m[1] : m[2] === '*' ? '*' : Number(m[2]));
    }
    return tokens;
  }

  function compile(list) {
    const out = [];
    for (const r of list) {
      if (!r || typeof r.pattern !== 'string' || !Array.isArray(r.edits)) continue;
      let pattern = r.pattern.trim();
      let method = '';
      const mm = pattern.match(/^(GET|POST|PUT|PATCH|DELETE)\s+(.*)$/i);
      if (mm) { method = mm[1].toUpperCase(); pattern = mm[2]; }
      if (!pattern) continue;
      const edits = r.edits
        .filter((e) => e && typeof e.path === 'string')
        .map((e) => ({ tokens: parsePath(e.path), value: e.value }))
        .filter((e) => e.tokens.length);
      if (edits.length) out.push({ method, re: globToRegExp(pattern), edits });
    }
    return out;
  }

  function matching(method, urlStr) {
    let u;
    try { u = new URL(urlStr, location.href); } catch (e) { return []; }
    const forms = [u.href, u.pathname + u.search, u.host + u.pathname + u.search];
    return rules.filter((r) => (!r.method || r.method === method) && forms.some((f) => r.re.test(f)));
  }

  // ---------- editing the JSON ----------
  // A new value keeps the type of the old one: "1,980,500" replaces a number with a number.
  function coerce(old, v) {
    if (typeof v === 'string') {
      if (typeof old === 'number' && /^\s*-?(\d[\d,]*(\.\d+)?|\.\d+)\s*$/.test(v)) return Number(v.replace(/[,\s]/g, ''));
      if (typeof old === 'boolean' && /^\s*(true|false)\s*$/i.test(v)) return v.trim().toLowerCase() === 'true';
    }
    return v;
  }

  // Only changes values that already exist. It never invents new keys.
  function setAt(node, tokens, v, count) {
    if (node === null || typeof node !== 'object' || !tokens.length) return;
    const t = tokens[0];
    const rest = tokens.slice(1);
    if (t === '*') {
      if (!Array.isArray(node)) return;
      node.forEach((_, i) => {
        if (rest.length) setAt(node[i], rest, v, count);
        else { node[i] = coerce(node[i], v); count.n++; }
      });
      return;
    }
    if (!Object.prototype.hasOwnProperty.call(node, t)) return;
    if (rest.length) setAt(node[t], rest, v, count);
    else { node[t] = coerce(node[t], v); count.n++; }
  }

  function applyEdits(data, hits) {
    const count = { n: 0 };
    for (const r of hits) for (const e of r.edits) setAt(data, e.tokens, e.value, count);
    return count.n;
  }

  function capture(method, url, status, text) {
    if (typeof text !== 'string' || !text || text.length > MAX_TEXT) return;
    const first = text.trimStart()[0];
    if (first !== '{' && first !== '[') return;
    let href;
    try { href = new URL(url, location.href).href; } catch (e) { return; }
    const i = captures.findIndex((c) => c.method === method && c.url === href);
    if (i >= 0) captures.splice(i, 1);           // a repeated call replaces the older copy
    captures.push({ id: ++captureSeq, method, url: href, status, size: text.length, text });
    if (captures.length > MAX_CAPTURES) captures.shift();
  }

  // ---------- fetch ----------
  const origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function (input, init) {
      const p = origFetch.apply(this, arguments);
      let method = 'GET';
      let url = '';
      try {
        const isReq = typeof Request !== 'undefined' && input instanceof Request;
        url = isReq ? input.url : String(input && input.url ? input.url : input);
        method = String((init && init.method) || (isReq && input.method) || 'GET').toUpperCase();
      } catch (e) {
        return p;
      }
      return p.then((res) => handleFetch(res, method, url));
    };
  }

  async function handleFetch(res, method, url) {
    try {
      if (!res || res.type === 'opaque' || !res.ok || [204, 205, 304].includes(res.status)) return res;
      if (!/json/i.test(res.headers.get('content-type') || '')) return res;
      if (!ready) await readyPromise;                     // wait (briefly) for the rules
      const text = await res.clone().text();
      capture(method, url, res.status, text);
      const hits = matching(method, url);
      if (!hits.length) return res;
      let data;
      try { data = JSON.parse(text); } catch (e) { return res; }
      if (!applyEdits(data, hits)) return res;
      const headers = new Headers(res.headers);
      headers.delete('content-length');
      headers.delete('content-encoding');
      const out = new Response(JSON.stringify(data), { status: res.status, statusText: res.statusText, headers });
      try { Object.defineProperty(out, 'url', { value: res.url }); } catch (e) {}
      return out;
    } catch (e) {
      return res;                                          // never break the page
    }
  }

  // ---------- XMLHttpRequest ----------
  const xhrProto = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
  if (xhrProto) {
    const origOpen = xhrProto.open;
    const textDesc = Object.getOwnPropertyDescriptor(xhrProto, 'responseText');
    const respDesc = Object.getOwnPropertyDescriptor(xhrProto, 'response');

    const origSend = xhrProto.send;
    const origAbort = xhrProto.abort;

    xhrProto.open = function (method, url, isAsync) {
      try {
        this.__dm = { method: String(method || 'GET').toUpperCase(), url: String(url), mod: undefined, async: isAsync !== false, waiting: false, cancelled: false };
      } catch (e) {}
      return origOpen.apply(this, arguments);
    };

    // Until content.js has delivered the rules, hold back the request itself (a response can't be
    // held once it has arrived). Synchronous requests can't wait and go straight out.
    xhrProto.send = function () {
      const m = this.__dm;
      if (ready || !m || !m.async) return origSend.apply(this, arguments);
      const xhr = this;
      const args = arguments;
      m.waiting = true;
      readyPromise.then(() => {
        m.waiting = false;
        if (!m.cancelled) { try { origSend.apply(xhr, args); } catch (e) {} }
      });
    };

    xhrProto.abort = function () {
      if (this.__dm && this.__dm.waiting) this.__dm.cancelled = true;
      return origAbort.apply(this, arguments);
    };

    // The edited copy is worked out once per request, the first time the page reads the response.
    const modified = (xhr) => {
      const m = xhr.__dm;
      if (!m || xhr.readyState !== 4 || !ready || xhr.status < 200 || xhr.status >= 300) return null;
      if (m.mod !== undefined) return m.mod;
      m.mod = null;
      try {
        const type = xhr.responseType;
        let data;
        let original;
        if (type === '' || type === 'text') {
          original = textDesc.get.call(xhr);
          if (!/json/i.test(xhr.getResponseHeader('content-type') || '') && !/^\s*[{[]/.test(original)) return null;
          data = JSON.parse(original);
        } else if (type === 'json') {
          data = respDesc.get.call(xhr);
          original = JSON.stringify(data);
          data = JSON.parse(original);
        } else {
          return null;
        }
        capture(m.method, m.url, xhr.status, original);
        const hits = matching(m.method, m.url);
        if (hits.length && applyEdits(data, hits)) m.mod = { type, data };
      } catch (e) {
        m.mod = null;
      }
      return m.mod;
    };

    if (textDesc && textDesc.get) {
      Object.defineProperty(xhrProto, 'responseText', {
        configurable: true,
        enumerable: textDesc.enumerable,
        get() {
          const v = textDesc.get.call(this);
          const mod = modified(this);
          return mod ? JSON.stringify(mod.data) : v;
        },
      });
    }
    if (respDesc && respDesc.get) {
      Object.defineProperty(xhrProto, 'response', {
        configurable: true,
        enumerable: respDesc.enumerable,
        get() {
          const v = respDesc.get.call(this);
          const mod = modified(this);
          if (!mod) return v;
          return mod.type === 'json' ? mod.data : JSON.stringify(mod.data);
        },
      });
    }
  }
})();
