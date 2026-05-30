#!/usr/bin/env python3
"""Capture v0.8.0 README screenshots + demo video from the dev container.

Runs against http://ardi:9801 which has Local + cloudy registered, so the
multi-machine UI shows real data. Saves to docs/. After the per-tab screenshots
it records a short walkthrough video; ffmpeg converts that into demo.gif.
"""
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

from playwright.async_api import async_playwright

URL = "http://ardi:9801"
DOCS = Path(__file__).resolve().parents[1] / "docs"
VIDEO_DIR = DOCS / "_video"
VIEWPORT = {"width": 1280, "height": 820}

# Tabs we'll snapshot at the local-host context.
LOCAL_SHOTS = [
    ("gpu",        "gpu.png"),
    ("containers", "containers.png"),
    ("services",   "services.png"),
    ("host",       "host.png"),
]
# Hosts tab: open and snapshot the registry with cloudy's check expanded.
HOSTS_SHOT = "hosts.png"
OVERVIEW_SHOT = "overview.png"


async def wait_for_overview_data(page):
    # Wait until the All-hosts table has at least one populated row.
    await page.wait_for_selector("#fleet_tbl tbody tr td.hostcell", timeout=15000)
    # Give one render cycle for KPIs to fill in.
    await page.wait_for_timeout(800)


async def go_to_tab(page, tab_id):
    await page.locator(f'.tab[data-t="{tab_id}"]').click()
    await page.wait_for_timeout(900)   # let renderers settle


async def select_host(page, name):
    await page.locator(f'.hpill[data-h="{name}"]').click()
    await page.wait_for_timeout(700)


async def snap(page, out):
    out = DOCS / out
    out.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(out), full_page=False)
    print(f"  wrote {out.relative_to(DOCS.parent)}")


async def main():
    DOCS.mkdir(parents=True, exist_ok=True)
    if VIDEO_DIR.exists():
        shutil.rmtree(VIDEO_DIR)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # ── per-tab still screenshots ─────────────────────────────────────────
        ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
        page = await ctx.new_page()
        print(f"opening {URL}")
        await page.goto(URL, wait_until="networkidle")
        await wait_for_overview_data(page)

        # Make sure we're on Local for the per-subsystem shots.
        await select_host(page, "local")

        # Overview is already on by default.
        await go_to_tab(page, "overview")
        await page.wait_for_timeout(600)
        await snap(page, OVERVIEW_SHOT)

        for tab_id, fname in LOCAL_SHOTS:
            await go_to_tab(page, tab_id)
            await snap(page, fname)

        # Hosts tab: expand cloudy's check by hitting Test so the screenshot
        # shows the full capability checklist + OS badge.
        await go_to_tab(page, "hosts")
        # Wait for the registry list to render.
        try:
            await page.wait_for_selector('.hostrow[data-h="cloudy"]', timeout=8000)
            # If the last check is already cached the section is open; otherwise
            # click Test and wait for the checklist to expand.
            row = page.locator('.hostrow[data-h="cloudy"]')
            check_visible = await row.locator('.hcheck.show').count()
            if not check_visible:
                await row.locator('.hbtn-test').click()
                await page.wait_for_selector(
                    '.hostrow[data-h="cloudy"] .hcheck.show', timeout=20000)
                await page.wait_for_timeout(500)
        except Exception as e:
            print(f"  warning: couldn't expand cloudy's check ({e}) — snapping anyway")
        await snap(page, HOSTS_SHOT)

        await ctx.close()

        # ── walkthrough video ─────────────────────────────────────────────────
        print("\nrecording walkthrough video")
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        vctx = await browser.new_context(
            viewport=VIEWPORT, device_scale_factor=1,
            record_video_dir=str(VIDEO_DIR),
            record_video_size=VIEWPORT,
        )
        vpage = await vctx.new_page()
        await vpage.goto(URL, wait_until="networkidle")
        await wait_for_overview_data(vpage)

        async def beat(ms=1100):
            await vpage.wait_for_timeout(ms)

        # Storyline: Overview → cloudy pill → Host → Services → Local → GPU → Hosts → Overview
        await select_host(vpage, "local")
        await go_to_tab(vpage, "overview");  await beat(1600)
        await select_host(vpage, "cloudy");  await beat(1200)
        await go_to_tab(vpage, "host");      await beat(1400)
        await go_to_tab(vpage, "services");  await beat(1500)
        await select_host(vpage, "local");   await beat(900)
        await go_to_tab(vpage, "gpu");       await beat(1300)
        await go_to_tab(vpage, "hosts");     await beat(1400)
        await go_to_tab(vpage, "overview");  await beat(900)

        await vctx.close()
        await browser.close()

    # The video file Playwright wrote — there'll be exactly one webm.
    webms = list(VIDEO_DIR.glob("*.webm"))
    if not webms:
        print("no video recorded", file=sys.stderr); return 1
    webm = webms[0]
    gif = DOCS / "demo.gif"
    palette = VIDEO_DIR / "palette.png"

    print(f"converting {webm.name} -> docs/demo.gif via ffmpeg (with palette)")
    # Generate a colour palette tuned for the dark theme, then build the gif
    # at 12 fps and 800px wide — good balance of clarity and file size for a
    # README hero.
    common_filter = "fps=12,scale=800:-2:flags=lanczos"
    subprocess.run([
        "ffmpeg", "-y", "-i", str(webm),
        "-vf", f"{common_filter},palettegen=max_colors=128",
        str(palette),
    ], check=True, capture_output=True)
    subprocess.run([
        "ffmpeg", "-y", "-i", str(webm), "-i", str(palette),
        "-lavfi", f"{common_filter} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5",
        str(gif),
    ], check=True, capture_output=True)
    print(f"  wrote docs/demo.gif ({gif.stat().st_size//1024} KB)")

    shutil.rmtree(VIDEO_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
