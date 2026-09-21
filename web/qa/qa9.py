"""QA for the 1.5.0 panel features against the stub backend on :8790:
mode chips, artifact kinds (iframe / image / file card), monitor, collapsible
code and WebSocket auto-reconnect across a server restart."""

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8790"
OUT = Path(__file__).parent
TOKEN = json.loads((Path("/tmp/uaweb/config/unknown-agent/web_auth.json")).read_text())["owner_token"]

report = {}


def shot(page, name) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"))
    print("shot:", name)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.goto(BASE)
    page.wait_for_selector("#tok")
    page.fill("#tok", TOKEN)
    page.click("text=Enter console")
    page.wait_for_selector(".composer")

    # -- agent run: three artifact kinds + mode chip ---------------------- #
    page.fill(".composer textarea", "crea una pagina con un logo para qa9")
    page.click(".send")
    time.sleep(1.2)
    shot(page, "s1_mobile_monitor")
    page.wait_for_selector(".artifact", timeout=30_000)
    time.sleep(1.0)
    report["artifacts"] = page.locator(".artifact").count()
    report["iframe_prev"] = page.locator(".artifact iframe").count()
    report["img_prev"] = page.locator(".artifact img.imgprev").count()
    report["file_card"] = page.locator(".artifact .filecard").count()
    report["chip_agent"] = page.locator(".chip-agent").count()
    shot(page, "s2_mobile_artifacts")

    # -- websocket auto-reconnect across a server restart ------------------ #
    page.request.get(BASE + "/admin/kill")

    def health_up() -> bool:
        try:
            return page.request.get(BASE + "/api/health", timeout=2000).status == 200
        except Exception:
            return False

    t = time.time()
    while health_up() and time.time() - t < 10:  # confirm the old process is gone
        time.sleep(0.3)
    shot(page, "s3_mobile_offline")
    t = time.time()
    while not health_up() and time.time() - t < 30:  # wrapper brought it back
        time.sleep(0.5)
    assert health_up()
    time.sleep(1.0)
    page.fill(".composer textarea", "hola, prueba tras reconexion")
    page.wait_for_selector(".send:not([disabled])", timeout=10_000)
    page.click(".send")
    page.wait_for_selector(".codeblock", timeout=30_000)
    time.sleep(1.5)
    shot(page, "s4_mobile_reconnected_code")

    # -- desktop: collapsible code + full transcript ------------------------ #
    page.set_viewport_size({"width": 1440, "height": 900})
    time.sleep(0.8)
    shot(page, "s5_desktop_collapsed")
    page.locator(".codeblock .bar button", has_text="expand").first.click()
    time.sleep(0.6)
    shot(page, "s6_desktop_expanded")
    report["overflow_desktop"] = page.evaluate(
        "() => ({sw: document.documentElement.scrollWidth, iw: window.innerWidth})"
    )
    browser.close()

(OUT / "qa9_report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
