// Demo Overlay - content script
// Applies scenario rules (selector -> replacement) to the page and provides a click-to-pick tool.

(() => {
  if (window.__demoOverlayLoaded) return;
  window.__demoOverlayLoaded = true;

  const KEY = 'demo';
  const IS_TOP = window === window.top;
  // about:blank and srcdoc frames have no host of their own, so they use their parent's
  const HOST = (() => {
    let h = location.hostname;
    if (!h) { try { h = window.parent.location.hostname; } catch (e) {} }
    return (h || '').toLowerCase();
  })();

  let state = { enabled: false, activeProfile: '', profiles: {}, rules: [] };
  const originals = new Map(); // element -> snapshot of original content
  let observer = null;
  let timer = null;
  let uiHost = null;
  let uiRoot = null;
  let picking = false;
  let ui = { toolbar: false };      // presenter toolbar visibility (shared by all tabs)

  // ---------- state ----------
  function normalize(s) {
    s = s || {};
    return {
      enabled: !!s.enabled,
      activeProfile: s.activeProfile || '',
      profiles: s.profiles || {},
      rules: Array.isArray(s.rules) ? s.rules : [],
    };
  }

  async function load() {
    const r = await chrome.storage.local.get(KEY);
    state = normalize(r[KEY]);
  }

  function save() {
    return chrome.storage.local.set({ [KEY]: state });
  }

  function fill(str) {
    const vars = state.profiles[state.activeProfile] || {};
    return String(str == null ? '' : str).replace(/\{\{\s*([\w.-]+)\s*\}\}/g, (m, k) =>
      Object.prototype.hasOwnProperty.call(vars, k) ? vars[k] : m
    );
  }

  // A rule's host can be empty (all), one host, a list ("a.com, b.com") or a wildcard ("*.a.com").
  function hostMatches(pattern) {
    if (!pattern) return true;
    return String(pattern).split(/[\s,;]+/).filter(Boolean).some((p) => {
      p = p.toLowerCase();
      if (p === '*') return true;
      if (p.startsWith('*.')) return HOST === p.slice(2) || HOST.endsWith(p.slice(1));
      return HOST === p;
    });
  }

  // A rule's page limits it to some pages of the app: "/patients*", "/reports/*". It is compared with the
  // path + ?query + #hash that is in the address bar RIGHT NOW, so it keeps working in single-page apps.
  function pageMatches(pattern) {
    if (!pattern) return true;
    const here = location.pathname + location.search + location.hash;
    return String(pattern).split(/[\s,;]+/).filter(Boolean).some((p) => {
      const re = new RegExp('^' + p.split('*').map((x) => x.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$');
      return re.test(here) || re.test(location.pathname);
    });
  }

  // ---------- finding elements ----------
  // Normal CSS; "host-el >>> inner" enters a shadow root; and if a plain selector finds nothing,
  // the open shadow roots of the page are searched as well.
  let shadowSeen = false;     // the page hook reported that the app uses shadow DOM
  let shadowRoots = [];       // open shadow roots found by the last pass

  function collectShadowRoots() {
    const out = [];
    const walk = (root) => {
      root.querySelectorAll('*').forEach((el) => {
        if (el.shadowRoot) { out.push(el.shadowRoot); walk(el.shadowRoot); }
      });
    };
    walk(document);
    return out;
  }

  function queryAll(selector) {
    try {
      if (selector.includes('>>>')) {
        const parts = selector.split(/\s*>>>\s*/).filter(Boolean);
        let roots = [document];
        for (let i = 0; i < parts.length - 1; i++) {
          const next = [];
          roots.forEach((r) => r.querySelectorAll(parts[i]).forEach((el) => el.shadowRoot && next.push(el.shadowRoot)));
          roots = next;
        }
        const out = [];
        roots.forEach((r) => r.querySelectorAll(parts[parts.length - 1]).forEach((el) => out.push(el)));
        return out;
      }
      const found = Array.from(document.querySelectorAll(selector));
      if (found.length || !shadowRoots.length) return found;
      const out = [];
      shadowRoots.forEach((r) => r.querySelectorAll(selector).forEach((el) => out.push(el)));
      return out;
    } catch (e) {
      return [];          // an invalid selector must never break the page
    }
  }

  // ---------- applying rules ----------
  function setAttr(el, name, value) {
    if (value === null || value === undefined) el.removeAttribute(name);
    else el.setAttribute(name, value);
  }

  function snapshot(el) {
    return {
      html: el.innerHTML,
      src: el.getAttribute('src'),
      srcset: el.getAttribute('srcset'),
      style: el.getAttribute('style'),
    };
  }

  function restore(el, s) {
    if (el.innerHTML !== s.html) el.innerHTML = s.html;
    setAttr(el, 'src', s.src);
    setAttr(el, 'srcset', s.srcset);
    setAttr(el, 'style', s.style);
  }

  function restoreAll() {
    for (const [el, s] of originals) restore(el, s);
    originals.clear();
  }

  function applyRule(el, rule) {
    if (!originals.has(el)) originals.set(el, snapshot(el));
    const value = fill(rule.value);
    switch (rule.type) {
      case 'hide':
        el.style.setProperty('display', 'none', 'important');
        break;
      case 'image':
        if (el.tagName === 'IMG') {
          if (el.getAttribute('src') !== value) {
            el.removeAttribute('srcset');
            el.setAttribute('src', value);
          }
        } else {
          el.style.backgroundImage = 'url("' + value.replace(/"/g, '%22') + '")';
        }
        break;
      case 'html':
        if (el.innerHTML !== value) el.innerHTML = value;
        break;
      default:
        if (el.textContent !== value) el.textContent = value;
    }
  }

  function apply() {
    stopObserver();
    try {
      if (!state.enabled) {
        restoreAll();
        return;
      }
      const matched = new Set();
      const hitRules = [];
      shadowRoots = shadowSeen ? collectShadowRoots() : [];
      for (const rule of state.rules) {
        if (rule.disabled) continue;
        if (rule.type === 'api') continue;          // handled by the page hook, not by the DOM pass
        if (!hostMatches(rule.host) || !pageMatches(rule.page)) continue;
        // a placeholder with no value in this profile: leave the page's own text alone, never show "{{...}}"
        if (rule.type !== 'hide' && /\{\{[^}]*\}\}/.test(fill(rule.value))) continue;
        const els = queryAll(rule.selector);
        if (els.length) hitRules.push(rule.id);
        els.forEach((el) => {
          matched.add(el);
          applyRule(el, rule);
        });
      }
      if (!IS_TOP) reportFrame(hitRules);
      // put back anything that no longer has a rule
      for (const [el, s] of originals) {
        if (!matched.has(el)) {
          restore(el, s);
          originals.delete(el);
        }
      }
    } finally {
      startObserver();
      postApi();
      updateBadge();
      renderToolbar();
    }
  }

  // Re-apply when the app re-renders (React/Angular/Vue etc.)
  function startObserver() {
    if (observer || !state.enabled || !document.documentElement) return;
    observer = new MutationObserver(() => {
      clearTimeout(timer);
      timer = setTimeout(apply, 100);
    });
    const opts = { childList: true, subtree: true, characterData: true };
    observer.observe(document.documentElement, opts);
    shadowRoots.forEach((r) => observer.observe(r, opts));
  }

  function stopObserver() {
    if (observer) {
      observer.disconnect();
      observer = null;
    }
  }

  // ---------- UI (shadow DOM so page CSS can't interfere) ----------
  function ensureUI() {
    if (uiRoot && uiHost.isConnected) return uiRoot;
    uiHost = document.createElement('demo-overlay-ui');
    uiHost.style.cssText = 'all:initial;position:fixed;z-index:2147483647;top:0;left:0;width:0;height:0;';
    uiRoot = uiHost.attachShadow({ mode: 'open' });
    uiRoot.innerHTML = `
      <style>
        * { box-sizing: border-box; font-family: system-ui, -apple-system, Segoe UI, sans-serif; }
        .badge { position: fixed; left: 12px; bottom: 12px; background: #111827; color: #fff;
                 font-size: 11px; padding: 5px 10px; border-radius: 999px; opacity: .85;
                 letter-spacing: .04em; pointer-events: none; display: none; }
        .hl { position: fixed; border: 2px solid #2563eb; background: rgba(37,99,235,.15);
              pointer-events: none; border-radius: 3px; }
        .tip { position: fixed; top: 12px; left: 50%; transform: translateX(-50%); background: #111827;
               color: #fff; font-size: 13px; padding: 8px 14px; border-radius: 8px; pointer-events: none; }
        .dlg { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 360px;
               background: #fff; color: #111827; border-radius: 12px; padding: 16px;
               box-shadow: 0 20px 50px rgba(0,0,0,.35); font-size: 13px; }
        .dlg h3 { margin: 0 0 10px; font-size: 14px; }
        .dlg label { display: block; margin: 8px 0 3px; font-weight: 600; font-size: 12px; }
        .dlg select, .dlg textarea { width: 100%; padding: 6px 8px; border: 1px solid #d1d5db;
               border-radius: 6px; font-size: 13px; }
        .dlg textarea { height: 70px; resize: vertical; }
        .dlg .sel { font-family: ui-monospace, Consolas, monospace; font-size: 11px; color: #6b7280;
               word-break: break-all; }
        .dlg .hint { font-size: 11px; color: #6b7280; margin-top: 3px; }
        .row { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
        button { padding: 6px 14px; border-radius: 6px; border: 1px solid #d1d5db; background: #fff;
                 cursor: pointer; font-size: 13px; }
        button.primary { background: #2563eb; border-color: #2563eb; color: #fff; }
        .bar { position: fixed; right: 12px; bottom: 12px; display: none; align-items: center; gap: 6px;
               background: #111827; color: #fff; padding: 6px 8px; border-radius: 10px; font-size: 12px;
               box-shadow: 0 6px 20px rgba(0,0,0,.35); }
        .bar button, .bar select { padding: 4px 9px; font-size: 12px; border-radius: 6px; border: 1px solid #374151;
               background: #1f2937; color: #fff; cursor: pointer; }
        .bar button.primary { background: #2563eb; border-color: #2563eb; }
        .bar b { letter-spacing: .04em; }
        .vdlg { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 660px;
                max-width: 94vw; max-height: 84vh; display: flex; flex-direction: column; background: #fff;
                color: #111827; border-radius: 12px; padding: 14px 16px; box-shadow: 0 20px 50px rgba(0,0,0,.35);
                font-size: 13px; }
        .vhead { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
        .vhead h3 { margin: 0; font-size: 14px; display: flex; align-items: center; gap: 8px; }
        .vhint { font-size: 11px; color: #6b7280; margin: 3px 0 8px; }
        .vmiss { font-size: 11px; color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca;
                 border-radius: 6px; padding: 5px 9px; margin: 0 0 8px; }
        .vscroll { overflow: auto; border: 1px solid #e5e7eb; border-radius: 8px; }
        .vtable { width: 100%; border-collapse: collapse; }
        .vtable th { text-align: left; font-size: 11px; color: #6b7280; padding: 6px 10px;
                     border-bottom: 1px solid #e5e7eb; background: #f9fafb; position: sticky; top: 0; }
        .vtable td { padding: 6px 10px; border-bottom: 1px solid #f3f4f6; vertical-align: top; }
        .vtable tr:last-child td { border-bottom: 0; }
        .vname { font-family: ui-monospace, Consolas, monospace; font-size: 12px; font-weight: 600; }
        .vval { font-family: ui-monospace, Consolas, monospace; font-size: 12px; white-space: pre-wrap;
                word-break: break-all; cursor: pointer; }
        .vval.clamp { display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
        .vdlg textarea { width: 100%; min-height: 64px; max-height: 220px; resize: vertical; padding: 5px 8px;
                 border: 1px solid #93c5fd; border-radius: 6px; font-family: ui-monospace, Consolas, monospace;
                 font-size: 12px; }
        .vdel { border: 0; background: none; color: #9ca3af; cursor: pointer; font-size: 13px; padding: 2px 6px; }
        .vdel:hover { color: #dc2626; }
        .vadd { display: flex; gap: 6px; padding: 8px 2px 0; }
        .vadd input { flex: 1; min-width: 0; padding: 5px 8px; border: 1px solid #d1d5db; border-radius: 6px;
                      font-size: 12px; font-family: ui-monospace, Consolas, monospace; }
        .vadd input.k { flex: 0 0 150px; }
      </style>
      <div class="badge" id="badge"></div>
      <div class="bar" id="bar"><b>DEMO</b>
        <button id="tgl"></button><select id="prof"></select>
        <button id="vars" title="Variables table (Alt+Shift+V)">Variables</button>
        <button id="rld" title="Reload the page (clears anything typed)">Reload</button>
        <button id="hid" title="Hide toolbar (Alt+Shift+T)">x</button></div>`;
    bindToolbar(uiRoot);
    (document.documentElement || document).appendChild(uiHost);
    return uiRoot;
  }

  function updateBadge() {
    if (!IS_TOP) return;          // iframes get their data replaced but show no badge or toolbar
    const root = ensureUI();
    const b = root.getElementById('badge');
    if (!b) return;
    if (state.enabled) {
      b.textContent = 'DEMO DATA' + (state.activeProfile ? ' · ' + state.activeProfile : '') +
        (apiChangedSinceLoad() ? ' · reload to refresh API data' : '');
      b.style.display = 'block';
    } else {
      b.style.display = 'none';
    }
  }

  // ---------- presenter toolbar and shortcuts ----------
  function track(event) {
    if (!IS_TOP) return;
    try {
      chrome.runtime.sendMessage({ type: 'track', event, profile: state.activeProfile, host: HOST }).catch(() => {});
    } catch (e) { /* extension was reloaded; nothing to do */ }
  }

  // API data is fetched once per page load, so when a change affects it the page reloads itself.
  function afterChange() {
    if (apiChangedSinceLoad()) location.reload();
  }

  async function toggleDemo() {
    await load();
    state.enabled = !state.enabled;
    await save();
    apply();
    track(state.enabled ? 'demo_on' : 'demo_off');
    afterChange();
  }

  async function setProfile(name) {
    await load();
    if (!state.profiles[name]) return;
    state.activeProfile = name;
    await save();
    apply();
    track('profile_switch');
    try { chrome.runtime.sendMessage({ type: 'setMyProfile', profile: name }).catch(() => {}); } catch (e) {}
    afterChange();
  }

  async function nextProfile() {
    await load();
    const names = Object.keys(state.profiles);
    if (names.length < 2) return;
    await setProfile(names[(names.indexOf(state.activeProfile) + 1) % names.length]);
  }

  function toggleToolbar() {
    ui = Object.assign({}, ui, { toolbar: !ui.toolbar });
    chrome.storage.local.set({ demoUI: ui });
    renderToolbar();
  }

  function bindToolbar(root) {
    root.getElementById('tgl').onclick = toggleDemo;
    root.getElementById('prof').onchange = (e) => setProfile(e.target.value);
    root.getElementById('vars').onclick = toggleVars;
    root.getElementById('rld').onclick = () => location.reload();
    root.getElementById('hid').onclick = toggleToolbar;
  }

  function renderToolbar() {
    if (!IS_TOP) return;
    const root = ensureUI();
    const bar = root.getElementById('bar');
    if (!bar) return;
    bar.style.display = ui.toolbar ? 'flex' : 'none';
    if (!ui.toolbar) return;
    const tgl = root.getElementById('tgl');
    tgl.textContent = state.enabled ? 'Demo data' : 'Real data';
    tgl.className = state.enabled ? 'primary' : '';
    const sel = root.getElementById('prof');
    const names = Object.keys(state.profiles);
    if (sel.dataset.sig !== names.join('|')) {
      sel.replaceChildren(...names.map((n) => { const o = document.createElement('option'); o.value = o.textContent = n; return o; }));
      sel.dataset.sig = names.join('|');
    }
    sel.value = state.activeProfile;
    sel.style.display = names.length > 1 ? '' : 'none';
  }

  // ---------- variables table (in-page modal) ----------
  // The popup edits variables in a cramped textarea; JSON list variables are unreadable there.
  // This modal shows the active profile's variables as a table in the same shadow DOM as the
  // toolbar, and edits them through the same save path the toolbar uses.
  let varsDlg = null;
  let varsKeyHandler = null;

  function missingVars() {
    // {{names}} used by rules that apply here but are not defined in the active profile. fill()
    // leaves unknown placeholders alone, so those rules run with the real data - worth showing.
    const vars = state.profiles[state.activeProfile] || {};
    const missing = new Set();
    for (const rule of state.rules) {
      if (rule.disabled) continue;
      if (!hostMatches(rule.host || '') || !pageMatches(rule.page)) continue;
      for (const m of String(rule.value || '').matchAll(/\{\{\s*([\w.-]+)\s*\}\}/g)) {
        if (m[1] && !hasOwn(vars, m[1])) missing.add(m[1]);
      }
    }
    return [...missing];
  }

  function toggleVars() {
    if (varsDlg) { closeVars(); return; }
    if (!IS_TOP) return;
    openVars();
  }

  async function openVars() {
    await load();                    // the popup may have switched the profile while we were idle
    if (!state.profiles[state.activeProfile] && Object.keys(state.profiles).length) {
      state.activeProfile = Object.keys(state.profiles)[0];
    }
    const root = ensureUI();
    const dlg = document.createElement('div');
    dlg.className = 'vdlg';
    dlg.innerHTML = `
      <div class="vhead"><h3>Variables <select id="vprof"></select></h3>
        <button id="vclose" title="Close (Esc)">x</button></div>
      <div class="vhint">Click a value to edit it. Enter or clicking away saves, Esc cancels. A value can be any text, including a whole JSON list.</div>
      <div class="vmiss" id="vmiss" style="display:none"></div>
      <div class="vscroll"><table class="vtable">
        <thead><tr><th style="width:170px">Variable</th><th>Value</th><th style="width:26px"></th></tr></thead>
        <tbody id="vrows"></tbody></table></div>
      <div class="vadd"><input class="k" id="vnewk" placeholder="new_name"><input id="vnewv" placeholder="value">
        <button id="vadd" class="primary">Add</button></div>`;
    root.appendChild(dlg);
    varsDlg = dlg;
    dlg.querySelector('#vclose').onclick = closeVars;
    dlg.querySelector('#vprof').onchange = async (e) => {
      await setProfile(e.target.value);      // saves, re-applies, reloads the page when API data changed
      refreshVars();
    };
    dlg.querySelector('#vadd').onclick = addVar;
    for (const id of ['vnewk', 'vnewv']) {
      dlg.querySelector('#' + id).addEventListener('keydown', (e) => { if (e.key === 'Enter') addVar(); });
    }
    varsKeyHandler = (e) => {
      if (e.key !== 'Escape') return;
      const ed = dlg.querySelector('textarea');
      if (ed) { ed.dataset.cancel = '1'; ed.blur(); }   // first Esc cancels the edit, second closes
      else closeVars();
    };
    window.addEventListener('keydown', varsKeyHandler, true);
    refreshVars();
  }

  function closeVars() {
    if (!varsDlg) return;
    varsDlg.remove();
    varsDlg = null;
    if (varsKeyHandler) { window.removeEventListener('keydown', varsKeyHandler, true); varsKeyHandler = null; }
  }

  function refreshVars() {
    if (!varsDlg) return;
    const dlg = varsDlg;
    const sel = dlg.querySelector('#vprof');
    sel.replaceChildren(...Object.keys(state.profiles).map((n) => {
      const o = document.createElement('option'); o.value = o.textContent = n; return o;
    }));
    sel.value = state.activeProfile;
    const missing = missingVars();
    const miss = dlg.querySelector('#vmiss');
    if (missing.length) {
      miss.style.display = '';
      miss.textContent = 'Used by rules but missing in this profile (those rules keep the real data): ' + missing.join(', ');
    } else {
      miss.style.display = 'none';
    }
    const vars = state.profiles[state.activeProfile] || {};
    const rows = dlg.querySelector('#vrows');
    rows.replaceChildren();
    for (const [k, v] of Object.entries(vars)) {
      const tr = document.createElement('tr');
      const name = document.createElement('td');
      name.className = 'vname';
      name.textContent = k;
      const val = document.createElement('td');
      const cell = document.createElement('div');
      cell.className = 'vval clamp';
      cell.textContent = String(v);
      cell.title = String(v).slice(0, 400);
      cell.onclick = () => editVar(cell, k);
      val.appendChild(cell);
      const del = document.createElement('td');
      const x = document.createElement('button');
      x.className = 'vdel';
      x.textContent = '✕';
      x.title = 'Delete variable';
      x.onclick = async () => { await writeVar(k, null); refreshVars(); };
      del.appendChild(x);
      tr.append(name, val, del);
      rows.appendChild(tr);
    }
    if (!Object.keys(vars).length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 3;
      td.style.cssText = 'color:#6b7280;font-size:12px;padding:10px;';
      td.textContent = 'No variables yet — add one below (for example company = Acme Corp).';
      tr.appendChild(td);
      rows.appendChild(tr);
    }
  }

  function editVar(cell, key) {
    const td = cell.parentElement;
    const original = String((state.profiles[state.activeProfile] || {})[key] ?? '');
    const ta = document.createElement('textarea');
    ta.value = original;
    td.replaceChildren(ta);
    ta.focus();
    ta.onblur = async () => {
      if (ta.dataset.cancel) { refreshVars(); return; }
      if (ta.value === original) { refreshVars(); return; }
      await writeVar(key, ta.value);
      refreshVars();
    };
    ta.onkeydown = (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ta.blur(); }   // Enter saves, Shift+Enter is a new line
    };
  }

  async function writeVar(name, value) {
    if (!name) return;
    await load();                      // merge with whatever the popup changed in the meantime
    const p = state.profiles[state.activeProfile] || (state.profiles[state.activeProfile] = {});
    if (value === null) delete p[name]; else p[name] = value;
    await save();
    apply();
    afterChange();                     // API data is filled once per page load: reload when it changed
  }

  async function addVar() {
    if (!varsDlg) return;
    const k = varsDlg.querySelector('#vnewk').value.trim();
    const v = varsDlg.querySelector('#vnewv').value;
    if (!k) { varsDlg.querySelector('#vnewk').focus(); return; }
    await writeVar(k, v);
    refreshVars();
  }

  // ---------- page scan (for the AI assistant) and broken-rule check ----------
  function scanPage() {
    const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK', 'DEMO-OVERLAY-UI', 'SVG', 'PATH']);
    const roots = [document].concat(collectShadowRoots());
    const items = [];
    let order = 0;
    for (const root of roots) {
      root.querySelectorAll('*').forEach((el) => {
        if (skip.has(el.tagName.toUpperCase()) || el.closest('head, demo-overlay-ui')) return;
        const isImg = el.tagName === 'IMG';
        if (!isImg && el.children.length) return;                       // text lives in leaf elements
        const text = isImg ? (el.getAttribute('alt') || el.getAttribute('src') || '') : (el.textContent || '').trim();
        if (!text || text.length > 80 || !el.getClientRects().length) return;
        const score = (/\d/.test(text) ? 2 : 0) + (/^H[1-3]$/.test(el.tagName) ? 1 : 0);
        items.push({ el, tag: el.tagName.toLowerCase(), text: text.replace(/\s+/g, ' '), score, order: order++ });
      });
    }
    items.sort((a, b) => b.score - a.score || a.order - b.order);
    const seen = new Set();
    const out = [];
    for (const it of items) {
      if (out.length >= 150) break;
      const selector = selectorFor(it.el);
      if (selector.length > 400 || seen.has(selector)) continue;      // the server accepts selectors up to 500 characters
      seen.add(selector);
      out.push({ selector, tag: it.tag, text: it.text });
    }
    return out;
  }

  // Rules for elements inside iframes match nothing in the top page, so each iframe tells the top frame
  // which rules it matched. Only a rule that matched nowhere counts as broken.
  const frameMatched = new Set();
  let lastReport = '';
  function reportFrame(ids) {
    const sig = ids.join('|');
    if (sig === lastReport) return;
    lastReport = sig;
    try { window.top.postMessage({ __demoOverlay: 'frame-report', ids }, '*'); } catch (e) {}
  }
  window.addEventListener('message', (e) => {
    const m = e.data;
    if (IS_TOP && m && m.__demoOverlay === 'frame-report' && Array.isArray(m.ids) && e.source !== window) {
      m.ids.forEach((id) => frameMatched.add(String(id)));
    }
  });

  function brokenRules() {
    shadowRoots = collectShadowRoots();
    return state.rules
      .filter((r) => !r.disabled && r.type !== 'api' && hostMatches(r.host) && pageMatches(r.page) && !frameMatched.has(String(r.id)) && !queryAll(r.selector).length)
      .map((r) => ({ id: r.id, selector: r.selector, type: r.type, value: r.value }));
  }

  // ---------- selector generation ----------
  function selectorFor(el) {
    const root = el.getRootNode ? el.getRootNode() : document;
    if (root !== document && root.host) return selectorFor(root.host) + ' >>> ' + localSelector(el, root);
    return localSelector(el, document);
  }

  function localSelector(el, scope) {
    const isUnique = (sel) => {
      try {
        return scope.querySelectorAll(sel).length === 1;
      } catch (e) {
        return false;
      }
    };
    const stableId = (id) => id && !/\d{4,}|^:|[a-f0-9]{8,}/i.test(id);

    if (stableId(el.id)) {
      const s = '#' + CSS.escape(el.id);
      if (isUnique(s)) return s;
    }
    for (const a of ['data-testid', 'data-test', 'data-qa', 'data-cy', 'aria-label', 'name']) {
      const v = el.getAttribute(a);
      if (v) {
        const s = el.tagName.toLowerCase() + '[' + a + '="' + v.replace(/"/g, '\\"') + '"]';
        if (isUnique(s)) return s;
      }
    }
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement) {
      let part = cur.tagName.toLowerCase();
      if (stableId(cur.id)) {
        parts.unshift('#' + CSS.escape(cur.id));
        const s = parts.join(' > ');
        if (isUnique(s)) return s;
        break;
      }
      const parent = cur.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
      }
      parts.unshift(part);
      const s = parts.join(' > ');
      if (isUnique(s)) return s;
      cur = parent;
    }
    return parts.join(' > ');
  }

  // ---------- picker ----------
  function startPicker() {
    if (picking) return;
    picking = true;
    const root = ensureUI();
    const box = document.createElement('div');
    box.className = 'hl';
    const tip = document.createElement('div');
    tip.className = 'tip';
    tip.textContent = 'Click an element to capture it  ·  Esc to cancel';
    root.appendChild(box);
    root.appendChild(tip);

    let target = null;
    const events = ['mousedown', 'mouseup', 'pointerdown', 'pointerup', 'click'];

    const stopAll = (e) => {
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
    };

    const onMove = (e) => {
      if (e.target === uiHost) return;
      target = (e.composedPath && e.composedPath()[0]) || e.target;      // reaches into shadow roots
      const r = target.getBoundingClientRect();
      box.style.left = r.left + 'px';
      box.style.top = r.top + 'px';
      box.style.width = r.width + 'px';
      box.style.height = r.height + 'px';
    };

    const cleanup = () => {
      picking = false;
      window.removeEventListener('mousemove', onMove, true);
      window.removeEventListener('keydown', onKey, true);
      events.forEach((n) => window.removeEventListener(n, onBlock, true));
      box.remove();
      tip.remove();
    };

    const onKey = (e) => {
      if (e.key === 'Escape') {
        stopAll(e);
        cleanup();
      }
    };

    const onBlock = (e) => {
      if (e.target === uiHost) return;
      stopAll(e);
      if (e.type === 'click' && target) {
        const el = target;
        cleanup();
        openEditor(el);
      }
    };

    window.addEventListener('mousemove', onMove, true);
    window.addEventListener('keydown', onKey, true);
    events.forEach((n) => window.addEventListener(n, onBlock, true));
  }

  function openEditor(el) {
    const root = ensureUI();
    const selector = selectorFor(el);
    const isImg = el.tagName === 'IMG';
    const current = isImg ? el.getAttribute('src') || '' : (el.textContent || '').trim();

    const dlg = document.createElement('div');
    dlg.className = 'dlg';
    dlg.innerHTML = `
      <h3>New replacement rule</h3>
      <div class="sel"></div>
      <label>Action</label>
      <select id="type">
        <option value="text">Replace text</option>
        <option value="hide">Hide element</option>
        <option value="image">Replace image (URL)</option>
        <option value="html">Replace HTML</option>
      </select>
      <label>New value</label>
      <textarea id="value"></textarea>
      <div class="hint">Use variables like {{company}}, {{currency}} - values come from the active profile.</div>
      <div class="row"><button id="cancel">Cancel</button><button class="primary" id="save">Save rule</button></div>`;
    root.appendChild(dlg);
    dlg.querySelector('.sel').textContent = selector;
    const typeSel = dlg.querySelector('#type');
    const valueEl = dlg.querySelector('#value');
    valueEl.value = current;
    if (isImg) typeSel.value = 'image';
    typeSel.addEventListener('change', () => {
      valueEl.disabled = typeSel.value === 'hide';
    });

    dlg.querySelector('#cancel').onclick = () => dlg.remove();
    dlg.querySelector('#save').onclick = async () => {
      await load();
      const rule = {
        id: 'r' + Date.now().toString(36),
        host: HOST,
        selector,
        type: typeSel.value,
        value: typeSel.value === 'hide' ? '' : valueEl.value,
      };
      state.rules.push(rule);
      state.enabled = true;
      await save();
      dlg.remove();
      apply();
      // if logged in with a scenario loaded, also save it on the server
      chrome.runtime
        .sendMessage({ type: 'pushRule', rule })
        .then((r) => r && r.ok === false && console.warn('[Demo Overlay] not saved to server:', r.error))
        .catch(() => {});
    };
  }

  // ---------- bridge to the page hook (page-hook.js runs in the page's own world) ----------
  const TAG = '__demoOverlay';
  const hasOwn = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  let stateLoaded = false;   // never answer the page hook with empty rules before storage has been read
  let lastApiSig = null;     // what the page hook was last given
  let loadedApiSig = null;   // what it had when the page first asked for data

  // Turns the stored "api" rules into plain edits, with {{variables}} filled in for the active profile.
  function compileApi() {
    if (!state.enabled) return [];
    const vars = state.profiles[state.activeProfile] || {};
    const out = [];
    for (const rule of state.rules) {
      if (rule.type !== 'api' || rule.disabled) continue;
      if (!hostMatches(rule.host) || !pageMatches(rule.page)) continue;
      let edits;
      try { edits = JSON.parse(rule.value); } catch (e) { continue; }
      if (!Array.isArray(edits)) continue;
      const list = [];
      for (const ed of edits) {
        if (!ed || typeof ed.path !== 'string') continue;
        let v = ed.value;
        if (typeof v === 'string') {
          const whole = v.match(/^\s*\{\{\s*([\w.-]+)\s*\}\}\s*$/);
          if (whole) {
            if (!hasOwn(vars, whole[1])) continue;   // variable missing: leave the real data alone
            v = vars[whole[1]];
          } else {
            v = fill(v);
          }
        }
        list.push({ path: ed.path, value: v });
      }
      if (list.length) out.push({ id: rule.id, pattern: rule.selector, edits: list });
    }
    return out;
  }

  function postApi(force) {
    const rules = compileApi();
    const sig = JSON.stringify(rules);
    if (!force && sig === lastApiSig) return;
    lastApiSig = sig;
    if (loadedApiSig === null) loadedApiSig = sig;
    window.postMessage({ [TAG]: 'to-page', type: 'rules', rules }, '*');
  }

  // API data is fetched once per page load, so a changed profile needs a reload to take effect.
  function apiChangedSinceLoad() {
    return loadedApiSig !== null && lastApiSig !== null && lastApiSig !== loadedApiSig;
  }

  const waiting = new Map();
  let reqSeq = 0;
  window.addEventListener('message', (e) => {
    const m = e.data;
    if (e.source !== window || !m || m[TAG] !== 'from-page') return;
    if (m.type === 'shadow') { shadowSeen = true; clearTimeout(timer); timer = setTimeout(apply, 50); }
    else if (m.type === 'hello') { if (stateLoaded) postApi(true); }   // before that, the first apply() sends the rules
    else if (m.type === 'reply' && waiting.has(m.reqId)) {
      const done = waiting.get(m.reqId);
      waiting.delete(m.reqId);
      done(m.data);
    }
  });

  function askPage(type, extra) {
    return new Promise((resolve) => {
      const reqId = ++reqSeq;
      waiting.set(reqId, resolve);
      window.postMessage({ [TAG]: 'to-page', type, reqId, ...extra }, '*');
      setTimeout(() => { if (waiting.delete(reqId)) resolve(null); }, 1500);
    });
  }

  // ---------- wiring ----------
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!IS_TOP) return false;       // popup, shortcuts and scans talk to the top frame only
    if (msg && msg.type === 'command') {
      if (msg.name === 'toggle-demo') toggleDemo();
      else if (msg.name === 'next-profile') nextProfile();
      else if (msg.name === 'toggle-toolbar') toggleToolbar();
      else if (msg.name === 'toggle-vars') toggleVars();
      sendResponse({ ok: true });
    } else if (msg && msg.type === 'openVars') {
      openVars();
      sendResponse({ ok: true });
    } else if (msg && msg.type === 'scan') {
      sendResponse({ ok: true, elements: scanPage() });
    } else if (msg && msg.type === 'health') {
      sendResponse({ ok: true, broken: brokenRules() });
    } else if (msg && msg.type === 'startPicker') {
      startPicker();
      sendResponse({ ok: true });
    } else if (msg && msg.type === 'ping') {
      sendResponse({ ok: true, host: HOST });
    } else if (msg && msg.type === 'getCaptures') {
      askPage('captures').then((calls) => sendResponse({ ok: true, calls: calls || [] }));
      return true;
    } else if (msg && msg.type === 'getCapture') {
      askPage('capture', { id: msg.id }).then((call) => sendResponse({ ok: !!call, call }));
      return true;
    }
    return false;
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    if (changes.demoUI) {
      ui = changes.demoUI.newValue || { toolbar: false };
      renderToolbar();
    }
    if (changes[KEY]) {
      state = normalize(changes[KEY].newValue);
      apply();
    }
  });

  load().then(async () => {
    try { ui = (await chrome.storage.local.get('demoUI')).demoUI || ui; } catch (e) {}
    stateLoaded = true;
    apply();
    if (IS_TOP) {
      // one "page view" per page load, and only on pages that have rules for this host
      if (state.enabled && state.rules.some((r) => !r.disabled && hostMatches(r.host) && pageMatches(r.page))) track('page_view');
    }
    // run again once the DOM is ready, since we start at document_start
    document.addEventListener('DOMContentLoaded', apply, { once: true });
    window.addEventListener('load', apply, { once: true });
    // Single-page apps change the address without loading a page. Re-apply when it changes, so data
    // that belongs to the new page appears and data that belongs to the old page is put back.
    let lastHref = location.href;
    setInterval(() => { if (location.href !== lastHref) { lastHref = location.href; apply(); } }, 300);
    window.addEventListener('popstate', apply);
    window.addEventListener('hashchange', apply);
  });
})();
