import asyncio, base64, pathlib
from playwright.async_api import async_playwright

HERE = pathlib.Path(__file__).parent
JOBS = [
    # svg file, output png, css width, css height, scale (deviceScaleFactor)
    ("icon.svg",   "homelab-monitor-icon-1024.png",  512,  512, 2),  # -> 1024x1024
    ("icon.svg",   "homelab-monitor-icon-512.png",   512,  512, 1),  # -> 512x512
    ("banner.svg", "homelab-monitor-banner-1500.png",1500, 500, 2),  # -> 3000x1000
]

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            executable_path=r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe")
        for svg, out, w, h, scale in JOBS:
            svg_text = (HERE / svg).read_text(encoding="utf-8")
            page = await browser.new_page(viewport={"width": w, "height": h},
                                          device_scale_factor=scale)
            html = ("<!doctype html><html><head><style>"
                    "*{margin:0;padding:0}html,body{background:transparent}"
                    f"svg{{display:block;width:{w}px;height:{h}px}}"
                    "</style></head><body>" + svg_text + "</body></html>")
            await page.set_content(html, wait_until="networkidle")
            await page.screenshot(path=str(HERE / out), omit_background=True,
                                  clip={"x":0,"y":0,"width":w,"height":h})
            await page.close()
            print("wrote", out, f"({w*scale}x{h*scale})")
        await browser.close()

asyncio.run(main())
