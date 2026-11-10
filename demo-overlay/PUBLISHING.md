# Publishing and rolling out the extension

## Option A: your own team (no store review)
1. **Load unpacked** (what you do now): chrome://extensions, Developer mode, Load unpacked.
2. **Force-install for a company** with the Chrome policy `ExtensionInstallForcelist` (Windows Group Policy or Google Admin console). The extension must be hosted in the store (Option B, unlisted) or on your own update server.

## Option B: Chrome Web Store
1. Run `python build-store-zip.py`. It makes `demo-overlay-store-<version>.zip` with only runtime files. Raise `"version"` in manifest.json for every upload.
2. Register at https://chrome.google.com/webstore/devconsole (one-time registration fee, currently US$5).
3. Upload the zip. Choose visibility:
   * **Unlisted**: anyone with the link can install; not searchable. Best for a sales team.
   * **Private**: only members of your Google Workspace domain or listed testers.
   * **Public**: searchable. Expect closer review.
4. Fill in the listing. Suggested wording:
   * Single purpose: "Replace on-screen data in web applications with scenario-based sample data for product demos."
   * Permission justifications:
     * `host_permissions <all_urls>`: the user chooses any web app to demo; rules run only on hosts that a loaded scenario names.
     * `storage`: keeps the scenario, profiles and login on the device.
     * `activeTab`: the popup talks to the page you are on.
   * Remote code: **No**. The extension runs only code in the package. (The AI assistant runs on your server and only returns data.)
5. Privacy tab. Be exact about what leaves the browser:
   * To **your own server** only: login, scenario data, page scan text when you press "Build with AI" (up to 150 short text snippets), usage events (event name, profile, host).
   * Nothing is sent to third parties by the extension. Your server sends the brief and page scan to Anthropic's API when the assistant is used, so say that in the privacy policy.
   * Publish a privacy policy page (required because the extension handles user data) and link it.
6. Submit. First review usually takes a few days; broad host permissions can take longer. Reply to reviewer questions with the justifications above.

## Before you ship to other people
* Host the server on HTTPS and set `APP_ENV=production` (see the backend README). The popup's API URL must be https.
* Agree what may be demoed. Demo data should be invented; never put real customer or patient data into profiles or the AI brief.
* The "DEMO DATA" badge is always shown on pages where a scenario is active, so nobody mistakes the numbers for real ones.
