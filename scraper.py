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

    print(f"Requesting Cloudflare session via FlareSolverr: {url}")
    response = requests.post(FLARESOLVERR_URL, json=payload, headers=headers, timeout=70)
    response.raise_for_status()

    data = response.json()
    if data.get("status") == "ok":
        solution = data["solution"]
        return solution.get("cookies", []), solution.get("userAgent", "")

    raise RuntimeError(f"FlareSolverr failed: {data.get('message')}")


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

        print(f"Navigating to: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)

        try:
            await page.wait_for_selector(".homeTeam, [class*='homeTeam']", timeout=15000)
        except Exception:
            print("Warning: Selector timeout reached.")

        print("Scrolling page dynamically until match loading completes...")
        previous_count = 0
        no_change_iterations = 0

        for step in range(35):
            await page.evaluate("window.scrollBy(0, 1500)")
            await page.wait_for_timeout(800)

            # Click any load-more elements if present
            load_more = await page.query_selector("a#loadMore, .load-more, #btn_load_more")
            if load_more and await load_more.is_visible():
                try:
                    await load_more.click()
                    await page.wait_for_timeout(1200)
                except Exception:
                    pass

            current_count = await page.locator(".homeTeam, [class*='homeTeam']").count()
            if current_count == previous_count and current_count > 0:
                no_change_iterations += 1
                if no_change_iterations >= 4:
                    print(f"Match loading completed at {current_count} total matches.")
                    break
            else:
                no_change_iterations = 0
                previous_count = current_count

        content = await page.content()
        await browser.close()
        return content


def get_probabilities(row):
    # 1. Primary check on designated probability containers
    prob_element = row.select_one(".fprt, .tr_probabilities, .predict-probabilities, [class*='prob']")
    if prob_element:
        text = prob_element.get_text(" ", strip=True)
        nums = [int(n) for n in re.findall(r"\b\d{1,3}\b", text)]
        for i in range(len(nums) - 2):
            h, d, a = nums[i], nums[i + 1], nums[i + 2]
            if 90 <= (h + d + a) <= 110:
                return h, d, a

    # 2. Sanitized fallback check across the container
    row_copy = BeautifulSoup(str(row), "html.parser")
    for noisy_selector in [
        ".homeTeam", ".awayTeam", "[class*='homeTeam']", "[class*='awayTeam']",
        ".forebet_odds", "[class*='odds']", ".l_score", "[class*='score']",
        ".st-time", "[class*='time']", ".date", "[class*='date']"
    ]:
        for tag in row_copy.select(noisy_selector):
            tag.decompose()

    numbers = [int(n) for n in re.findall(r"\b\d{1,3}\b", row_copy.get_text(" ", strip=True))]
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
        curr = home_el.parent
        row_container = None

        # Ascend DOM hierarchy to find the complete row container that includes probability data
        while curr and curr.name not in ["body", "html"]:
            if curr.select_one(".awayTeam, [class*='awayTeam']"):
                homes = curr.select(".homeTeam, [class*='homeTeam']")
                aways = curr.select(".awayTeam, [class*='awayTeam']")
                if len(homes) == 1 and len(aways) == 1:
                    if get_probabilities(curr) is not None:
                        row_container = curr
                        break
            curr = curr.parent

        if row_container and row_container not in match_rows:
            match_rows.append(row_container)

    stats = {
        "raw_detected": len(home_elements),
        "validated_parsed": len(match_rows),
        "selected_picks": 0,
    }

    print(f"DOM Analysis: {stats['raw_detected']} match nodes found, {stats['validated_parsed']} full rows validated.")

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

    sorted_results = sorted(
        results,
        key=lambda item: (item["probability"], item["coefficient"]),
        reverse=True,
    )

    stats["selected_picks"] = len(sorted_results)
    return sorted_results, stats


def send_telegram_message(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables.")

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

    picks, stats = scrape_forebet(target_day)

    lines = [
        f"⚽ Forebet picks for {target_day.upper()}",
        f"Filter: Home or Away win probability ≥ {MINIMUM_PROBABILITY}%\n",
    ]

    if picks:
        for item in picks:
            coef_str = f"{item['coefficient']:.2f}" if item["coefficient"] > 0 else "N/A"
            lines.append(f"• {item['home']} vs {item['away']}")
            lines.append(f"  Pick: {item['pick']} ({item['probability']}%) | COEF: {coef_str}\n")
    else:
        lines.append("No matches found matching criteria.\n")

    lines.append("---")
    lines.append("📊 Validation Diagnostics:")
    lines.append(f"• Match nodes detected in DOM: {stats['raw_detected']}")
    lines.append(f"• Validated match rows parsed: {stats['validated_parsed']}")
    lines.append(f"• Picks meeting criteria (≥{MINIMUM_PROBABILITY}%): {stats['selected_picks']}")

    message = "\n".join(lines)
    send_telegram_message(message)
    print("Telegram notification dispatched successfully.")
