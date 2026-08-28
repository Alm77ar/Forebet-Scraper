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

    print(f"Obtaining Cloudflare clearance via FlareSolverr for: {url}")
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

        print(f"Navigating to URL: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)

        print("Scrolling full page to load all dynamic content...")
        previous_height = 0
        for step in range(15):
            current_height = await page.evaluate("document.body.scrollHeight")
            if current_height == previous_height and step > 4:
                break
            previous_height = current_height
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)

        content = await page.content()
        await browser.close()
        return content


def get_probabilities(row):
    # 1. Direct target of Forebet probability elements if available
    p1 = row.select_one(".forebet_p1, [class*='p1']")
    p2 = row.select_one(".forebet_p2, [class*='p2']")
    p3 = row.select_one(".forebet_p3, [class*='p3']")

    if p1 and p2 and p3:
        try:
            h = int(re.search(r"\d+", p1.get_text()).group())
            d = int(re.search(r"\d+", p2.get_text()).group())
            a = int(re.search(r"\d+", p3.get_text()).group())
            if 90 <= (h + d + a) <= 110:
                return h, d, a
        except (ValueError, AttributeError):
            pass

    # 2. Fallback: Strip team names from row copy to prevent digits in team names (e.g. "Mainz 05") from skewing calculations
    row_copy = BeautifulSoup(str(row), "html.parser")
    for team_tag in row_copy.select(".homeTeam, .awayTeam, [class*='homeTeam'], [class*='awayTeam']"):
        team_tag.decompose()

    numbers = [int(n) for n in re.findall(r"\b\d{1,3}\b", row_copy.get_text(" ", strip=True))]

    # Find 3 consecutive numbers summing to ~100%
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

    home_elements = soup.select(".homeTeam, [class*='homeTeam']")
    match_rows = []

    for home_el in home_elements:
        # Stop at the immediate parent row tag (tr, div, or li) containing awayTeam
        row = home_el.find_parent(
            lambda tag: tag.name in ["tr", "div", "li"]
            and tag.select_one(".awayTeam, [class*='awayTeam']") is not None
        )
        if row and row not in match_rows:
            # Confirm element is a single row wrapper
            if len(row.select(".homeTeam, [class*='homeTeam']")) == 1:
                match_rows.append(row)

    print(f"Total match rows parsed: {len(match_rows)}")

    results = []
    seen_matches = set()

    for row in match_rows:
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
