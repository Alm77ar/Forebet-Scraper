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

# --- Tunables pulled out so they're easy to bump without hunting through the code ---
HYDRATION_TIMEOUT_MS = 40000       # was 20000 - give slow AJAX more room
MAX_SCROLL_STEPS = 45              # was 30 - more room to reach the bottom
SCROLL_PAUSE_MS = 700              # was 600 - slightly gentler pacing
STAGNATION_STEPS_REQUIRED = 10     # was 5 - don't bail on a brief lull
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
    """
    Click EVERY visible 'load more' / pagination control on the page, not just
    the first one. Sites that group matches by league often render one such
    control per section, so query_selector (singular) silently misses all but
    the first section's button.

    IMPORTANT: Forebet's real pagination control is a bare <span> with an
    inline onclick handler calling their ltodrows(...) JS function - it has
    NO id or class, so generic id/class selectors never match it. We target
    that onclick pattern directly. The previous broad [class*='show-more'] /
    [class*='loadMore'] patterns were accidentally matching Google AdSense's
    "Discover more" content-recommendation widget instead (visible as stray
    popups in debug screenshots) - those have been removed.
    """
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
                await page.wait_for_timeout(600)  # ltodrows() fetches via AJAX, give it time
        except Exception:
            # Button may have detached from DOM after a previous click re-rendered
            # the list; that's fine, just move on to the next one.
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

        # Mask automation footprint to allow sub-resource AJAX calls through Cloudflare
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        page = await context.new_page()

        print(f"Navigating to match table: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)

        # Save what the page looked like immediately after the initial clearance-backed
        # load, before any of our own scrolling/clicking. If Cloudflare is soft-blocking
        # the hydration AJAX calls, this snapshot will already look "stuck".
        # Wrapped in try/except: a debug screenshot failing (e.g. timing out on an
        # unusually tall page) must never crash the actual scrape/send run.
        try:
            await page.screenshot(path=f"{DEBUG_DIR}/{run_label}_00_initial.png", full_page=True, timeout=60000)
        except Exception as e:
            print(f"Warning: initial debug screenshot failed/timed out, continuing anyway: {e}")

        # Wait for dynamic matches to hydrate into the DOM
        try:
            await page.wait_for_function(
                "document.querySelectorAll('.homeTeam, [class*=\"homeTeam\"]').length > 10",
                timeout=HYDRATION_TIMEOUT_MS,
            )
            print("Match list hydrated successfully.")
        except Exception:
            print("Warning: Initial hydration timeout reached. Continuing with incremental scroll...")

        # Incremental step scroll to trigger IntersectionObserver and scroll listeners
        print("Executing incremental step scrolling...")
        last_count = 0
        stagnant_steps = 0

        for step in range(MAX_SCROLL_STEPS):
            await page.evaluate("window.scrollBy(0, 800);")
            await page.wait_for_timeout(SCROLL_PAUSE_MS)

            clicked = await click_all_more_buttons(page)

            current_count = await page.locator(".homeTeam, [class*='homeTeam']").count()
            step_counts.append(current_count)

            # Per-step diagnostic logging so a stalled run is easy to diagnose
            # from the Action logs alone, without needing to reproduce locally.
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
                  f"stopping with {last_count} matches. Consider raising MAX_SCROLL_STEPS "
                  f"if this happens consistently.")

        # Final snapshot + raw HTML, saved regardless of outcome, so every run leaves
        # a trail you can inspect after the fact via the workflow's uploaded artifact.
        # This is where the crash happened: a full-page screenshot on a page that has
        # grown very tall (e.g. 1000+ matches after a real "load more" click) can take
        # far longer than Playwright's default 30s timeout. Same try/except pattern as
        # above - if it still fails even with the longer timeout, fall back to a
        # viewport-only (non-full-page) screenshot instead of giving up entirely, and
        # if even that fails, skip the screenshot rather than crash the whole run.
        try:
            await page.screenshot(path=f"{DEBUG_DIR}/{run_label}_01_final.png", full_page=True, timeout=90000)
        except Exception as e:
            print(f"Warning: full-page final screenshot failed/timed out ({e}), trying viewport-only fallback...")
            try:
                await page.screenshot(path=f"{DEBUG_DIR}/{run_label}_01_final.png", full_page=False, timeout=30000)
            except Exception as e2:
                print(f"Warning: viewport screenshot also failed, skipping final screenshot entirely: {e2}")

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


def extract_match_meta(container):
    """
    Pulls the three pieces needed for the message header line:
      - flag_code: 2-letter country code from the flag image filename
                   (e.g. .../images/fc/br.png -> "br"), converted to an
                   actual flag emoji below since Telegram can't embed an
                   inline logo image in a plain text message.
      - league_tag: the short competition code shown beneath the flag
                   on the site (e.g. "Br2").
      - match_datetime: the raw date/time string Forebet displays
                   (e.g. "08/28/2026 11:30 PM").
    Any piece that isn't found falls back to an empty string so a single
    missing field never breaks the whole row.
    """
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

    return flag_code, league_tag, match_datetime


def flag_emoji(code):
    """Converts a 2-letter country code into its Unicode flag emoji.
    Falls back to '' for codes that aren't a plain 2-letter alphabetic
    code (some competitions use confederation logos, not country flags)."""
    if not code or len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + (ord(c.upper()) - ord("A"))) for c in code)


def get_coefficient(row):
    """
    Forebet's "Coef." column renders as <span class="lscrsp">. There is a
    visually adjacent but DIFFERENT column, "Live coef.", which uses
    <span class="lscrsp lcurodd"> - same base class, extra modifier class.
    Selecting plain ".lscrsp" without excluding ".lcurodd" would grab
    whichever one BeautifulSoup finds first, silently mixing the two columns.
    ":not(.lcurodd)" excludes the live-odds variant explicitly.

    Also note: Forebet renders this column in EITHER American odds format
    (+155, -154) or decimal odds format (1.33) depending on locale/session -
    confirmed by comparing two different scrape runs. Both are normalized to
    decimal odds here so sorting/output stays consistent either way.
    """
    odds_element = row.select_one(".lscrsp:not(.lcurodd)")
    if not odds_element:
        return 0.0

    text = odds_element.get_text(strip=True)

    if not text or text in ("-", "no"):
        return 0.0

    # American odds format: +155 / -154
    american_match = re.fullmatch(r"[+-]\d+", text)
    if american_match:
        value = int(text)
        if value > 0:
            return round((value / 100) + 1, 2)
        else:
            return round((100 / abs(value)) + 1, 2)

    # Decimal odds format: 1.33
    decimal_match = re.fullmatch(r"\d+(?:\.\d+)?", text)
    if decimal_match:
        return float(text)

    return 0.0


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
        else:
            pick = "2 — Away win"
            prob = away_prob

        flag_code, league_tag, match_datetime = extract_match_meta(row_container)

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
            }
        )

    stats = {
        "raw_detected": raw_detected_count,
        "validated_parsed": validated_count,
        "skipped_no_container": skipped_no_container,
        "selected_picks": len(results),
    }

    # Sorted by coefficient first (highest payout first), probability as tiebreaker.
    sorted_results = sorted(
        results,
        key=lambda item: (item["coefficient"], item["probability"]),
        reverse=True,
    )

    return sorted_results, stats


def probability_emoji(prob):
    """Color-coded circle for the probability tier. Telegram messages can't
    render custom text colors (that's a platform limit, not a code choice),
    so colored emoji stand in as the visual cue instead."""
    if prob >= 90:
        return "🟢"
    if prob >= 80:
        return "🟡"
    return "🟠"


def send_telegram_message(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables.")

    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    # Split on blank-line boundaries (between picks) rather than a blind
    # character-count slice, so we never cut an HTML tag in half mid-chunk
    # and break formatting for the rest of that message part.
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
        f"⚽ <b>Forebet picks for {target_day.upper()}</b>",
        f"<i>Filter: Home or Away win probability ≥ {MINIMUM_PROBABILITY}%</i>\n",
    ]

    if picks:
        for item in picks:
            coef_str = f"{item['coefficient']:.2f}" if item["coefficient"] > 0 else "N/A"
            dot = probability_emoji(item["probability"])
            home = html.escape(item["home"])
            away = html.escape(item["away"])
            pick_text = html.escape(item["pick"])
            header_bits = [b for b in [item["flag"], html.escape(item["league_tag"]), html.escape(item["datetime"])] if b]
            if header_bits:
                lines.append(f"{dot} {' '.join(header_bits)}")
                lines.append(f"<b>{home} vs {away}</b>")
            else:
                lines.append(f"{dot} <b>{home} vs {away}</b>")
            lines.append(f"Pick: <b>{pick_text}</b> ({item['probability']}%) | COEF: <code>{coef_str}</code>\n")
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
