"""
Scrape https://renderz.app/<season>/players using Playwright.
Defaults to season 25; override with SEASON env var.

Strategy (robust to API path changes):
  1. Launch headless Chromium with stealth tweaks.
  2. Capture EVERY JSON response from renderz.app whose body contains a
     `players` array (or a list of player-shaped objects). We don't lock
     ourselves to a specific URL because the site can change the path.
  3. Dismiss any cookie banner.
  4. Navigate to /<season>/players, wait for player cards to render.
  5. Walk pagination by URL params; if that captures nothing, fall back to
     clicking "Next" buttons.
  6. On failure, save debug_screenshot.png + debug_page.html so we can see
     what the page actually looked like.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import (
    async_playwright,
    Response,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

SEASON = os.environ.get("SEASON", "24")
URL = os.environ.get("RENDERZ_URL", f"https://renderz.app/{SEASON}/players")
OUTPUT_FILE = Path(os.environ.get("OUTPUT_FILE", "players.json"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "0"))
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "90000"))
RESPONSE_TIMEOUT_S = float(os.environ.get("RESPONSE_TIMEOUT_S", "120"))
HEADLESS = os.environ.get("HEADLESS", "1") not in ("0", "false", "False", "")
DEBUG = os.environ.get("DEBUG", "1") not in ("0", "false", "False", "")
DEBUG_DIR = Path(os.environ.get("DEBUG_DIR", "."))

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5].map(()=>({}))});
window.chrome = window.chrome || { runtime: {} };
"""

# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

NAME_KEYS = ("name", "commonName", "displayName", "fullName",
             "knownAs", "nickname", "shortName")
FIRST_KEYS = ("firstName", "first_name", "firstname")
LAST_KEYS = ("lastName", "last_name", "lastname", "surname")
PRICE_KEYS = ("price", "prices", "auctionPrice", "currentPrice", "marketPrice",
              "lowestBin", "lowestPrice", "buyNowPrice", "bin")


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
        for nest in ("prices", "market", "auction", "player", "card"):
            sub = record.get(nest)
            if isinstance(sub, dict):
                v = extract_price(sub)
                if v is not None:
                    return v
        return None
    if isinstance(val, dict):
        for k in ("current", "value", "amount", "lowest", "buyNow"):
            if k in val:
                val = val[k]
                break
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def looks_like_player(rec: Any) -> bool:
    """Heuristic: does this dict look like a single player record?"""
    if not isinstance(rec, dict):
        return False
    keys = set(rec)
    # Must have a name-ish key AND at least one stat-ish key
    has_name = bool(keys & set(NAME_KEYS) | keys & set(FIRST_KEYS) | keys & set(LAST_KEYS))
    has_stat = bool(keys & {
        "rating", "ovr", "overall", "ratingValue", "pace", "shooting",
        "passing", "dribbling", "defending", "physical", "id", "playerId",
    })
    return has_name and has_stat


def find_players_in_body(body: Any) -> list[dict] | None:
    """Look for a list of player-shaped dicts anywhere in the JSON body."""
    if isinstance(body, dict):
        # Common case: top-level "players" key
        for key in ("players", "results", "items", "data", "cards"):
            v = body.get(key)
            if isinstance(v, list) and v and looks_like_player(v[0]):
                return v
        # Nested search
        for v in body.values():
            found = find_players_in_body(v)
            if found:
                return found
    elif isinstance(body, list):
        if body and looks_like_player(body[0]):
            return body
    return None


# ---------------------------------------------------------------------------
# Cookie / overlay dismissal
# ---------------------------------------------------------------------------

CONSENT_SELECTORS = [
    'button:has-text("Accept all")',
    'button:has-text("Accept All")',
    'button:has-text("ACCEPT ALL")',
    'button:has-text("Accept")',
    'button:has-text("Agree")',
    'button:has-text("AGREE")',
    'button:has-text("I agree")',
    'button:has-text("Got it")',
    'button:has-text("OK")',
    'button:has-text("Continue")',
    '[id*="accept"]',
    '[id*="consent"] button',
    '[class*="accept" i]',
    '[aria-label*="accept" i]',
]


async def dismiss_overlays(page: Page) -> None:
    for sel in CONSENT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                await loc.click(timeout=2000)
                print(f"[overlay] dismissed via selector: {sel}")
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Main scrape
# ---------------------------------------------------------------------------

class Captured:
    def __init__(self):
        self.responses: list[dict] = []  # list of {"url":..., "body":..., "players":[...]}
        self.api_log: list[str] = []
        self.new_event = asyncio.Event()


async def scrape() -> dict:
    cap = Captured()

    async def handle_response(response: Response) -> None:
        try:
            url = response.url
            if "renderz.app" not in url:
                return
            cap.api_log.append(f"{response.request.method} {response.status} {url}")
            if response.status != 200:
                return
            ctype = (response.headers.get("content-type") or "").lower()
            if "json" not in ctype:
                return
            try:
                body = await response.json()
            except Exception:
                return
            players = find_players_in_body(body)
            if not players:
                return
            cap.responses.append({"url": url, "body": body, "players": players})
            print(f"[capture] {len(players)} players from {url}")
            cap.new_event.set()
        except Exception as e:
            print(f"[handler] error: {e}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="Europe/London",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()
        page.on("response", lambda r: asyncio.create_task(handle_response(r)))

        print(f"[nav] {URL}")
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            print("[nav] domcontentloaded timeout — continuing")

        # Try to dismiss overlays before waiting for content
        await dismiss_overlays(page)

        # Wait for player content to actually render OR for a JSON capture.
        # Whichever happens first wins.
        deadline = asyncio.get_running_loop().time() + RESPONSE_TIMEOUT_S
        player_selectors = [
            '[class*="player-card" i]',
            '[class*="playerCard" i]',
            '[data-testid*="player" i]',
            'a[href*="/player/"]',
            'a[href*="/players/"]',
        ]
        first_capture_ok = False
        while asyncio.get_running_loop().time() < deadline:
            if cap.responses:
                first_capture_ok = True
                break
            # Also check DOM for player elements
            for sel in player_selectors:
                try:
                    if await page.locator(sel).first.is_visible(timeout=300):
                        print(f"[dom] player content visible via {sel}")
                        break
                except Exception:
                    continue
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except PlaywrightTimeoutError:
                pass
            await asyncio.sleep(1.0)

        if not first_capture_ok:
            await save_debug(page, cap, reason="no_initial_capture")
            await browser.close()
            raise RuntimeError(
                "Page loaded but no player JSON was captured. "
                "Saved debug_screenshot.png and debug_page.html for inspection. "
                f"Captured {len(cap.api_log)} renderz.app API hits — see log above."
            )

        # Determine page count from first capture, if possible
        first = cap.responses[0]
        body = first["body"]
        page_count = 1
        if isinstance(body, dict):
            pd = body.get("pageData") or body.get("pagination") or {}
            if isinstance(pd, dict):
                page_count = int(pd.get("pageCount") or pd.get("totalPages") or 1)
            elif "totalPages" in body:
                page_count = int(body["totalPages"] or 1)
        print(f"[info] detected pageCount={page_count}, first batch={len(first['players'])}")

        last_page = page_count if MAX_PAGES <= 0 else min(page_count, MAX_PAGES)

        # Walk subsequent pages by URL param. If a navigation produces no new
        # capture, try clicking a "Next" button as fallback.
        for n in range(2, last_page + 1):
            before = len(cap.responses)
            url_n = f"{URL}?page={n}&sortDirection=DESC&sortType=rating"
            print(f"[nav] page {n}: {url_n}")
            try:
                await page.goto(url_n, wait_until="domcontentloaded",
                                timeout=NAV_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                print(f"[nav] timeout on page {n}")
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            # Wait for new capture
            for _ in range(20):  # up to 10s
                if len(cap.responses) > before:
                    break
                await asyncio.sleep(0.5)
            if len(cap.responses) == before:
                # Fallback: click a Next button
                for nsel in ['button:has-text("Next")', 'a:has-text("Next")',
                             '[aria-label*="next" i]', 'button[aria-label*="page" i]']:
                    try:
                        await page.locator(nsel).first.click(timeout=1500)
                        print(f"[click] next via {nsel}")
                        break
                    except Exception:
                        continue
                for _ in range(20):
                    if len(cap.responses) > before:
                        break
                    await asyncio.sleep(0.5)
            if len(cap.responses) == before:
                print(f"[warn] page {n} produced no new capture — stopping pagination")
                break

        await browser.close()

    # Flatten all captured player records (dedupe on id if present)
    seen_ids: set = set()
    flat: list[dict] = []
    for resp in cap.responses:
        for rec in resp["players"]:
            pid = rec.get("id") or rec.get("playerId") or rec.get("_id")
            if pid is not None:
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
            flat.append({
                "name": extract_name(rec),
                "price": extract_price(rec),
                "raw": rec,
            })

    return {
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
        "season": SEASON,
        "totalPlayers": len(flat),
        "responsesCaptured": len(cap.responses),
        "players": flat,
    }


async def save_debug(page: Page, cap: Captured, reason: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(DEBUG_DIR / "debug_screenshot.png"),
                              full_page=True)
        print(f"[debug] saved {DEBUG_DIR / 'debug_screenshot.png'}")
    except Exception as e:
        print(f"[debug] screenshot failed: {e}")
    try:
        html = await page.content()
        (DEBUG_DIR / "debug_page.html").write_text(html, encoding="utf-8")
        print(f"[debug] saved {DEBUG_DIR / 'debug_page.html'} ({len(html)} bytes)")
    except Exception as e:
        print(f"[debug] html dump failed: {e}")
    try:
        title = await page.title()
        url = page.url
    except Exception:
        title, url = "<n/a>", "<n/a>"
    info = {
        "reason": reason,
        "title": title,
        "url": url,
        "capturedResponses": len(cap.responses),
        "apiCallsSeen": cap.api_log[-50:],
    }
    (DEBUG_DIR / "debug_info.json").write_text(
        json.dumps(info, indent=2, ensure_ascii=False))
    print(f"[debug] saved {DEBUG_DIR / 'debug_info.json'}")
    print(f"[debug] page title: {title!r}")
    print(f"[debug] last 20 renderz.app API hits:")
    for line in cap.api_log[-20:]:
        print(f"  {line}")


def main() -> int:
    try:
        result = asyncio.run(scrape())
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1

    OUTPUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nWrote {result['totalPlayers']} players "
          f"from {result['responsesCaptured']} captured responses -> "
          f"{OUTPUT_FILE.resolve()}")
    if result["players"]:
        sample = result["players"][0]
        print(f"Sample: name={sample['name']!r} price={sample['price']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
