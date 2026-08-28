import os
import sys
import asyncio
from playwright.async_api import async_playwright
import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TARGET_URLS = {
    "today": "https://www.forebet.com/en/football-tips-and-predictions-for-today",
    "tomorrow": "https://www.forebet.com/en/football-tips-and-predictions-for-tomorrow"
}

async def scrape_forebet(target_day: str):
    url = TARGET_URLS.get(target_day, TARGET_URLS["today"])
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            # Navigate with networkidle wait
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Flexible selector strategy for match rows
            row_selector = ".rcnttr, .schema-row, div[class*='rcnttr']"
            await page.wait_for_selector(row_selector, timeout=30000)

            rows = await page.query_selector_all(row_selector)

            for row in rows:
                try:
                    # Extract Teams
                    home_elem = await row.query_selector(".homeTeam")
                    away_elem = await row.query_selector(".awayTeam")
                    if not home_elem or not away_elem:
                        continue
                    
                    home_team = (await home_elem.inner_text()).strip()
                    away_team = (await away_elem.inner_text()).strip()

                    # Extract Probabilities
                    prob_spans = await row.query_selector_all(".tr_probabilities span")
                    if len(prob_spans) < 3:
                        continue
                    
                    p_home = float((await prob_spans[0].inner_text()).strip())
                    p_away = float((await prob_spans[2].inner_text()).strip())

                    # Criteria: Home or Away >= 75%
                    if p_home >= 75.0 or p_away >= 75.0:
                        picked_side = "Home (1)" if p_home >= 75.0 else "Away (2)"
                        highest_prob = p_home if p_home >= 75.0 else p_away

                        # Extract Odds / COEF
                        odds_elem = await row.query_selector(".forebet_odds")
                        coef_val = 0.0
                        if odds_elem:
                            raw_text = (await odds_elem.inner_text()).strip().split()
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

        except Exception as e:
            print(f"Scraper encountered an error: {e}")
        finally:
            await browser.close()

    results.sort(key=lambda x: x["coef"], reverse=True)
    return results

def send_telegram_message(message: str):
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

    picks = asyncio.run(scrape_forebet(target_day))
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
