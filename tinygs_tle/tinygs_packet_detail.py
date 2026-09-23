#!/usr/bin/env python3
"""Fetch per-station reception details (doppler, freq error, rssi, snr, usec_time) for one TinyGS packet id.

Same Cloudflare-gating story as tinygs_fetch.py: this has to go through a
real headless browser hitting the packet detail page, which triggers the
SPA to call `/v3/packet/<id>`.

Usage:
    tinygs_packet_detail.py PACKET_ID [--out packet_<id>.json] [--auth-state FILE]
"""

import argparse
import asyncio
import json

from playwright.async_api import async_playwright


async def fetch(packet_id: str, out: str, auth_state: str | None = None) -> None:
    captured = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            storage_state=auth_state,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        )
        page = await ctx.new_page()
        got = asyncio.Event()

        async def on_response(resp):
            if "api.tinygs.com" in resp.url and "/packet/" in resp.url:
                try:
                    captured[resp.url] = await resp.json()
                except Exception as e:
                    captured[resp.url] = {"_error": str(e)}
                got.set()

        page.on("response", on_response)
        await page.goto(
            f"https://app.tinygs.com/packet/{packet_id}",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        try:
            await asyncio.wait_for(got.wait(), timeout=45)
        except asyncio.TimeoutError:
            pass
        await page.wait_for_timeout(2000)
        await browser.close()

    with open(out, "w") as f:
        json.dump(captured, f, indent=1)
    print("captured:", list(captured))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("packet_id", help="TinyGS packet id")
    ap.add_argument(
        "--out", default=None, help="output JSON path (default: packet_<id>.json)"
    )
    ap.add_argument(
        "--auth-state",
        default=None,
        help="Playwright storage_state JSON with the TinyGS login (optional)",
    )
    args = ap.parse_args()
    out = args.out or f"packet_{args.packet_id}.json"
    asyncio.run(fetch(args.packet_id, out, args.auth_state))


if __name__ == "__main__":
    main()
