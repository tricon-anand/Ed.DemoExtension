const KEY = 'demo';
let state = { enabled: false, activeProfile: '', profiles: {}, rules: [] };
let tabHost = '';
let activeTab = null;

const $ = (id) => document.getElementById(id);
const send = (msg) => chrome.runtime.sendMessage(msg);

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
  // make sure there is always an active profile
  const names = Object.keys(state.profiles);
  if (!names.length) {
    state.profiles.Default = {};
    state.activeProfile = 'Default';
  } else if (!state.profiles[state.activeProfile]) {
    state.activeProfile = names[0];
  }
}

function save() {
  return chrome.storage.local.set({ [KEY]: state });
}

function flash(msg) {
  $('status').textContent = msg;
  setTimeout(() => ($('status').textContent = ''), 2500);
}

function varsToText(obj) {
  return Object.entries(obj || {})
    .map(([k, v]) => k + ' = ' + v)
    .join('\n');
}

function textToVars(text) {
  const out = {};
  text.split('\n').forEach((line) => {
    const i = line.indexOf('=');
    if (i < 1) return;
    const k = line.slice(0, i).trim();
    if (k) out[k] = line.slice(i + 1).trim();
  });
  return out;
}

function render() {
  $('enabled').checked = state.enabled;
  $('onlabel').textContent = state.enabled ? 'On' : 'Off';

  const sel = $('profile');
  sel.innerHTML = '';
  Object.keys(state.profiles).forEach((name) => {
    const o = document.createElement('option');
    o.value = name;
    o.textContent = name;
    if (name === state.activeProfile) o.selected = true;
    sel.appendChild(o);
  });
  $('vars').value = varsToText(state.profiles[state.activeProfile]);

  $('host').textContent = tabHost ? '· ' + tabHost : '';
  const list = $('rules');
  list.innerHTML = '';
  const rules = state.rules.filter((r) => !tabHost || ruleHostMatches(r.host, tabHost));
  if (!rules.length) {
    list.innerHTML = '<div class="empty">No rules for this site yet. Use "Pick an element" to add one.</div>';
    return;
  }
  rules.forEach((rule) => {
    const div = document.createElement('div');
    div.className = 'rule';

    const meta = document.createElement('div');
    meta.className = 'meta';
    const chk = document.createElement('input');
    chk.type = 'checkbox';
    chk.checked = !rule.disabled;
    chk.title = 'Enable / disable this rule';
    chk.onchange = async () => {
      rule.disabled = !chk.checked;
      await save();
    };
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = rule.type;
    const s = document.createElement('span');
    s.className = 'sel';
    s.textContent = rule.selector;
    s.title = rule.selector;
    const del = document.createElement('button');
    del.className = 'x';
    del.textContent = '×';
    del.title = 'Delete rule';
    del.onclick = async () => {
      state.rules = state.rules.filter((r) => r !== rule);
      await save();
      render();
    };
    meta.append(chk, tag, s, del);
    div.appendChild(meta);

    if (rule.type !== 'hide') {
      const input = document.createElement('input');
      input.type = 'text';
      input.value = rule.value || '';
      input.onchange = async () => {
        rule.value = input.value;
        await save();
        flash('Saved');
      };
      div.appendChild(input);
    }
    list.appendChild(div);
  });
}

// ---------- server login and sync ----------
function serverMsg(text, bad) {
  const el = $('serverStatus');
  el.textContent = text || '';
  el.style.color = bad ? '#dc2626' : '#059669';
}

async function renderServer() {
  const st = await send({ type: 'status' });
  $('loggedOut').style.display = st.loggedIn ? 'none' : 'block';
  $('loggedIn').style.display = st.loggedIn ? 'block' : 'none';
  if (!st.loggedIn) {
    $('apiBase').value = st.apiBase;
    return;
  }
  $('who').textContent = st.email + ' (' + st.role + ')' + (st.version ? ' · synced v' + st.version : '');
  const r = await send({ type: 'listScenarios' });
  const sel = $('scenarioSel');
  sel.innerHTML = '';
  if (!r.ok) return serverMsg(r.error, true);
  r.scenarios.forEach((s) => {
    const o = document.createElement('option');
    o.value = s.id;
    o.textContent = s.name;
    if (s.id === st.scenarioId) o.selected = true;
    sel.appendChild(o);
  });
  if (!r.scenarios.length) serverMsg('No scenarios yet. Create one in the admin page.', true);
}

async function initServer() {
  $('loginBtn').onclick = async () => {
    serverMsg('Logging in...');
    const r = await send({
      type: 'login',
      apiBase: $('apiBase').value.trim(),
      email: $('email').value.trim(),
      password: $('password').value,
    });
    $('password').value = '';
    if (!r.ok) return serverMsg(r.error, true);
    serverMsg('');
    renderServer();
  };

  $('syncBtn').onclick = async () => {
    const id = Number($('scenarioSel').value);
    if (!id) return;
    serverMsg('Syncing...');
    const r = await send({ type: 'loadScenario', scenarioId: id });
    if (!r.ok) return serverMsg(r.error, true);
    await load(); // re-read the cached bundle the worker just wrote
    render();
    serverMsg(r.changed ? 'Loaded version ' + r.version : 'Already up to date (v' + r.version + ')');
    renderServer();
  };

  $('logoutBtn').onclick = async () => {
    await send({ type: 'logout' });
    serverMsg('');
    renderServer();
  };

  await renderServer();
}

// ---------- API data (network) helper ----------
// Lists the JSON calls the page made, lets you pick fields from one, and saves an "api" rule.
function flatten(value, path, out) {
  if (out.length >= 400) return;
  if (value !== null && typeof value === 'object') {
    if (Array.isArray(value)) {
      value.forEach((v, i) => flatten(v, path + '[' + i + ']', out));
    } else {
      for (const [k, v] of Object.entries(value)) {
        if (/[.\[\]]/.test(k)) continue; // such keys can't be written as a path
        flatten(v, path ? path + '.' + k : k, out);
      }
    }
  } else {
    out.push({ path, value });
  }
}

function shortValue(v) {
  const s = typeof v === 'string' ? '"' + v + '"' : String(v);
  return s.length > 70 ? s.slice(0, 67) + '...' : s;
}

async function pageMessage(msg) {
  if (!activeTab) throw new Error('No active tab');
  try {
    return await chrome.tabs.sendMessage(activeTab.id, msg, { frameId: 0 });
  } catch (e) {
    throw new Error('Reload the page once, then try again');
  }
}

async function netRefresh() {
  const list = $('netList');
  list.innerHTML = '';
  $('netDetail').style.display = 'none';
  let r;
  try {
    r = await pageMessage({ type: 'getCaptures' });
  } catch (e) {
    return flash(e.message);
  }
  if (!r.calls.length) {
    list.innerHTML = '<div class="empty">No JSON calls seen yet. Reload the page and use the app, then try again.</div>';
    return;
  }
  r.calls.forEach((c) => {
    const row = document.createElement('div');
    row.className = 'call';
    let path = c.url;
    try { const u = new URL(c.url); path = u.pathname + u.search; } catch (e) {}
    const m = document.createElement('span'); m.className = 'm'; m.textContent = c.method;
    const u = document.createElement('span'); u.className = 'u'; u.textContent = path; u.title = c.url;
    const s = document.createElement('span'); s.className = 's'; s.textContent = c.status + ' · ' + Math.max(1, Math.round(c.size / 1024)) + ' KB';
    row.append(m, u, s);
    row.onclick = () => openCall(c);
    list.appendChild(row);
  });
}

async function openCall(call) {
  let r;
  try {
    r = await pageMessage({ type: 'getCapture', id: call.id });
  } catch (e) {
    return flash(e.message);
  }
  if (!r || !r.ok) return flash('That call is no longer available');
  let data;
  try { data = JSON.parse(r.call.text); } catch (e) { return flash('Not valid JSON'); }

  const leaves = [];
  flatten(data, '', leaves);
  let pathname = call.url;
  try { pathname = new URL(call.url).pathname; } catch (e) {}
  $('netPattern').value = '*' + pathname + '*';
  $('netFilter').value = '';
  $('netDetail').style.display = 'block';

  const box = $('netLeaves');
  box.innerHTML = '';
  const rows = leaves.map((leaf) => {
    const el = document.createElement('div');
    el.className = 'leaf';
    const path = document.createElement('input');
    path.type = 'text'; path.className = 'path'; path.value = leaf.path;
    const cur = document.createElement('div');
    cur.className = 'cur'; cur.textContent = 'now: ' + shortValue(leaf.value);
    const val = document.createElement('input');
    val.type = 'text'; val.placeholder = 'new value';
    el.append(path, cur, val);
    box.appendChild(el);
    return { el, path, val, leaf };
  });
  if (!rows.length) box.innerHTML = '<div class="empty">Nothing editable in this response.</div>';

  $('netFilter').oninput = () => {
    const q = $('netFilter').value.trim().toLowerCase();
    rows.forEach((r) => {
      const hay = (r.leaf.path + ' ' + String(r.leaf.value)).toLowerCase();
      r.el.style.display = !q || hay.includes(q) ? '' : 'none';
    });
  };

  $('netCancel').onclick = () => { $('netDetail').style.display = 'none'; };
  $('netSave').onclick = async () => {
    const edits = rows
      .filter((r) => r.val.value !== '' && r.path.value.trim())
      .map((r) => ({ path: r.path.value.trim(), value: r.val.value }));
    const pattern = $('netPattern').value.trim();
    if (!pattern) return flash('Enter a URL pattern');
    if (!edits.length) return flash('Type a new value for at least one field');
    const rule = {
      id: 'r' + Date.now().toString(36),
      host: tabHost,
      selector: pattern,
      type: 'api',
      value: JSON.stringify(edits),
    };
    state.rules.push(rule);
    state.enabled = true;
    await save();
    render();
    $('netDetail').style.display = 'none';
    flash('API rule saved. Reload the page to see it.');
    send({ type: 'pushRule', rule }).then((x) => x && x.ok === false && flash('Not saved to server: ' + x.error));
  };
}

// The JSON responses the page itself loaded (page-hook keeps the originals). Sending their structure
// with the build request lets the assistant write real api paths, so the brief no longer has to
// spell out endpoint shapes. Only valid JSON bodies are sent; the server caps sizes again.
async function gatherCaptures() {
  const r = await pageMessage({ type: 'getCaptures' });
  const calls = (r && r.calls) || [];          // newest first
  const out = [];
  const seen = new Set();
  for (const c of calls) {
    if (out.length >= 12) break;
    const d = await pageMessage({ type: 'getCapture', id: c.id });
    if (!d || !d.ok) continue;
    let path = d.call.url, body;
    try { path = new URL(d.call.url).pathname; } catch (e) {}
    if (seen.has(path)) continue;              // same endpoint, different query string: one sample is enough
    try { body = JSON.stringify(JSON.parse(d.call.text)); } catch (e) { continue; }
    if (body.length > 60000) continue;
    seen.add(path);
    out.push({ url: d.call.url, method: d.call.method || 'GET', body });
  }
  return out;
}

// The side panel stays open while you switch tabs, so it follows the active tab.
async function refreshTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  activeTab = tab || null;
  try {
    tabHost = tab && tab.url ? new URL(tab.url).hostname : '';
  } catch (e) {
    tabHost = '';
  }
}

// same rule as the page: empty = all, a list, or *.example.com
function ruleHostMatches(pattern, host) {
  if (!pattern) return true;
  return String(pattern).split(/[\s,;]+/).filter(Boolean).some((p) => {
    p = p.toLowerCase();
    if (p === '*') return true;
    if (p.startsWith('*.')) return host === p.slice(2) || host.endsWith(p.slice(1));
    return host === p;
  });
}

function editingNow() {
  const el = document.activeElement;
  return !!el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) && el.id !== 'enabled';
}

async function init() {
  await refreshTab();
  await load();
  render();
  initServer();

  $('netRefresh').onclick = netRefresh;

  $('enabled').onchange = async (e) => {
    state.enabled = e.target.checked;
    await save();
    render();
  };

  $('profile').onchange = async (e) => {
    state.activeProfile = e.target.value;
    await save();
    render();
    send({ type: 'track', event: 'profile_switch', profile: state.activeProfile, host: tabHost });
    send({ type: 'setMyProfile', profile: state.activeProfile });
  };

  const ui = (await chrome.storage.local.get('demoUI')).demoUI || {};
  $('toolbarOn').checked = !!ui.toolbar;
  $('toolbarOn').onchange = (e) => chrome.storage.local.set({ demoUI: { toolbar: e.target.checked } });
  initAI();

  $('newProfile').onclick = async () => {
    const name = (prompt('Profile name (e.g. UK, Germany, CFO):') || '').trim();
    if (!name || state.profiles[name]) return;
    // start from a copy of the current profile so variable names carry over
    state.profiles[name] = Object.assign({}, state.profiles[state.activeProfile]);
    state.activeProfile = name;
    await save();
    render();
  };

  $('delProfile').onclick = async () => {
    if (Object.keys(state.profiles).length < 2) return flash('Keep at least one profile');
    if (!confirm('Delete profile "' + state.activeProfile + '"?')) return;
    delete state.profiles[state.activeProfile];
    state.activeProfile = Object.keys(state.profiles)[0];
    await save();
    render();
  };

  $('saveVars').onclick = async () => {
    state.profiles[state.activeProfile] = textToVars($('vars').value);
    await save();
    flash('Variables saved');
  };

  $('pick').onclick = async () => {
    await refreshTab();
    if (!activeTab) return;
    try {
      await chrome.tabs.sendMessage(activeTab.id, { type: 'startPicker' }, { frameId: 0 });
      flash('Now click an element on the page (Esc cancels)');
    } catch (e) {
      flash('Reload the page once, then try again');
    }
  };

  // follow tab switches and page navigation
  const follow = async () => { await refreshTab(); if (!editingNow()) render(); };
  chrome.tabs.onActivated.addListener(follow);
  chrome.tabs.onUpdated.addListener((id, info) => { if (activeTab && id === activeTab.id && (info.status === 'complete' || info.url)) follow(); });
  chrome.windows.onFocusChanged.addListener(follow);
  // changes made elsewhere (picker, toolbar, shortcuts, sync) show up straight away
  chrome.storage.onChanged.addListener(async (changes, area) => {
    if (area === 'local' && changes[KEY] && !editingNow()) { await load(); render(); }
  });

  $('export').onclick = async () => {
    const json = JSON.stringify(state, null, 2);
    $('io').value = json;
    try {
      await navigator.clipboard.writeText(json);
      flash('Copied to clipboard');
    } catch (e) {
      flash('Select the text and copy it');
    }
  };

  $('import').onclick = async () => {
    try {
      const incoming = normalize(JSON.parse($('io').value));
      if (!Object.keys(incoming.profiles).length) throw new Error('no profiles');
      state = incoming;
      if (!state.profiles[state.activeProfile]) state.activeProfile = Object.keys(state.profiles)[0];
      await save();
      render();
      flash('Imported');
    } catch (e) {
      flash('Invalid scenario JSON');
    }
  };

  $('clear').onclick = async () => {
    if (!confirm('Remove all rules and profiles?')) return;
    state = { enabled: false, activeProfile: 'Default', profiles: { Default: {} }, rules: [] };
    await save();
    render();
  };
}

// ---------- AI assistant ----------
let aiRuns = [];     // proposals waiting for approval: [{id, incomplete}]

function aiMsg(text, bad) {
  const el = $('aiOut');
  el.textContent = text || '';
  el.style.color = bad ? '#dc2626' : '#6b7280';
  el.style.whiteSpace = 'pre-wrap';
  el.style.wordBreak = 'break-word';
}

const tail = (sel) => (sel.length > 46 ? '...' + sel.slice(-43) : sel);

function detailText(p) {
  return p.rules.map((x) => `• ${tail(x.selector)} → ${x.value}`).concat(
    p.profiles.map((x) => `• ${x.name}: ` + Object.entries(x.variables).filter(([k]) => k[0] !== '_').map(([k, v]) => `${k}=${v}`).join(', '))).join('\n');
}

// Reads the event stream of POST /agent/scenarios/{id}/build/stream and calls onEvent(kind, data)
// for every event, so the UI can paint rules while the assistant is still working. The side panel
// is an extension page with <all_urls> host permission, so it may call the API directly with the
// same token the background worker stores in storage.
async function aiStream(body, onEvent) {
  const { auth } = await chrome.storage.local.get('auth');
  if (!auth || !auth.token) throw new Error('Log in first (Server section at the top of the popup).');
  const st = await send({ type: 'status' });
  if (!st.scenarioId) throw new Error('Choose a scenario in the Server section and press Load / Sync first. Importing JSON does not count.');
  const base = (st.apiBase || 'http://localhost:8001').replace(/\/+$/, '');
  let res;
  try {
    res = await fetch(base + `/agent/scenarios/${st.scenarioId}/build/stream`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + auth.token, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw new Error('Cannot reach server at ' + base);
  }
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.json()).detail; } catch (e) {}
    throw new Error(typeof detail === 'string' && detail ? detail : 'Server error ' + res.status);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {            // one event per blank-line-separated block
        const block = buf.slice(0, i);
        buf = buf.slice(i + 2);
        let kind = 'message', data = '';
        for (const line of block.split('\n')) {
          if (line.startsWith('event:')) kind = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        if (!data) continue;                              // keep-alive comment lines carry no data
        let ev;
        try { ev = JSON.parse(data); } catch (e) { continue; }
        onEvent(kind, ev);
      }
    }
  } finally {
    reader.cancel().catch(() => {});                      // no dangling connection if a handler throws
  }
}

function initAI() {
  const busy = (on) => ['aiBuild', 'aiApply'].forEach((id) => ($(id).disabled = on));

  $('aiBuild').onclick = async () => {
    const brief = $('aiBrief').value.trim();
    if (brief.length < 3) return aiMsg('Describe the demo first', true);
    if (brief.length > 1500) return aiMsg('The description is too long (' + brief.length + ' characters). Use one or two sentences, not pasted data.', true);
    busy(true); aiRuns = []; $('aiApply').style.display = 'none';
    // everything the assistant proposes is painted as soon as its validation passes, so a run that
    // stops early still leaves the rules that were built so far on screen
    const live = { rules: [], profiles: [], notes: [] };
    let head = 'Asking the assistant... rules appear as they are proposed\n';
    const paint = () => aiMsg((live.rules.length || live.profiles.length ? '' : head) + liveText(live));
    try {
      aiMsg('Gathering captured API responses...');
      let apiJson = [];
      try { apiJson = await gatherCaptures(); } catch (e) {}
      if (!apiJson.length) {
        throw new Error('No API responses captured yet. Please reload the test page so the extension intercepts the API requests, then click Build again.');
      }
      aiMsg('Building API scenario from ' + apiJson.length + ' captured endpoint' + (apiJson.length === 1 ? '' : 's') + '...');
      paint();
      let final = null;
      await aiStream({ brief, snapshot: [], api_json: apiJson, host: tabHost }, (kind, ev) => {
        if (kind === 'rule') { live.rules.push(ev.rule); paint(); }
        else if (kind === 'profile') { live.profiles.push(ev.profile); paint(); }
        else if (kind === 'tool' && ev.ok === false) { live.notes.push(ev.name + ': ' + ev.output); paint(); }  // the model self-corrects
        else if (kind === 'step' && !live.rules.length && !live.profiles.length) { head = 'Thinking (step ' + ev.step + ')...\n'; paint(); }
        else if (kind === 'done') final = ev;
        else if (kind === 'error') throw new Error(ev.detail || 'The assistant failed.');
      });
      if (!final) throw new Error('The connection closed before the assistant finished.');
      const p = final.proposal;
      if (!p.rules.length && !p.profiles.length) throw new Error('The assistant proposed nothing. Try a clearer description.');
      aiRuns = [{ id: final.run_id, incomplete: !p.complete }];
      if (p.complete) {
        aiMsg(`Proposal: ${p.rules.length} rule(s), ${p.profiles.length} profile(s). ${p.summary || ''}\n` + detailText(p));
        $('aiApply').textContent = 'Apply';
      } else {
        const problems = Array.isArray(p.problems) && p.problems.length ? '\nWarnings: ' + p.problems.join(' ') : '';
        aiMsg(`Partial proposal: ${p.rules.length} rule(s), ${p.profiles.length} profile(s). The assistant stopped before finishing.\n`
          + detailText(p) + problems);
        $('aiApply').textContent = `Apply anyway (${p.rules.length} rule${p.rules.length === 1 ? '' : 's'})`;
      }
      $('aiApply').style.display = '';
    } catch (e) {
      // whatever was built before the stop is already on screen; add the reason it stopped
      const built = live.rules.length || live.profiles.length;
      aiMsg(liveText(live) + (built ? '\n\nStopped: ' + e.message : e.message), !built);
    } finally { busy(false); }
  };

  $('aiApply').onclick = async () => {
    busy(true);
    const warns = [];
    let activeProfile = '';
    try {
      for (const run of aiRuns) {
        const r = await send({ type: 'aiApply', runId: run.id, allowIncomplete: !!run.incomplete });
        if (!r.ok) throw new Error(r.error);
        if (r.result && Array.isArray(r.result.warnings)) warns.push(...r.result.warnings);
        if (r.result && Array.isArray(r.result.profiles_touched) && r.result.profiles_touched.length) {
          activeProfile = r.result.profiles_touched[0];     // the extension switched to the profile the run built for
        }
      }
      aiRuns = [];
      $('aiApply').style.display = 'none';
      await load(); render();
      aiMsg('Applied and synced' + (activeProfile ? ' — active profile: ' + activeProfile : '')
        + '. Reload the page to see it.'
        + (warns.length ? '\nWarnings: ' + warns.join(' ') : ''));
    } catch (e) { aiMsg(e.message, true); } finally { busy(false); }
  };
}

// live view while the assistant works: what has been built so far, plus its self-corrections
function liveText(live) {
  const head = (live.rules.length || live.profiles.length)
    ? 'So far: ' + live.rules.length + ' rule(s), ' + live.profiles.length + ' profile(s)\n' : '';
  const items = live.rules.map((x) => `• ${tail(x.selector)} → ${x.value}`).concat(
    live.profiles.map((x) => `• ${x.name}: ` + Object.entries(x.variables).filter(([k]) => k[0] !== '_').map(([k, v]) => `${k}=${v}`).join(', ')));
  const notes = live.notes.length ? '\n' + live.notes.slice(-3).map((n) => '… ' + n).join('\n') : '';
  return head + items.join('\n') + notes;
}

init();
