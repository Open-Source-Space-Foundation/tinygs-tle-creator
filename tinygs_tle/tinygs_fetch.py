#!/usr/bin/env python3
"""Fetch a window of satellite packets from TinyGS by intercepting the SPA's API responses.

TinyGS's `api.tinygs.com` stalls all non-browser clients (Cloudflare
gating), so this drives a real (headless) browser with Playwright, loads
the satellite page, and captures the `/v4/packets` response the page's own
JS makes. This is the only approach that reliably works.

With --auth-state (a Playwright storage_state JSON holding TinyGS's
`sessionToken`/`userId` localStorage entries), the page runs logged in and
sends those as request headers. The output's `_meta` records whether the
packets request actually carried a session token (never the token itself).

Usage:
    tinygs_fetch.py [--sat PROVES_Electra] [--out data/tinygs_packets.json]
                    [--auth-state ~/.config/tinygs/auth.json]
"""

import argparse
import asyncio
import json

from playwright.async_api import async_playwright

DEFAULT_SAT = "PROVES_Electra"


async def fetch(sat: str, out: str, auth_state: str | None = None) -> None:
    captured = {}
    meta = {"auth_state_used": bool(auth_state), "packets_request_authenticated": None}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            storage_state=auth_state,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        )
        page = await ctx.new_page()
        got_packets = asyncio.Event()

        async def on_response(resp):
            u = resp.url
            if "api.tinygs.com" in u and (
                "packets" in u or "/satellite/" in u or "/satellite%2F" in u
            ):
                try:
                    captured[u] = await resp.json()
                except Exception as e:
                    captured[u] = {"_error": str(e), "_status": resp.status}
                if "packets" in u:
                    meta["packets_request_authenticated"] = bool(
                        (await resp.request.all_headers()).get("sessiontoken")
                    )
                    meta["packets_status"] = resp.status
                    got_packets.set()

        page.on("response", on_response)
        await page.goto(
            f"https://app.tinygs.com/satellite/{sat}",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        try:
            await asyncio.wait_for(got_packets.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        await page.wait_for_timeout(3000)
        await browser.close()

    captured["_meta"] = meta
    with open(out, "w") as f:
        json.dump(captured, f, indent=1)
    print("captured", len(captured), "responses ->", out)
    for k, v in captured.items():
        if k == "_meta":
            print(" ", k, v)
            continue
        n = (
            len(v.get("packets", []))
            if isinstance(v, dict)
            else (len(v) if isinstance(v, list) else "?")
        )
        print(" ", k, "items:", n)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--sat",
        default=DEFAULT_SAT,
        help=f"TinyGS satellite slug (default: {DEFAULT_SAT})",
    )
    ap.add_argument("--out", default="tinygs_packets.json", help="output JSON path")
    ap.add_argument(
        "--auth-state",
        default=None,
        help="Playwright storage_state JSON with the TinyGS login (optional)",
    )
    args = ap.parse_args()
    asyncio.run(fetch(args.sat, args.out, args.auth_state))


if __name__ == "__main__":
    main()
