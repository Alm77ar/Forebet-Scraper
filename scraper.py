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

    print(f"Requesting Cloudflare clearance via FlareSolverr: {url}")
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
                "--disable-dev-shm-usage",
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
            viewport={"width": 1440, "height": 900},
        )
        await context.add_cookies(pw_cookies)

        # Mask automation footprint to allow sub-resource AJAX calls through Cloudflare
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        page = await context.new_page()

        print(f"Navigating to match table: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)

        # Wait for dynamic matches to hydrate into the DOM
        try:
            await page.wait_for_function(
                "document.querySelectorAll('.homeTeam, [class*=\"homeTeam\"]').length > 10",
                timeout=20000,
            )
            print("Match list hydrated successfully.")
        except Exception:
            print("Warning: Initial hydration timeout reached. Continuing with incremental scroll...")

        # Incremental step scroll to trigger IntersectionObserver and scroll listeners
        print("Executing incremental step scrolling...")
        last_count = 0
        stagnant_steps = 0

        for step in range(30):
            await page.evaluate("window.scrollBy(0, 800);")
            await page.wait_for_timeout(600)

            # Click any dynamic pagination or 'More' buttons if present
            try:
                more_btn = await page.query_selector(
                    "#btn_more, .schema-more, a[id*='more'], button[id*='more']"
                )
                if more_btn and await more_btn.is_visible():
                    await more_btn.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            current_count = await page.locator(".homeTeam, [class*='homeTeam']").count()

            if current_count == last_count and current_count > 15:
                stagnant_steps += 1
                if stagnant_steps >= 5:
                    print(f"Scroll complete: Captured {current_count} matches.")
                    break
            else:
                stagnant_steps = 0
                last_count = current_count

        content = await page.content()
        await browser.close()
        return content


def extract_probabilities_from_container(container):
    # 1. Target dedicated outcome percentage elements (.fprt or forebet_p1/p2/p3)
    fprt_elements = container.select(".fprt, .forebet_p1, .forebet_p2, .forebet_p3, [class*='prob']")
    if fprt_elements:
        combined_text = " ".join([el.get_text(" ", strip=True) for el in fprt_elements])
        numbers = [int(n) for n in re.findall(r"\b\d{1,3}\b", combined_text)]
        for i in range(len(numbers) - 2):
            h, d, a = numbers[i], numbers[i + 1], numbers[i + 2]
            if 90 <= (h + d + a) <= 110:
                return h, d, a

    # 2. Fallback: Strip non-probability elements and parse clean numerical content
    clean_copy = BeautifulSoup(str(container), "html.parser")
    for tag_name in [
        ".homeTeam", ".awayTeam", "[class*='homeTeam']", "[class*='awayTeam']",
        ".forebet_odds", "[class*='odds']", ".l_score", "[class*='score']",
        ".st-time", "[class*='time']", ".date", "[class*='date']", "a"
    ]:
        for tag in clean_copy.select(tag_name):
            tag.decompose()

    numbers = [int(n) for n in re.findall(r"\b\d{1,3}\b", clean_copy.get_text(" ", strip=True))]
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
    raw_detected_count = len(home_elements)

    results = []
    seen_matches = set()
    validated_count = 0

    for home_el in home_elements:
        # Ascend DOM until finding nearest parent containing awayTeam, probabilities, and only 1 match
        curr = home_el.parent
        row_container = None
        probabilities = None

        while curr and curr.name not in ["html", "body"]:
            away_el = curr.select_one(".awayTeam, [class*='awayTeam']")
            if away_el:
                homes_in_curr = curr.select(".homeTeam, [class*='homeTeam']")
                if len(homes_in_curr) == 1:
                    probs = extract_probabilities_from_container(curr)
                    if probs is not None:
                        row_container = curr
                        probabilities = probs
                        break
            curr = curr.parent

        if not row_container or probabilities is None:
            continue

        home_element = row_container.select_one(".homeTeam, [class*='homeTeam']")
        away_element = row_container.select_one(".awayTeam, [class*='awayTeam']")

        if not home_element or not away_element:
            continue

        home_team = home_element.get_text(" ", strip=True)
        away_team = away_element.get_text(" ", strip=True)

        match_key = (home_team, away_team)
        if match_key in seen_matches:
            continue

        seen_matches.add(match_key)
        validated_count += 1

        home_prob, draw_prob, away_prob = probabilities

        if home_prob < MINIMUM_PROBABILITY and away_prob < MINIMUM_PROBABILITY:
            continue

        if home_prob >= away_prob:
            pick = "1 — Home win"
            prob = home_prob
        else:
            pick = "2 — Away win"
            prob = away_prob

        results.append(
            {
                "home": home_team,
                "away": away_team,
                "pick": pick,
                "probability": prob,
                "coefficient": get_coefficient(row_container),
            }
        )

    stats = {
        "raw_detected": raw_detected_count,
        "validated_parsed": validated_count,
        "selected_picks": len(results),
    }

    sorted_results = sorted(
        results,
        key=lambda item: (item["probability"], item["coefficient"]),
        reverse=True,
    )

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
