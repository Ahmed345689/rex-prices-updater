            fut = pending.pop(int(current), None)
            if fut and not fut.done():
                """
Scrape https://renderz.app/24/players using Playwright.

Why Playwright?
The site is a SvelteKit app. The player list is fetched via
`POST /api/players/filter`, which is gated by a custom `x-secure-token`
handshake (`/api/secure-token/init` returns a binary blob decoded by an
obfuscated JS function). Reproducing that handshake in pure `requests`
is fragile, so we let a real browser perform it and we just intercept
the resulting JSON responses.

Strategy:
  1. Launch headless Chromium.
  2. Subscribe to `page.on("response", ...)` and capture every JSON body
     whose URL matches `/api/players/filter`.
  3. Navigate to /24/players, wait for the first response.
  4. For each subsequent page, navigate to `?page=N&sortDirection=DESC&sortType=rating`
     (the route auto-syncs to URL search params, which triggers a new
     POST /api/players/filter), and capture the response.
  5. Stop when we've reached the last page (per `pageData.pageCount`)
     or hit `MAX_PAGES`.
  6. Extract `name` + `price` for every player and write `players.json`.

Output schema:
{
  "scrapedAt": "<ISO8601 UTC>",
  "season": "24",
  "totalPlayers": <int>,
  "players": [
    {"name": "...", "price": <int|null>, "raw": { ...full original record... }},
    ...
  ]
}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Response, TimeoutError as PlaywrightTimeoutError

URL = "https://renderz.app/24/players"
API_PATH = "/api/players/filter"
OUTPUT_FILE = Path(os.environ.get("OUTPUT_FILE", "players.json"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "0"))  # 0 = unlimited
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "60000"))
RESPONSE_TIMEOUT_S = float(os.environ.get("RESPONSE_TIMEOUT_S", "45"))
HEADLESS = os.environ.get("HEADLESS", "1") not in ("0", "false", "False", "")


# ---------------------------------------------------------------------------
# Helpers to extract a name + price out of a player record without knowing
# the exact schema upfront.
# ---------------------------------------------------------------------------

NAME_KEYS = (
    "name", "commonName", "displayName", "fullName",
    "knownAs", "nickname", "shortName",
)
FIRST_KEYS = ("firstName", "first_name", "firstname")
LAST_KEYS = ("lastName", "last_name", "lastname", "surname")
PRICE_KEYS = (
    "price", "prices", "auctionPrice", "currentPrice", "marketPrice",
    "lowestBin", "lowestPrice", "buyNowPrice", "bin",
)


def _first_present(record: dict, keys) -> Any:
    for k in keys:
        if k in record and record[k] not in (None, "", 0):
            return record[k]
    for k in keys:
        if k in record:
            return record[k]
    return None


def extract_name(record: dict) -> str | None:
    name = _first_present(record, NAME_KEYS)
    if name:
        return str(name)
    first = _first_present(record, FIRST_KEYS)
    last = _first_present(record, LAST_KEYS)
    if first or last:
        return " ".join(p for p in (first, last) if p)
    # Some APIs nest the player inside `player` / `card` / `details`
    for nest in ("player", "card", "details", "data"):
        sub = record.get(nest)
        if isinstance(sub, dict):
            n = extract_name(sub)
            if n:
                return n
    return None


def extract_price(record: dict) -> int | None:
    val = _first_present(record, PRICE_KEYS)
    if val is None:
        # Sometimes nested under `prices` or `market`
        for nest in ("prices", "market", "auction", "player", "card"):
            sub = record.get(nest)
            if isinstance(sub, dict):
                v = extract_price(sub)
                if v is not None:
                    return v
        return None
    if isinstance(val, dict):
        # e.g. {"current": 12345}
        for k in ("current", "value", "amount", "lowest", "buyNow"):
            if k in val:
                val = val[k]
                break
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Main scrape
# ---------------------------------------------------------------------------

async def scrape() -> dict:
    captured: dict[int, dict] = {}  # page_number -> json body
    pending: dict[int, asyncio.Future] = {}

    async def handle_response(response: Response) -> None:
        try:
            if API_PATH not in response.url or response.request.method != "POST":
                return
            if response.status != 200:
                print(f"[response] {response.status} {response.url}")
                return
            try:
                body = await response.json()
            except Exception as e:
                print(f"[response] JSON parse failed: {e}")
                return

            page_data = body.get("pageData") or {}
            current = page_data.get("currentPage") or 1
            total_pages = page_data.get("pageCount") or 1
            row_count = page_data.get("rowCount")
            players = body.get("players") or []
            print(f"[response] page {current}/{total_pages}, "
                  f"{len(players)} players, total={row_count}")
            captured[int(current)] = body
fut.set_result(body)
        except Exception as e:
            print(f"[response] handler error: {e}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        page = await context.new_page()
        page.on("response", lambda r: asyncio.create_task(handle_response(r)))

        # 1) First page — register the future BEFORE navigation
        loop = asyncio.get_running_loop()
        pending[1] = loop.create_future()
        print(f"[nav] {URL}")
        await page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        try:
            first_body = await asyncio.wait_for(pending[1], timeout=RESPONSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            await browser.close()
            raise RuntimeError(
                "Timed out waiting for the first /api/players/filter response. "
                "The page may have changed or be blocked."
            )

        page_count = int((first_body.get("pageData") or {}).get("pageCount", 1))
        row_count = (first_body.get("pageData") or {}).get("rowCount")
        print(f"[info] pageCount={page_count} rowCount={row_count}")

        # 2) Walk remaining pages by navigating with ?page=N
        last_page = page_count
        if MAX_PAGES > 0:
            last_page = min(last_page, MAX_PAGES)

        for n in range(2, last_page + 1):
            pending[n] = loop.create_future()
            url_n = f"{URL}?page={n}&sortDirection=DESC&sortType=rating"
            print(f"[nav] {url_n}")
            try:
                await page.goto(url_n, wait_until="domcontentloaded",
                                timeout=NAV_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                print(f"[nav] timeout on page {n}, continuing")
            try:
                await asyncio.wait_for(pending[n], timeout=RESPONSE_TIMEOUT_S)
            except asyncio.TimeoutError:
                print(f"[warn] no response captured for page {n}, skipping")
                pending.pop(n, None)
                continue

        await browser.close()

    # 3) Flatten the captured pages into a single ordered list of players
    flat: list[dict] = []
    for page_num in sorted(captured):
        body = captured[page_num]
        for rec in (body.get("players") or []):
            if not isinstance(rec, dict):
                continue
            flat.append({
                "name": extract_name(rec),
                "price": extract_price(rec),
                "raw": rec,
            })

    return {
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
        "season": "24",
        "totalPlayers": len(flat),
        "pagesScraped": len(captured),
        "players": flat,
    }


def main() -> int:
    try:
        result = asyncio.run(scrape())
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1

    OUTPUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nWrote {result['totalPlayers']} players "
          f"from {result['pagesScraped']} pages -> {OUTPUT_FILE.resolve()}")
    if result["players"]:
        sample = result["players"][0]
        print(f"Sample: name={sample['name']!r} price={sample['price']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

