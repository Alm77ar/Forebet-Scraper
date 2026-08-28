import os
import re
import sys
import asyncio
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TARGET_URLS = {
    "today": "https://www.forebet.com/en/football-tips-and-predictions-for-today",
    "tomorrow": "https://www.forebet.com/en/football-tips-and-predictions-for-tomorrow",
}

MINIMUM_PROBABILITY = 75


async def fetch_html_playwright(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await stealth_async(page)

        print(f"Navigating to: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # Wait for initial match containers to appear
        try:
            await page.wait_for_selector(
                ".rcnt, div[class*='rcnt'], .schema-row, tr[class*='schema']",
                timeout=20000,
            )
        except Exception:
            print("Selector wait timed out, continuing with loaded content...")

        # Scroll incrementally to trigger lazy-loaded match rows
        print("Scrolling page to load lazy content...")
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, 1500)")
            await page.wait_for_timeout(800)

        content = await page.content()
        await browser.close()
        return content


def numbers_in_text(text):
    return [
        int(value)
        for value in re.findall(r"(?<![\d.])(\d{1,3})(?![\d.])", text)
    ]


def get_probabilities(row):
    # Search targeted containers first
    containers = row.select(
        ".tr_probabilities, "
        "[class*='probabilities'], "
        "[class*='probability'], "
        "[class*='prob']"
    )

    for container in containers:
        values = numbers_in_text(container.get_text(" ", strip=True))
        if len(values) == 3 and 90 <= sum(values) <= 110:
            return values[0], values[1], values[2]

    # Fallback search across all child elements inside the row
    for element in row.find_all(["div", "span", "td"]):
        values = numbers_in_text(element.get_text(" ", strip=True))
        if len(values) == 3 and 90 <= sum(values) <= 110:
            return values[0], values[1], values[2]

    return None


def get_coefficient(row):
    odds_element = row.select_one(".forebet_odds, [class*='odds']")
    if not odds_element:
        return 0.0

    match = re.search(r"\d+(?:\.\d+)?", odds_element.get_text(" ", strip=True))
    return float(match.group()) if match else 0.0


def scrape_forebet(target_day):
    url = TARGET_URLS.get(target_day, TARGET_URLS["today"])
    html_content = asyncio.run(fetch_html_playwright(url))
    soup = BeautifulSoup(html_content, "html.parser")

    # Expanded selector query to catch secondary/minor league match wrappers
    rows = soup.select(
        "div.rcnt, "
        "div[class*='rcnt'], "
        ".schema-row, "
        "tr.schema-row, "
        "tr[class*='schema'], "
        "div[class*='schema']"
    )

    print(f"Total raw match rows detected: {len(rows)}")

    results = []
    seen_matches = set()

    for row in rows:
        home_element = row.select_one(".homeTeam, [class*='homeTeam']")
        away_element = row.select_one(".awayTeam, [class*='awayTeam']")

        if not home_element or not away_element:
            continue

        home_team = home_element.get_text(" ", strip=True)
        away_team = away_element.get_text(" ", strip=True)
        probabilities = get_probabilities(row)

        if not home_team or not away_team or probabilities is None:
            continue

        home_prob, draw_prob, away_prob = probabilities

        # Filter against Home or Away probability threshold
        if home_prob < MINIMUM_PROBABILITY and away_prob < MINIMUM_PROBABILITY:
            continue

        if home_prob >= away_prob:
            pick = "1 — Home win"
            prob = home_prob
        else:
            pick = "2 — Away win"
            prob = away_prob

        match_key = (home_team, away_team)
        if match_key in seen_matches:
            continue

        seen_matches.add(match_key)
        results.append(
            {
                "home": home_team,
                "away": away_team,
                "pick": pick,
                "probability": prob,
                "coefficient": get_coefficient(row),
            }
        )

    return sorted(
        results,
        key=lambda item: (item["probability"], item["coefficient"]),
        reverse=True,
    )


def send_telegram_message(message):
    import requests

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub secrets.")

    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    for start in range(0, len(message), 4000):
        response = requests.post(
            telegram_url,
            json={"chat_id": CHAT_ID, "text": message[start : start + 4000]},
            timeout=30,
        )
        response.raise_for_status()


if __name__ == "__main__":
    target_day = sys.argv[1].lower() if len(sys.argv) > 1 else "today"
    if target_day not in TARGET_URLS:
        target_day = "today"

    picks = scrape_forebet(target_day)

    if picks:
        lines = [
            f"⚽ Forebet picks for {target_day.upper()}",
            f"Filter: Home or Away win probability ≥ {MINIMUM_PROBABILITY}%\n",
        ]

        for item in picks:
            coef_str = f"{item['coefficient']:.2f}" if item["coefficient"] > 0 else "N/A"
            lines.append(f"• {item['home']} vs {item['away']}")
            lines.append(f"  Pick: {item['pick']} ({item['probability']}%) | COEF: {coef_str}\n")

        message = "\n".join(lines)
    else:
        message = f"No matches found for {target_day.upper()} matching ≥ {MINIMUM_PROBABILITY}% probability criteria."

    send_telegram_message(message)
    print(f"Telegram message sent. Total matches selected: {len(picks)}")
