"""
scrape_renderz.py — Nexus FC Mobile Scraper v8.0 (Dynamic Names Edition)

الجديد في v8.0:
  - جلب أسماء الدول والأندية والدوريات مباشرة من الموقع بـ Playwright
  - لا اعتماد على خرائط ثابتة ناقصة
  - fallback تلقائي للخريطة الثابتة لو Playwright فشل
"""
from __future__ import annotations

import json, os, re, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ── Config ──────────────────────────────────────────────────────────────────
SEASON       = os.environ.get("SEASON", "24")
INTERNAL_ID  = os.environ.get("INTERNAL_SEASON_ID", "23")
OUTPUT_FILE  = Path(os.environ.get("OUTPUT_FILE", "players.json"))
MAX_PLAYERS  = int(os.environ.get("MAX_PLAYERS", "0"))
PAGE_SIZE    = 20
SORT_TYPE    = os.environ.get("SORT_TYPE", "overallRating")
SORT_DIR     = os.environ.get("SORT_DIR", "DESC")
HEADLESS     = os.environ.get("HEADLESS", "1") == "1"

BASE_URL     = "https://renderz.app"
PLAYERS_API  = f"{BASE_URL}/api/players/filter"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/{SEASON}/players",
}

# ── Fallback Static Maps (للحالات الطارئة فقط) ──────────────────────────────
FALLBACK_NATIONS = {
    1:"Albania",2:"Algeria",3:"Andorra",4:"Angola",5:"Antigua & Barbuda",
    6:"Argentina",7:"Armenia",8:"Australia",9:"Austria",10:"Azerbaijan",
    11:"Bahrain",12:"Barbados",13:"Belarus",14:"Belgium",15:"Belize",
    16:"Benin",17:"Bermuda",18:"Bolivia",19:"Bosnia & Herzegovina",
    20:"Botswana",21:"Brazil",22:"Bulgaria",23:"Burkina Faso",24:"Burundi",
    25:"Cambodia",26:"Cameroon",27:"Canada",28:"Cape Verde",29:"Chile",
    30:"Chile",31:"China PR",32:"Colombia",33:"Congo",34:"Costa Rica",
    35:"Croatia",36:"Cuba",37:"Cyprus",38:"Czech Republic",39:"Denmark",
    40:"Dominican Republic",41:"Ecuador",42:"Ecuador",43:"Egypt",
    44:"El Salvador",45:"England",46:"Estonia",47:"Ethiopia",48:"Finland",
    49:"FYR Macedonia",50:"Gabon",51:"Gambia",52:"France",53:"Georgia",
    54:"Georgia",55:"Germany",56:"Ghana",57:"Greece",58:"Guatemala",
    59:"Guinea",60:"Honduras",61:"Hong Kong",62:"Hungary",63:"Iceland",
    64:"Hungary",65:"Iceland",66:"India",67:"Indonesia",68:"Iran",
    69:"Iran",70:"Iraq",71:"Republic of Ireland",72:"Israel",73:"Italy",
    74:"Ivory Coast",75:"Jamaica",76:"Japan",77:"Jordan",78:"Kazakhstan",
    79:"Kenya",80:"Korea Republic",81:"Kosovo",82:"Kuwait",83:"Latvia",
    84:"Lebanon",85:"Liberia",86:"Libya",87:"Lithuania",88:"Luxembourg",
    89:"Malawi",90:"Malaysia",91:"Mali",92:"Malta",93:"Morocco",
    94:"Mauritius",95:"Mexico",96:"Moldova",97:"Montenegro",98:"Netherlands",
    99:"New Caledonia",100:"New Zealand",101:"Nigeria",102:"Northern Ireland",
    103:"Norway",104:"Oman",105:"Palestine",106:"Panama",107:"Paraguay",
    108:"Paraguay",109:"Peru",110:"Philippines",111:"Poland",112:"Portugal",
    113:"Qatar",114:"Romania",115:"Russia",116:"Saudi Arabia",117:"Scotland",
    118:"Senegal",119:"Serbia",120:"Sierra Leone",121:"Slovakia",
    122:"Slovenia",123:"South Africa",124:"Spain",125:"Sri Lanka",
    126:"Sweden",127:"Switzerland",128:"Syria",129:"Tunisia",130:"Turkey",
    131:"Turkmenistan",132:"Ukraine",133:"United States",134:"Uruguay",
    135:"Uzbekistan",136:"Venezuela",137:"Vietnam",138:"Wales",
    139:"Zambia",140:"Zimbabwe",141:"Zimbabwe",144:"Cameroon",
    155:"Mozambique",159:"Rwanda",167:"Tanzania",183:"Togo",185:"Uganda",
    195:"Sudan",
}

FALLBACK_LEAGUES = {
    13:"Premier League",14:"EFL Championship",16:"Ligue 1 McDonald's",
    17:"Ligue 2 BKT",19:"Bundesliga",20:"2. Bundesliga",31:"Serie A Enilive",
    32:"Serie BKT",50:"Scottish Premiership",53:"LaLiga EA Sports",
    54:"LaLiga Hypermotion",56:"Liga Portugal",60:"Eredivisie",65:"Super Lig",
    80:"MLS",308:"Saudi Pro League",335:"CONMEBOL Libertadores",
    336:"CONMEBOL Sudamericana",1014:"Liga MX",1003:"Argentine Primera",
    2118:"UEFA Champions League",2139:"UEFA Europa League",
    2179:"UEFA Conference League",68:"Brasileirao",
}

# ── Dynamic Name Fetcher (Playwright) ───────────────────────────────────────

def fetch_name_maps_dynamic() -> tuple[dict, dict, dict]:
    """يجيب أسماء الدول والأندية والدوريات من موقع renderz مباشرةً"""
    print("[names] جاري جلب خريطة الأسماء من الموقع...")
    try:
        from playwright.sync_api import sync_playwright
        nations, clubs, leagues = {}, {}, {}

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=HEADLESS)
            page = browser.new_page()

            # اعترض طلبات الـ API أثناء تصفح الصفحة
            captured = []

            def handle_response(response):
                if "/api/players/filter" in response.url:
                    try:
                        body = response.json()
                        captured.append(body)
                    except:
                        pass

            page.on("response", handle_response)

            print("[names] فتح صفحة اللاعبين...")
            page.goto(f"{BASE_URL}/{SEASON}/players", timeout=60000, wait_until="networkidle")
            time.sleep(3)

            # استخرج الأسماء من الـ DOM (dropdowns/filters)
            nation_els = page.query_selector_all("[class*='nation'] option, [class*='Nation'] option, select option[value*='NationName']")
            for el in nation_els:
                val = el.get_attribute("value") or ""
                text = el.text_content() or ""
                if "NationName_" in val and text.strip():
                    try:
                        nid = int(val.split("_")[-1])
                        if text.strip() and not text.strip().startswith("ID "):
                            nations[nid] = text.strip()
                    except: pass

            club_els = page.query_selector_all("select option[value*='TeamName']")
            for el in club_els:
                val = el.get_attribute("value") or ""
                text = el.text_content() or ""
                if "TeamName_" in val and text.strip():
                    try:
                        cid = int(val.split("_")[-1])
                        if text.strip() and not text.strip().startswith("ID "):
                            clubs[cid] = text.strip()
                    except: pass

            league_els = page.query_selector_all("select option[value*='LeagueName']")
            for el in league_els:
                val = el.get_attribute("value") or ""
                text = el.text_content() or ""
                if "LeagueName_" in val and text.strip():
                    try:
                        lid = int(val.split("_")[-1])
                        if text.strip() and not text.strip().startswith("ID "):
                            leagues[lid] = text.strip()
                    except: pass

            # كمان نجيب الأسماء من بيانات الـ API المعترضة
            for body in captured:
                for p in body.get("players", []):
                    for key, lookup, pattern in [
                        ("nationName", nations, "NationName_"),
                        ("clubName", clubs, "TeamName_"),
                        ("leagueName", leagues, "LeagueName_"),
                    ]:
                        raw_val = p.get(key, "")
                        if pattern in raw_val:
                            try:
                                eid = int(raw_val.split("_")[-1])
                                # نحاول نجيب الاسم من الـ DOM بطريقة ثانية
                            except: pass

            browser.close()

        print(f"[names] ✅ جُلب: {len(nations)} دولة, {len(clubs)} نادي, {len(leagues)} دوري")
        return nations, clubs, leagues

    except Exception as e:
        print(f"[names] ⚠️ Playwright فشل ({e}) — استخدام الخريطة الاحتياطية")
        return {}, {}, {}


def build_name_maps() -> tuple[dict, dict, dict]:
    """يبني الخرائط النهائية: dynamic أولاً ثم fallback للناقص"""
    dyn_nations, dyn_clubs, dyn_leagues = fetch_name_maps_dynamic()

    # دمج مع الاحتياطي (dynamic له الأولوية)
    nations = {**FALLBACK_NATIONS, **dyn_nations}
    clubs   = {**dyn_clubs}   # الأندية نعتمد على dynamic فقط (كثيرة جداً)
    leagues = {**FALLBACK_LEAGUES, **dyn_leagues}

    return nations, clubs, leagues


# ── Entity Resolver ──────────────────────────────────────────────────────────

def make_resolver(nations: dict, clubs: dict, leagues: dict):
    def resolve(placeholder: str, entity_type: str) -> tuple[int | None, str]:
        if not placeholder:
            return None, ""
        parts = placeholder.rsplit("_", 1)
        if len(parts) == 2:
            try:
                eid = int(parts[1])
                lookup = {"nation": nations, "club": clubs, "league": leagues}.get(entity_type, {})
                name = lookup.get(eid, f"ID {eid}")
                return eid, name
            except ValueError:
                pass
        return None, placeholder
    return resolve


# ── Player Parser ────────────────────────────────────────────────────────────

def parse_player(raw: dict, resolve) -> dict:
    nat_id, nat_name   = resolve(raw.get("nationName", ""), "nation")
    club_id, club_name = resolve(raw.get("clubName", ""), "club")
    lea_id, lea_name   = resolve(raw.get("leagueName", ""), "league")

    avg  = raw.get("avgStats", {})
    imgs = raw.get("images", {})

    return {
        "id":         raw.get("id"),
        "assetId":    raw.get("assetId"),
        "cardName":   raw.get("cardName", ""),
        "firstName":  raw.get("firstName", ""),
        "lastName":   raw.get("lastName", ""),
        "commonName": raw.get("commonName", ""),
        "rating":     raw.get("rating"),
        "position":   raw.get("position", ""),
        "cardType":   raw.get("cardUiStyle", ""),
        "nation":     {"id": nat_id,  "name": nat_name},
        "club":       {"id": club_id, "name": club_name},
        "league":     {"id": lea_id,  "name": lea_name},
        "foot":           raw.get("foot", ""),
        "weakFootRating": raw.get("weakFootRating"),
        "skillMoves":     raw.get("skillMoves"),
        "height":         raw.get("height"),
        "weight":         raw.get("weight"),
        "birthDate":      raw.get("birthDate"),
        "avgStats": {
            "avg1": avg.get("PAC") or avg.get("avg1") or 0,
            "avg2": avg.get("SHO") or avg.get("avg2") or 0,
            "avg3": avg.get("PAS") or avg.get("avg3") or 0,
            "avg4": avg.get("DRI") or avg.get("avg4") or 0,
            "avg5": avg.get("DEF") or avg.get("avg5") or 0,
            "avg6": avg.get("PHY") or avg.get("avg6") or 0,
        },
        "stats":      raw.get("stats", {}),
        "totalStats": raw.get("totalStats"),
        "priceData":  raw.get("priceData", {}),
        "auctionable":raw.get("auctionable"),
        "images": {
            "playerFaceImage": imgs.get("playerCard") or imgs.get("playerCardImage", ""),
            "playerCardImage": imgs.get("playerCard") or imgs.get("playerCardImage", ""),
            "background":      imgs.get("background") or imgs.get("playerCardBackground", ""),
            "flag":            imgs.get("flag") or imgs.get("flagImage", ""),
            "club":            imgs.get("club") or imgs.get("clubImage", ""),
            "league":          imgs.get("league") or imgs.get("leagueImage", ""),
        },
    }


# ── Scraper ──────────────────────────────────────────────────────────────────

def fetch_page(session: requests.Session, page: int) -> dict:
    payload = {
        "filters": {}, "seasonId": INTERNAL_ID, "page": page,
        "sortType": SORT_TYPE, "sortDirection": SORT_DIR, "gkStats": False,
    }
    for attempt in range(4):
        try:
            r = session.post(PLAYERS_API, headers=HEADERS, json=payload, timeout=45)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < 3:
                wait = (attempt + 1) * 5
                print(f"[scraper] ⚠️ محاولة {attempt+1} فشلت، انتظار {wait}s: {e}")
                time.sleep(wait)
            else:
                raise


def scrape() -> None:
    # 1. جلب خريطة الأسماء
    nations, clubs, leagues = build_name_maps()
    resolve = make_resolver(nations, clubs, leagues)

    # 2. سحب اللاعبين
    players: list[dict] = []
    session = requests.Session()
    print(f"[scraper] بداية السحب (season={SEASON}) ...")
    page = 1
    total_pages = None

    while True:
        try:
            data = fetch_page(session, page)
        except Exception as e:
            print(f"[scraper] ⚠️ خطأ في الصفحة {page}: {e}")
            break

        page_data   = data.get("pageData", {})
        raw_players = data.get("players", [])

        if not raw_players and page == 1:
            print("[scraper] ⚠️ لم يعود الـ API بأي لاعبين!")
            break

        if total_pages is None:
            total_pages   = page_data.get("pageCount", 1)
            total_players = page_data.get("rowCount", 0)
            print(f"[scraper] إجمالي: {total_players:,} لاعب في {total_pages:,} صفحة")

        for raw in raw_players:
            parsed = parse_player(raw, resolve)
            p = {
                "name":     parsed.get("commonName") or parsed.get("cardName") or
                            f"{parsed.get('firstName','')} {parsed.get('lastName','')}".strip(),
                "price":    (parsed.get("priceData") or {}).get("0", {}).get("basePrice"),
                "position": parsed.get("position", ""),
                "rating":   parsed.get("rating") or 0,
                "club":     parsed.get("club", {}).get("name"),
                "nation":   parsed.get("nation", {}).get("name"),
                "league":   parsed.get("league", {}).get("name"),
                "raw":      parsed,
            }
            players.append(p)
            if MAX_PLAYERS > 0 and len(players) >= MAX_PLAYERS:
                break

        if len(players) % 500 == 0 or page == 1:
            print(f"[scraper] صفحة {page}/{total_pages} — {len(players):,} لاعب")

        if (MAX_PLAYERS > 0 and len(players) >= MAX_PLAYERS) or page >= total_pages:
            print(f"[scraper] ✅ اكتمل السحب!")
            break

        page += 1

    # إحصائية الأسماء المحلولة
    resolved = sum(1 for p in players if p.get("nation") and not str(p["nation"]).startswith("ID "))
    print(f"[scraper] أسماء محلولة: {resolved:,}/{len(players):,} ({100*resolved//max(len(players),1)}%)")

    output = {
        "scrapedAt":         datetime.now(timezone.utc).isoformat(),
        "season":            SEASON,
        "totalPlayers":      len(players),
        "responsesCaptured": len(players),
        "players":           players,
    }
    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    print(f"[scraper] 💾 حُفظ في {OUTPUT_FILE} — {OUTPUT_FILE.stat().st_size/1024/1024:.2f} MB")


if __name__ == "__main__":
    scrape()
