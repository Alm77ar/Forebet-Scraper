import os
import re
import sys
import asyncio
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://localhost:8191/v1")

TARGET_URLS = {
    "today": "https://www.forebet.com/en/football-tips-and-predictions-for-today",
    "tomorrow": "https://www.forebet.com/en/football-tips-and-predictions-for-tomorrow",
}

MINIMUM_PROBABILITY = 75


def get_flaresolverr_clearance(url):
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": 60000,
    }
    headers = {"Content-Type": "application/json"}

    print(f"Obtaining Cloudflare session clearance via FlareSolverr for: {url}")
    response = requests.post(FLARESOLVERR_URL, json=payload, headers=headers, timeout=70)
    response.raise_for_status()

    data = response.json()
    if data.get("status") == "ok":
        solution = data["solution"]
        return solution.get("cookies", []), solution.get("userAgent", "")

    raise RuntimeError(f"FlareSolverr clearance failed: {data.get('message')}")


async def fetch_full_html(url):
    cookies, user_agent = get_flaresolverr_clearance(url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )

        pw_cookies = [
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c.get("path", "/"),
            }
            for c in cookies
        ]

        context = await browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1280, "height": 1000},
        )
        await context.add_cookies(pw_cookies)

        page = await context.new_page()

        print(f"Navigating to match table: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)

        # Dynamic scroll loop: scroll until page height stops increasing or 15 iterations reached
        print("Scrolling page dynamically to trigger all lazy-loaded matches...")
        previous_height = 0
        for step in range(15):
            current_height = await page.evaluate("document.body.scrollHeight")
            if current_height == previous_height and step > 5:
                break
            previous_height = current_height
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)

        content = await page.content()
        await browser.close()
        return content


def get_probabilities(row):
    # Extract row text directly to prevent picking up single-digit recommendation tags (.fprt)
    row_text = row.get_text(" ", strip=True)
    numbers = [int(n) for n in re.findall(r"\b\d{1,3}\b", row_text)]

    # Search for 3 consecutive integers summing to ~100% (1X2 probability percentages)
    for i in range(len(numbers) - 2):
        h, d, a = numbers[i], numbers[i + 1], numbers[i + 2]
        if 90 <= (h + d + a) <= 110:
            return h, d, a

    return None


def get_coefficient(row):
    odds_element = row.select_one(".forebet_odds, [class*='odds']")
    if not odds_element:
        return 0.0

    match = re.search(r"\d+(?:\.\d+)?", odds_element.get_text(" ", strip=True))
    return float(match.group()) if match else 0.0


def scrape_forebet(target_day):
    url = TARGET_URLS.get(target_day, TARGET_URLS["today"])
    html_content = asyncio.run(fetch_full_html(url))
    soup = BeautifulSoup(html_content, "html.parser")

    rows = soup.select(
        "div.rcnt, div[class*='rcnt'], .schema-row, tr.schema-row, tr[class*='schema']"
    )

    print(f"Total raw match rows parsed from DOM: {len(rows)}")

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
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub secrets."
        )

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
            coef_str = (
                f"{item['coefficient']:.2f}"
                if item["coefficient"] > 0
                else "N/A"
            )
            lines.append(f"• {item['home']} vs {item['away']}")
            lines.append(
                f"  Pick: {item['pick']} ({item['probability']}%) | COEF: {coef_str}\n"
            )

        message = "\n".join(lines)
    else:
        message = f"No matches found for {target_day.upper()} matching ≥ {MINIMUM_PROBABILITY}% probability criteria."

    send_telegram_message(message)
    print(f"Telegram message sent. Total matches selected: {len(picks)}")
