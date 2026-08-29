import os
import re
import sys
import html
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

# --- Tunables ---
HYDRATION_TIMEOUT_MS = 40000
MAX_SCROLL_STEPS = 45
SCROLL_PAUSE_MS = 700
STAGNATION_STEPS_REQUIRED = 10
DEBUG_DIR = "debug_artifacts"


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


async def click_all_more_buttons(page):
    clicked = 0
    buttons = await page.query_selector_all(
        "#btn_more, .schema-more, a[id*='more'], button[id*='more'], "
        "span[onclick*='ltodrows']"
    )
    for btn in buttons:
        try:
            if await btn.is_visible():
                await btn.click(timeout=2000)
                clicked += 1
                await page.wait_for_timeout(600)
        except Exception:
            continue
    return clicked


async def fetch_full_html(url, run_label="run"):
    cookies, user_agent = get_flaresolverr_clearance(url)

    os.makedirs(DEBUG_DIR, exist_ok=True)
    step_counts = []

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

        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        page = await context.new_page()

        print(f"Navigating to match table: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)

        try:
            await page.screenshot(path=f"{DEBUG_DIR}/{run_label}_00_initial.png", full_page=True, timeout=60000)
        except Exception as e:
            print(f"Warning: initial debug screenshot failed/timed out, continuing anyway: {e}")

        try:
            await page.wait_for_function(
                "document.querySelectorAll('.homeTeam, [class*=\"homeTeam\"]').length > 10",
                timeout=HYDRATION_TIMEOUT_MS,
            )
            print("Match list hydrated successfully.")
        except Exception:
            print("Warning: Initial hydration timeout reached. Continuing with incremental scroll...")

        print("Executing incremental step scrolling...")
        last_count = 0
        stagnant_steps = 0

        for step in range(MAX_SCROLL_STEPS):
            await page.evaluate("window.scrollBy(0, 800);")
            await page.wait_for_timeout(SCROLL_PAUSE_MS)

            clicked = await click_all_more_buttons(page)

            current_count = await page.locator(".homeTeam, [class*='homeTeam']").count()
            step_counts.append(current_count)

            print(
                f"  step {step:02d}: matches_detected={current_count} "
                f"more_buttons_clicked={clicked} stagnant_steps={stagnant_steps}"
            )

            if current_count == last_count and current_count > 15:
                stagnant_steps += 1
                if stagnant_steps >= STAGNATION_STEPS_REQUIRED:
                    print(f"Scroll complete: Captured {current_count} matches "
                          f"(stagnant for {STAGNATION_STEPS_REQUIRED} steps).")
                    break
            else:
                stagnant_steps = 0
                last_count = current_count
        else:
            print(f"Reached MAX_SCROLL_STEPS ({MAX_SCROLL_STEPS}) without full stagnation; "
                  f"stopping with {last_count} matches.")

        try:
            await page.screenshot(path=f"{DEBUG_DIR}/{run_label}_01_final.png", full_page=True, timeout=90000)
        except Exception as e:
            print(f"Warning: full-page final screenshot failed ({e}), trying viewport fallback...")
            try:
                await page.screenshot(path=f"{DEBUG_DIR}/{run_label}_01_final.png", full_page=False, timeout=30000)
            except Exception as e2:
                print(f"Warning: viewport screenshot failed, skipping final screenshot: {e2}")

        content = await page.content()
        with open(f"{DEBUG_DIR}/{run_label}_final.html", "w", encoding="utf-8") as f:
            f.write(content)
        with open(f"{DEBUG_DIR}/{run_label}_step_counts.txt", "w") as f:
            f.write("step,matches_detected\n")
            for i, c in enumerate(step_counts):
                f.write(f"{i},{c}\n")

        await browser.close()
        return content


def extract_probabilities_from_container(container):
    fprt_elements = container.select(".fprt, .forebet_p1, .forebet_p2, .forebet_p3, [class*='prob']")
    if fprt_elements:
        combined_text = " ".join([el.get_text(" ", strip=True) for el in fprt_elements])
        numbers = [int(n) for n in re.findall(r"\b\d{1,3}\b", combined_text)]
        for i in range(len(numbers) - 2):
            h, d, a = numbers[i], numbers[i + 1], numbers[i + 2]
            if 90 <= (h + d + a) <= 110:
                return h, d, a

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


def extract_match_meta(container):
    flag_code = ""
    img_el = container.select_one("img.flsc")
    if img_el and img_el.get("src"):
        match = re.search(r"/fc/([a-zA-Z0-9_-]+)\.png", img_el["src"])
        if match:
            flag_code = match.group(1)

    tag_el = container.select_one(".shortTag")
    league_tag = tag_el.get_text(strip=True) if tag_el else ""

    date_el = container.select_one(".date_bah")
    match_datetime = date_el.get_text(strip=True) if date_el else ""

    match_url = ""
    link_el = container.select_one("a.tnmscn")
    if link_el and link_el.get("href"):
        href = link_el["href"]
        match_url = href if href.startswith("http") else f"https://www.forebet.com{href}"

    return flag_code, league_tag, match_datetime, match_url


def flag_emoji(code):
    if not code or len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + (ord(c.upper()) - ord("A"))) for c in code)


def get_coefficient(row):
    odds_element = row.select_one(".lscrsp:not(.lcurodd)")
    if not odds_element:
        return 0.0

    text = odds_element.get_text(strip=True)

    if not text or text in ("-", "no"):
        return 0.0

    american_match = re.fullmatch(r"[+-]\d+", text)
    if american_match:
        value = int(text)
        if value > 0:
            return round((value / 100) + 1, 2)
        else:
            return round((100 / abs(value)) + 1, 2)

    decimal_match = re.fullmatch(r"\d+(?:\.\d+)?", text)
    if decimal_match:
        return float(text)

    return 0.0


def parse_h2h_letters(page_html, candidate_team_name, max_entries=5):
    soup = BeautifulSoup(page_html, "html.parser")

    h2h_module = None
    for module in soup.select(".moduletable"):
        title_el = module.select_one(".mptlt")
        if title_el and "head to head" in title_el.get_text(strip=True).lower():
            h2h_module = module
            break

    if not h2h_module:
        return ""

    rows = h2h_module.select(".st_rmain > .st_row")
    candidate_norm = candidate_team_name.strip().lower()
    letters = []

    for row in rows[:max_entries]:
        hteam_el = row.select_one(".st_hteam a")
        ateam_el = row.select_one(".st_ateam a")
        score_el = row.select_one(".st_rescnt .st_res")
        if not hteam_el or not ateam_el or not score_el:
            continue

        hteam = hteam_el.get_text(strip=True)
        ateam = ateam_el.get_text(strip=True)
        score_match = re.match(r"(\d+)\s*-\s*(\d+)", score_el.get_text(strip=True))
        if not score_match:
            continue
        home_goals, away_goals = int(score_match.group(1)), int(score_match.group(2))

        if hteam.strip().lower() == candidate_norm:
            letters.append("W" if home_goals > away_goals else "L" if home_goals < away_goals else "T")
        elif ateam.strip().lower() == candidate_norm:
            letters.append("W" if away_goals > home_goals else "L" if away_goals < home_goals else "T")

    return " ".join(letters)


async def fetch_h2h_for_picks(picks):
    if not picks:
        return {}

    sample_url = picks[0]["match_url"]
    cookies, user_agent = get_flaresolverr_clearance(sample_url)
    h2h_map = {}

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
        context = await browser.new_context(user_agent=user_agent, viewport={"width": 1440, "height": 900})
        await context.add_cookies(pw_cookies)
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()

        for item in picks:
            url = item.get("match_url")
            if not url:
                continue
            try:
                print(f"Fetching H2H for: {item['home']} vs {item['away']}")
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(800)
                content = await page.content()
                h2h_map[url] = parse_h2h_letters(content, item["candidate_team"])
            except Exception as e:
                print(f"Warning: H2H fetch failed for {url}, leaving blank: {e}")
                h2h_map[url] = ""

        await browser.close()

    return h2h_map


def format_h2h_boxes(h2h_str):
    """
    Compact colored letter badges formatted with wider spacing for a dedicated line.
    """
    if not h2h_str:
        return ""
    
    box_map = {
        "W": "🟩W",
        "T": "🟨T",
        "L": "🟥L",
    }

    letters = h2h_str.split()
    return "  ".join([box_map.get(l, l) for l in letters])


def scrape_forebet(target_day):
    url = TARGET_URLS.get(target_day, TARGET_URLS["today"])
    html_content = asyncio.run(fetch_full_html(url, run_label=target_day))
    soup = BeautifulSoup(html_content, "html.parser")

    home_elements = soup.select(".homeTeam, [class*='homeTeam']")
    raw_detected_count = len(home_elements)

    results = []
    seen_matches = set()
    validated_count = 0
    skipped_no_container = 0

    for home_el in home_elements:
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
            skipped_no_container += 1
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
            candidate_team = home_team
        else:
            pick = "2 — Away win"
            prob = away_prob
            candidate_team = away_team

        flag_code, league_tag, match_datetime, match_url = extract_match_meta(row_container)

        results.append(
            {
                "home": home_team,
                "away": away_team,
                "pick": pick,
                "probability": prob,
                "coefficient": get_coefficient(row_container),
                "flag": flag_emoji(flag_code),
                "league_tag": league_tag,
                "datetime": match_datetime,
                "match_url": match_url,
                "candidate_team": candidate_team,
                "h2h": "",
            }
        )

    stats = {
        "raw_detected": raw_detected_count,
        "validated_parsed": validated_count,
        "skipped_no_container": skipped_no_container,
        "selected_picks": len(results),
    }

    if results:
        h2h_map = asyncio.run(fetch_h2h_for_picks(results))
        for item in results:
            item["h2h"] = h2h_map.get(item["match_url"], "")

    sorted_results = sorted(
        results,
        key=lambda item: (item["coefficient"], item["probability"]),
        reverse=True,
    )

    return sorted_results, stats


def probability_emoji(prob):
    if prob >= 90:
        return "🟢"
    if prob >= 80:
        return "🟡"
    return "🟠"


def send_telegram_message(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables.")

    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    blocks = message.split("\n\n")
    chunks = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > 3900:
            if current:
                chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)

    for chunk in chunks:
        response = requests.post(
            telegram_url,
            json={
                "chat_id": CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        response.raise_for_status()


if __name__ == "__main__":
    target_day = sys.argv[1].lower() if len(sys.argv) > 1 else "today"
    if target_day not in TARGET_URLS:
        target_day = "today"

    picks, stats = scrape_forebet(target_day)

    lines = [
        f"⚽ <b> picks for {target_day.upper()}</b>",
        f"<i>Filter: Win probability ≥ {MINIMUM_PROBABILITY}%</i>\n",
    ]

    if picks:
        for item in picks:
            coef_str = f"{item['coefficient']:.2f}" if item["coefficient"] > 0 else "N/A"
            dot = probability_emoji(item["probability"])
            home = html.escape(item["home"])
            away = html.escape(item["away"])
            pick_text = html.escape(item["pick"])
            
            # Metadata header formatting
            meta_parts = [b for b in [item["flag"], html.escape(item["league_tag"]), html.escape(item["datetime"])] if b]
            meta_str = " • ".join(meta_parts)
            
            if meta_str:
                lines.append(f"{dot} {meta_str}")
            else:
                lines.append(f"{dot}")
                
            lines.append(f"<b>{home} vs {away}</b>")
            lines.append(f"🎯 Pick: <b>{pick_text}</b> ({item['probability']}%) | 📈 Coef: <code>{coef_str}</code>")
            
            if item.get("h2h"):
                candidate = html.escape(item["candidate_team"])
                h2h_formatted = format_h2h_boxes(item["h2h"])
                # Label on top, formatted badges isolated on their own line below
                lines.append(f"📊 H2H ({candidate}):")
                lines.append(f"{h2h_formatted}\n")
            else:
                lines.append("")
    else:
        lines.append("No matches found matching criteria.\n")

    lines.append("---")
    lines.append("📊 <b>Validation Diagnostics:</b>")
    lines.append(f"• Match nodes detected in DOM: {stats['raw_detected']}")
    lines.append(f"• Validated match rows parsed: {stats['validated_parsed']}")
    lines.append(f"• Rows skipped (no valid container found): {stats['skipped_no_container']}")
    lines.append(f"• Picks meeting criteria (≥{MINIMUM_PROBABILITY}%): {stats['selected_picks']}")

    message = "\n".join(lines)
    send_telegram_message(message)
    print("Telegram notification dispatched successfully.")
    print(f"Debug artifacts (screenshots, final HTML, step-by-step counts) saved to ./{DEBUG_DIR}/")
