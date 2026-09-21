"""Chromium QA: screenshots at mobile / tablet / desktop + overflow checks."""

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8787"
OUT = Path(__file__).parent
TOKEN = json.loads((Path("/tmp/uaweb/config/unknown-agent/web_auth.json")).read_text())["owner_token"]

DESKTOP = {"width": 1440, "height": 900}
TABLET = {"width": 820, "height": 1180}
MOBILE = {"width": 390, "height": 844}

report = {}


def overflow(page) -> dict:
    return page.evaluate(
        "() => ({sw: document.documentElement.scrollWidth, iw: window.innerWidth, "
        "bw: document.body.scrollWidth})"
    )


def login(page) -> None:
    page.goto(BASE)
    page.wait_for_selector("#tok")
    page.fill("#tok", TOKEN)
    page.click("text=Enter console")
    page.wait_for_selector(".composer")


def chat_seed(page) -> None:
    page.fill(".composer textarea", "/status")
    page.click(".send")
    page.wait_for_selector(".msg.command")


def stream_shot(page, name) -> None:
    page.fill(".composer textarea", "Escribe un haiku breve sobre terminales unix")
    page.click(".send")
    try:
        page.wait_for_selector(".stream", timeout=20000)
        time.sleep(0.7)
        page.screenshot(path=str(OUT / name))
    finally:
        page.wait_for_selector(".msg.assistant:not(.typing)", timeout=30000)


with sync_playwright() as pw:
    browser = pw.chromium.launch()

    # desktop
    ctx = browser.new_context(viewport=DESKTOP, device_scale_factor=1)
    page = ctx.new_page()
    page.goto(BASE)
    page.wait_for_selector("#tok")
    page.screenshot(path=str(OUT / "01-login-desktop.png"))
    report["login-desktop"] = overflow(page)
    login(page)
    chat_seed(page)
    page.screenshot(path=str(OUT / "02-chat-desktop.png"))
    report["chat-desktop"] = overflow(page)
    stream_shot(page, "03-stream-desktop.png")
    page.click('nav.rail button[aria-label="Console"]')
    page.wait_for_selector(".dash .tabs")
    page.screenshot(path=str(OUT / "04-dash-desktop.png"))
    report["dash-desktop"] = overflow(page)
    ctx.close()

    # tablet
    ctx = browser.new_context(viewport=TABLET)
    page = ctx.new_page()
    login(page)
    chat_seed(page)
    page.screenshot(path=str(OUT / "05-chat-tablet.png"))
    report["chat-tablet"] = overflow(page)
    ctx.close()

    # mobile
    ctx = browser.new_context(
        viewport=MOBILE, device_scale_factor=2, is_mobile=True, has_touch=True,
        user_agent="Mozilla/5.0 (Linux; Android 14) Chrome/126 Mobile",
    )
    page = ctx.new_page()
    page.goto(BASE)
    page.wait_for_selector("#tok")
    page.screenshot(path=str(OUT / "06-login-mobile.png"))
    report["login-mobile"] = overflow(page)
    login(page)
    chat_seed(page)
    page.screenshot(path=str(OUT / "07-chat-mobile.png"))
    report["chat-mobile"] = overflow(page)
    stream_shot(page, "08-stream-mobile.png")
    report["stream-mobile"] = overflow(page)
    page.click('nav.rail button[aria-label="Console"]')
    page.wait_for_selector(".dash .tabs")
    page.screenshot(path=str(OUT / "09-dash-mobile.png"))
    report["dash-mobile"] = overflow(page)
    ctx.close()

    browser.close()

print(json.dumps(report, indent=2))
bad = {k: v for k, v in report.items() if v["sw"] > v["iw"]}
print("OVERFLOW-ISSUES:", bad or "none")
