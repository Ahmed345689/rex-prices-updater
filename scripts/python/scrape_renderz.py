"""
scrape_renderz.py — Nexus FC Mobile Scraper v7.2 (Deep-Scan Edition)

الجديد في v7.2:
  - إصلاح فحص الـ JS عبر الدخول مباشرة لملف الـ Entry/Manifest الخاص بـ SvelteKit.
  - بدون Playwright نهائياً — يعمل بـ requests فقط.
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
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":   "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ── Dynamic Bundle Finder ───────────────────────────────────────────────────────

def get_dynamic_js_url() -> str:
    """تبحث في كود الصفحة وتستخرج ملفات الـ JS، وإذا فشلت تبحث في الـ entry script"""
    print("[maps] جاري فحص الصفحة الرئيسية بحثاً عن مسارات الجافاسكريبت الحالية...")
    session = requests.Session()
    valid_urls = []
    
    try:
        # المحاولة 1: الدخول لصفحة اللاعبين وقراءة الكود بالكامل
        target_url = f"{BASE_URL}/{SEASON}/players"
        r = session.get(target_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        
        # البحث عن أي روابط جافاسكريبت تنتهي بـ .js جوة الـ chunks
        chunks = re.findall(r'/_app/immutable/chunks/[A-Za-z0-9_$-]+\.js', r.text)
        
        # تحسين البحث ليلقط الصيغ الأخرى للـ imports
        chunks += re.findall(r'src=["\'](.*?/chunks/.*?\.js)["\']', r.text)
        chunks += re.findall(r'href=["\'](.*?/chunks/.*?\.js)["\']', r.text)

        for c in chunks:
            full_url = c if c.startswith("http") else f"{BASE_URL}{c if c.startswith('/') else '/' + c}"
            if full_url not in valid_urls:
                valid_urls.append(full_url)
                
        # المحاولة 2: إذا لم نجد ملفات، نبحث عن ملف الـ start أو entry في السورس
        if not valid_urls:
            print("[maps] لم نجد روابط مباشرة، جاري البحث عن ملف الـ entry script...")
            entry_match = re.search(r'/_app/immutable/entry/start\.[A-Za-z0-9_-]+\.js', r.text)
            if entry_match:
                entry_url = f"{BASE_URL}{entry_match.group(0)}"
                print(f"[maps] فحص ملف الـ Entry: {entry_url}")
                entry_res = session.get(entry_url, headers=HEADERS, timeout=20)
                if entry_res.status_code == 200:
                    # استخراج كل أسماء الـ chunks المذكورة داخل ملف الـ start
                    more_chunks = re.findall(r'[A-Za-z0-9_$-]+\.js', entry_res.text)
                    for mc in more_chunks:
                        if len(mc) > 10:  # تخطي الأسماء القصيرة جداً غير الصالحة
                            url = f"{BASE_URL}/_app/immutable/chunks/{mc}"
                            if url not in valid_urls:
                                valid_urls.append(url)

        print(f"[maps] إجمالي الملفات المكتشفة للفحص: {len(valid_urls)}")
        
        # التمرير على الملفات للبحث عن مصفوفة الترجمة
        for url in valid_urls:
            try:
                res = session.get(url, headers=HEADERS, timeout=10)
                if res.status_code == 200 and ("NationName_" in res.text or "ClubName_" in res.text):
                    print(f"[maps] 🎉 نجاح! تم العثور على الملف الصحيح: {url.split('/')[-1]}")
                    return url
            except Exception:
                continue
                
    except Exception as e:
        print(f"[maps] ⚠️ خطأ أثناء البحث الديناميكي: {e}")
    
    # حل أخير في حالة الفشل التام
    raise RuntimeError("⚠️ فشل السكرابر في العثور على ملف خرائط الأسماء (JS Bundle) بعد التحديث الجديد للموقع.")


# ── Name Extractor ──────────────────────────────────────────────────────────────

def extract_name_maps(src: str) -> tuple[dict, dict, dict]:
    """يستخرج الأسماء الحقيقية للدول/الفرق/الدوريات من الـ JS bundle."""
    key_to_var: dict[str, str] = {}
    for m in re.finditer(
        r'((?:NationName|TeamName|LeagueName|ClubName)_(\d+)):([A-Za-z_$][A-Za-z0-9_$]{1,8})',
        src,
    ):
        key_to_var[m.group(1)] = m.group(3)

    var_to_en: dict[str, str] = {}
    for m in re.finditer(
        r'([A-Za-z_$][A-Za-z0-9_$]{1,8})=\([^)]{0,20}\)=>\(\{"da-DK":[^,]+,"de-DE":[^,]+,"en-US":([A-Za-z_$][A-Za-z0-9_$]{1,8})',
        src,
    ):
        var_to_en[m.group(1)] = m.group(2)

    en_to_name: dict[str, str] = {}
    for m in re.finditer(
        r'(?:^|[,;=\s])([A-Za-z_$][A-Za-z0-9_$]{1,8})=\(\)=>"([^"]{1,80})"',
        src,
    ):
        en_to_name[m.group(1)] = m.group(2)

    nations: dict[int, str] = {}
    clubs:   dict[int, str] = {}
    leagues: dict[int, str] = {}

    for key, outer_var in key_to_var.items():
        en_var = var_to_en.get(outer_var)
        if not en_var:
            continue
        name = en_to_name.get(en_var)
        if not name:
            continue
        entity_id = int(key.split("_")[-1])
        if key.startswith("NationName"):
            nations[entity_id] = name
        elif key.startswith(("TeamName", "ClubName")):
            clubs[entity_id] = name
        elif key.startswith("LeagueName"):
            leagues[entity_id] = name

    return nations, clubs, leagues


def load_name_maps() -> tuple[dict, dict, dict]:
    """يجيب الـ JS bundle الديناميكي ويستخرج الأسماء."""
    js_url = get_dynamic_js_url()
    print(f"[maps] جاري تحميل الـ JS bundle من {js_url} ...")
    r = requests.get(js_url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    src = r.text
    print(f"[maps] حجم الـ bundle: {len(src):,} chars — جاري الاستخراج ...")
    nations, clubs, leagues = extract_name_maps(src)
    print(f"[maps] ✅  دول={len(nations)}  أندية={len(clubs)}  دوريات={len(leagues)}")
    return nations, clubs, leagues


# ── Player Parser ───────────────────────────────────────────────────────────────

def _resolve_id(placeholder: str) -> int | None:
    parts = placeholder.rsplit("_", 1)
    if len(parts) == 2:
        try:
            return int(parts[1])
        except ValueError:
            pass
    return None


def parse_player(
    raw: dict,
    nations: dict[int, str],
    clubs:   dict[int, str],
    leagues: dict[int, str],
) -> dict:
    nation_id  = _resolve_id(raw.get("nationName", ""))
    club_id    = _resolve_id(raw.get("clubName",   ""))
    league_id  = _resolve_id(raw.get("leagueName", ""))

    nation_name  = nations.get(nation_id,  raw.get("nationName",  "")) if nation_id  is not None else raw.get("nationName",  "")
    club_name    = clubs.get(club_id,      raw.get("clubName",    "")) if club_id    is not None else raw.get("clubName",    "")
    league_name  = leagues.get(league_id,  raw.get("leagueName",  "")) if league_id  is not None else raw.get("leagueName",  "")

    avg   = raw.get("avgStats", {})
    stats = raw.get("stats", {})
    imgs = raw.get("images", {})

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
            "id":   nation_id,
            "name": nation_name,
        },
        "club": {
            "id":   club_id,
            "name": club_name,
        },
        "league": {
            "id":   league_id,
            "name": league_name,
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
    HEADERS["Origin"] = BASE_URL
    HEADERS["Referer"] = f"{BASE_URL}/{SEASON}/players"
    HEADERS["Accept"] = "application/json"
    
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
    nations, clubs, leagues = load_name_maps()

    players: list[dict] = []
    session = requests.Session()

    print(f"[scraper] بداية السحب (season={SEASON}, internalId={INTERNAL_ID}) ...")
    page = 1
    total_pages = None

    while True:
        try:
            data = fetch_page(session, page)
        except Exception as e:
            print(f"[scraper] ⚠️  خطأ في الصفحة {page}: {e}")
            break

        page_data   = data.get("pageData", {})
        raw_players = data.get("players", [])

        if total_pages is None:
            total_pages   = page_data.get("pageCount", 1)
            total_players = page_data.get("rowCount", 0)
            print(f"[scraper] إجمالي: {total_players:,} لاعب في {total_pages:,} صفحة")

        for raw in raw_players:
            parsed = parse_player(raw, nations, clubs, leagues)
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

        if len(players) % 100 == 0 or len(players) <= PAGE_SIZE:
            print(f"[scraper] صفحة {page}/{total_pages} — {len(players):,} لاعب حتى الآن")

        if MAX_PLAYERS > 0 and len(players) >= MAX_PLAYERS:
            print(f"[scraper] ✅  وصلنا للحد المحدد ({MAX_PLAYERS})")
            break

        if page >= total_pages:
            print(f"[scraper] ✅  اكتمل السحب — كل الصفحات")
            break

        page += 1

    # حفظ النتائج
    output = {
        "scrapedAt":        datetime.now(timezone.utc).isoformat(),
        "season":           SEASON,
        "totalPlayers":     len(players),
        "responsesCaptured": len(players),
        "players":          players,
    }

    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    print(f"\n[scraper] 💾  حُفظ {len(players):,} لاعب في {OUTPUT_FILE}")

    def _name(val):
        if isinstance(val, dict): return val.get("name") or ""
        return str(val) if val else ""

    real_nations = sum(1 for p in players if _name(p.get("nation")) and not _name(p.get("nation")).startswith("NationName"))
    real_clubs   = sum(1 for p in players if _name(p.get("club"))   and not _name(p.get("club")).startswith("TeamName"))
    real_leagues = sum(1 for p in players if _name(p.get("league")) and not _name(p.get("league")).startswith("LeagueName"))
    total = len(players)
    print(f"[stats] دول حقيقية:   {real_nations}/{total} ({real_nations/total*100:.1f}%)")
    print(f"[stats] أندية حقيقية: {real_clubs}/{total} ({real_clubs/total*100:.1f}%)")
    print(f"[stats] دوريات حقيقية:{real_leagues}/{total} ({real_leagues/total*100:.1f}%)")


if __name__ == "__main__":
    scrape()
