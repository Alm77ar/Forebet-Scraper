"""
goodsport_scraper.py

Provider for good-sport.co's football prediction pages.

Kept in its own module/class - mirroring ForebetScraper in forebet_scraper.py -
so each prediction source's site-specific parsing (selectors, pagination,
quirks) lives in its own file rather than growing inside the main script.
Both providers return the SAME normalized pick dict shape (see
GoodSportScraper.scrape docstring) so scraper.py can merge, sort, and send
picks from multiple sources without caring which one produced them - the
same "shared interface, separate implementation classes" pattern you'd get
from separate .cs files implementing a common interface.

NOTE: good-sport.co did not appear to be behind Cloudflare/FlareSolverr in
initial testing (a plain requests.get() returned full prediction data), so
this provider is intentionally lighter-weight than the Forebet one - no
Playwright/FlareSolverr needed. If that turns out to be wrong in practice
(e.g. intermittent challenge pages), the fetch step here is the only place
that would need to grow FlareSolverr support to match forebet_scraper.py.

IMPORTANT: the selectors marked "ASSUMED" below are inferred from a
markdown-rendered view of the page, not the real raw HTML class names.
Confirm/fix them against a real saved HTML source (same way the Forebet
H2H markup was confirmed) before relying on this in production.
"""

import re
import requests
from bs4 import BeautifulSoup


class GoodSportScraper:
    """
    Scraper for good-sport.co's daily football prediction listing pages.
    """

    BASE_URLS = {
        "today": "https://good-sport.co/football-predictions-for-today/",
        "tomorrow": "https://good-sport.co/football-predictions-for-tomorrow/",
    }

    DEFAULT_MIN_PROBABILITY = 85
    MAX_PAGES = 25  # safety cap; site showed up to ~22 pages on a busy day

    def __init__(self, min_probability=None, session=None):
        self.min_probability = min_probability or self.DEFAULT_MIN_PROBABILITY
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                )
            }
        )

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def scrape(self, target_day="today"):
        """
        Fetches and filters picks for target_day ("today" or "tomorrow").

        Returns a list of dicts in the SAME shape used by the Forebet
        provider, so scraper.py can merge lists from both sources before
        sorting/sending:

            {
                "source": "GoodSport",
                "home": str,
                "away": str,
                "pick": str,             # e.g. "1 — Home win" / "2 — Away win"
                "probability": int,      # the winning side's % (>= min_probability)
                "coefficient": float,    # 0.0 - this site doesn't expose odds
                "flag": "",              # not available on this site
                "league_tag": str,
                "datetime": str,
                "match_url": str,
                "candidate_team": str,   # team the probability applies to
                "h2h": "",               # not available on this site
            }
        """
        base_url = self.BASE_URLS.get(target_day, self.BASE_URLS["today"])
        all_cards = []
        page_num = 1

        while page_num <= self.MAX_PAGES:
            page_url = base_url if page_num == 1 else f"{base_url}?_page={page_num}"
            html_content = self._fetch_page(page_url)
            if html_content is None:
                break

            cards = self._parse_cards(html_content)
            if not cards:
                break
            all_cards.extend(cards)

            total_pages = self._get_total_pages(html_content)
            if total_pages and page_num >= total_pages:
                break
            page_num += 1

        picks = []
        for card in all_cards:
            card_data = self._card_to_pick(card)
            if card_data is None:
                continue
            pick = self._to_pick_dict(card_data)
            if pick is not None:
                picks.append(pick)
        return picks

    # ------------------------------------------------------------------ #
    # Fetching
    # ------------------------------------------------------------------ #

    def _fetch_page(self, url):
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except Exception as e:
            print(f"GoodSportScraper: failed to fetch {url}: {e}")
            return None

    def _get_total_pages(self, html_content):
        soup = BeautifulSoup(html_content, "html.parser")
        max_page = 1
        for a in soup.select("a[href*='_page=']"):
            m = re.search(r"_page=(\d+)", a.get("href", ""))
            if m:
                max_page = max(max_page, int(m.group(1)))
        return max_page

    # ------------------------------------------------------------------ #
    # Parsing (ASSUMED selectors - confirm against real saved HTML)
    # ------------------------------------------------------------------ #

    def _parse_cards(self, html_content):
        """
        Each match on the listing page renders as a card with:
          - a title link "Team A – Team B" (real anchor, ASSUMED selector
            below - ".elementor-post" is a common Elementor loop-item class
            but MUST be confirmed against the real page source)
          - a competition tag/link just above the card
          - a small stats table: Time / 1 / X / 2 / 1X2 / U/O / BTTS / Score

        Returns a list of BeautifulSoup card elements - one per match.
        """
        soup = BeautifulSoup(html_content, "html.parser")

        # ASSUMED: Elementor posts/loop-grid items. Replace with the real
        # container class once confirmed from a saved HTML source.
        cards = soup.select(".elementor-post, article, .jet-listing-grid__item")
        return cards

    def _card_to_pick(self, card):
        title_el = card.select_one("h3 a, .elementor-post__title a, a[href*='/predictions/']")
        if not title_el:
            return None

        title_text = title_el.get_text(" ", strip=True)
        match_url = title_el.get("href", "")

        # Titles look like "Team A – Team B" (en dash) on this site
        if "–" in title_text:
            home_team, away_team = [t.strip() for t in title_text.split("–", 1)]
        elif "-" in title_text:
            home_team, away_team = [t.strip() for t in title_text.split("-", 1)]
        else:
            return None

        league_el = card.find_previous(class_=re.compile("term|category|tag"))
        league_tag = league_el.get_text(strip=True) if league_el else ""

        # Percentages: three consecutive "NN%" tokens = home / draw / away
        text = card.get_text(" ", strip=True)
        percentages = re.findall(r"(\d{1,3})%", text)
        if len(percentages) < 3:
            return None
        home_prob, draw_prob, away_prob = (int(p) for p in percentages[:3])

        time_match = re.search(r"\b(\d{1,2}:\d{2})\b", text)
        match_time = time_match.group(1) if time_match else ""

        return {
            "home_team": home_team,
            "away_team": away_team,
            "home_prob": home_prob,
            "draw_prob": draw_prob,
            "away_prob": away_prob,
            "league_tag": league_tag,
            "match_time": match_time,
            "match_url": match_url,
        }

    # ------------------------------------------------------------------ #
    # Normalization + filtering
    # ------------------------------------------------------------------ #

    def _to_pick_dict(self, card_data):
        home_prob = card_data["home_prob"]
        away_prob = card_data["away_prob"]

        if home_prob < self.min_probability and away_prob < self.min_probability:
            return None

        if home_prob >= away_prob:
            pick = "1 — Home win"
            prob = home_prob
            candidate_team = card_data["home_team"]
        else:
            pick = "2 — Away win"
            prob = away_prob
            candidate_team = card_data["away_team"]

        return {
            "source": "GoodSport",
            "home": card_data["home_team"],
            "away": card_data["away_team"],
            "pick": pick,
            "probability": prob,
            "coefficient": 0.0,
            "flag": "",
            "league_tag": card_data["league_tag"],
            "datetime": card_data["match_time"],
            "match_url": card_data["match_url"],
            "candidate_team": candidate_team,
            "h2h": "",
        }
