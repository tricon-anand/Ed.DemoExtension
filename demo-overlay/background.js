// Demo Overlay - background service worker: talks to the FastAPI server.
const DEFAULT_API = 'http://localhost:8001';

const store = {
  get: (k) => chrome.storage.local.get(k).then((r) => r[k]),
  set: (obj) => chrome.storage.local.set(obj),
  remove: (k) => chrome.storage.local.remove(k),
};

async function call(path, { method = 'GET', body, headers = {}, auth, keepSession = false } = {}) {
  auth = auth || (await store.get('auth'));
  const base = ((auth && auth.apiBase) || DEFAULT_API).replace(/\/+$/, '');
  const h = { ...headers };
  if (auth && auth.token) h.Authorization = 'Bearer ' + auth.token;
  let payload = body;
  if (body && !(body instanceof URLSearchParams)) {
    h['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(base + path, { method, headers: h, body: payload });
  } catch (e) {
    throw new Error('Cannot reach server at ' + base);
  }
  if (res.status === 304) return { status: 304, res };
  if (!res.ok) {
    let detail = '';
    try {
      detail = (await res.json()).detail;
      // validation errors (422) come as a list: show which field is wrong
      if (Array.isArray(detail)) detail = detail.map((d) => (d.loc ? d.loc.slice(1).join('.') + ': ' : '') + d.msg).join('; ');
    } catch (e) {}
    if (res.status === 401 && !keepSession) {
      await store.remove('auth');
      throw new Error('Session expired. Please log in again.');
    }
    throw new Error(typeof detail === 'string' && detail ? detail : 'Server error ' + res.status);
  }
  return { status: res.status, res, data: res.status === 204 ? null : await res.json() };
}

async function status() {
  const auth = await store.get('auth');
  const server = await store.get('server');
  return {
    ok: true,
    loggedIn: !!auth,
    email: auth ? auth.email : '',
    role: auth ? auth.role : '',
    apiBase: (auth && auth.apiBase) || DEFAULT_API,
    scenarioId: server ? server.scenarioId : null,
    version: server ? server.version : null,
  };
}

async function login({ apiBase, email, password }) {
  apiBase = (apiBase || DEFAULT_API).trim();
  // FastAPI's OAuth2 form calls the email field "username"
  const form = new URLSearchParams({ username: email, password });
  const t = await call('/auth/login', { method: 'POST', body: form, auth: { apiBase }, keepSession: true });
  const token = t.data.access_token;
  const me = await call('/auth/me', { auth: { apiBase, token } });
  await store.set({ auth: { apiBase, token, email: me.data.email, role: me.data.role } });
  return { ok: true };
}

async function loadScenario(scenarioId, force = false) {
  const server = await store.get('server');
  const same = server && server.scenarioId === scenarioId;
  const headers = {};
  if (same && server.etag && !force) headers['If-None-Match'] = server.etag;

  const r = await call(`/scenarios/${scenarioId}/bundle`, { headers });
  if (r.status === 304) return { ok: true, changed: false, version: server.version };

  const b = r.data;
  track('sync', scenarioId);
  const prev = await store.get('demo');
  // keep the presenter's chosen profile if it still exists
  const keep = same && prev && b.profiles[prev.activeProfile] ? prev.activeProfile : b.activeProfile;
  await store.set({
    demo: { enabled: true, activeProfile: keep, profiles: b.profiles, rules: b.rules },
    server: { scenarioId, etag: r.res.headers.get('ETag'), version: b.version, syncedAt: Date.now() },
  });
  return { ok: true, changed: true, version: b.version };
}

async function pushRule(rule) {
  const server = await store.get('server');
  if (!server || !(await store.get('auth'))) return { ok: true, skipped: true };
  await call(`/scenarios/${server.scenarioId}/rules`, {
    method: 'POST',
    body: { selector: rule.selector, type: rule.type, value: rule.value },
  });
  return loadScenario(server.scenarioId, true);
}

async function server() {
  if (!(await store.get('auth'))) throw new Error('Log in first (Server section at the top of the popup).');
  const s = await store.get('server');
  if (!s) throw new Error('Choose a scenario in the Server section and press Load / Sync first. Importing JSON does not count.');
  return s;
}

// Usage events are best effort: a failure here must never get in the way of the demo.
async function track(event, scenarioId, extra = {}) {
  try {
    const s = scenarioId ? { scenarioId } : await server();
    await call('/usage', { method: 'POST', keepSession: true,
      body: { event, scenario_id: s.scenarioId, host: extra.host || '', profile: extra.profile || '' } });
  } catch (e) { /* ignore */ }
}

async function handle(msg) {
  switch (msg && msg.type) {
    case 'status': return status();
    case 'login': return login(msg);
    case 'logout':
      await store.remove('auth');
      await store.remove('server');
      return { ok: true };
    case 'listScenarios': {
      const r = await call('/scenarios');
      return { ok: true, scenarios: r.data };
    }
    case 'loadScenario': return loadScenario(msg.scenarioId, !!msg.force);
    case 'pushRule': return pushRule(msg.rule);
    case 'track': await track(msg.event, null, msg); return { ok: true };
    case 'setMyProfile': {
      try {
        const s = await server();
        await call(`/scenarios/${s.scenarioId}/my-profile`, { method: 'PUT', body: { profile: msg.profile }, keepSession: true });
      } catch (e) { /* the profile may be local only */ }
      return { ok: true };
    }
    // AI assistant: the server proposes, a person approves.
    // aiBuild is the one-shot fallback; the popup uses the /build/stream endpoint directly so
    // partial results (rules/profiles) arrive while the agent is still working.
    case 'aiBuild': {
      const s = await server();
      const r = await call(`/agent/scenarios/${s.scenarioId}/build`, { method: 'POST',
        body: { brief: msg.brief, snapshot: msg.snapshot || [], host: msg.host || '' }, keepSession: true });
      return { ok: true, run: r.data };
    }
    case 'aiApply': {
      const s = await server();
      const r = await call(`/agent/runs/${msg.runId}/apply`, { method: 'POST',
        body: msg.allowIncomplete ? { allow_incomplete: true } : undefined, keepSession: true });
      await loadScenario(s.scenarioId, true);
      return { ok: true, result: r.data };
    }
    default: return { ok: false, error: 'Unknown message' };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handle(msg)
    .then(sendResponse)
    .catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));
  return true; // keep the channel open for the async reply
});

// Keyboard shortcuts (chrome://extensions/shortcuts lets you change them) go to the page's top frame.
chrome.commands.onCommand.addListener(async (name, tab) => {
  try {
    if (!tab || !tab.id) [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    await chrome.tabs.sendMessage(tab.id, { type: 'command', name }, { frameId: 0 });
  } catch (e) { /* page has no content script (chrome:// pages, the web store) */ }
});

// Clicking the toolbar icon opens the side panel (it stays open while you switch tabs).
if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
}
