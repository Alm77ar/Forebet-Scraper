import os
import re
import sys

from bs4 import BeautifulSoup
from curl_cffi import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TARGET_URLS = {
    "today": "https://www.forebet.com/en/football-tips-and-predictions-for-today",
    "tomorrow": "https://www.forebet.com/en/football-tips-and-predictions-for-tomorrow",
}

MINIMUM_PROBABILITY = 75


def numbers_in_text(text):
    return [
        int(value)
        for value in re.findall(r"(?<![\d.])(\d{1,3})(?![\d.])", text)
    ]


def get_probabilities(row):
    """
    Forebet values are ordered:
    1 = home win, X = draw, 2 = away win.
    """
    containers = row.select(
        ".tr_probabilities, "
        "[class*='probabilities'], "
        "[class*='probability'], "
        "[class*='prob']"
    )

    for container in containers:
        values = numbers_in_text(container.get_text(" ", strip=True))

        if len(values) == 3 and 95 <= sum(values) <= 105:
            return values[0], values[1], values[2]

    # Backup if Forebet changes the class name.
    for element in row.find_all(["div", "span", "td"]):
        values = numbers_in_text(element.get_text(" ", strip=True))

        if len(values) == 3 and 95 <= sum(values) <= 105:
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

    try:
        # impersonate="chrome124" spoofs desktop browser TLS fingerprints to bypass 403 blocks
        response = requests.get(url, impersonate="chrome124", timeout=30)
        response.raise_for_status()

    except Exception as error:
        raise RuntimeError(f"Could not connect to Forebet: {error}") from error

    soup = BeautifulSoup(response.text, "html.parser")

    rows = soup.select(
        "div.rcnt, "
        "div[class*='rcnt'], "
        ".schema-row, "
        "tr.schema-row"
    )

    print(f"Potential Forebet match rows found: {len(rows)}")

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

        home_probability, draw_probability, away_probability = probabilities

        # Only check 1 (home) and 2 (away). Ignore X (draw).
        if (
            home_probability < MINIMUM_PROBABILITY
            and away_probability < MINIMUM_PROBABILITY
        ):
            continue

        if home_probability >= away_probability:
            pick = "1 — Home win"
            probability = home_probability
        else:
            pick = "2 — Away win"
            probability = away_probability

        match_key = (home_team, away_team)

        if match_key in seen_matches:
            continue

        seen_matches.add(match_key)

        results.append(
            {
                "home": home_team,
                "away": away_team,
                "pick": pick,
                "probability": probability,
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
            json={
                "chat_id": CHAT_ID,
                "text": message[start:start + 4000],
            },
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
            f"Filter: column 1 or 2 must be at least {MINIMUM_PROBABILITY}%",
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
                f"  Pick: {item['pick']} "
                f"({item['probability']}%) | COEF: {coefficient}"
            )
            lines.append("")

        message = "\n".join(lines)
    else:
        message = (
            f"No matches found for {target_day.upper()} where "
            f"column 1 or 2 is at least {MINIMUM_PROBABILITY}%."
        )

    send_telegram_message(message)
    print(f"Telegram message sent. Matches selected: {len(picks)}")
