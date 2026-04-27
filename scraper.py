import asyncio
import json
import logging
import re
from datetime import datetime
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

SCRAPER_URL = "https://renderz.app/players"

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

async def scrape_players():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        logger.info(f"🚀 Connecting to {SCRAPER_URL}")
        await page.goto(SCRAPER_URL, wait_until="networkidle", timeout=60000)

        # انتظر الكروت تظهر
        await page.wait_for_timeout(3000)

        html = await page.content()
        await browser.close()
        return html

def parse_players_from_html(html_content):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, 'lxml')

    selectors = [
        'div[class*="player"]', 'div[class*="card"]',
        'div[class*="PlayerCard"]', 'a[href*="/player/"]',
        'div[class*="item"]', 'article'
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
                logger.info(f"✓ {name} - ${price:,}")
        except:
            continue

    return players

def save_to_json(players):
    output = {
        "last_updated": datetime.utcnow().isoformat() + "Z",
        "source": "renderz.app",
        "total_players": len(players),
        "players": players
    }
    with open('players.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    logger.info(f"💾 Saved {len(players)} players to players.json")

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("🎮 FC Renderz Price Scraper Starting")
    logger.info("=" * 50)

    html = asyncio.run(scrape_players())
    players = parse_players_from_html(html)
    save_to_json(players)

    if not players:
        logger.error("❌ No players found. Inspect the HTML structure.")
        exit(1)

    logger.info("✅ Done!")
