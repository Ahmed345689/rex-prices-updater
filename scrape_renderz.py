"""
Scrape https://renderz.app/<season>/players — v3 (infinite-scroll + multi-strategy)

STRATEGY:
  1. Navigate to page, capture first API response
  2. PRIMARY: scroll to bottom repeatedly to trigger infinite scroll
  3. FALLBACK A: click "Load More" / "Next" buttons
  4. FALLBACK B: direct API calls with page/offset params (various URL patterns)
  5. Stop only when 3 consecutive scroll attempts return no new players

v3 CHANGES vs v2:
  - Infinite scroll is now the PRIMARY strategy (not pagination URL)
  - Tries 3 different API URL patterns in fallback
  - Keeps scrolling until 3 consecutive no-new-data cycles
  - Deduplication runs incrementally so we always know exact new count
  - Better page-count extraction from DOM (looks for counter text like "1000 players")
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
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
)

# playwright-stealth (اختياري — لو مش مثبّت يشتغل بدونه)
try:
    from playwright_stealth import stealth_async as _stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    print("[stealth] playwright-stealth not installed — using manual patches only")

# ── Config ───────────────────────────────────────────────────────────────────

SEASON             = os.environ.get("SEASON", "25")
URL                = os.environ.get("RENDERZ_URL", f"https://renderz.app/{SEASON}/players")
OUTPUT_FILE        = Path(os.environ.get("OUTPUT_FILE", "players.json"))
MAX_PLAYERS             = int(os.environ.get("MAX_PLAYERS", "0"))
NAV_TIMEOUT_MS          = int(os.environ.get("NAV_TIMEOUT_MS", "120000"))
IDLE_TIMEOUT_S          = float(os.environ.get("IDLE_TIMEOUT_S", "20"))
MAX_IDLE_CYCLES         = int(os.environ.get("MAX_IDLE_CYCLES", "3"))
WAIT_FIRST_RESPONSE_S   = float(os.environ.get("WAIT_FIRST_RESPONSE_S", "90"))
HEADLESS                = os.environ.get("HEADLESS", "1") not in ("0","false","False","")
DEBUG_DIR               = Path(os.environ.get("DEBUG_DIR", "."))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ── Comprehensive stealth patches ─────────────────────────────────────────────
STEALTH_JS = r"""
// 1. Hide webdriver
Object.defineProperty(navigator,'webdriver',{get:()=>undefined,configurable:true});

// 2. Fake languages & platform
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
Object.defineProperty(navigator,'platform',{get:()=>'Win32'});
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
Object.defineProperty(navigator,'deviceMemory',{get:()=>8});

// 3. Fake plugins (non-empty)
const pluginData=[
  {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'Portable Document Format'},
  {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:''},
  {name:'Native Client',filename:'internal-nacl-plugin',description:''},
];
Object.defineProperty(navigator,'plugins',{get:()=>{
  const arr=pluginData.map(p=>{const o=Object.create(Plugin.prototype||{});Object.assign(o,p);return o;});
  Object.defineProperty(arr,'length',{value:arr.length});
  return arr;
}});

// 4. Chrome runtime
window.chrome=window.chrome||{};
window.chrome.runtime=window.chrome.runtime||{};

// 5. Permissions API spoof
if(navigator.permissions){
  const origQuery=navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query=params=>
    params.name==='notifications'
      ? Promise.resolve({state:Notification.permission})
      : origQuery(params);
}

// 6. WebGL vendor spoof
try{
  const getParam=WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter=function(p){
    if(p===37445)return 'Intel Inc.';
    if(p===37446)return 'Intel Iris OpenGL Engine';
    return getParam.call(this,p);
  };
  const getParam2=WebGL2RenderingContext.prototype.getParameter;
  WebGL2RenderingContext.prototype.getParameter=function(p){
    if(p===37445)return 'Intel Inc.';
    if(p===37446)return 'Intel Iris OpenGL Engine';
    return getParam2.call(this,p);
  };
}catch(e){}

// 7. Remove automation-related properties
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
"""

# ── Field name helpers ───────────────────────────────────────────────────────

NAME_KEYS  = ("name","commonName","displayName","fullName","knownAs","nickname","shortName")
FIRST_KEYS = ("firstName","first_name","firstname")
LAST_KEYS  = ("lastName","last_name","lastname","surname")
PRICE_KEYS = ("price","prices","auctionPrice","currentPrice","marketPrice",
              "lowestBin","lowestPrice","buyNowPrice","bin")


def _first(rec: dict, keys) -> Any:
    for k in keys:
        if k in rec and rec[k] not in (None, "", 0):
            return rec[k]
    for k in keys:
        if k in rec:
            return rec[k]
    return None


def extract_name(rec: dict) -> str | None:
    v = _first(rec, NAME_KEYS)
    if v: return str(v)
    first = _first(rec, FIRST_KEYS)
    last  = _first(rec, LAST_KEYS)
    if first or last:
        return " ".join(p for p in (first, last) if p)
    for nk in ("player","card","details","data"):
        sub = rec.get(nk)
        if isinstance(sub, dict):
            n = extract_name(sub)
            if n: return n
    return None


def extract_price(rec: dict) -> int | None:
    val = _first(rec, PRICE_KEYS)
    if val is None:
        for nk in ("prices","market","auction","player","card"):
            sub = rec.get(nk)
            if isinstance(sub, dict):
                v = extract_price(sub)
                if v is not None: return v
        return None
    if isinstance(val, dict):
        for k in ("current","value","amount","lowest","buyNow"):
            if k in val: val = val[k]; break
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def looks_like_player(rec: Any) -> bool:
    if not isinstance(rec, dict): return False
    keys = set(rec)
    has_name = bool(keys & set(NAME_KEYS) | keys & set(FIRST_KEYS) | keys & set(LAST_KEYS))
    has_stat = bool(keys & {"rating","ovr","overall","ratingValue","pace","shooting",
                            "passing","dribbling","defending","physical","id","playerId",
                            "assetId","baseId"})
    return has_name and has_stat


def find_players_in_body(body: Any) -> list[dict] | None:
    if isinstance(body, dict):
        for key in ("players","results","items","data","cards","pageItems",
                    "pageData","content","list","records","hits"):
            v = body.get(key)
            if isinstance(v, list) and v and looks_like_player(v[0]):
                return v
        for v in body.values():
            found = find_players_in_body(v)
            if found: return found
    elif isinstance(body, list):
        if body and looks_like_player(body[0]):
            return body
    return None


# ── Normalisation helpers ────────────────────────────────────────────────────

def extract_asset_id(rec: dict) -> str | None:
    for k in ("assetId","asset_id","id","playerId","player_id","_id",
              "uid","cardId","baseId","resourceId"):
        v = rec.get(k)
        if v is not None and str(v).strip():
            return str(v)
    return None


_PAC=("pace","pac","speed"); _SHO=("shooting","sho","shot")
_PAS=("passing","pas","pass"); _DRI=("dribbling","dri","dribble")
_DEF=("defending","def","defense","defence"); _PHY=("physical","phy","physicality","strength")
_DIV=("diving","div","GKDiving"); _HAN=("handling","han","GKHandling")
_KIC=("kicking","kic","GKKicking"); _REF=("reflexes","ref","GKReflexes")
_SPD=("speed","spd","GKSpeed"); _POS_GK=("positioning","pos","GKPositioning")
NEST=["stats","attributes","baseStats","cardAttributes","playerAttributes","attr","base"]

def _pick(rec, *kgs):
    for keys in kgs:
        for k in (keys if isinstance(keys,(list,tuple)) else [keys]):
            v = rec.get(k)
            if isinstance(v,(int,float)) and v >= 0: return int(v)
    return 0

def _pick_n(rec, nest, *fg):
    for nk in nest:
        sub = rec.get(nk)
        if isinstance(sub, dict):
            r = _pick(sub, *fg)
            if r: return r
    return _pick(rec, *fg)

def extract_avg_stats(rec):
    for nk in NEST:
        sub = rec.get(nk)
        if isinstance(sub, list) and sub:
            sm: dict = {}
            for item in sub:
                if isinstance(item, dict):
                    n = str(item.get("name") or item.get("shortName") or "").upper()
                    v = item.get("value") or item.get("val") or 0
                    if n and isinstance(v,(int,float)): sm[n]=int(v)
            if sm:
                def g(ks): return next((sm[k.upper()] for k in ks if k.upper() in sm),0)
                return {"avg1":g(_PAC),"avg2":g(_SHO),"avg3":g(_PAS),
                        "avg4":g(_DRI),"avg5":g(_DEF),"avg6":g(_PHY)}
    return {"avg1":_pick_n(rec,NEST,_PAC),"avg2":_pick_n(rec,NEST,_SHO),
            "avg3":_pick_n(rec,NEST,_PAS),"avg4":_pick_n(rec,NEST,_DRI),
            "avg5":_pick_n(rec,NEST,_DEF),"avg6":_pick_n(rec,NEST,_PHY)}

def extract_gk_stats(rec):
    gk=NEST+["gkStats"]
    return {"avg1":_pick_n(rec,gk,_DIV),"avg2":_pick_n(rec,gk,_HAN),
            "avg3":_pick_n(rec,gk,_KIC),"avg4":_pick_n(rec,gk,_REF),
            "avg5":_pick_n(rec,gk,_SPD),"avg6":_pick_n(rec,gk,_POS_GK)}

_FACE=["playerFaceImage","faceImage","playerImage","headshot","avatar","photo","portraitImage",
       "face","headImage","imageUrl","image_url","imgUrl","img"]
_CARD=["playerCardImage","cardImage","fullBodyImage","playerBodyImage","cardUrl","card_url","cardImg"]

def _pick_url(rec, keys):
    for k in keys:
        v = rec.get(k)
        if isinstance(v,str) and v.startswith("http"): return v
    for nk in ("images","media","assets","photos","thumbnails"):
        sub = rec.get(nk)
        if isinstance(sub,dict):
            for k in keys:
                v = sub.get(k)
                if isinstance(v,str) and v.startswith("http"): return v
    return ""

def extract_images(rec):
    return {"playerFaceImage":_pick_url(rec,_FACE),"playerCardImage":_pick_url(rec,_CARD)}

_POS_MAP={"goalkeeper":"GK","centreback":"CB","centerback":"CB","rightback":"RB","leftback":"LB",
          "centermidfield":"CM","centralmidfield":"CM","defensivemidfield":"CDM",
          "attackingmidfield":"CAM","rightwing":"RW","leftwing":"LW","rightmidfield":"RM",
          "leftmidfield":"LM","rightwingback":"RWB","leftwingback":"LWB",
          "centreforward":"ST","centerforward":"ST","striker":"ST"}

def norm_pos(raw):
    if not raw: return "—"
    s=str(raw).strip()
    if len(s)<=4 and s.isalpha(): return s.upper()
    return _POS_MAP.get(s.lower().replace(" ","").replace("-","").replace("_",""), s)

def extract_obj(rec, key):
    obj=rec.get(key)
    if isinstance(obj,dict):
        for k in ("name","commonName","shortName","displayName","label"):
            v=obj.get(k)
            if isinstance(v,str) and v.strip(): return {"id":obj.get("id"),"name":v.strip()}
    if isinstance(obj,str) and obj.strip(): return {"id":None,"name":obj.strip()}
    return {"id":None,"name":None}

# ── Consent dismissal ─────────────────────────────────────────────────────────

CONSENT=['button:has-text("Accept all")','button:has-text("Accept")','button:has-text("Agree")',
         'button:has-text("I agree")','button:has-text("Got it")','button:has-text("OK")',
         '[id*="accept"]','[id*="consent"] button','[class*="accept" i]']

async def dismiss_overlays(page: Page):
    for sel in CONSENT:
        try:
            loc=page.locator(sel).first
            if await loc.is_visible(timeout=1200):
                await loc.click(timeout=1500)
                print(f"[overlay] {sel}")
                await page.wait_for_timeout(400)
                return
        except Exception: continue

# ── Capture state ─────────────────────────────────────────────────────────────

class Captured:
    def __init__(self):
        self.all_players: list[dict] = []     # raw player records (deduplicated)
        self.seen_ids: set = set()
        self.api_log: list[str] = []
        self.intercepted_api_base: str | None = None   # the actual API URL found in traffic

    def add_players(self, players: list[dict]) -> int:
        """Add new players, return count of newly added."""
        added = 0
        for rec in players:
            pid = extract_asset_id(rec)
            key = pid if pid else f"{extract_name(rec)}_{rec.get('rating') or rec.get('ovr') or ''}"
            if key in self.seen_ids: continue
            self.seen_ids.add(key)
            self.all_players.append(rec)
            added += 1
        return added

# ── Total count from DOM ──────────────────────────────────────────────────────

async def get_total_from_dom(page: Page) -> int | None:
    """Try to read total player count from page DOM."""
    patterns = [
        r'(\d[\d,]+)\s*(?:players?|cards?|results?)',
        r'(?:total|showing|of)\s*:?\s*(\d[\d,]+)',
        r'(\d[\d,]+)\s*(?:items?|records?)',
    ]
    try:
        text = await page.inner_text("body", timeout=3000)
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                total = int(m.group(1).replace(",",""))
                if total > 50:
                    print(f"[dom] total players from page text: {total}")
                    return total
    except Exception: pass
    return None

# ── Strategy 1: Infinite scroll ───────────────────────────────────────────────

async def strategy_infinite_scroll(page: Page, cap: Captured, response_queue: asyncio.Queue) -> int:
    """
    Scroll to bottom, wait for new API responses, repeat.
    Returns total new players added.
    """
    print("[strategy] infinite scroll")
    idle_cycles = 0
    total_added = 0
    last_height = 0

    while idle_cycles < MAX_IDLE_CYCLES:
        if MAX_PLAYERS > 0 and len(cap.all_players) >= MAX_PLAYERS:
            print(f"[scroll] reached MAX_PLAYERS={MAX_PLAYERS}")
            break

        before = len(cap.all_players)

        # Scroll to bottom
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            # Try clicking load-more button
            clicked = False
            for sel in ['button:has-text("Load more")','button:has-text("Load More")',
                        'button:has-text("Show more")','button:has-text("More")',
                        '[data-testid*="load-more"]','[class*="load-more" i]',
                        '[class*="loadMore" i]']:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=800):
                        await loc.click(timeout=1000)
                        print(f"[btn] clicked: {sel}")
                        clicked = True
                        break
                except Exception: continue

            if not clicked:
                idle_cycles += 1
                print(f"[scroll] same height, idle_cycle={idle_cycles}/{MAX_IDLE_CYCLES}")
                await asyncio.sleep(2)
                continue

        last_height = new_height
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(300)

        # Also try scrolling the main content div
        for scroll_sel in ['[class*="player" i]','[class*="results" i]',
                           'main','[role="main"]','[class*="content" i]']:
            try:
                await page.locator(scroll_sel).first.evaluate(
                    "el => el.scrollTo(0, el.scrollHeight)", timeout=500
                )
            except Exception: continue

        # Wait for new data
        deadline = asyncio.get_running_loop().time() + IDLE_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            try:
                players = response_queue.get_nowait()
                added = cap.add_players(players)
                if added:
                    total_added += added
                    print(f"[scroll] +{added} new  (total={len(cap.all_players)})")
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.5)

        after = len(cap.all_players)
        if after == before:
            idle_cycles += 1
            print(f"[scroll] no new players, idle_cycle={idle_cycles}/{MAX_IDLE_CYCLES}")
        else:
            idle_cycles = 0  # reset on success

    return total_added


# ── Strategy 2: Direct API pagination ─────────────────────────────────────────

async def strategy_api_pagination(page: Page, cap: Captured, api_base: str) -> int:
    """
    Call the API directly with page/offset/cursor params.
    Returns total new players added.
    """
    print(f"[strategy] direct API pagination from {api_base}")

    # Build candidate URL templates
    url_templates = [
        f"{api_base}?page={{n}}&sortDirection=DESC&sortType=rating",
        f"{api_base}?page={{n}}&sort=rating&order=desc",
        f"{api_base}?page={{n}}",
        f"{api_base}?offset={{offset}}&limit=23&sortDirection=DESC&sortType=rating",
        f"{api_base}?offset={{offset}}&limit=23",
        f"{api_base}/{SEASON}/players?page={{n}}",
    ]

    total_added = 0
    consecutive_empty = 0

    for template in url_templates:
        print(f"[api] trying template: {template.format(n=2, offset=23)}")
        for n in range(2, 500):
            if MAX_PLAYERS > 0 and len(cap.all_players) >= MAX_PLAYERS:
                break
            offset = (n - 1) * 23
            url = template.format(n=n, offset=offset)
            try:
                resp = await page.request.get(url, timeout=15000)
                if resp.status != 200:
                    print(f"[api] {resp.status} on page {n}, stopping template")
                    break
                body = await resp.json()
                players = find_players_in_body(body)
                if not players:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        print(f"[api] 2 empty pages in a row — next template")
                        break
                    continue
                consecutive_empty = 0
                added = cap.add_players(players)
                total_added += added
                print(f"[api] page={n} +{added} new  (total={len(cap.all_players)})")
                if added == 0:
                    print(f"[api] page {n} returned all-duplicates — stopping template")
                    break
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"[api] error page {n}: {e}")
                break
        if total_added > 0:
            break  # first working template is enough

    return total_added

# ── Main scrape ───────────────────────────────────────────────────────────────

async def scrape() -> dict:
    cap = Captured()
    response_queue: asyncio.Queue = asyncio.Queue()

    async def on_response(response: Response):
        try:
            url = response.url
            if "renderz.app" not in url: return
            cap.api_log.append(f"{response.request.method} {response.status} {url}")
            if response.status != 200: return
            ctype = (response.headers.get("content-type") or "").lower()
            if "json" not in ctype: return
            body = await response.json()
            players = find_players_in_body(body)
            if not players: return

            # Save the API base URL for pagination fallback
            if cap.intercepted_api_base is None:
                # Strip query string and trailing path segment to get base
                clean = url.split("?")[0]
                cap.intercepted_api_base = clean
                print(f"[intercept] API base: {clean}")

            added = cap.add_players(players)
            print(f"[capture] {len(players)} in response (+{added} new)  url={url[:80]}")
            await response_queue.put(players)
        except Exception as e:
            print(f"[handler] {e}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-features=IsolateOrigins,site-per-process",
                "--flag-switches-begin",
                "--disable-site-isolation-trials",
                "--flag-switches-end",
                "--window-size=1366,900",
                "--start-maximized",
                "--disable-infobars",
                "--disable-notifications",
                "--ignore-certificate-errors",
                "--allow-running-insecure-content",
            ],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="Europe/London",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        # Apply comprehensive stealth patches
        await ctx.add_init_script(STEALTH_JS)

        # Apply playwright-stealth if available (handles deeper fingerprint spoofing)
        page = await ctx.new_page()
        if HAS_STEALTH:
            await _stealth_async(page)
            print("[stealth] playwright-stealth applied")

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        # ── Navigate ──────────────────────────────────────────────────────────
        sort_url = f"{URL}?sortDirection=DESC&sortType=rating"
        print(f"[nav] {sort_url}")
        try:
            await page.goto(sort_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            print("[nav] timeout — continuing with whatever loaded")

        # Small human-like pause after navigation
        await asyncio.sleep(2)
        await dismiss_overlays(page)

        # Simulate human mouse movement to trigger lazy load
        try:
            await page.mouse.move(683, 400)
            await asyncio.sleep(0.5)
            await page.mouse.move(400, 300)
            await asyncio.sleep(0.3)
        except Exception:
            pass

        # Wait for first API response — up to WAIT_FIRST_RESPONSE_S seconds
        print(f"[wait] waiting up to {int(WAIT_FIRST_RESPONSE_S)}s for first player response…")
        deadline = asyncio.get_running_loop().time() + WAIT_FIRST_RESPONSE_S
        while asyncio.get_running_loop().time() < deadline:
            if len(cap.all_players) > 0:
                break
            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
            except PlaywrightTimeoutError:
                pass
            # Gentle scroll to trigger lazy content
            try:
                await page.evaluate("window.scrollBy(0, 300)")
                await asyncio.sleep(0.5)
                await page.evaluate("window.scrollBy(0, -300)")
            except Exception:
                pass
            await asyncio.sleep(1)

        if len(cap.all_players) == 0:
            await _save_debug(page, cap, "no_initial_players")
            await browser.close()
            raise RuntimeError(
                f"No players captured after navigation. API calls seen:\n" +
                "\n".join(f"  {l}" for l in cap.api_log[-20:])
            )

        total_dom = await get_total_from_dom(page)
        print(f"[info] first batch={len(cap.all_players)}  dom_total={total_dom}")

        # ── Strategy 1: infinite scroll ───────────────────────────────────────
        s1_added = await strategy_infinite_scroll(page, cap, response_queue)
        print(f"[scroll] done — added {s1_added}  total={len(cap.all_players)}")

        # ── Strategy 2: direct API if scroll didn't get much ──────────────────
        should_try_api = (
            cap.intercepted_api_base is not None and
            (total_dom is None or len(cap.all_players) < total_dom * 0.8)
        )
        if should_try_api:
            s2_added = await strategy_api_pagination(page, cap, cap.intercepted_api_base)
            print(f"[api] done — added {s2_added}  total={len(cap.all_players)}")

        await browser.close()

    # ── Flatten + normalise ───────────────────────────────────────────────────
    flat: list[dict] = []
    for rec in cap.all_players:
        pid      = extract_asset_id(rec)
        raw_pos  = rec.get("position") or rec.get("pos") or ""
        is_gk    = norm_pos(raw_pos) == "GK"
        images   = extract_images(rec)
        avg      = extract_avg_stats(rec)
        club     = extract_obj(rec, "club")
        nation   = extract_obj(rec, "nation")
        league   = extract_obj(rec, "league")

        normalised = {
            **rec,
            "assetId":  pid or f"{extract_name(rec)}_{rec.get('rating',0)}",
            "rating":   int(rec.get("rating") or rec.get("ovr") or rec.get("overall") or 0),
            "position": norm_pos(raw_pos),
            "avgStats": avg,
            "images":   images,
            "club":     club,
            "nation":   nation,
            "league":   league,
        }
        if is_gk:
            normalised["avgGkStats"] = extract_gk_stats(rec)

        flat.append({
            "name":     extract_name(rec),
            "price":    extract_price(rec),
            "position": normalised["position"],
            "rating":   normalised["rating"],
            "club":     club["name"],
            "nation":   nation["name"],
            "league":   league["name"],
            "raw":      normalised,
        })

    return {
        "scrapedAt":         datetime.now(timezone.utc).isoformat(),
        "season":            SEASON,
        "totalPlayers":      len(flat),
        "responsesCaptured": len(set(cap.api_log)),
        "players":           flat,
    }


async def _save_debug(page: Page, cap: Captured, reason: str):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try: await page.screenshot(path=str(DEBUG_DIR/"debug_screenshot.png"), full_page=True)
    except Exception: pass
    try: (DEBUG_DIR/"debug_page.html").write_text(await page.content(), encoding="utf-8")
    except Exception: pass
    info = {"reason":reason,"url":page.url,"apiLog":cap.api_log[-50:],"players":len(cap.all_players)}
    (DEBUG_DIR/"debug_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))
    print(f"[debug] {reason}  url={page.url}  players={len(cap.all_players)}")


def main() -> int:
    try:
        result = asyncio.run(scrape())
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1

    OUTPUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    total = result["totalPlayers"]
    print(f"\n✓ {total} players → {OUTPUT_FILE.resolve()}")
    if result["players"]:
        s = result["players"][0]
        r = s.get("raw", {})
        print(f"  name={s['name']!r}  rating={s['rating']}  pos={s['position']}")
        print(f"  avgStats={r.get('avgStats')}")
        print(f"  faceImg={'YES' if r.get('images',{}).get('playerFaceImage') else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
