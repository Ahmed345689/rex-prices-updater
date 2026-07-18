"""
scrape_renderz.py — Nexus FC Mobile Scraper v9.0 (Playwright Edition)

الحل النهائي للـ 403:
  - Playwright + Chromium حقيقي يعدّي Cloudflare تلقائياً.
  - يزور صفحة اللاعبين ويلتقط الـ API responses مباشرة من الشبكة.
  - fallback لـ requests عادي لو Playwright مش متاح.
"""
from __future__ import annotations

import json, os, re, sys, time
from datetime import datetime, timezone
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
SEASON       = os.environ.get("SEASON", "24")
INTERNAL_ID  = os.environ.get("INTERNAL_SEASON_ID", "23")
OUTPUT_FILE  = Path(os.environ.get("OUTPUT_FILE", "players.json"))
MAX_PLAYERS  = int(os.environ.get("MAX_PLAYERS", "0"))
HEADLESS     = os.environ.get("HEADLESS", "1") == "1"
PAGE_SIZE    = 20
SORT_TYPE    = os.environ.get("SORT_TYPE", "overallRating")
SORT_DIR     = os.environ.get("SORT_DIR", "DESC")

BASE_URL     = "https://renderz.app"
PLAYERS_API  = f"{BASE_URL}/api/players/filter"

# ── Static Maps ────────────────────────────────────────────────────────────────
STATIC_NATIONS = {
    1: "Albania", 2: "Algeria", 3: "Andorra", 4: "Angola", 5: "Antigua & Barbuda", 6: "Argentina", 7: "Armenia", 8: "Australia",
    9: "Austria", 10: "Azerbaijan", 14: "Belgium", 18: "Bolivia", 19: "Bosnia & Herzegovina", 21: "Brazil", 22: "Bulgaria",
    26: "Cameroon", 27: "Canada", 30: "Chile", 31: "China PR", 32: "Colombia", 34: "Costa Rica", 35: "Croatia", 37: "Cyprus",
    38: "Czech Republic", 39: "Denmark", 42: "Ecuador", 43: "Egypt", 45: "England", 52: "France", 54: "Georgia", 55: "Germany",
    56: "Ghana", 57: "Greece", 64: "Hungary", 65: "Iceland", 66: "India", 69: "Iran", 70: "Iraq", 71: "Republic of Ireland",
    72: "Israel", 73: "Italy", 74: "Ivory Coast", 75: "Jamaica", 76: "Japan", 80: "Korea Republic", 93: "Morocco", 98: "Netherlands",
    100: "New Zealand", 101: "Nigeria", 102: "Northern Ireland", 103: "Norway", 108: "Paraguay", 109: "Peru", 111: "Poland",
    112: "Portugal", 114: "Romania", 115: "Russia", 116: "Saudi Arabia", 117: "Scotland", 118: "Senegal", 121: "Slovakia",
    122: "Slovenia", 123: "South Africa", 124: "Spain", 126: "Sweden", 127: "Switzerland", 129: "Tunisia", 130: "Turkey",
    132: "Ukraine", 133: "United States", 134: "Uruguay", 136: "Venezuela", 138: "Wales", 139: "Zambia", 141: "Zimbabwe"
}
STATIC_LEAGUES = {
    13: "Premier League", 14: "EFL Championship", 16: "Ligue 1 McDonald's", 17: "Ligue 2 BKT", 19: "Bundesliga",
    20: "2. Bundesliga", 31: "Serie A Enilive", 32: "Serie BKT", 50: "Scottish Premiership", 53: "LaLiga EA Sports",
    54: "LaLiga Hypermotion", 56: "Liga Portugal", 60: "Eredivisie", 65: "Super Lig", 80: "MLS", 308: "Saudi Pro League",
    335: "CONMEBOL Libertadores", 336: "CONMEBOL Sudamericana", 2118: "UEFA Champions League", 2139: "UEFA Europa League",
    2179: "UEFA Conference League"
}
STATIC_CLUBS = {
    1: "Arsenal", 2: "Aston Villa", 3: "Blackburn Rovers", 4: "Chelsea", 5: "Coventry City", 7: "Everton", 9: "Leeds United",
    10: "Leicester City", 11: "Liverpool", 12: "Manchester City", 13: "Manchester United", 18: "Newcastle United",
    19: "Norwich City", 21: "Nottingham Forest", 43: "Southampton", 44: "Southend United", 46: "Tottenham Hotspur",
    144: "Paris Saint-Germain", 240: "Atlético de Madrid", 241: "FC Barcelona", 243: "Real Madrid", 449: "Juventus",
    453: "Inter", 456: "Milan", 461: "Lazio", 481: "Napoli", 483: "Roma", 503: "FC Porto", 523: "Sporting CP", 527: "Benfica",
    614: "Celtic", 653: "Galatasaray", 654: "Fenerbahçe", 1007: "Al Nassr", 1011: "Al Hilal", 1012: "Al Ittihad",
    1013: "Al Ahli", 112606: "Inter Miami CF"
}


def resolve_entity_name(placeholder: str, entity_type: str) -> str:
    if not placeholder:
        return ""
    parts = placeholder.rsplit("_", 1)
    if len(parts) == 2:
        try:
            entity_id = int(parts[1])
            if entity_type == "nation":   return STATIC_NATIONS.get(entity_id, f"ID {entity_id}")
            elif entity_type == "league": return STATIC_LEAGUES.get(entity_id, f"ID {entity_id}")
            elif entity_type == "club":   return STATIC_CLUBS.get(entity_id, f"ID {entity_id}")
        except ValueError:
            pass
    return placeholder


def parse_player(raw: dict) -> dict:
    raw_nation = raw.get("nationName", "")
    raw_club   = raw.get("clubName", "")
    raw_league = raw.get("leagueName", "")

    def _id(val):
        if "_" in str(val):
            try: return int(str(val).split("_")[-1])
            except: return None
        return None

    avg  = raw.get("avgStats", {})
    imgs = raw.get("images", {})

    return {
        "id":        raw.get("id"),
        "assetId":   raw.get("assetId"),
        "cardName":  raw.get("cardName", ""),
        "firstName": raw.get("firstName", ""),
        "lastName":  raw.get("lastName", ""),
        "commonName":raw.get("commonName", ""),
        "rating":    raw.get("rating"),
        "position":  raw.get("position", ""),
        "cardType":  raw.get("cardUiStyle", ""),
        "nation":  {"id": _id(raw_nation), "name": resolve_entity_name(raw_nation, "nation") or raw_nation},
        "club":    {"id": _id(raw_club),   "name": resolve_entity_name(raw_club,   "club")   or raw_club},
        "league":  {"id": _id(raw_league), "name": resolve_entity_name(raw_league, "league") or raw_league},
        "foot":           raw.get("foot", ""),
        "weakFootRating": raw.get("weakFootRating"),
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


def flatten_player(parsed: dict) -> dict:
    return {
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


# ── Playwright Scraper (الحل الأساسي) ─────────────────────────────────────────

def scrape_with_playwright() -> list[dict]:
    """
    يستخدم Playwright لفتح متصفح Chromium حقيقي.
    يزور صفحة اللاعبين ويعمل inject لـ JS يستدعي الـ API مباشرة
    بنفس الـ cookies والـ headers بتاعة المتصفح — يعدّي Cloudflare تلقائي.
    """
    from playwright.sync_api import sync_playwright

    players: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        # إخفاء علامات الأتمتة
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
        """)

        print(f"[playwright] فتح المتصفح وزيارة {BASE_URL}/{SEASON}/players ...")
        page.goto(f"{BASE_URL}/{SEASON}/players", wait_until="networkidle", timeout=90000)
        print("[playwright] ✅ الصفحة اتحملت — جاري جلب البيانات ...")
        time.sleep(3)

        # انتظار تحميل الصفحة وعدوّ أي Cloudflare challenge
        print("[playwright] انتظار تحميل الصفحة الكامل ...")
        time.sleep(5)

        page_num = 1
        total_pages = None

        while True:
            print(f"[playwright] جلب صفحة {page_num} ...")

            payload = {
                "filters":       {},
                "seasonId":      INTERNAL_ID,
                "page":          page_num,
                "sortType":      SORT_TYPE,
                "sortDirection": SORT_DIR,
                "gkStats":       False,
            }

            # context.request.post يبعت الـ cookies بتاعة المتصفح تلقائي
            for attempt in range(3):
                try:
                    api_response = context.request.post(
                        PLAYERS_API,
                        data=json.dumps(payload),
                        headers={
                            "Content-Type": "application/json",
                            "Accept":       "application/json, text/plain, */*",
                            "Referer":      f"{BASE_URL}/{SEASON}/players",
                            "Origin":       BASE_URL,
                        },
                    )
                    status = api_response.status
                    print(f"[playwright] صفحة {page_num} — status: {status}")

                    if status == 403:
                        print(f"[playwright] 403 — إعادة تحميل الصفحة ومحاولة {attempt+1}/3 ...")
                        page.reload(wait_until="networkidle", timeout=60000)
                        time.sleep(8)
                        continue

                    if status != 200:
                        print(f"[playwright] ❌ status غير متوقع: {status}")
                        break

                    data = api_response.json()
                    break

                except Exception as e:
                    print(f"[playwright] ⚠️  خطأ في المحاولة {attempt+1}: {e}")
                    time.sleep(5)
            else:
                print(f"[playwright] ❌ فشل جلب صفحة {page_num} بعد 3 محاولات")
                break

            page_data   = data.get("pageData", {})
            raw_players = data.get("players", [])

            if not raw_players and page_num == 1:
                print("[playwright] ⚠️  لا يوجد لاعبون — تحقق من INTERNAL_SEASON_ID")
                break

            if total_pages is None:
                total_pages   = page_data.get("pageCount", 1)
                total_players = page_data.get("rowCount", 0)
                print(f"[playwright] إجمالي: {total_players:,} لاعب في {total_pages:,} صفحة")

            for raw in raw_players:
                players.append(flatten_player(parse_player(raw)))
                if MAX_PLAYERS > 0 and len(players) >= MAX_PLAYERS:
                    break

            print(f"[playwright] صفحة {page_num}/{total_pages} — {len(players):,} لاعب")

            if MAX_PLAYERS > 0 and len(players) >= MAX_PLAYERS:
                break
            if page_num >= total_pages:
                print("[playwright] ✅ اكتمل السحب!")
                break

            page_num += 1
            time.sleep(2)

        browser.close()

    return players


# ── Requests Fallback (احتياطي) ────────────────────────────────────────────────

def scrape_with_requests() -> list[dict]:
    import requests

    session = requests.Session()
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "application/json",
        "Content-Type":    "application/json",
        "Origin":          BASE_URL,
        "Referer":         f"{BASE_URL}/{SEASON}/players",
        "sec-fetch-dest":  "empty",
        "sec-fetch-mode":  "cors",
        "sec-fetch-site":  "same-origin",
    }

    # warmup
    try:
        session.get(f"{BASE_URL}/{SEASON}/players", timeout=30)
        time.sleep(2)
    except Exception as e:
        print(f"[requests] warmup failed: {e}")

    players: list[dict] = []
    page_num = 1
    total_pages = None

    while True:
        payload = {
            "filters": {}, "seasonId": INTERNAL_ID,
            "page": page_num, "sortType": SORT_TYPE,
            "sortDirection": SORT_DIR, "gkStats": False,
        }
        for attempt in range(4):
            try:
                r = session.post(PLAYERS_API, headers=headers, json=payload, timeout=45)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                if attempt < 3:
                    time.sleep((attempt + 1) * 8)
                else:
                    print(f"[requests] ❌ فشل الصفحة {page_num}: {e}")
                    return players

        page_data   = data.get("pageData", {})
        raw_players = data.get("players", [])

        if not raw_players and page_num == 1:
            print("[requests] ⚠️  لا يوجد لاعبون")
            break

        if total_pages is None:
            total_pages   = page_data.get("pageCount", 1)
            print(f"[requests] {page_data.get('rowCount',0):,} لاعب في {total_pages} صفحة")

        for raw in raw_players:
            players.append(flatten_player(parse_player(raw)))
            if MAX_PLAYERS > 0 and len(players) >= MAX_PLAYERS:
                break

        if MAX_PLAYERS > 0 and len(players) >= MAX_PLAYERS:
            break
        if page_num >= total_pages:
            break

        page_num += 1
        time.sleep(1.5)

    return players


# ── Main ───────────────────────────────────────────────────────────────────────

def save(players: list[dict]) -> None:
    output = {
        "scrapedAt":         datetime.now(timezone.utc).isoformat(),
        "season":            SEASON,
        "totalPlayers":      len(players),
        "responsesCaptured": len(players),
        "players":           players,
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    print(f"\n[scraper] 💾 {OUTPUT_FILE} ({OUTPUT_FILE.stat().st_size/1024/1024:.2f} MB)")

    CHUNK_SIZE = 500
    chunks_dir = OUTPUT_FILE.parent / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    chunk_files = []
    for i in range(0, len(players), CHUNK_SIZE):
        cn = (i // CHUNK_SIZE) + 1
        cf = chunks_dir / f"players_{cn:03d}.json"
        cf.write_text(json.dumps({
            "chunk": cn, "total": len(players), "chunkSize": CHUNK_SIZE,
            "scrapedAt": output["scrapedAt"], "players": players[i:i+CHUNK_SIZE],
        }, ensure_ascii=False), encoding="utf-8")
        chunk_files.append(f"chunks/players_{cn:03d}.json")

    (OUTPUT_FILE.parent / "players_index.json").write_text(json.dumps({
        "scrapedAt": output["scrapedAt"], "totalPlayers": len(players),
        "chunkSize": CHUNK_SIZE, "totalChunks": len(chunk_files), "chunks": chunk_files,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"[scraper] 📦 {len(chunk_files)} chunk — index جاهز")


def scrape() -> None:
    players = []
    try:
        import playwright
        print("[scraper] ✅ Playwright متاح — استخدام المتصفح الحقيقي")
        players = scrape_with_playwright()
    except ImportError:
        print("[scraper] ⚠️  Playwright غير متاح — fallback لـ requests")
        players = scrape_with_requests()

    if players:
        save(players)
    else:
        print("[scraper] ❌ لم يتم جمع أي لاعبين")
        sys.exit(1)


if __name__ == "__main__":
    scrape()
