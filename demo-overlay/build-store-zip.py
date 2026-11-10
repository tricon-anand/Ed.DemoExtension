"""Builds demo-overlay-store.zip with only the files the extension needs (no test pages, no samples).
Run:  python build-store-zip.py     Upload the zip to the Chrome Web Store developer dashboard."""
import json, zipfile
from pathlib import Path

root = Path(__file__).parent
files = ["manifest.json", "background.js", "content.js", "page-hook.js", "popup.html", "popup.js",
         "icons/icon16.png", "icons/icon32.png", "icons/icon48.png", "icons/icon128.png"]
version = json.loads((root / "manifest.json").read_text())["version"]
out = root / f"demo-overlay-store-{version}.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in files:
        z.write(root / f, f)          # files sit at the top of the zip, not inside a folder
print("wrote", out, f"({out.stat().st_size // 1024} KB)")
