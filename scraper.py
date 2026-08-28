import os
import re
import sys

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TARGET_URLS = {
    "today": "https://www.forebet.com/en/football-tips-and-predictions-for-today",
    "tomorrow": "https://www.forebet.com/en/football-tips-and-predictions-for-tomorrow",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def get_number(text):
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def scrape_forebet(target_day):
    url = TARGET_URLS.get(target_day, TARGET_URLS["today"])

    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"Could not load Forebet: {error}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select(".rcnttr, .schema-row, div[class*='rcnttr']")

    if not rows:
        title = soup.title.get_text(strip=True) if soup.title else "Unknown"
        print("No match rows found.")
        print(f"Page title received: {title}")
        return []

    results = []

    for row in rows:
        home_element = row.select_one(".homeTeam")
        away_element = row.select_one(".awayTeam")
        probability_elements = row.select(".tr_probabilities span")

        if not home_element or not away_element or len(probability_elements) < 3:
            continue

        home_probability = get_number(
            probability_elements[0].get_text(" ", strip=True)
        )
        away_probability = get_number(
            probability_elements[2].get_text(" ", strip=True)
        )

        if home_probability is None or away_probability is None:
            continue

        if home_probability < 75 and away_probability < 75:
            continue

        if home_probability >= away_probability:
            pick = "Home win (1)"
            highest_probability = home_probability
        else:
            pick = "Away win (2)"
            highest_probability = away_probability

        odds_element = row.select_one(".forebet_odds")
        coefficient = 0.0

        if odds_element:
            coefficient_found = get_number(
                odds_element.get_text(" ", strip=True)
            )
            if coefficient_found is not None:
                coefficient = coefficient_found

        results.append(
            {
                "home": home_element.get_text(" ", strip=True),
                "away": away_element.get_text(" ", strip=True),
                "pick": pick,
                "probability": highest_probability,
                "coefficient": coefficient,
            }
        )

    return sorted(
        results,
        key=lambda item: item["coefficient"],
        reverse=True,
    )


def send_telegram_message(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub secrets."
        )

    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    for start in range(0, len(message), 4000):
        message_part = message[start:start + 4000]

        try:
            response = requests.post(
                telegram_url,
                json={
                    "chat_id": CHAT_ID,
                    "text": message_part,
                },
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise RuntimeError(
                f"Telegram message failed: {response.text}"
            ) from error


if __name__ == "__main__":
    target_day = sys.argv[1].lower() if len(sys.argv) > 1 else "today"

    if target_day not in TARGET_URLS:
        target_day = "today"

    picks = scrape_forebet(target_day)

    if picks:
        lines = [
            f"⚽ Forebet picks for {target_day.upper()}",
            "Filter: home or away win probability of 75% or more",
            "",
        ]

        for item in picks:
            coefficient = (
                f"{item['coefficient']:.2f}"
                if item["coefficient"] > 0
                else "N/A"
            )

            lines.append(f"• {item['home']} vs {item['away']}")
            lines.append(
                f"  Pick: {item['pick']} — "
                f"{item['probability']:.0f}% | COEF: {coefficient}"
            )
            lines.append("")

        message = "\n".join(lines)
    else:
        message = (
            f"No matches found for {target_day.upper()} "
            "with a win probability of 75% or more."
        )

    send_telegram_message(message)
    print("Telegram message sent successfully.")
