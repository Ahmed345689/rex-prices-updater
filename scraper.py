import cloudscraper
import json
import logging
import re
import time
from bs4 import BeautifulSoup
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

SCRAPER_URL = "https://renderz.app/players"
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

def find_player_cards(soup):
    selectors = [
        'div[class*="player"]',
        'div[class*="card"]',
        'div[class*="PlayerCard"]',
        'div[class*="market"]',
        'div[data-testid*="card"]',
        'a[href*="/player/"]',
        'div[class*="item"]',
        'article',
        'div.bg-gray-800'
    ]
    for selector in selectors:
        cards = soup.select(selector)
        if len(cards) > 5:
            logger.info(f"✅ Found {len(cards)} player cards using selector: {selector}")
            return cards, selector
    logger.warning("⚠️ No cards found with known selectors")
    return [], None

def parse_players_from_html(html_content):
    soup = BeautifulSoup(html_content, 'lxml')
    player_elements, used_selector = find_player_cards(soup)

    if not player_elements:
        return [], 0

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
                logger.info(f" ✓ {name} - ${price:,} (raw: {price_text})")
        except:
            continue

    return players, len(player_elements)

def scrape_with_retry():
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})

    for attempt in range(MAX_RETRIES):
        try:
            logger.info(f"🚀 Attempt {attempt + 1}/{MAX_RETRIES}: Connecting to {SCRAPER_URL}")
            response = scraper.get(SCRAPER_URL, timeout=30)
            response.raise_for_status()

            players, total_found = parse_players_from_html(response.text)

            if players:
                logger.info(f"✅ Successfully extracted {len(players)} valid players from {total_found} cards")
                return players
            else:
                logger.warning(f"⚠️ Attempt {attempt + 1}: Found {total_found} cards but 0 valid players")

        except Exception as e:
            logger.error(f"❌ Attempt {attempt + 1} failed: {e}")

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY * (attempt + 1))

    return []

def save_to_json(players):
    output = {
        "last_updated": datetime.utcnow().isoformat() + "Z",
        "source": "fcrenderz.com",
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
    logger.info("="*50)
    logger.info("🎮 FC Renderz Price Scraper Starting")
    logger.info("="*50)

    players = scrape_with_retry()
    save_to_json(players)

    if not players:
        logger.error("❌ FATAL: No players found. Check selectors or site structure.")
        exit(1)

    logger.info("✅ Scraping completed successfully")
