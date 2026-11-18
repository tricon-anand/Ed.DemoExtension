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

  // Without a login nothing is shown or changed on the page: no rewriting, badge, toolbar, modals or shortcuts.
  let loggedIn = false;

  async function load() {
    const r = await chrome.storage.local.get([KEY, 'auth']);
    loggedIn = !!(r.auth && r.auth.token);
    state = normalize(loggedIn ? r[KEY] : null);
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
        .vdlg { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 1100px;
                max-width: 96vw; min-height: 70vh; max-height: 94vh; display: flex; flex-direction: column; background: #fff;
                color: #111827; border-radius: 12px; padding: 14px 16px; box-shadow: 0 20px 50px rgba(0,0,0,.35);
                font-size: 13px; }
        .vscope { display: inline-flex; gap: 0; margin: 0 0 8px; border: 1px solid #d1d5db; border-radius: 8px; overflow: hidden; align-self: flex-start; }
        .vscope button { border: 0; background: #fff; padding: 5px 14px; font-size: 12px; cursor: pointer; color: #374151; }
        .vscope button.on { background: #111827; color: #fff; }
        .vscope button + button { border-left: 1px solid #d1d5db; }
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
        .vname { font-size: 13px; font-weight: 600; }
        .vval.vlink { color: #1d4ed8; }
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
        .vsavebox { border-top: 1px solid #e5e7eb; margin-top: 10px; padding: 10px 2px 0; }
        .vsaveq { font-size: 12px; font-weight: 600; margin: 0 0 8px; }
        .vsavebox input { flex: 1; min-width: 0; padding: 5px 8px; border: 1px solid #d1d5db;
                          border-radius: 6px; font-size: 12px; }
        .vpane { font-size: 12px; }
        .vpane .crumb { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; margin-bottom: 6px; }
        .vpane .crumb button { padding: 2px 8px; font-size: 11px; border-radius: 999px; border: 1px solid #d1d5db;
                               background: #fff; cursor: pointer; }
        .vpane .crumb button.last { background: #2563eb; color: #fff; border-color: #2563eb; }
        .vpane .sep { color: #9ca3af; }
        .vmodes { margin-left: auto; display: flex; gap: 4px; }
        .vmodes button { padding: 2px 10px; font-size: 11px; border-radius: 6px; border: 1px solid #d1d5db;
                         background: #fff; cursor: pointer; }
        .vmodes button.on { background: #111827; color: #fff; border-color: #111827; }
        .vbody { max-height: calc(90vh - 150px); overflow: auto; }
        .vdetail { padding-top: 2px; }
        .vpane .crumb button.vback { background: #f3f4f6; font-weight: 600; margin-right: 6px; }
        .vsub { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e5e7eb; }
        .vsub th { text-align: left; font-size: 11px; color: #6b7280; padding: 4px 8px;
                   border-bottom: 1px solid #e5e7eb; background: #f9fafb; }
        .vsub td { padding: 4px 8px; border-bottom: 1px solid #f3f4f6; font-size: 12px; vertical-align: middle; }
        .vsub.rows th, .vsub.rows td { white-space: nowrap; }
        .vsub.rows th:first-child, .vsub.rows td:first-child { position: sticky; left: 0; background: #fff; }
        .vsub.rows th:first-child { background: #f9fafb; }
        .vsub td.acts { text-align: right; width: 1%; }
        .vsub td.vfield { color: #374151; font-weight: 600; width: 170px; }
        .vsubval.empty { color: #9ca3af; font-style: italic; }
        .vaddrow { display: flex; gap: 6px; margin-top: 8px; }
        .vsub tr:last-child td { border-bottom: 0; }
        .vsubval { cursor: pointer; padding: 2px 4px; border-radius: 4px; display: inline-block; min-width: 24px; min-height: 16px; }
        .vsubval:hover { background: #e0f2fe; color: #0284c7; }
        .vsubedit { width: 100%; min-width: 60px; padding: 2px 4px; font-size: 12px; border: 1px solid #2563eb; border-radius: 4px; outline: none; }
        .vchip { border: 1px solid #93c5fd; background: #eff6ff; color: #1d4ed8; border-radius: 999px;
                 padding: 1px 8px; font-size: 11px; cursor: pointer; font-family: ui-monospace, Consolas, monospace; }
        .vmore { font-size: 11px; color: #6b7280; margin-top: 4px; }
        .rdlg { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 840px;
                max-width: 95vw; max-height: 85vh; display: flex; flex-direction: column; background: #fff;
                color: #111827; border-radius: 12px; padding: 14px 16px; box-shadow: 0 20px 50px rgba(0,0,0,.35);
                font-size: 13px; z-index: 2147483647; }
        .rhead { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
        .rhead h3 { margin: 0; font-size: 14px; display: flex; align-items: center; gap: 8px; }
        .rcontrols { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
        .rcontrols input[type=text] { flex: 1; padding: 5px 8px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 12px; }
        .rcontrols label { font-size: 12px; color: #4b5563; display: flex; align-items: center; gap: 4px; cursor: pointer; white-space: nowrap; }
        .rtable { width: 100%; border-collapse: collapse; }
        .rtable th { text-align: left; font-size: 11px; color: #6b7280; padding: 6px 8px;
                     border-bottom: 1px solid #e5e7eb; background: #f9fafb; position: sticky; top: 0; }
        .rtable td { padding: 6px 8px; border-bottom: 1px solid #f3f4f6; vertical-align: middle; font-size: 12px; }
        .rtable tr:last-child td { border-bottom: 0; }
        .rtable tr:hover td { background: #f9fafb; }
        .rsel { font-family: ui-monospace, Consolas, monospace; font-size: 11px; color: #374151; word-break: break-all; max-width: 260px; }
        .rval { font-family: ui-monospace, Consolas, monospace; font-size: 12px; cursor: pointer; padding: 2px 4px; border-radius: 4px; word-break: break-all; }
        .rval:hover { background: #e0f2fe; color: #0284c7; }
        .redit { width: 100%; padding: 4px 6px; font-family: ui-monospace, Consolas, monospace; font-size: 12px; border: 1px solid #2563eb; border-radius: 4px; outline: none; }
        .rtag { font-size: 10px; background: #eff6ff; color: #1d4ed8; padding: 2px 6px; border-radius: 4px; font-weight: 600; text-transform: uppercase; }
        .rtag.hide { background: #fef2f2; color: #b91c1c; }
        .rtag.api { background: #fdf4ff; color: #a21caf; }
        .rtag.image { background: #f0fdf4; color: #15803d; }
        .rtag.html { background: #fffbeb; color: #b45309; }
        .radd { display: flex; gap: 6px; padding: 10px 2px 0; border-top: 1px solid #e5e7eb; margin-top: 8px; }
        .radd select { padding: 5px 8px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 12px; }
        .radd input { min-width: 0; padding: 5px 8px; border: 1px solid #d1d5db; border-radius: 6px;
                      font-size: 12px; font-family: ui-monospace, Consolas, monospace; }
        .radd input.rnewsel { flex: 0 0 280px; }
        .radd input.rnewval { flex: 1; }
      </style>
      <div class="badge" id="badge"></div>
      <div class="bar" id="bar"><b>DEMO</b>
        <button id="tgl"></button><select id="prof"></select>
        <button id="vars" title="Variables table (Alt+Shift+V)">Variables</button>
        <button id="rules" title="Rules table (Alt+Shift+R)">Rules</button>
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
    root.getElementById('rules').onclick = toggleRules;
    root.getElementById('rld').onclick = () => location.reload();
    root.getElementById('hid').onclick = toggleToolbar;
  }

  function renderToolbar() {
    if (!IS_TOP) return;
    const root = ensureUI();
    const bar = root.getElementById('bar');
    if (!bar) return;
    bar.style.display = ui.toolbar && loggedIn ? 'flex' : 'none';
    if (!ui.toolbar || !loggedIn) return;
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
  let varsDraft = null;   // edits happen on a draft copy of the profile: { name, original, dirty }

  // "This page" shows only the variables that rules for THIS page use: a page-text rule whose element is on
  // the page right now, or an api rule whose URL pattern matches a call the page has made. "All variables"
  // shows everything in the profile.
  let varsScope = 'all';
  let pageCaptures = [];             // the API calls this page made (from the page hook), read when the modal opens
  const addedHere = new Set();       // variables just added: stay visible in "This page" until the modal closes

  function varsUsedHere() {
    const used = new Set();
    // only calls made on the screen the app is showing now: a single-page app keeps the earlier screens' calls
    const route = location.pathname + location.hash.split('?')[0];
    const calls = pageCaptures.filter((c) => c.route === undefined || c.route === route);
    for (const rule of state.rules) {
      if (rule.disabled) continue;
      if (!hostMatches(rule.host || '') || !pageMatches(rule.page)) continue;
      if (rule.type === 'api') {
        if (!calls.some((c) => VH.urlPatternMatches(rule.selector, c.method, c.url, location.href))) continue;
      } else {
        let n = 0;
        try { n = queryAll(rule.selector).length; } catch (e) { n = 0; }
        if (!n) continue;
      }
      for (const m of String(rule.value || '').matchAll(/\{\{\s*([\w.-]+)\s*\}\}/g)) if (m[1]) used.add(m[1]);
    }
    return used;
  }

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

  function toast(msg) {
    const root = ensureUI();
    const tip = document.createElement('div');
    tip.className = 'tip';
    tip.style.display = 'block';
    tip.textContent = msg;
    root.appendChild(tip);
    setTimeout(() => tip.remove(), 5000);
  }

  async function openVars() {
    try {
      if (rulesDlg) closeRules();
      await load();                    // the popup may have switched the profile while we were idle
      if (!state.profiles[state.activeProfile] && Object.keys(state.profiles).length) {
        state.activeProfile = Object.keys(state.profiles)[0];
      }
      const root = ensureUI();
      pageCaptures = (await askPage('captures')) || [];
      addedHere.clear();
      // Edits land on a draft copy so the page can preview them live; nothing is persisted until
      // Save, which asks whether to update the existing profile or to save it as a new one.
      const vars = state.profiles[state.activeProfile] || (state.profiles[state.activeProfile] = {});
      varsDraft = { name: state.activeProfile, original: Object.assign({}, vars), dirty: false };
      state.profiles[state.activeProfile] = Object.assign({}, vars);
      apply();                         // preview the (unchanged) draft
      const dlg = document.createElement('div');
      dlg.className = 'vdlg';
      dlg.innerHTML = `
        <div class="vhead"><h3>Variables — <span id="vprofname"></span></h3>
          <div style="display:flex;gap:6px">
            <button id="vsave" class="primary">Save</button>
            <button id="vclose" title="Close (Esc)">x</button>
          </div></div>
        <div class="vscope" id="vscope">
          <button data-s="page" id="vscopepage">This page</button>
          <button data-s="all" id="vscopeall">All variables</button>
        </div>
        <div class="vhint">Click a value to edit it. Lists open as a table where you can edit cells and add or remove rows. The page previews your changes live; nothing is final until you press Save.</div>
        <div class="vmiss" id="vmiss" style="display:none"></div>
        <div class="vscroll"><table class="vtable">
          <thead><tr><th style="width:170px">Name</th><th>Value</th><th style="width:26px"></th></tr></thead>
          <tbody id="vrows"></tbody></table></div>
        <div class="vadd"><input class="k" id="vnewk" placeholder="new_name"><input id="vnewv" placeholder="value">
          <button id="vadd" class="primary">Add</button></div>
        <div class="vsavebox" id="vsavebox" style="display:none">
          <div class="vsaveq">Save these changes?</div>
          <div class="row">
            <button id="vupd" class="primary">Update profile</button>
            <button id="vnew">Save as new profile…</button>
            <button id="vdiscard">Discard</button>
          </div>
          <div class="row" id="vnewrow" style="display:none">
            <input id="vnewname" placeholder="New profile name">
            <button id="vnewok" class="primary">Create</button>
            <button id="vnewback">Back</button>
          </div>
        </div>`;
      root.appendChild(dlg);
      varsDlg = dlg;
      dlg.querySelector('#vprofname').textContent = varsDraft.name;
      dlg.querySelector('#vclose').onclick = () => closeVars(true);
      dlg.querySelector('#vsave').onclick = () => { if (varsDraft && varsDraft.dirty) showSaveChoice(); else closeVars(false); };
      dlg.querySelector('#vupd').onclick = saveVarsUpdate;
      dlg.querySelector('#vnew').onclick = () => {
        dlg.querySelector('#vnewrow').style.display = '';
        dlg.querySelector('#vnewname').focus();
      };
      dlg.querySelector('#vnewback').onclick = () => { dlg.querySelector('#vnewrow').style.display = 'none'; };
      dlg.querySelector('#vnewok').onclick = saveVarsAsNew;
      dlg.querySelector('#vnewname').addEventListener('keydown', (e) => { if (e.key === 'Enter') saveVarsAsNew(); });
      dlg.querySelector('#vdiscard').onclick = discardVars;
      dlg.querySelector('#vadd').onclick = addVar;
      for (const id of ['vnewk', 'vnewv']) {
        dlg.querySelector('#' + id).addEventListener('keydown', (e) => { if (e.key === 'Enter') addVar(); });
      }
      varsKeyHandler = (e) => {
        if (e.key !== 'Escape') return;
        const subEd = dlg.querySelector('input.vsubedit');
        if (subEd) { subEd.dataset.cancel = '1'; subEd.blur(); }
        else {
          const ed = dlg.querySelector('textarea.vedit');
          if (ed) { ed.dataset.cancel = '1'; ed.blur(); }   // first Esc cancels the edit, next closes the pane, then the modal
          else if (paneState) goBack();
          else closeVars(true);
        }
      };
      window.addEventListener('keydown', varsKeyHandler, true);
      // open on "This page" when something on this page uses a variable, otherwise on "All variables"
      const usedNow = varsUsedHere();
      varsScope = Object.keys(vars).some((k) => usedNow.has(k)) ? 'page' : 'all';
      for (const b of dlg.querySelectorAll('#vscope button')) b.onclick = () => { varsScope = b.dataset.s; refreshVars(); };
      refreshVars();
    } catch (e) {
      console.error('[Demo Overlay] variables table:', e);
      toast('Variables table failed: ' + e.message + ' — see the page console (F12)');
    }
  }

  function closeVars(interactive) {
    if (!varsDlg) return;
    // X and Esc with uncommitted edits do not close the modal: they open the save-choice box instead
    if (interactive && varsDraft && varsDraft.dirty) { showSaveChoice(); return; }
    if (varsDraft) {                   // closing with uncommitted edits: put the original profile back
      state.profiles[varsDraft.name] = varsDraft.original;
      apply();
    }
    varsDraft = null;
    closePane(true);
    varsDlg.remove();
    varsDlg = null;
    if (varsKeyHandler) { window.removeEventListener('keydown', varsKeyHandler, true); varsKeyHandler = null; }
  }

  // The save box: update the existing profile, fork the draft into a new one, or throw it away.
  function showSaveChoice() {
    if (!varsDlg) return;
    if (!varsDraft) { closeVars(false); return; }
    varsDlg.querySelector('#vupd').textContent = 'Update "' + varsDraft.name + '"';
    varsDlg.querySelector('#vnewrow').style.display = 'none';
    varsDlg.querySelector('#vsavebox').style.display = '';
  }

  async function saveVarsUpdate() {
    if (!varsDlg || !varsDraft) return;
    await save();                      // the draft object already sits in state.profiles
    const name = varsDraft.name;
    const variables = Object.assign({}, state.profiles[name]);
    varsDraft = null;
    afterChange();                     // API data is filled once per page load: reload when it changed
    closeVars(false);
    toast('Profile "' + name + '" updated');
    chrome.runtime
      .sendMessage({ type: 'saveProfile', name, variables })
      .then((r) => r && r.ok === false && console.warn('[Demo Overlay] profile not saved to server:', r.error))
      .catch(() => {});
  }

  async function saveVarsAsNew() {
    if (!varsDlg || !varsDraft) return;
    const name = varsDlg.querySelector('#vnewname').value.trim();
    if (!name) { varsDlg.querySelector('#vnewname').focus(); return; }
    if (hasOwn(state.profiles, name)) { toast('A profile named "' + name + '" already exists'); return; }
    state.profiles[name] = Object.assign({}, state.profiles[varsDraft.name]);   // the draft becomes the new profile
    state.profiles[varsDraft.name] = varsDraft.original;                        // the old profile keeps its values
    state.activeProfile = name;
    const variables = Object.assign({}, state.profiles[name]);
    await save();
    varsDraft = null;
    afterChange();
    closeVars(false);
    toast('Saved as new profile "' + name + '"');
    chrome.runtime
      .sendMessage({ type: 'saveProfile', name, variables })
      .then((r) => r && r.ok === false && console.warn('[Demo Overlay] profile not saved to server:', r.error))
      .catch(() => {});
  }

  function discardVars() {
    if (varsDraft) {
      state.profiles[varsDraft.name] = varsDraft.original;
      apply();                         // revert the preview
      varsDraft = null;
    }
    closeVars(false);
  }

  function refreshVars() {
    if (!varsDlg) return;
    const dlg = varsDlg;
    const missing = missingVars();
    const miss = dlg.querySelector('#vmiss');
    if (missing.length) {
      miss.style.display = paneState ? 'none' : '';      // stay hidden while a variable's own page is open
      miss.textContent = 'Used by rules but missing in this profile (those rules keep the real data): ' + missing.map(VH.prettyName).join(', ');
    } else {
      miss.style.display = 'none';
    }
    const vars = state.profiles[state.activeProfile] || {};
    const used = varsUsedHere();
    const here = Object.keys(vars).filter((k) => used.has(k) || addedHere.has(k));
    dlg.querySelector('#vscopepage').textContent = 'This page (' + here.length + ')';
    dlg.querySelector('#vscopeall').textContent = 'All variables (' + Object.keys(vars).length + ')';
    dlg.querySelector('#vscopepage').className = varsScope === 'page' ? 'on' : '';
    dlg.querySelector('#vscopeall').className = varsScope === 'all' ? 'on' : '';
    const rows = dlg.querySelector('#vrows');
    rows.replaceChildren();
    const shown = varsScope === 'page' ? Object.entries(vars).filter(([k]) => here.includes(k)) : Object.entries(vars);
    for (const [k, v] of shown) {
      const tr = document.createElement('tr');
      const name = document.createElement('td');
      name.className = 'vname';
      name.textContent = VH.prettyName(k);
      name.title = k;                                  // the real variable name, on hover
      name.dataset.key = k;
      const val = document.createElement('td');
      const cell = document.createElement('div');
      cell.className = 'vval clamp';
      const parsed = tryJson(v);
      if (parsed) {
        cell.textContent = VH.friendlySummary(parsed); // lists and records open as a table instead of the text editor
        cell.classList.add('vlink');
        cell.onclick = () => togglePane(k);
      } else {
        cell.textContent = String(v);
        cell.onclick = () => editVar(cell, k);
      }
      cell.title = parsed ? 'Click to edit' : String(v).slice(0, 400);
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
    if (!shown.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 3;
      td.style.cssText = 'color:#6b7280;font-size:12px;padding:10px;';
      td.textContent = Object.keys(vars).length
        ? 'No variables are used on this page. Switch to "All variables" to see everything in this profile.'
        : 'No variables yet — add one below (for example company = Acme Corp).';
      tr.appendChild(td);
      rows.appendChild(tr);
    }
  }

  function editVar(cell, key) {
    const td = cell.parentElement;
    const original = String((state.profiles[state.activeProfile] || {})[key] ?? '');
    const ta = document.createElement('textarea');
    ta.className = 'vedit';
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
    if (!name || !varsDraft) return;
    const p = state.profiles[varsDraft.name] || (state.profiles[varsDraft.name] = {});
    if (value === null) delete p[name]; else p[name] = value;
    varsDraft.dirty = true;
    apply();                           // preview the draft on the page; persisted only on Save
  }

  async function addVar() {
    if (!varsDlg) return;
    const k = varsDlg.querySelector('#vnewk').value.trim();
    const v = varsDlg.querySelector('#vnewv').value;
    if (!k) { varsDlg.querySelector('#vnewk').focus(); return; }
    addedHere.add(k);                  // a new variable no rule uses yet would vanish from "This page": keep it visible
    await writeVar(k, v);
    refreshVars();
  }

  // ---------- rules table (in-page modal) ----------
  let rulesDlg = null;
  let rulesKeyHandler = null;
  let rulesFilter = '';
  let rulesSiteOnly = true;

  function toggleRules() {
    if (rulesDlg) { closeRules(); return; }
    if (!IS_TOP) return;
    openRules();
  }

  async function openRules() {
    try {
      if (varsDlg) closeVars(false);
      await load();
      const root = ensureUI();
      if (rulesDlg) rulesDlg.remove();
      const dlg = document.createElement('div');
      dlg.className = 'rdlg';
      dlg.innerHTML = `
        <div class="rhead">
          <h3>Rules <span id="rhost" style="font-size:12px;color:#6b7280;font-weight:normal"></span></h3>
          <div style="display:flex;gap:6px">
            <button id="rpick" class="primary" title="Pick an element on this page to create a rule">Pick element</button>
            <button id="rclose" title="Close (Esc)">x</button>
          </div>
        </div>
        <div class="rcontrols">
          <input type="text" id="rfilter" placeholder="Filter rules by selector, value, or type...">
          <label><input type="checkbox" id="rsiteonly" ${rulesSiteOnly ? 'checked' : ''}> This site only</label>
        </div>
        <div class="vscroll">
          <table class="rtable">
            <thead>
              <tr>
                <th style="width:36px;text-align:center">Active</th>
                <th style="width:65px">Type</th>
                <th style="width:260px">Target / Selector</th>
                <th>Replacement value</th>
                <th style="width:65px;text-align:center">Page</th>
                <th style="width:28px"></th>
              </tr>
            </thead>
            <tbody id="rrows"></tbody>
          </table>
        </div>
        <div class="radd">
          <select id="rnewtype" title="Rule action type">
            <option value="text">text</option>
            <option value="hide">hide</option>
            <option value="image">image</option>
            <option value="html">html</option>
            <option value="api">api</option>
          </select>
          <input class="rnewsel" id="rnewsel" placeholder="CSS selector (e.g. h1, .total, #id)">
          <input class="rnewval" id="rnewval" placeholder="Value (e.g. Acme Corp or {{company}})">
          <button id="rnewadd" class="primary">Add rule</button>
        </div>
      `;
      root.appendChild(dlg);
      rulesDlg = dlg;

      dlg.querySelector('#rhost').textContent = '· ' + HOST;
      dlg.querySelector('#rclose').onclick = () => closeRules();
      dlg.querySelector('#rpick').onclick = () => {
        closeRules();
        startPicker();
      };

      const filterInput = dlg.querySelector('#rfilter');
      filterInput.value = rulesFilter;
      filterInput.oninput = (e) => {
        rulesFilter = e.target.value;
        refreshRules();
      };

      const siteCheck = dlg.querySelector('#rsiteonly');
      siteCheck.onchange = (e) => {
        rulesSiteOnly = e.target.checked;
        refreshRules();
      };

      const typeSel = dlg.querySelector('#rnewtype');
      const selInput = dlg.querySelector('#rnewsel');
      const valInput = dlg.querySelector('#rnewval');
      const updatePlaceholders = () => {
        const t = typeSel.value;
        if (t === 'hide') {
          selInput.placeholder = 'CSS selector to hide (e.g. .ad, #banner)';
          valInput.placeholder = '(not needed for hide)';
          valInput.disabled = true;
          valInput.value = '';
        } else if (t === 'api') {
          selInput.placeholder = 'URL pattern (e.g. */api/students* or GET /api/*)';
          valInput.placeholder = 'path = value or [{"path":"a","value":"b"}]';
          valInput.disabled = false;
        } else if (t === 'image') {
          selInput.placeholder = 'CSS selector for img (e.g. img.logo, .avatar)';
          valInput.placeholder = 'New image URL';
          valInput.disabled = false;
        } else if (t === 'html') {
          selInput.placeholder = 'CSS selector (e.g. .card, div.content)';
          valInput.placeholder = 'HTML replacement string';
          valInput.disabled = false;
        } else {
          selInput.placeholder = 'CSS selector (e.g. h1, .price, #company)';
          valInput.placeholder = 'Replacement text (e.g. Acme Corp or {{company}})';
          valInput.disabled = false;
        }
      };
      typeSel.onchange = updatePlaceholders;

      const addManualRule = async () => {
        const selector = selInput.value.trim();
        if (!selector) { selInput.focus(); return; }
        const type = typeSel.value;
        let value = '';
        if (type !== 'hide') {
          value = type === 'api' ? formatApiRuleValue(valInput.value) : valInput.value;
        }
        const rule = {
          id: 'r' + Date.now().toString(36),
          host: (type === 'api' || !rulesSiteOnly) ? '*' : HOST,
          selector,
          type,
          value,
        };
        state.rules.push(rule);
        state.enabled = true;
        await save();
        apply();
        if (type === 'api') postApi(true);
        refreshRules();
        chrome.runtime.sendMessage({ type: 'pushRule', rule }).catch(() => {});
        toast('Rule added');
        selInput.value = '';
        valInput.value = '';
        selInput.focus();
      };
      dlg.querySelector('#rnewadd').onclick = addManualRule;
      selInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          if (typeSel.value === 'hide') addManualRule();
          else valInput.focus();
        }
      });
      valInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') addManualRule();
      });

      rulesKeyHandler = (e) => {
        if (e.key !== 'Escape') return;
        const ed = dlg.querySelector('input.redit');
        if (ed) { ed.dataset.cancel = '1'; ed.blur(); }
        else closeRules();
      };
      window.addEventListener('keydown', rulesKeyHandler, true);
      refreshRules();
    } catch (e) {
      console.error('[Demo Overlay] rules table:', e);
      toast('Rules table failed: ' + e.message);
    }
  }

  function closeRules() {
    if (!rulesDlg) return;
    rulesDlg.remove();
    rulesDlg = null;
    if (rulesKeyHandler) {
      window.removeEventListener('keydown', rulesKeyHandler, true);
      rulesKeyHandler = null;
    }
  }

  // Rule changes made here are local first (instant preview); this tells the server too, otherwise the next
  // sync would bring a deleted rule back and drop edits. Rules that never reached the server are skipped.
  function syncRule(type, ruleId, changes) {
    chrome.runtime
      .sendMessage({ type, ruleId, changes })
      .then((r) => r && r.ok === false && toast('Not saved to the server: ' + r.error))
      .catch(() => {});
  }

  function refreshRules() {
    if (!rulesDlg) return;
    const rows = rulesDlg.querySelector('#rrows');
    rows.replaceChildren();

    const q = rulesFilter.trim().toLowerCase();
    const filtered = state.rules.filter((rule) => {
      if (rulesSiteOnly && rule.host && !hostMatches(rule.host)) return false;
      if (!q) return true;
      return (
        (rule.selector || '').toLowerCase().includes(q) ||
        (rule.value || '').toLowerCase().includes(q) ||
        (rule.type || '').toLowerCase().includes(q) ||
        (rule.host || '').toLowerCase().includes(q)
      );
    });

    if (!filtered.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 6;
      td.style.cssText = 'color:#6b7280;font-size:12px;padding:12px;text-align:center;';
      td.textContent = state.rules.length
        ? 'No rules match the current filter.'
        : 'No rules created yet. Click "Pick element" to add one.';
      tr.appendChild(td);
      rows.appendChild(tr);
      return;
    }

    for (const rule of filtered) {
      const tr = document.createElement('tr');

      // 1. Active checkbox
      const tdActive = document.createElement('td');
      tdActive.style.textAlign = 'center';
      const chk = document.createElement('input');
      chk.type = 'checkbox';
      chk.checked = !rule.disabled;
      chk.title = rule.disabled ? 'Enable rule' : 'Disable rule';
      chk.onchange = async () => {
        rule.disabled = !chk.checked;
        await save();
        apply();
        refreshRules();
        syncRule('updateRule', rule.id, { disabled: rule.disabled });
      };
      tdActive.appendChild(chk);

      // 2. Type tag
      const tdType = document.createElement('td');
      const tag = document.createElement('span');
      tag.className = 'rtag ' + (rule.type || '');
      tag.textContent = rule.type || 'text';
      tdType.appendChild(tag);

      // 3. Target / Selector
      const tdSel = document.createElement('td');
      const selDiv = document.createElement('div');
      selDiv.className = 'rsel';
      selDiv.textContent = rule.selector || '';
      selDiv.title = (rule.host ? '[' + rule.host + '] ' : '') + (rule.selector || '');
      tdSel.appendChild(selDiv);

      // 4. Replacement Value
      const tdVal = document.createElement('td');
      if (rule.type === 'hide') {
        const span = document.createElement('span');
        span.style.color = '#9ca3af';
        span.style.fontStyle = 'italic';
        span.textContent = '(element hidden)';
        tdVal.appendChild(span);
      } else {
        const valDiv = document.createElement('div');
        valDiv.className = 'rval';
        valDiv.textContent = rule.value === '' ? '""' : (rule.value || '');
        valDiv.title = 'Click to edit value';
        valDiv.onclick = () => editRuleValue(valDiv, rule);
        tdVal.appendChild(valDiv);
      }

      // 5. Page Match count
      const tdMatch = document.createElement('td');
      tdMatch.style.textAlign = 'center';
      if (rule.type === 'api') {
        tdMatch.innerHTML = '<span style="color:#a21caf;font-size:11px">API</span>';
      } else if (rule.disabled) {
        tdMatch.innerHTML = '<span style="color:#9ca3af;font-size:11px">—</span>';
      } else {
        let count = 0;
        try { count = queryAll(rule.selector).length; } catch (e) { count = 0; }
        if (count > 0) {
          tdMatch.innerHTML = `<span style="color:#059669;font-weight:600;font-size:11px" title="${count} element(s) found on page">✓ ${count}</span>`;
        } else {
          tdMatch.innerHTML = `<span style="color:#dc2626;font-size:11px" title="No elements found on page">⚠ 0</span>`;
        }
      }

      // 6. Delete
      const tdDel = document.createElement('td');
      const delBtn = document.createElement('button');
      delBtn.className = 'vdel';
      delBtn.textContent = '✕';
      delBtn.title = 'Delete rule';
      delBtn.onclick = async () => {
        state.rules = state.rules.filter((r) => r !== rule);
        await save();
        apply();
        refreshRules();
        toast('Rule deleted');
        syncRule('deleteRule', rule.id);
      };
      tdDel.appendChild(delBtn);

      tr.append(tdActive, tdType, tdSel, tdVal, tdMatch, tdDel);
      rows.appendChild(tr);
    }
  }

  function editRuleValue(cell, rule) {
    const td = cell.parentElement;
    const inp = document.createElement('input');
    inp.className = 'redit';
    inp.type = 'text';
    inp.value = rule.value || '';
    td.replaceChildren(inp);
    inp.focus();
    inp.select();

    const finish = async (commit) => {
      if (!commit || inp.dataset.cancel === '1') {
        refreshRules();
        return;
      }
      if (inp.value === (rule.value || '')) {
        refreshRules();
        return;
      }
      rule.value = inp.value;
      await save();
      apply();
      toast('Rule updated');
      refreshRules();
      syncRule('updateRule', rule.id, { value: rule.value });
    };

    inp.onblur = () => finish(true);
    inp.onkeydown = (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        inp.blur();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        inp.dataset.cancel = '1';
        inp.blur();
      }
    };
  }

  function formatApiRuleValue(val) {
    const trimmed = String(val || '').trim();
    if (!trimmed) return '[]';
    if (trimmed.startsWith('[')) {
      try {
        const arr = JSON.parse(trimmed);
        if (Array.isArray(arr)) return trimmed;
      } catch (e) {}
    }
    if (trimmed.startsWith('{')) {
      try {
        const obj = JSON.parse(trimmed);
        if (obj && typeof obj === 'object') {
          if (typeof obj.path === 'string') return JSON.stringify([obj]);
          const list = Object.entries(obj).map(([path, v]) => ({
            path,
            value: typeof v === 'object' ? JSON.stringify(v) : String(v ?? ''),
          }));
          return JSON.stringify(list);
        }
      } catch (e) {}
    }
    // Lines of "path = value" or "path: value"
    const lines = trimmed.split('\n').map((l) => l.trim()).filter(Boolean);
    const edits = [];
    for (const line of lines) {
      const eq = line.indexOf('=') !== -1 ? line.indexOf('=') : line.indexOf(':');
      if (eq > 0) {
        const path = line.slice(0, eq).trim();
        const v = line.slice(eq + 1).trim();
        edits.push({ path, value: v });
      } else {
        edits.push({ path: line, value: '' });
      }
    }
    return edits.length ? JSON.stringify(edits) : '[]';
  }

  // ---------- JSON values: a table pane instead of a text editor ----------
  // A whole-list variable (students_json = '[{"name":"Asha"}, ...]') is unreadable as text, so
  // arrays of objects render like the page's own tables, nested values drill down, and Raw JSON
  // mode keeps full control. The profile value stays ONE string: raw mode writes back
  // JSON.stringify (compact, one line) so the popup's "name = value" format survives.
  const PANE_ROW_CAP = 50;
  const VH = globalThis.DemoVarHelpers;   // friendly names, typed cells, row edits (varhelpers.js)

  function tryJson(text) {
    const t = String(text == null ? '' : text).trim();
    if (!t.startsWith('{') && !t.startsWith('[')) return null;
    try { const v = JSON.parse(t); return v && typeof v === 'object' ? v : null; } catch (e) { return null; }
  }

  function resolvePath(root, path) {
    let cur = root;
    for (const step of path) cur = cur[step];
    return cur;
  }

  // Opening a list or record replaces the variable list with its own page inside the modal (Back returns);
  // opening something inside it (a row's own list) is one more page, so nested data is always readable.
  let paneState = null;   // { td, key, root, path, mode } — the page that is open, if any
  const LIST_SCREEN = '.vhint, #vmiss, .vscroll, .vadd';      // the parts that make up the variable list

  function togglePane(key) { openPane(key); }

  function closePane(skipRefresh) {
    if (!paneState) return;
    const dlg = varsDlg;
    paneState.td.remove();
    paneState = null;
    if (dlg && !skipRefresh) {
      dlg.querySelectorAll(LIST_SCREEN).forEach((el) => { el.style.display = ''; });
      refreshVars();                                            // summaries, missing-variable note
    }
  }

  // One step back: raw editor -> table, a nested page -> its parent, the top page -> the variable list.
  function goBack() {
    if (!paneState) return;
    if (paneState.mode === 'raw') paneState.mode = 'table';
    else if (paneState.path.length) paneState.path = paneState.path.slice(0, -1);
    else { closePane(); return; }
    paneState.limit = 0;
    renderPane();
  }

  function openPane(key) {
    closePane(true);
    const root = tryJson((state.profiles[state.activeProfile] || {})[key]);
    if (!root || !varsDlg) return;                       // the value changed while the modal was open
    varsDlg.querySelectorAll(LIST_SCREEN).forEach((el) => { el.style.display = 'none'; });
    const page = document.createElement('div');
    page.className = 'vdetail';
    varsDlg.querySelector('#vsavebox').before(page);
    paneState = { td: page, key, root, path: [], mode: 'table' };
    renderPane();
  }

  function renderPane() {
    if (!paneState) return;
    const { td, key, root, path, mode } = paneState;
    td.replaceChildren();
    const pane = document.createElement('div');
    pane.className = 'vpane';
    td.appendChild(pane);

    const crumb = document.createElement('div');
    crumb.className = 'crumb';
    const back = mk('button', 'vback', '← Back');
    back.title = 'Back (Esc)';
    back.onclick = goBack;
    crumb.appendChild(back);
    const home = mk('button', null, 'Variables');
    home.onclick = () => closePane();
    crumb.appendChild(home);
    crumb.appendChild(sep());
    let prefix = [];
    crumb.appendChild(crumbBtn(VH.prettyName(key), [], prefix.length === path.length));
    for (const step of path) {
      prefix = prefix.concat(step);
      const label = typeof step === 'number' ? 'Row ' + (step + 1) : VH.prettyName(step);
      crumb.appendChild(sep());
      crumb.appendChild(crumbBtn(label, prefix.slice(), prefix.length === path.length));
    }
    const modes = document.createElement('div');
    modes.className = 'vmodes';
    for (const [label, m] of [['Table', 'table'], ['Raw JSON', 'raw']]) {
      const b = document.createElement('button');
      b.textContent = label;
      if (mode === m) b.className = 'on';
      b.onclick = () => { paneState.mode = m; renderPane(); };
      modes.appendChild(b);
    }
    crumb.appendChild(modes);
    pane.appendChild(crumb);

    const body = document.createElement('div');
    body.className = 'vbody';
    pane.appendChild(body);
    const cur = resolvePath(root, path);
    if (mode === 'table') renderJsonTable(body, cur, path);
    else renderRawEditor(body, key);
  }

  function crumbBtn(label, pathPrefix, last) {
    const b = document.createElement('button');
    b.textContent = label;
    if (last) b.className = 'last';
    b.onclick = () => { paneState.path = pathPrefix; paneState.mode = 'table'; paneState.limit = 0; renderPane(); };
    return b;
  }

  function sep() {
    const s = document.createElement('span');
    s.className = 'sep';
    s.textContent = '›';
    return s;
  }

  function note(body, text) {
    const n = document.createElement('div');
    n.className = 'vmore';
    n.textContent = text;
    body.appendChild(n);
  }

  // ---------- table view of a list / object variable (spreadsheet style) ----------

  // Writes `value` at `path` inside the open variable (an empty path replaces the whole variable).
  function assignAt(rootVal, path, value) {
    if (!path.length) return value;
    let cur = rootVal;
    for (let i = 0; i < path.length - 1; i++) {
      const k = path[i];
      if (cur[k] === null || typeof cur[k] !== 'object') cur[k] = typeof path[i + 1] === 'number' ? [] : {};
      cur = cur[k];
    }
    cur[path[path.length - 1]] = value;
    return rootVal;
  }

  // Saves the open variable after any change and refreshes its one-line summary in the main table.
  async function commitPane() {
    await writeVar(paneState.key, JSON.stringify(paneState.root));
    if (!varsDlg) return;                             // the page may have reloaded to refresh API data
    const nameTd = [...varsDlg.querySelectorAll('.vname')].find((el) => el.dataset.key === paneState.key);
    const valDiv = nameTd && nameTd.parentElement && nameTd.parentElement.querySelector('.vval');
    if (valDiv) {
      valDiv.textContent = VH.friendlySummary(paneState.root);
      valDiv.title = 'Click to edit';
    }
  }

  async function changeAt(path, value) {
    paneState.root = assignAt(paneState.root, path, value);
    await commitPane();
  }

  function mk(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  const nestedChipText = (v) => {
    if (Array.isArray(v) && !v.length) return 'empty';
    if (Array.isArray(v)) return v.length + (v.every((x) => x === null || typeof x !== 'object') ? (v.length === 1 ? ' value' : ' values') : (v.length === 1 ? ' row' : ' rows'));
    if (v && typeof v === 'object') return Object.keys(v).length + ' fields';
    return '—';
  };

  function renderJsonTable(body, cur, path) {
    if (Array.isArray(cur) && cur.length && cur.every((x) => x && typeof x === 'object' && !Array.isArray(x))) renderRowsTable(body, cur, path);
    else if (Array.isArray(cur)) renderValuesTable(body, cur, path);
    else if (cur && typeof cur === 'object') renderFieldsTable(body, cur, path);
    else note(body, 'This is a single value — edit it from the main list.');
  }

  // One cell. `col` = { path, type } says where the value sits inside `item` and how to treat it.
  function renderCell(td, item, col, rowPath) {
    const value = VH.getAt(item, col.path);
    const fullPath = rowPath.concat(col.path);
    if (col.type === 'nested') {
      const chip = mk('button', 'vchip', nestedChipText(value));
      chip.title = 'Open';
      chip.onclick = () => { paneState.path = fullPath; paneState.mode = 'table'; paneState.limit = 0; renderPane(); };
      td.appendChild(chip);
    } else if (col.type === 'boolean') {
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = !!value;
      box.onchange = async () => { await changeAt(fullPath, box.checked); };
      td.appendChild(box);
    } else {
      const text = VH.cellText(value, col.type);
      const cell = mk('div', 'vsubval' + (text === '' ? ' empty' : ''), text === '' ? 'empty' : text);
      cell.title = 'Click to edit';
      cell.onclick = (e) => { e.stopPropagation(); editCell(td, value, col, fullPath); };
      td.appendChild(cell);
    }
  }

  function editCell(td, value, col, fullPath) {
    const inp = document.createElement('input');
    inp.className = 'vsubedit';
    inp.type = 'text';
    inp.value = VH.cellText(value, col.type);
    inp.placeholder = col.type === 'list' ? 'a, b, c' : '';
    td.replaceChildren(inp);
    inp.focus();
    inp.select();
    const finish = async () => {
      if (!paneState) return;
      if (inp.dataset.cancel === '1' || inp.value === VH.cellText(value, col.type)) { renderPane(); return; }
      const r = VH.coerce(inp.value, col.type, value);
      if (!r.ok) { toast(r.error); renderPane(); return; }       // keeps the old value and says why
      await changeAt(fullPath, r.value);
      renderPane();
    };
    inp.onblur = finish;
    inp.onkeydown = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); inp.blur(); }
      else if (e.key === 'Escape') { e.preventDefault(); inp.dataset.cancel = '1'; inp.blur(); }
    };
  }

  function iconButton(label, title, onclick) {
    const b = mk('button', 'vdel', label);
    b.title = title;
    b.onclick = onclick;
    return b;
  }

  function footer(body, addLabel, onAdd, shown, total) {
    const bar = mk('div', 'vaddrow');
    const add = mk('button', 'primary', addLabel);
    add.onclick = onAdd;
    bar.appendChild(add);
    if (total > shown) {
      const more = mk('button', null, 'Show ' + Math.min(PANE_ROW_CAP, total - shown) + ' more (' + shown + ' of ' + total + ')');
      more.onclick = () => { paneState.limit = shown + PANE_ROW_CAP; renderPane(); };
      bar.appendChild(more);
    }
    body.appendChild(bar);
  }

  // A list of records: rows and columns, like a spreadsheet.
  function renderRowsTable(body, rows, path) {
    const cols = VH.columnsFor(rows, 40);
    const limit = Math.max(paneState.limit || PANE_ROW_CAP, 1);
    const table = mk('table', 'vsub rows');
    const head = document.createElement('tr');
    for (const c of cols) { const th = mk('th', null, c.label); th.title = c.path.join('.'); head.appendChild(th); }
    head.appendChild(mk('th', null, ''));
    table.appendChild(head);
    rows.slice(0, limit).forEach((item, i) => {
      const tr = document.createElement('tr');
      for (const c of cols) { const td = document.createElement('td'); renderCell(td, item, c, path.concat(i)); tr.appendChild(td); }
      const acts = mk('td', 'acts');
      acts.append(
        iconButton('⧉', 'Duplicate this row', async () => {
          await changeAt(path, VH.insertAfter(rows, i, VH.cloneRow(item)));
          renderPane();
        }),
        iconButton('✕', 'Delete this row', async () => {
          await changeAt(path, VH.removeAt(rows, i));
          renderPane();
        }),
      );
      tr.appendChild(acts);
      table.appendChild(tr);
    });
    body.appendChild(table);
    const hidden = VH.columnsFor(rows).length - cols.length;
    if (hidden > 0) note(body, '+' + hidden + ' more columns not shown');
    footer(body, '+ Add row', async () => {
      const next = VH.insertAfter(rows, rows.length - 1, VH.blankRow(VH.columnsFor(rows)));
      paneState.limit = Math.max(limit, next.length);
      await changeAt(path, next);
      renderPane();
    }, Math.min(limit, rows.length), rows.length);
  }

  // A list of plain values (names, numbers).
  function renderValuesTable(body, list, path) {
    const first = list.find((x) => x !== null && x !== undefined);
    const type = typeof first === 'number' ? 'number' : typeof first === 'boolean' ? 'boolean' : 'text';
    const limit = Math.max(paneState.limit || PANE_ROW_CAP, 1);
    if (!list.length) note(body, 'This list is empty.');
    else {
      const table = mk('table', 'vsub');
      list.slice(0, limit).forEach((item, i) => {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        renderCell(td, item, { path: [], type }, path.concat(i));
        const acts = mk('td', 'acts');
        acts.appendChild(iconButton('✕', 'Delete this value', async () => { await changeAt(path, VH.removeAt(list, i)); renderPane(); }));
        tr.append(td, acts);
        table.appendChild(tr);
      });
      body.appendChild(table);
    }
    footer(body, '+ Add value', async () => {
      const next = list.concat([type === 'number' ? 0 : type === 'boolean' ? false : '']);
      paneState.limit = Math.max(limit, next.length);
      await changeAt(path, next);
      renderPane();
    }, Math.min(limit, list.length), list.length);
  }

  // A single record: field name on the left, value on the right.
  function renderFieldsTable(body, obj, path) {
    const table = mk('table', 'vsub');
    for (const [k, v] of Object.entries(obj)) {
      const tr = document.createElement('tr');
      const label = mk('td', 'vfield', VH.prettyName(k));
      label.title = k;
      const td = document.createElement('td');
      const kind = VH.kindOf(v);
      const type = kind === 'number' || kind === 'boolean' || kind === 'list' ? kind : kind === 'object' || kind === 'nested' ? 'nested' : 'text';
      renderCell(td, obj, { path: [k], type }, path);
      tr.append(label, td);
      table.appendChild(tr);
    }
    body.appendChild(table);
  }

  function renderRawEditor(body, key) {
    const original = String((state.profiles[state.activeProfile] || {})[key] ?? '');
    const ta = document.createElement('textarea');
    ta.style.minHeight = '320px';
    try { ta.value = JSON.stringify(JSON.parse(original), null, 2); }   // pretty for editing
    catch (e) { ta.value = original; }
    body.appendChild(ta);
    const err = document.createElement('div');
    err.className = 'vmore';
    body.appendChild(err);
    const row = document.createElement('div');
    row.className = 'row';
    const save = document.createElement('button');
    save.className = 'primary';
    save.textContent = 'Save';
    const cancel = document.createElement('button');
    cancel.textContent = 'Cancel';
    row.append(cancel, save);
    body.appendChild(row);
    save.onclick = async () => {
      let parsed;
      try { parsed = JSON.parse(ta.value); }
      catch (ex) { err.textContent = 'Not valid JSON: ' + ex.message; return; }
      err.textContent = '';
      await writeVar(key, JSON.stringify(parsed));     // compact, ONE line: popup-safe and engine-safe
      if (!varsDlg) return;                            // the page may have reloaded to refresh API data
      if (parsed && typeof parsed === 'object') {
        paneState.root = parsed;
        paneState.path = [];
        paneState.mode = 'table';
        renderPane();
      } else closePane();
      refreshVars();
    };
    cancel.onclick = goBack;
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
    if (!loggedIn && msg && msg.type !== 'ping') {
      sendResponse({ ok: false, error: 'Log in first.' });
      return false;
    }
    if (msg && msg.type === 'command') {
      if (msg.name === 'toggle-demo') toggleDemo();
      else if (msg.name === 'next-profile') nextProfile();
      else if (msg.name === 'toggle-toolbar') toggleToolbar();
      else if (msg.name === 'toggle-vars') toggleVars();
      else if (msg.name === 'toggle-rules') toggleRules();
      sendResponse({ ok: true });
    } else if (msg && msg.type === 'openVars') {
      openVars();
      sendResponse({ ok: true });
    } else if (msg && msg.type === 'openRules') {
      openRules();
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
    if (changes.auth) {
      // logging in or out: re-read everything; on logout close any open modal and clear the page
      load().then(() => {
        if (!loggedIn) { if (varsDlg) closeVars(false); if (rulesDlg) closeRules(); }
        apply();
      });
      return;
    }
    if (changes[KEY]) {
      state = normalize(loggedIn ? changes[KEY].newValue : null);
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
