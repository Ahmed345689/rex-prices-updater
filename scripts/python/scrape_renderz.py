"""
Scrape https://renderz.app/<season>/players using Playwright.
Defaults to season 25; override with SEASON env var.

v2 FIXES:
- avgStats normalization (avg1=PAC … avg6=PHY) from any API field shape
- GK avgGkStats (div/han/kic/ref/spd/pos) extracted separately
- images.playerFaceImage + images.playerCardImage from 10+ possible key names
- assetId extracted from 8+ possible id field names
- position normalized to short codes (GK, ST, CAM …)
- club/nation/league always stored as {id, name} objects matching app's RawPlayer
- Dedup fallback: name+rating when no unique id exists
"""
from __future__ import annotations

import asyncio
import json
import os
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

# ── Config ──────────────────────────────────────────────────────────────────

SEASON            = os.environ.get("SEASON", "25")
URL               = os.environ.get("RENDERZ_URL", f"https://renderz.app/{SEASON}/players")
OUTPUT_FILE       = Path(os.environ.get("OUTPUT_FILE", "players.json"))
MAX_PAGES         = int(os.environ.get("MAX_PAGES", "0"))
NAV_TIMEOUT_MS    = int(os.environ.get("NAV_TIMEOUT_MS", "90000"))
RESPONSE_TIMEOUT_S = float(os.environ.get("RESPONSE_TIMEOUT_S", "120"))
HEADLESS          = os.environ.get("HEADLESS", "1") not in ("0", "false", "False", "")
DEBUG_DIR         = Path(os.environ.get("DEBUG_DIR", "."))

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
STEALTH_JS = r"""
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5].map(()=>({}))});
window.chrome=window.chrome||{runtime:{}};
"""

# ── Name / price extraction ──────────────────────────────────────────────────

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
    if v:
        return str(v)
    first = _first(rec, FIRST_KEYS)
    last  = _first(rec, LAST_KEYS)
    if first or last:
        return " ".join(p for p in (first, last) if p)
    for nk in ("player","card","details","data"):
        sub = rec.get(nk)
        if isinstance(sub, dict):
            n = extract_name(sub)
            if n:
                return n
    return None


def extract_price(rec: dict) -> int | None:
    val = _first(rec, PRICE_KEYS)
    if val is None:
        for nk in ("prices","market","auction","player","card"):
            sub = rec.get(nk)
            if isinstance(sub, dict):
                v = extract_price(sub)
                if v is not None:
                    return v
        return None
    if isinstance(val, dict):
        for k in ("current","value","amount","lowest","buyNow"):
            if k in val:
                val = val[k]; break
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def looks_like_player(rec: Any) -> bool:
    if not isinstance(rec, dict):
        return False
    keys = set(rec)
    has_name = bool(keys & set(NAME_KEYS) | keys & set(FIRST_KEYS) | keys & set(LAST_KEYS))
    has_stat = bool(keys & {"rating","ovr","overall","ratingValue","pace","shooting",
                            "passing","dribbling","defending","physical","id","playerId",
                            "assetId","baseId"})
    return has_name and has_stat


def find_players_in_body(body: Any) -> list[dict] | None:
    if isinstance(body, dict):
        for key in ("players","results","items","data","cards","pageItems",
                    "pageData","content","list","records"):
            v = body.get(key)
            if isinstance(v, list) and v and looks_like_player(v[0]):
                return v
        for v in body.values():
            found = find_players_in_body(v)
            if found:
                return found
    elif isinstance(body, list):
        if body and looks_like_player(body[0]):
            return body
    return None

# ── AssetId extraction ───────────────────────────────────────────────────────

def extract_asset_id(rec: dict) -> str | None:
    for k in ("assetId","asset_id","id","playerId","player_id","_id",
              "uid","cardId","baseId","resourceId"):
        v = rec.get(k)
        if v is not None and str(v).strip():
            return str(v)
    return None

# ── Stats normalization ──────────────────────────────────────────────────────

def _pick(rec: dict, *key_groups) -> int:
    for keys in key_groups:
        for k in (keys if isinstance(keys,(list,tuple)) else [keys]):
            v = rec.get(k)
            if isinstance(v,(int,float)) and v >= 0:
                return int(v)
    return 0

def _pick_nested(rec: dict, nest_keys: list, *fg) -> int:
    for nk in nest_keys:
        sub = rec.get(nk)
        if isinstance(sub, dict):
            r = _pick(sub, *fg)
            if r: return r
    return _pick(rec, *fg)

_PAC = ("pace","pac","speed","Pace","PAC")
_SHO = ("shooting","sho","shot","Shooting","SHO")
_PAS = ("passing","pas","pass","Passing","PAS")
_DRI = ("dribbling","dri","dribble","Dribbling","DRI")
_DEF = ("defending","def","defense","defence","Defending","DEF")
_PHY = ("physical","phy","physicality","strength","Physical","PHY")
_DIV = ("diving","div","GKDiving","gkDiving")
_HAN = ("handling","han","GKHandling","gkHandling")
_KIC = ("kicking","kic","GKKicking","gkKicking")
_REF = ("reflexes","ref","GKReflexes","gkReflexes")
_SPD = ("speed","spd","GKSpeed","gkSpeed")
_POS = ("positioning","pos","GKPositioning","gkPositioning")

NEST = ["stats","attributes","baseStats","cardAttributes","playerAttributes","attr","base"]


def extract_avg_stats(rec: dict) -> dict:
    # Try list-style [{name,value}] attributes
    for nk in NEST:
        sub = rec.get(nk)
        if isinstance(sub, list) and sub:
            sm: dict[str,int] = {}
            for item in sub:
                if isinstance(item, dict):
                    n = str(item.get("name") or item.get("shortName") or "").upper()
                    v = item.get("value") or item.get("val") or 0
                    if n and isinstance(v,(int,float)):
                        sm[n] = int(v)
            if sm:
                def g(ks): return next((sm[k.upper()] for k in ks if k.upper() in sm), 0)
                return {"avg1":g(_PAC),"avg2":g(_SHO),"avg3":g(_PAS),
                        "avg4":g(_DRI),"avg5":g(_DEF),"avg6":g(_PHY)}
    # Dict style
    return {
        "avg1": _pick_nested(rec, NEST, _PAC),
        "avg2": _pick_nested(rec, NEST, _SHO),
        "avg3": _pick_nested(rec, NEST, _PAS),
        "avg4": _pick_nested(rec, NEST, _DRI),
        "avg5": _pick_nested(rec, NEST, _DEF),
        "avg6": _pick_nested(rec, NEST, _PHY),
    }


def extract_gk_stats(rec: dict) -> dict:
    return {
        "avg1": _pick_nested(rec, NEST+["gkStats"], _DIV),
        "avg2": _pick_nested(rec, NEST+["gkStats"], _HAN),
        "avg3": _pick_nested(rec, NEST+["gkStats"], _KIC),
        "avg4": _pick_nested(rec, NEST+["gkStats"], _REF),
        "avg5": _pick_nested(rec, NEST+["gkStats"], _SPD),
        "avg6": _pick_nested(rec, NEST+["gkStats"], _POS),
    }

# ── Image extraction ─────────────────────────────────────────────────────────

_FACE_KEYS = ["playerFaceImage","faceImage","playerImage","headshot","avatar",
              "photo","portraitImage","face","headImage","imageUrl","image_url","imgUrl","img"]
_CARD_KEYS = ["playerCardImage","cardImage","fullBodyImage","playerBodyImage",
              "cardUrl","card_url","cardImg"]


def _pick_url(rec: dict, keys: list[str]) -> str:
    for k in keys:
        v = rec.get(k)
        if isinstance(v,str) and v.startswith("http"):
            return v
    for nk in ("images","media","assets","photos","thumbnails"):
        sub = rec.get(nk)
        if isinstance(sub, dict):
            for k in keys:
                v = sub.get(k)
                if isinstance(v,str) and v.startswith("http"):
                    return v
    return ""


def extract_images(rec: dict) -> dict:
    return {
        "playerFaceImage": _pick_url(rec, _FACE_KEYS),
        "playerCardImage": _pick_url(rec, _CARD_KEYS),
    }

# ── Position normalization ────────────────────────────────────────────────────

_POS_MAP = {
    "goalkeeper":"GK","centreback":"CB","centerback":"CB","centre-back":"CB",
    "rightback":"RB","right-back":"RB","leftback":"LB","left-back":"LB",
    "centermidfield":"CM","centralmidfield":"CM","defensivemidfield":"CDM",
    "attackingmidfield":"CAM","rightwing":"RW","leftwing":"LW",
    "rightmidfield":"RM","leftmidfield":"LM","rightwingback":"RWB","leftwingback":"LWB",
    "centreforward":"ST","centerforward":"ST","striker":"ST",
}

def normalize_pos(raw: Any) -> str:
    if not raw: return "—"
    s = str(raw).strip()
    if len(s) <= 4 and s.isalpha(): return s.upper()
    key = s.lower().replace(" ","").replace("-","").replace("_","")
    return _POS_MAP.get(key, s)

# ── Club/nation/league extraction ─────────────────────────────────────────────

def extract_obj(rec: dict, key: str) -> dict:
    obj = rec.get(key)
    if isinstance(obj, dict):
        for k in ("name","commonName","shortName","displayName","label"):
            v = obj.get(k)
            if isinstance(v,str) and v.strip():
                return {"id": obj.get("id"), "name": v.strip()}
    if isinstance(obj, str) and obj.strip():
        return {"id": None, "name": obj.strip()}
    return {"id": None, "name": None}

# ── Cookie dismissal ──────────────────────────────────────────────────────────

CONSENT = [
    'button:has-text("Accept all")','button:has-text("Accept")','button:has-text("Agree")',
    'button:has-text("I agree")','button:has-text("Got it")','button:has-text("OK")',
    '[id*="accept"]','[id*="consent"] button','[class*="accept" i]',
]

async def dismiss_overlays(page: Page) -> None:
    for sel in CONSENT:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1200):
                await loc.click(timeout=1500)
                print(f"[overlay] dismissed: {sel}")
                await page.wait_for_timeout(400)
                return
        except Exception:
            continue

# ── Scrape ────────────────────────────────────────────────────────────────────

class Captured:
    def __init__(self):
        self.responses: list[dict] = []
        self.api_log: list[str] = []


async def scrape() -> dict:
    cap = Captured()

    async def on_response(response: Response) -> None:
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
            cap.responses.append({"url": url, "body": body, "players": players})
            print(f"[capture] {len(players)} players from {url}")
        except Exception as e:
            print(f"[handler] {e}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled","--no-sandbox","--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT, locale="en-US", timezone_id="Europe/London",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()
        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        print(f"[nav] {URL}")
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            print("[nav] timeout — continuing")

        await dismiss_overlays(page)

        # Wait for first capture
        deadline = asyncio.get_running_loop().time() + RESPONSE_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            if cap.responses: break
            try: await page.wait_for_load_state("networkidle", timeout=3000)
            except PlaywrightTimeoutError: pass
            await asyncio.sleep(1.0)

        if not cap.responses:
            await _save_debug(page, cap, "no_initial_capture")
            await browser.close()
            raise RuntimeError("No player JSON captured. Check debug artifacts.")

        # Pagination
        body0 = cap.responses[0]["body"]
        page_count = 1
        if isinstance(body0, dict):
            pd = body0.get("pageData") or body0.get("pagination") or {}
            if isinstance(pd, dict):
                page_count = int(pd.get("pageCount") or pd.get("totalPages") or 1)
            elif "totalPages" in body0:
                page_count = int(body0["totalPages"] or 1)
        print(f"[info] pageCount={page_count}")

        last_page = page_count if MAX_PAGES <= 0 else min(page_count, MAX_PAGES)
        if page_count == 1 and MAX_PAGES <= 0: last_page = 200

        for n in range(2, last_page + 1):
            before = len(cap.responses)
            url_n = f"{URL}?page={n}&sortDirection=DESC&sortType=rating"
            print(f"[nav] page {n}")
            try:
                await page.goto(url_n, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass
            try: await page.wait_for_load_state("networkidle", timeout=12000)
            except PlaywrightTimeoutError: pass
            for _ in range(20):
                if len(cap.responses) > before: break
                await asyncio.sleep(0.5)
            if len(cap.responses) == before:
                print(f"[warn] page {n} no capture — stopping")
                break

        await browser.close()

    # ── Flatten + normalise ──────────────────────────────────────────────────
    seen: set = set()
    flat: list[dict] = []
    for resp in cap.responses:
        for rec in resp["players"]:
            pid = extract_asset_id(rec)
            key = pid if pid else f"{extract_name(rec)}_{rec.get('rating') or rec.get('ovr') or ''}"
            if key in seen: continue
            seen.add(key)

            raw_pos = rec.get("position") or rec.get("pos") or ""
            is_gk   = normalize_pos(raw_pos) == "GK"
            images  = extract_images(rec)
            avg     = extract_avg_stats(rec)
            club    = extract_obj(rec, "club")
            nation  = extract_obj(rec, "nation")
            league  = extract_obj(rec, "league")

            normalised = {
                **rec,
                "assetId":   pid or key,
                "rating":    int(rec.get("rating") or rec.get("ovr") or rec.get("overall") or 0),
                "position":  normalize_pos(raw_pos),
                "avgStats":  avg,
                "images":    images,
                "club":      club,
                "nation":    nation,
                "league":    league,
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
        "responsesCaptured": len(cap.responses),
        "players":           flat,
    }


async def _save_debug(page: Page, cap: Captured, reason: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(DEBUG_DIR / "debug_screenshot.png"), full_page=True)
    except Exception: pass
    try:
        (DEBUG_DIR / "debug_page.html").write_text(await page.content(), encoding="utf-8")
    except Exception: pass
    info = {"reason": reason, "url": page.url, "apiLog": cap.api_log[-50:]}
    (DEBUG_DIR / "debug_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))
    print(f"[debug] reason={reason}  url={page.url}")


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
        print(f"  avgStats={r.get('avgStats')}  assetId={r.get('assetId')}")
        print(f"  faceImg={'YES' if r.get('images',{}).get('playerFaceImage') else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
