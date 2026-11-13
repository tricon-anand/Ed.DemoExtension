# Demo Overlay (Phase 1 MVP)

A Chrome extension that overlays scenario-based mock data on any web app, for sales demos.

## Install (2 minutes)

1. Open `chrome://extensions` and turn on **Developer mode** (top right).
2. Click **Load unpacked** and select this `demo-overlay` folder.
3. Pin the extension from the puzzle-piece menu.

## Try it on the included test page

In PowerShell, from inside this folder:

```powershell
python -m http.server 8000
```

1. Open `http://localhost:8000/test-page.html`.
2. Click the extension icon, open **Import / Export scenario**, paste the contents of `sample-scenario.json`, click **Import**.
3. Switch the **Profile** dropdown between US, UK and India. The page data changes instantly.
4. Toggle the switch **Off** to restore the original page.

## Use it on a real app

1. Open the app, click the extension icon, click **Pick an element on this page**.
2. Click any text, number or image on the page. Choose an action (replace text / hide / replace image / replace HTML) and type the new value.
3. Use variables like `{{company}}` or `{{currency}}` in values. Define them per profile (country, persona) in the popup.
4. Export the scenario to share it with teammates.

## How it works

- `content.js` stores rules as `selector -> replacement`, applies them, and re-applies on page changes (MutationObserver) so it survives React/Angular/Vue re-renders.
- A small "DEMO DATA" badge shows while active, so demo screens are never mistaken for real data.
- Everything is stored in `chrome.storage.local` for now. No backend yet.

## Known limits (planned for later phases)

- Text inside canvas charts, iframes and closed shadow DOM is not changed yet.
- Charts that load data from an API need network response overriding (Phase 3).
- Selectors can break when the target app updates; re-pick the element to fix.
- No login or team sharing until the FastAPI backend (Phase 2).

## Phase 3 Step 1: API data override (charts and tables)

Many apps draw charts and tables from JSON calls (fetch or XHR), not from page text. `page-hook.js` runs in the page's own JavaScript world and edits those JSON responses before the app sees them.

**API rule**
- type: `api`
- selector: a URL pattern. `*` matches anything. An optional method prefix works, for example `GET */api/dashboard*`.
- value: a JSON list of edits, for example `[{"path":"kpis.revenue","value":"{{revenue}}"},{"path":"monthly[*].sales","value":"1,980,500"}]`

**Path syntax:** dots for keys, `[n]` for an array item, `[*]` for every item.
**Rules:** only keys that already exist are changed. The new value takes the original type, so "1,980,500" replaces a number with a number. `{{variable}}` uses the active profile value.

**Create one without typing JSON:** reload the app page, open the popup, expand "API data (charts and tables)", click "Show captured calls", pick a call, type new values and save.

After switching profile, reload the page to refresh API data. The badge says so.

**Test:** in the demo-overlay folder run `python -m http.server 8000`, open `http://localhost:8000/test-page-api.html`, and use a scenario with `target_host` = `localhost`.


## Phase 3 Step 2: iframes, shadow DOM and several hosts

* **Iframes** are handled automatically (the extension runs in every frame, including `about:blank` and `srcdoc` frames).
* **Rule host** (admin page column "Host", optional). Empty = the scenario host. A list `app.acme.com, reports.acme.com`, or a wildcard `*.acme.com` (this also matches `acme.com`). One scenario can therefore change a page and the iframes and sibling apps it embeds.
* **Shadow DOM**: join selectors with `>>>`: `stat-card >>> .total`, nested `outer-card >>> inner-card >>> .deep`. The picker writes these for you. If a plain selector finds nothing, open shadow roots are searched too. *Closed* shadow roots are opened by the page hook, so rules can reach them.
* Test: serve this folder (`python -m http.server 8000`) and open `http://localhost:8000/test-page-frames.html`.

## Phase 3 Step 3: presenter toolbar and shortcuts

* Tick "Show presenter toolbar on pages" in the popup. A small bar appears bottom right: **Demo data / Real data** switch, profile picker, Reload, hide.
* Shortcuts (change at `chrome://extensions/shortcuts`): `Alt+Shift+D` demo/real, `Alt+Shift+P` next profile, `Alt+Shift+T` toolbar.
* When a switch changes API data, the page reloads itself so charts follow.

## Phase 3 Step 4 and Phase 4 (server side)

See the backend README: history and restore, audit log, usage analytics, templates, import/export, invitations, login throttling, per-user default profile, and the AI assistant.

* Popup "AI assistant": describe the demo, press **Build with AI** (it reads the page first), review, **Apply**. Rules the assistant creates use host `*`, so they show on every URL, not just the scenario's app host (you can narrow a rule in the admin page later). The build streams: each rule and profile appears in the popup the moment the server validates it, so you can follow the assistant working (including where it fixes its own mistakes). If it stops early — step limit or a network error — whatever was built so far stays on screen, and if the server kept it as a proposal you can press **Apply anyway** to use just those rules.
* Usage events (`page_view`, `profile_switch`, `demo_on`, `demo_off`, `sync`) are sent to your own server only, never to a third party.
