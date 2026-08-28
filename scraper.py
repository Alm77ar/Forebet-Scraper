import os
import sys
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TARGET_URLS = {
    "today": "https://www.forebet.com/en/football-tips-and-predictions-for-today",
    "tomorrow": "https://www.forebet.com/en/football-tips-and-predictions-for-tomorrow"
}

def scrape_forebet(target_day: str):
    url = TARGET_URLS.get(target_day, TARGET_URLS["today"])
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
    except Exception as e:
        print(f"HTTP Request failed: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select(".rcnttr, .schema-row, div[class*='rcnttr']")
    results = []

    for row in rows:
        try:
            home_elem = row.select_one(".homeTeam")
            away_elem = row.select_one(".awayTeam")
            if not home_elem or not away_elem:
                continue

            home_team = home_elem.get_text(strip=True)
            away_team = away_elem.get_text(strip=True)

            prob_spans = row.select(".tr_probabilities span")
            if len(prob_spans) < 3:
                continue

            p_home = float(prob_spans[0].get_text(strip=True))
            p_away = float(prob_spans[2].get_text(strip=True))

            # Criteria: Home or Away probability >= 75%
            if p_home >= 75.0 or p_away >= 75.0:
                picked_side = "Home (1)" if p_home >= 75.0 else "Away (2)"
                highest_prob = p_home if p_home >= 75.0 else p_away

                odds_elem = row.select_one(".forebet_odds")
                coef_val = 0.0
                if odds_elem:
                    raw_text = odds_elem.get_text(strip=True).split()
                    if raw_text:
                        try:
                            coef_val = float(raw_text[0])
                        except ValueError:
                            coef_val = 0.0

                results.append({
                    "home": home_team,
                    "away": away_team,
                    "pick": picked_side,
                    "prob": highest_prob,
                    "coef": coef_val
                })
        except Exception:
            continue

    # Sort descending by COEF
    results.sort(key=lambda x: x["coef"], reverse=True)
    return results

def send_telegram_message(message: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("Error: Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables.")
        return

    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    
    if len(message) > 4000:
        for i in range(0, len(message), 4000):
            payload = {"chat_id": CHAT_ID, "text": message[i:i+4000], "parse_mode": "Markdown"}
            requests.post(telegram_url, json=payload)
    else:
        payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(telegram_url, json=payload)

if __name__ == "__main__":
    target_day = sys.argv[1].lower() if len(sys.argv) > 1 else "today"
    if target_day not in ["today", "tomorrow"]:
        target_day = "today"

    picks = scrape_forebet(target_day)
    day_label = target_day.upper()

    if picks:
        lines = [f"⚽ *Forebet Matches for {day_label} (≥ 75% Prob sorted by COEF)*\n"]
        for item in picks:
            coef_str = f"{item['coef']:.2f}" if item['coef'] > 0 else "N/A"
            lines.append(
                f"• *{item['home']} vs {item['away']}*\n"
                f"  📊 Pick: *{item['pick']}* ({item['prob']}%) | 📈 COEF: *{coef_str}*\n"
            )
        msg = "\n".join(lines)
    else:
        msg = f"ℹ️ No matches found for *{day_label}* meeting the ≥ 75% probability criteria."

    send_telegram_message(msg)
