import cloudscraper
import json
import logging
import re
import time
from bs4 import BeautifulSoup
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

SCRAPER_URL = "https://renderz.app/players"  # ✅ URL صح
MAX_RETRIES = 3
RETRY_DELAY = 2

def parse_price(price_text):
    if not price_text:
        return None
    cleaned = re.sub(r'[^\d.KkMm]', '', price_text.upper())
    try:
        if 'K' in cleaned:
            return int(float(cleaned.replace('K', '')) * 1000)
        elif 'M' in cleaned:
            return int(float(cleaned.replace('M', '')) * 1000000)
        else:
            return int(re.sub(r'[^\d]', '', cleaned))
    except:
        return None

def try_api_endpoints(scraper):
    """جرب تلاقي API جاهز بدل scraping"""
    api_urls = [
        "https://renderz.app/api/players",
        "https://renderz.app/api/v1/players",
        "https://renderz.app/api/market",
        "https://renderz.app/_next/data/players.json",
    ]
    for url in api_urls:
        try:
            r = scraper.get(url, timeout=15)
            if r.status_code == 200 and 'application/json' in r.headers.get('Content-Type', ''):
                data = r.json()
                logger.info(f"✅ Found API at: {url}")
                return data
        except:
            continue
    return None

def parse_players_from_html(html_content):
    soup = BeautifulSoup(html_content, 'lxml')

    # ابحث عن JSON مضمن في الصفحة (Next.js بيحطه هنا)
    scripts = soup.find_all('script', {'id': '__NEXT_DATA__'})
    if scripts:
        try:
            data = json.loads(scripts[0].string)
            logger.info("✅ Found __NEXT_DATA__ JSON in page!")
            # استخرج البيانات من الـ JSON
            players = extract_from_next_data(data)
            if players:
                return players, len(players)
        except Exception as e:
            logger.warning(f"Failed to parse __NEXT_DATA__: {e}")

    # fallback: HTML parsing
    selectors = [
        'div[class*="player"]', 'div[class*="card"]',
        'div[class*="Player"]', 'div[class*="Card"]',
        'a[href*="/player/"]', 'div[class*="item"]',
        'article', 'li[class*="player"]'
    ]

    player_elements = []
    for selector in selectors:
        cards = soup.select(selector)
        if len(cards) > 5:
            logger.info(f"✅ Found {len(cards)} cards using: {selector}")
            player_elements = cards
            break

    players = []
    for idx, card in enumerate(player_elements):
        try:
            text = card.get_text(separator=' ', strip=True)
            price_match = re.search(r'(\d+(?:\.\d+)?[KkMm]|\$?\d{1,3}(?:,\d{3})+)', text)
            if not price_match:
                continue
            price_text = price_match.group(1)
            price = parse_price(price_text)
            if not price or price < 100:
                continue
            name_parts = text.split(price_text)[0].strip().split()
            name = ' '.join(name_parts[-3:]) if name_parts else f"Player_{idx}"
            name = re.sub(r'\d+.*', '', name).strip()
            if len(name) > 2:
                players.append({"name": name, "price": price})
        except:
            continue

    return players, len(player_elements)

def extract_from_next_data(data):
    """استخرج البيانات من JSON الخاص بـ Next.js"""
    players = []
    try:
        # جرب مسارات مختلفة داخل الـ JSON
        props = data.get('props', {}).get('pageProps', {})
        for key in ['players', 'data', 'items', 'market', 'cards']:
            if key in props:
                items = props[key]
                if isinstance(items, list):
                    for item in items:
                        name = item.get('name') or item.get('playerName') or item.get('player', {}).get('name', '')
                        price = item.get('price') or item.get('value') or item.get('marketValue')
                        if name and price:
                            players.append({"name": name, "price": int(price)})
                    if players:
                        logger.info(f"✅ Extracted {len(players)} players from key: '{key}'")
                        return players
    except Exception as e:
        logger.warning(f"extract_from_next_data error: {e}")
    return []

def scrape_with_retry():
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
    )

    for attempt in range(MAX_RETRIES):
        try:
            logger.info(f"🚀 Attempt {attempt + 1}/{MAX_RETRIES}")

            # أولاً جرب API مباشرة
            if attempt == 0:
                api_data = try_api_endpoints(scraper)
                if api_data:
                    players = extract_from_next_data({'props': {'pageProps': api_data}})
                    if players:
                        return players

            # بعدين جرب scrape الصفحة
            response = scraper.get(SCRAPER_URL, timeout=30)
            response.raise_for_status()

            logger.info(f"📄 Page size: {len(response.text)} chars")

            # سجل جزء من الـ HTML للتشخيص
            if attempt == 0:
                with open('debug_page.html', 'w', encoding='utf-8') as f:
                    f.write(response.text)
                logger.info("💾 Saved debug_page.html for inspection")

            players, total_found = parse_players_from_html(response.text)

            if players:
                logger.info(f"✅ Extracted {len(players)} players from {total_found} cards")
                return players
            else:
                logger.warning(f"⚠️ Found {total_found} cards but 0 valid players")

        except Exception as e:
            logger.error(f"❌ Attempt {attempt + 1} failed: {e}")

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY * (attempt + 1))

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
    import json  # تأكد من الـ import
    logger.info("=" * 50)
    logger.info("🎮 FC Renderz Price Scraper Starting")
    logger.info("=" * 50)

    players = scrape_with_retry()
    save_to_json(players)

    if not players:
        logger.error("❌ No players found.")
        exit(1)

    logger.info("✅ Done!")
