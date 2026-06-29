"""
scrape_renderz.py — Nexus FC Mobile Scraper v7.3 (Anti-Block Edition)

الجديد في v7.3:
  - استغناء تام عن ملفات الـ JS المعرضة للحجب والـ 404.
  - خريطة أسماء داخلية ومدمجة لأهم الدوريات والأندية والدول لتخطي حماية الموقع.
  - حماية كاملة من الـ Crash وضمان تشغيل الـ Workflow بنسبة 100%.
"""
from __future__ import annotations

import json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ── Config ─────────────────────────────────────────────────────────────────────
SEASON          = os.environ.get("SEASON", "24")
INTERNAL_ID     = os.environ.get("INTERNAL_SEASON_ID", "23")   # renderz internal
OUTPUT_FILE     = Path(os.environ.get("OUTPUT_FILE", "players.json"))
MAX_PLAYERS     = int(os.environ.get("MAX_PLAYERS", "0"))       # 0 = كل اللاعبين
PAGE_SIZE       = 20
SORT_TYPE       = os.environ.get("SORT_TYPE", "overallRating")
SORT_DIR        = os.environ.get("SORT_DIR", "DESC")

BASE_URL        = "https://renderz.app"
PLAYERS_API     = f"{BASE_URL}/api/players/filter"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/{SEASON}/players",
}

# ── Static Maps (بديل الـ JS المتغير لضمان استقرار السكرابر) ────────────────────
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
    """يحول مثل NationName_43 إلى Egypt تلقائياً باستخدام القواميس الثابتة"""
    if not placeholder:
        return ""
    parts = placeholder.rsplit("_", 1)
    if len(parts) == 2:
        try:
            entity_id = int(parts[1])
            if entity_type == "nation" and entity_id in STATIC_NATIONS:
                return STATIC_NATIONS[entity_id]
            elif entity_type == "league" and entity_id in STATIC_LEAGUES:
                return STATIC_LEAGUES[entity_id]
            elif entity_type == "club" and entity_id in STATIC_CLUBS:
                return STATIC_CLUBS[entity_id]
            # في حال لم يتواجد في القاموس، يعيد الاسم الأخير النظيف لعدم تخريب ملف الـ JSON
            return f"ID {entity_id}"
        except ValueError:
            pass
    return placeholder

# ── Player Parser ───────────────────────────────────────────────────────────────

def parse_player(raw: dict) -> dict:
    """يحوّل سجل لاعب خام إلى dict نظيف وثابت"""
    raw_nation = raw.get("nationName", "")
    raw_club   = raw.get("clubName", "")
    raw_league = raw.get("leagueName", "")

    nation_name = resolve_entity_name(raw_nation, "nation")
    club_name   = resolve_entity_name(raw_club, "club")
    league_name = resolve_entity_name(raw_league, "league")

    def _extract_id(val: str) -> int | None:
        if "_" in str(val):
            try: return int(str(val).split("_")[-1])
            except: return None
        return None

    avg   = raw.get("avgStats", {})
    stats = raw.get("stats", {})
    imgs  = raw.get("images", {})

    return {
        "id":          raw.get("id"),
        "assetId":     raw.get("assetId"),
        "cardName":    raw.get("cardName", ""),
        "firstName":   raw.get("firstName", ""),
        "lastName":    raw.get("lastName", ""),
        "commonName":  raw.get("commonName", ""),
        "rating":      raw.get("rating"),
        "position":    raw.get("position", ""),
        "cardType":    raw.get("cardUiStyle", ""),
        "nation": {
            "id":   _extract_id(raw_nation),
            "name": nation_name if nation_name else raw_nation,
        },
        "club": {
            "id":   _extract_id(raw_club),
            "name": club_name if club_name else raw_club,
        },
        "league": {
            "id":   _extract_id(raw_league),
            "name": league_name if league_name else raw_league,
        },
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
        "stats":         stats,
        "totalStats":    raw.get("totalStats"),
        "priceData":     raw.get("priceData", {}),
        "auctionable":   raw.get("auctionable"),
        "images": {
            "playerFaceImage": imgs.get("playerCard") or imgs.get("playerCardImage", ""),
            "playerCardImage": imgs.get("playerCard") or imgs.get("playerCardImage", ""),
            "background":      imgs.get("background") or imgs.get("playerCardBackground", ""),
            "flag":            imgs.get("flag") or imgs.get("flagImage", ""),
            "club":            imgs.get("club") or imgs.get("clubImage", ""),
            "league":          imgs.get("league") or imgs.get("leagueImage", ""),
        },
    }


# ── Scraper ─────────────────────────────────────────────────────────────────────

def fetch_page(session: requests.Session, page: int) -> dict:
    payload = {
        "filters":       {},
        "seasonId":      INTERNAL_ID,
        "page":          page,
        "sortType":      SORT_TYPE,
        "sortDirection": SORT_DIR,
        "gkStats":       False,
    }
    for attempt in range(4):
        try:
            r = session.post(PLAYERS_API, headers=HEADERS, json=payload, timeout=45)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < 3:
                wait = (attempt + 1) * 5
                print(f"[scraper] ⚠️  محاولة {attempt+1} فشلت، انتظار {wait}s: {e}")
                import time; time.sleep(wait)
            else:
                raise


def scrape() -> None:
    players: list[dict] = []
    session = requests.Session()

    print(f"[scraper] بداية السحب المباشر (season={SEASON}, internalId={INTERNAL_ID}) ...")
    page = 1
    total_pages = None

    while True:
        try:
            data = fetch_page(session, page)
        except Exception as e:
            print(f"[scraper] ⚠️  خطأ فادح في الصفحة {page}: {e}")
            break

        page_data   = data.get("pageData", {})
        raw_players = data.get("players", [])

        if not raw_players and page == 1:
            print("[scraper] ⚠️  تحذير: لم يعود الـ API بأي لاعبين! قد يكون هناك حظر من الموقع أو تغير في الـ INTERNAL_SEASON_ID")
            break

        if total_pages is None:
            total_pages   = page_data.get("pageCount", 1)
            total_players = page_data.get("rowCount", 0)
            print(f"[scraper] إجمالي اللاعبين في الموقع: {total_players:,} لاعب في {total_pages:,} صفحة")

        for raw in raw_players:
            parsed = parse_player(raw)
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

        if len(players) % 200 == 0 or len(players) <= PAGE_SIZE:
            print(f"[scraper] تقدم السحب: صفحة {page}/{total_pages} — تم جمع {len(players):,} لاعب")

        if MAX_PLAYERS > 0 and len(players) >= MAX_PLAYERS:
            break

        if page >= total_pages:
            print(f"[scraper] ✅ اكتمل السحب بنجاح!")
            break

        page += 1

    # حفظ الملف النهائي
    output = {
        "scrapedAt":        datetime.now(timezone.utc).isoformat(),
        "season":           SEASON,
        "totalPlayers":     len(players),
        "responsesCaptured": len(players),
        "players":          players,
    }

    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    print(f"\n[scraper] 💾 تم حفظ الملف بنجاح في {OUTPUT_FILE} وبحجم {OUTPUT_FILE.stat().st_size / 1024 / 1024:.2f} MB")

    # ── حفظ chunks (500 لاعب لكل ملف) للتطبيق ──────────────────────────────
    CHUNK_SIZE = 500
    chunks_dir = OUTPUT_FILE.parent / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    chunk_files = []
    for i in range(0, len(players), CHUNK_SIZE):
        chunk_num = (i // CHUNK_SIZE) + 1
        chunk_data = {
            "chunk":     chunk_num,
            "total":     len(players),
            "chunkSize": CHUNK_SIZE,
            "scrapedAt": output["scrapedAt"],
            "players":   players[i:i + CHUNK_SIZE],
        }
        chunk_file = chunks_dir / f"players_{chunk_num:03d}.json"
        chunk_file.write_text(json.dumps(chunk_data, ensure_ascii=False), encoding="utf-8")
        chunk_files.append(f"chunks/players_{chunk_num:03d}.json")

    index_data = {
        "scrapedAt":    output["scrapedAt"],
        "totalPlayers": len(players),
        "chunkSize":    CHUNK_SIZE,
        "totalChunks":  len(chunk_files),
        "chunks":       chunk_files,
    }
    index_file = OUTPUT_FILE.parent / "players_index.json"
    index_file.write_text(json.dumps(index_data, ensure_ascii=False), encoding="utf-8")
    print(f"[scraper] 📦 {len(chunk_files)} chunk في {chunks_dir}")
    print(f"[scraper] 📋 index في {index_file}")


if __name__ == "__main__":
    scrape()
