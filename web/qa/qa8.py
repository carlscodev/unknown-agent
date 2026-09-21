"""Ask-8 QA: real agent run in the browser, activity monitor, artifact preview,
collapsible code. Mobile first (390x844), then desktop (1440x900)."""

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8787"
OUT = Path(__file__).parent
TOKEN = json.loads((Path("/tmp/uaweb/config/unknown-agent/web_auth.json")).read_text())["owner_token"]

report = {}


def overflow(page) -> dict:
    return page.evaluate(
        "() => ({sw: document.documentElement.scrollWidth, iw: window.innerWidth})"
    )


def shot(page, name) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"))
    print("shot:", name)


def send(page, text) -> None:
    page.fill(".composer textarea", text)
    page.click(".send")


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.goto(BASE)
    page.wait_for_selector("#tok")
    page.fill("#tok", TOKEN)
    page.click("text=Enter console")
    page.wait_for_selector(".composer")
    shot(page, "a1_mobile_idle")

    # -- full agent run on mobile ----------------------------------------- #
    send(page, "crea un archivo qa8/demo8.html: una tarjeta centrada que diga QA OK, fondo oscuro. Solo ese archivo.")
    time.sleep(7)
    shot(page, "a2_mobile_working_early")
    # capture one frame with tool activity visible, if we can
    got_tool = False
    for _ in range(40):
        time.sleep(3)
        if page.locator(".artifact").count():
            break
        if not got_tool and page.locator(".ln.act.tool").count():
            shot(page, "a3_mobile_tools")
            got_tool = True
    page.wait_for_selector(".artifact", timeout=420_000)
    time.sleep(2)
    shot(page, "a4_mobile_artifact")
    report["mobile_overflow"] = overflow(page)

    # verify the preview iframe really loaded the agent file
    frame_src = page.locator(".artifact .frame").first.get_attribute("src")
    report["preview_src"] = frame_src
    report["preview_ok"] = page.locator(".artifact .frame").count() > 0

    # -- desktop: collapsible code ---------------------------------------- #
    page.set_viewport_size({"width": 1440, "height": 900})
    time.sleep(1)
    send(
        page,
        "lee qa8/demo8.html y responde con su contenido completo dentro de un unico "
        "bloque de codigo markdown, sin comentarios",
    )
    page.wait_for_selector(".codeblock", timeout=300_000)
    page.wait_for_selector(".codeblock.collapsed", timeout=15_000)
    time.sleep(2)
    shot(page, "a5_desktop_code_collapsed")
    report["desktop_overflow_collapsed"] = overflow(page)
    page.locator(".codeblock.collapsed .bar button", has_text="expand").first.click()
    time.sleep(1)
    shot(page, "a6_desktop_code_expanded")
    report["desktop_overflow_expanded"] = overflow(page)

    shot(page, "a7_desktop_full")
    browser.close()

(OUT / "qa8_report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
