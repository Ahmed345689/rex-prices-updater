import cloudscraper
import json
import logging
import re
import time
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2

# قائمة بـ API endpoints محتملة
API_ENDPOINTS = [
    "https://renderz.app/api/24/players",
    "https://renderz.app/api/players",
    "https://api.renderz.app/players",
    "https://api.renderz.app/24/players",
    "https://renderz.app/api/v1/players",
    "https://renderz.app/api/v2/players",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://renderz.app/",
    "Origin": "https://renderz.app",
}

def parse_price(price_text):
    if not price_text:
        return None
    cleaned = re.sub(r'[^\d.KkMm]', '', str(price_text).upper())
    try:
        if 'K' in cleaned:
            return int(float(cleaned.replace('K', '')) * 1000)
        elif 'M' in cleaned:
            return int(float(cleaned.replace('M', '')) * 1000000)
        else:
            return int(re.sub(r'[^\d]', '', cleaned)) if cleaned else None
    except:
        return None

def extract_players_from_json(data):
    """استخرج اللاعبين من أي شكل JSON"""
    players = []
    
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # جرب مفاتيح مختلفة
        for key in ['players', 'data', 'items', 'results', 'cards', 'content']:
            if key in data and isinstance(data[key], list):
                items = data[key]
                break
        else:
            items = []
    else:
        return []

    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            # استخرج الاسم
            name = (item.get('name') or item.get('playerName') or 
                   item.get('fullName') or item.get('player_name') or '')
            
            # استخرج السعر
            price_raw = (item.get('price') or item.get('marketPrice') or 
                        item.get('market_price') or item.get('value') or
                        item.get('marketValue') or item.get('market_value') or 0)
            
            price = parse_price(str(price_raw)) if price_raw else None
            
            if name and price and price > 100:
                players.append({
                    "name": name,
                    "price": price,
                    "rating": item.get('rating') or item.get('ovr') or item.get('overall', 0),
                    "position": item.get('position') or item.get('pos', ''),
                })
                logger.info(f"✓ {name} - ${price:,}")
        except Exception as e:
            continue

    return players

def scrape_with_retry():
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
    )

    # أولاً: جرب كل الـ API endpoints
    logger.info("🔍 Searching for API endpoints...")
    for url in API_ENDPOINTS:
        try:
            logger.info(f"  Trying: {url}")
            r = scraper.get(url, headers=HEADERS, timeout=15)
            logger.info(f"  Status: {r.status_code} | Content-Type: {r.headers.get('Content-Type', 'unknown')}")
            
            if r.status_code == 200:
                content_type = r.headers.get('Content-Type', '')
                if 'json' in content_type:
                    data = r.json()
                    players = extract_players_from_json(data)
                    if players:
                        logger.info(f"✅ Found {len(players)} players from: {url}")
                        return players
                    else:
                        # سجل الـ JSON عشان نفهم structure
                        with open('debug_api_response.json', 'w') as f:
                            json.dump(data, f, indent=2)
                        logger.warning(f"  Got JSON but couldn't extract players. Saved to debug_api_response.json")
        except Exception as e:
            logger.warning(f"  Failed: {e}")

    # ثانياً: جرب الصفحة الرئيسية مع params
    logger.info("\n🔍 Trying page with query params...")
    page_urls = [
        "https://renderz.app/24/players",
        "https://renderz.app/24/players?auctionable=true",
    ]
    
    for url in page_urls:
        try:
            r = scraper.get(url, headers=HEADERS, timeout=30)
            logger.info(f"  {url} -> Status: {r.status_code}, Size: {len(r.text)} chars")
            
            # احفظ الـ HTML للتشخيص
            with open('debug_page.html', 'w', encoding='utf-8') as f:
                f.write(r.text)
            logger.info("  💾 Saved debug_page.html")
            
            # ابحث عن JSON في الصفحة
            json_matches = re.findall(r'__NEXT_DATA__[^>]*>({.*?})</script>', r.text, re.DOTALL)
            if json_matches:
                data = json.loads(json_matches[0])
                # ابحث في كل الـ JSON
                players = search_json_recursive(data)
                if players:
                    return players
                    
        except Exception as e:
            logger.error(f"  Failed: {e}")

    return []

def search_json_recursive(data, depth=0):
    """ابحث في الـ JSON بشكل recursive عن قوائم فيها players"""
    if depth > 5:
        return []
    
    players = []
    
    if isinstance(data, dict):
        # جرب استخراج مباشر
        direct = extract_players_from_json(data)
        if len(direct) > 5:
            return direct
            
        for value in data.values():
            result = search_json_recursive(value, depth + 1)
            if len(result) > 5:
                return result
                
    elif isinstance(data, list) and len(data) > 5:
        result = extract_players_from_json(data)
        if result:
            return result
            
    return []

def save_to_json(players):
    output = {
        "last_updated": datetime.utcnow().isoformat() + "Z",
        "source": "renderz.app",
        "total_players": len(players),
        "players": players
    }
    with open('players.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    if players:
        with open('players_backup.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
    logger.info(f"💾 Saved {len(players)} players to players.json")

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("🎮 FC Renderz Price Scraper Starting")
    logger.info("=" * 50)

    players = scrape_with_retry()
    save_to_json(players)

    if not players:
        logger.error("❌ No players found.")
        logger.info("📋 Check debug_page.html and debug_api_response.json for clues")
        exit(1)

    logger.info("✅ Done!")
