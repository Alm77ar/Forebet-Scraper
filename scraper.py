import os
import sys
import html
import re
import asyncio
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ==========================================
# 1. HELPER & NORMALIZATION FUNCTIONS
# ==========================================

def _normalize_team_name(name: str) -> str:
    """
    Strips punctuation and standardizes spaces to catch variations
    like 'Al-Nassr' vs 'Al Nassr'.
    """
    if not name:
        return ""
    name = name.strip().lower()
    name = re.sub(r"[-_.]", " ", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def send_telegram_sync(bot_token: str, chat_id: str, message: str) -> bool:
    """
    Synchronous Telegram dispatcher using the 'requests' library.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": html.unescape(message),
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Telegram dispatch error: {e}", file=sys.stderr)
        return False


# ==========================================
# 2. FOREBET PARSING LOGIC
# ==========================================

def parse_h2h_letters(
    page_html: str,
    candidate_team_name: str,
    opponent_team_name: str = "",
    debug_label: str = "",
    max_results: int = 8,
) -> str:
    """
    Parses Forebet Head-to-Head rows into W/L/T letters.
    Handles missing <a> tags, score string variations, and opponent name fallbacks.
    """
    soup = BeautifulSoup(page_html, "html.parser")

    # Locate H2H container
    h2h_module = None
    for module in soup.select(".moduletable"):
        title_el = module.select_one(".mptlt")
        if title_el and "head to head" in title_el.get_text(strip=True).lower():
            h2h_module = module
            break

    if not h2h_module:
        return ""

    rmain = h2h_module.select_one(".st_rmain")
    rows = rmain.select(".st_row") if rmain else []
    if not rows:
        return ""

    candidate_norm = _normalize_team_name(candidate_team_name)
    opponent_norm = _normalize_team_name(opponent_team_name) if opponent_team_name else ""
    letters = []

    for row in rows:
        if len(letters) >= max_results:
            break

        # Extract text directly from wrapper elements (bypasses missing <a> tags)
        hteam_el = row.select_one(".st_hteam")
        ateam_el = row.select_one(".st_ateam")
        score_el = row.select_one(".st_rescnt .st_res")

        if not hteam_el or not ateam_el or not score_el:
            continue

        hteam = hteam_el.get_text(strip=True)
        ateam = ateam_el.get_text(strip=True)

        # Match numbers safely, accounting for extra text like '(AET)'
        score_match = re.search(r"(\d+)\s*-\s*(\d+)", score_el.get_text(strip=True))
        if not score_match:
            continue

        home_goals, away_goals = int(score_match.group(1)), int(score_match.group(2))

        hteam_norm = _normalize_team_name(hteam)
        ateam_norm = _normalize_team_name(ateam)

        # Check candidate match
        cand_is_home = candidate_norm == hteam_norm or candidate_norm in hteam_norm or hteam_norm in candidate_norm
        cand_is_away = candidate_norm == ateam_norm or candidate_norm in ateam_norm or ateam_norm in candidate_norm

        # Fallback check against opponent
        opp_is_home = opponent_norm == hteam_norm or opponent_norm in hteam_norm or hteam_norm in opponent_norm
        opp_is_away = opponent_norm == ateam_norm or opponent_norm in ateam_norm or ateam_norm in opponent_norm

        is_home = cand_is_home or opp_is_away
        is_away = cand_is_away or opp_is_home

        if is_home and not is_away:
            letters.append("W" if home_goals > away_goals else "L" if home_goals < away_goals else "T")
        elif is_away and not is_home:
            letters.append("W" if away_goals > home_goals else "L" if away_goals < home_goals else "T")

    return " ".join(letters)


# ==========================================
# 3. ASYNC SCRAPING ENGINE
# ==========================================

async def fetch_h2h_for_picks(page, picks: list) -> list:
    """
    Navigates through pick URLs using Playwright and attaches parsed H2H outcomes.
    """
    results = []

    for item in picks:
        label = f"{item['home']} vs {item['away']}"
        h2h_url = item.get("h2h_url")

        if not h2h_url:
            item["h2h"] = ""
            results.append(item)
            continue

        print(f"Fetching H2H data for: {label}...")
        h2h_result = ""

        for attempt in range(1, 4):
            try:
                await page.goto(h2h_url, wait_until="domcontentloaded", timeout=15000)
                content = await page.content()

                if "Bad gateway" in content:
                    print(f"  [{label}] Attempt {attempt}: Forebet transient 'Bad gateway'. Retrying...")
                    if attempt < 3:
                        await page.wait_for_timeout(3000)
                        continue

                opponent = item["away"] if item["candidate_team"] == item["home"] else item["home"]
                
                h2h_result = parse_h2h_letters(
                    content,
                    item["candidate_team"],
                    opponent_team_name=opponent,
                    debug_label=label,
                )

                if h2h_result or attempt == 3:
                    break

            except Exception as e:
                print(f"  [{label}] Attempt {attempt} encountered error: {e}", file=sys.stderr)
                if attempt < 3:
                    await page.wait_for_timeout(2000)

        item["h2h"] = h2h_result
        results.append(item)

    return results


# ==========================================
# 4. ENTRYPOINT
# ==========================================

async def main():
    # Read tokens from environment variables or define fallbacks
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

    sample_picks = [
        {
            "home": "Al Nassr",
            "away": "Al Hilal",
            "candidate_team": "Al Nassr",
            "h2h_url": "https://www.forebet.com/en/head-to-head/al-nassr-v-al-hilal",
        }
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        processed_picks = await fetch_h2h_for_picks(page, sample_picks)
        await browser.close()

    for pick in processed_picks:
        msg = (
            f"<b>Match:</b> {pick['home']} vs {pick['away']}\n"
            f"<b>Target:</b> {pick['candidate_team']}\n"
            f"<b>H2H:</b> {pick['h2h'] if pick['h2h'] else 'No records found'}"
        )
        print(f"\n--- Output ---\n{msg}\n")
        
        # Uncomment to dispatch via requests:
        # send_telegram_sync(bot_token, chat_id, msg)


if __name__ == "__main__":
    asyncio.run(main())
