#!/usr/bin/env python3
"""Capture README screenshots + demo video from the dev container.

Runs against http://ardi:9801 which has Local + remotes registered, so the
multi-machine UI shows real data. Saves to docs/. After the per-tab screenshots
it records a short walkthrough video; ffmpeg converts that into demo.gif.

Navigation uses the dashboard's own showTab()/setHost() globals (robust against
layout changes). A redaction pass masks MAC addresses and Tailscale (CGNAT
100.64/10) addresses in every frame so the public screenshots don't leak
hardware/VPN identifiers; private LAN IPs are left as-is.
"""
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from playwright.async_api import async_playwright

URL = "http://ardi:9801"
DOCS = Path(__file__).resolve().parents[1] / "docs"
VIDEO_DIR = DOCS / "_video"
VIEWPORT = {"width": 1280, "height": 860}

# Mask MAC addresses + Tailscale CGNAT (100.64.0.0/10) addresses on every frame.
REDACT_JS = r"""
(() => {
  const MAC = /([0-9a-f]{2}:){5}[0-9a-f]{2}/gi;
  const TS  = /\b100\.(6[4-9]|[7-9]\d|1[0-1]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b/g;
  // Anonymize this fleet's real host/user names for the public screenshots.
  const NAMES = [[/\bardi\b/gi,'host-a'], [/\bcloudy\b/gi,'host-b'],
                 [/\boldie\b/gi,'host-c'], [/\banakin\b/gi,'user']];
  function redact(){
    if(!document.body) return;
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const ns = []; while(w.nextNode()) ns.push(w.currentNode);
    for(const n of ns){
      let v = n.nodeValue; if(!v) continue;
      let r = v.replace(MAC,'··:··:··:··:··:··').replace(TS,'100.x.x.x');
      for(const [re,rep] of NAMES) r = r.replace(re,rep);
      if(r !== v) n.nodeValue = r;
    }
  }
  setInterval(redact, 350);
  if(document.readyState !== 'loading') redact();
  document.addEventListener('DOMContentLoaded', redact);
})();
"""

# (tab_id, host, filename). host=None keeps the current host context.
SHOTS = [
    ("overview",   "local",  "overview.png"),
    ("gpu",        "local",  "gpu.png"),
    ("models",     "local",  "models.png"),
    ("containers", "local",  "containers.png"),
    ("services",   "local",  "services.png"),
    ("host",       "local",  "system.png"),
    ("network",    "local",  "network.png"),
    ("security",   "cloudy", "security.png"),   # remote shows real REVIEW items
    ("hosts",      "local",  "hosts.png"),
]


async def settle(page, ms=1100):
    await page.wait_for_timeout(ms)


async def show(page, host, tab):
    if host:
        await page.evaluate(f"setHost({host!r})")
    await page.evaluate(f"showTab({tab!r})")
    await settle(page)
    await page.evaluate(REDACT_JS)
    await page.wait_for_timeout(250)


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
        await ctx.add_init_script(REDACT_JS)
        page = await ctx.new_page()
        print(f"opening {URL}")
        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_selector("#fleet_tbl tbody tr td.hostcell", timeout=15000)
        await settle(page, 900)

        for tab_id, host, fname in SHOTS:
            # The Hosts tab needs cloudy's check expanded for a useful shot.
            if tab_id == "hosts":
                await show(page, host, tab_id)
                try:
                    row = page.locator('.hostrow[data-h="cloudy"]')
                    if not await row.locator('.hcheck.show').count():
                        await row.locator('.hbtn-test, button:has-text("Test")').first.click()
                        await page.wait_for_selector('.hostrow[data-h="cloudy"] .hcheck.show', timeout=20000)
                        await page.wait_for_timeout(500)
                except Exception as e:
                    print(f"  warning: couldn't expand cloudy's check ({e})")
                await page.evaluate(REDACT_JS)
                await page.wait_for_timeout(300)
            else:
                await show(page, host, tab_id)
            await snap(page, fname)

        await ctx.close()

        # ── walkthrough video (showcases the new System/Network/Security tabs) ─
        print("\nrecording walkthrough video")
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        vctx = await browser.new_context(
            viewport=VIEWPORT, device_scale_factor=1,
            record_video_dir=str(VIDEO_DIR), record_video_size=VIEWPORT,
        )
        await vctx.add_init_script(REDACT_JS)
        vpage = await vctx.new_page()
        await vpage.goto(URL, wait_until="networkidle")
        await vpage.wait_for_selector("#fleet_tbl tbody tr td.hostcell", timeout=15000)
        await settle(vpage, 800)

        async def beat(ms=1250):
            await vpage.wait_for_timeout(ms)

        # Overview → cloudy → System → Network → Security → local → GPU → Models → Overview
        await show(vpage, "local",  "overview"); await beat(1500)
        await show(vpage, "cloudy", "overview"); await beat(900)
        await show(vpage, "cloudy", "host");     await beat(1500)
        await show(vpage, "cloudy", "network");  await beat(1600)
        await show(vpage, "cloudy", "security"); await beat(1800)
        await show(vpage, "local",  "gpu");      await beat(1400)
        await show(vpage, "local",  "models");   await beat(1400)
        await show(vpage, "local",  "overview"); await beat(900)

        await vctx.close()
        await browser.close()

    webms = list(VIDEO_DIR.glob("*.webm"))
    if not webms:
        print("no video recorded", file=sys.stderr); return 1
    webm = webms[0]
    gif = DOCS / "demo.gif"
    palette = VIDEO_DIR / "palette.png"
    print(f"converting {webm.name} -> docs/demo.gif via ffmpeg")
    common_filter = "fps=12,scale=860:-2:flags=lanczos"
    subprocess.run(["ffmpeg", "-y", "-i", str(webm),
                    "-vf", f"{common_filter},palettegen=max_colors=128", str(palette)],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-i", str(webm), "-i", str(palette),
                    "-lavfi", f"{common_filter} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5",
                    str(gif)], check=True, capture_output=True)
    print(f"  wrote docs/demo.gif ({gif.stat().st_size//1024} KB)")
    shutil.rmtree(VIDEO_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
