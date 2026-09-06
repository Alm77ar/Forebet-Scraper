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

Markup confirmed directly from a real saved copy of the listing page
(the site's "today" page, checked 05 Sep 2026). It's built on the WordPress
"Content Views" plugin, NOT Elementor cards - each match renders as:

    <div class="pt-cv-content-item" data-pid="...">
      <div class="pt-cv-specialp"><span class="terms">
        <a ...>League Name</a>, <a ...>ZZZ-Today's Tips</a>
      </span></div>
      <h4 class="pt-cv-title"><a href="MATCH_URL">Home Team – Away Team</a></h4>
      <div class="pt-cv-ctf-list">
        ... one .pt-cv-custom-fields.pt-cv-ctf-NNN block per data point ...
      </div>
    </div>

Each data point is a NUMBERED custom-field class rather than a
semantically-named one, confirmed stable across every card sampled on the
page:

    pt-cv-ctf-002  -> kickoff time (e.g. "00:30")
    pt-cv-ctf-016  -> home win probability   (e.g. "46%")
    pt-cv-ctf-017  -> draw probability       (e.g. "35%")
    pt-cv-ctf-018  -> away win probability   (e.g. "30%")
    pt-cv-ctf-008  -> 1X2 pick               ("1" / "X" / "2")
    pt-cv-ctf-009  -> Over/Under pick        ("O" / "U")
    pt-cv-ctf-010  -> BTTS pick              ("Yes" / "No")
    pt-cv-ctf-007  -> predicted correct score (e.g. "2:1")

(ctf-040 through ctf-047 are just the repeated column-header labels
"Time/1/X/2/1X2/U/O/BTTS/Score" baked into every card's markup - not data,
safe to ignore.)

No Cloudflare/FlareSolverr layer was needed in testing - a plain
requests.get() on this same URL returned the full listing directly, so
this provider is intentionally lighter-weight than the Forebet one. If
that changes in practice, the fetch step below is the only place that
would need FlareSolverr support added to match forebet_scraper.py.

No odds or H2H data is exposed on this listing page, so those fields are
always 0.0 / "" in the normalized output.
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

        Returns a tuple: (picks, stats)

        picks is a list of dicts in the SAME shape used by the Forebet
        provider, so scraper.py can merge lists from both sources before
        sorting/sending:

            {
                "source": "GoodSport",
                "home": str,
                "away": str,
                "pick": str,             # e.g. "1 — Home win" / "2 — Away win"
                "probability": int,      # winning side's % (>= min_probability)
                "coefficient": 0.0,      # not exposed by this site
                "flag": "",              # not exposed by this site
                "league_tag": str,
                "datetime": str,         # kickoff time as shown on the site
                "match_url": str,
                "candidate_team": str,   # team the probability applies to
                "h2h": "",               # not exposed by this site
            }

        stats is a dict of diagnostics mirroring the Forebet scraper's
        stats shape, so pagination/parsing issues (e.g. only some of the
        site's sub-pages actually getting fetched) are visible in the
        Telegram diagnostics block instead of silently under-counting:

            {
                "pages_fetched": int,       # how many listing pages were requested
                "pages_reported_by_site": int,  # total pages the site's own
                                                 # pagination links claim to have
                "raw_cards_detected": int,  # total .pt-cv-content-item found, all pages
                "validated_parsed": int,    # cards that yielded usable home/away/% data
                "skipped_no_data": int,     # cards that were detected but unparseable
                "selected_picks": int,      # final picks meeting min_probability
            }
        """
        base_url = self.BASE_URLS.get(target_day, self.BASE_URLS["today"])
        all_cards = []
        seen_pids = set()
        page_num = 1
        pages_fetched = 0
        pages_reported_by_site = 1
        pagination_confirmed_working = False

        while page_num <= self.MAX_PAGES:
            page_url = base_url if page_num == 1 else f"{base_url}?_page={page_num}"
            html_content = self._fetch_page(page_url)
            if html_content is None:
                print(f"GoodSportScraper: stopped at page {page_num} (fetch failed).")
                break
            pages_fetched += 1

            cards = self._parse_cards(html_content)
            print(f"GoodSportScraper: page {page_num} ({page_url}) -> {len(cards)} cards")
            if not cards:
                break

            # IMPORTANT: this site's pagination (pages 2+) is loaded via AJAX
            # (confirmed from real saved HTML: pt-cv-pagination has class
            # "pt-cv-ajax" and there is no real ?_page=/  /page/ URL anywhere
            # on the page). The ?_page=N query param used below is UNVERIFIED
            # and likely does nothing, meaning page 2+ probably just re-serves
            # page 1's identical content. Rather than silently duplicating
            # results, detect that by comparing each card's data-pid against
            # ones already seen - if a "new" page contributes zero new pids,
            # stop immediately instead of looping MAX_PAGES times on the
            # same 30-ish matches.
            new_pids_this_page = 0
            for card in cards:
                pid = card.get("data-pid")
                if pid and pid in seen_pids:
                    continue
                if pid:
                    seen_pids.add(pid)
                new_pids_this_page += 1
                all_cards.append(card)

            if page_num > 1 and new_pids_this_page == 0:
                print(f"GoodSportScraper WARNING: page {page_num} returned ZERO new matches - "
                      f"pagination via ?_page= is confirmed NOT working (site uses AJAX "
                      f"pagination, not URL params). Stopping here. Only page 1's "
                      f"{len(cards)} matches were actually retrieved this run - matches on "
                      f"pages 2+ are being MISSED. See module docstring for how to fix.")
                pages_fetched -= 1  # this fetch didn't actually get new data
                break
            elif page_num > 1:
                pagination_confirmed_working = True

            total_pages = self._get_total_pages(html_content)
            pages_reported_by_site = max(pages_reported_by_site, total_pages)
            if total_pages and page_num >= total_pages:
                print(f"GoodSportScraper: reached last page ({total_pages} total per site pagination).")
                break
            page_num += 1
        else:
            print(f"GoodSportScraper: hit MAX_PAGES safety cap ({self.MAX_PAGES}) - "
                  f"site may have more pages than this. Raise MAX_PAGES if so.")

        if pages_reported_by_site > 1 and not pagination_confirmed_working:
            print(f"GoodSportScraper WARNING: site reports {pages_reported_by_site} total pages "
                  f"but pagination could not be confirmed working - results below almost "
                  f"certainly only cover page 1.")

        raw_cards_detected = len(all_cards)
        validated_parsed = 0
        skipped_no_data = 0
        picks = []

        for card in all_cards:
            card_data = self._card_to_data(card)
            if card_data is None:
                skipped_no_data += 1
                continue
            validated_parsed += 1
            pick = self._to_pick_dict(card_data)
            if pick is not None:
                picks.append(pick)

        stats = {
            "pages_fetched": pages_fetched,
            "pages_reported_by_site": pages_reported_by_site,
            "raw_cards_detected": raw_cards_detected,
            "validated_parsed": validated_parsed,
            "skipped_no_data": skipped_no_data,
            "selected_picks": len(picks),
        }

        if pages_fetched < pages_reported_by_site:
            print(f"GoodSportScraper WARNING: only fetched {pages_fetched} of "
                  f"{pages_reported_by_site} pages the site reports - some matches "
                  f"were likely missed. Check MAX_PAGES or pagination URL pattern.")

        return picks, stats

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
    # Parsing - confirmed selectors, see module docstring for the mapping
    # ------------------------------------------------------------------ #

    def _parse_cards(self, html_content):
        soup = BeautifulSoup(html_content, "html.parser")
        return soup.select(".pt-cv-content-item")

    def _ctf_value(self, card, ctf_number):
        # IMPORTANT: real class names are zero-padded to 3 digits
        # (pt-cv-ctf-016, pt-cv-ctf-008, pt-cv-ctf-002 ...) - confirmed
        # directly from the real saved HTML. Passing a plain int without
        # padding (e.g. "16" instead of "016") silently matches nothing.
        el = card.select_one(f".pt-cv-ctf-{ctf_number:03d} .pt-cv-ctf-value")
        return el.get_text(strip=True) if el else ""

    def _card_to_data(self, card):
        title_el = card.select_one("h4.pt-cv-title a")
        if not title_el:
            return None

        title_text = title_el.get_text(" ", strip=True)
        match_url = title_el.get("href", "")

        # Titles use an en dash: "Home Team – Away Team"
        if "–" in title_text:
            home_team, away_team = [t.strip() for t in title_text.split("–", 1)]
        elif " - " in title_text:
            home_team, away_team = [t.strip() for t in title_text.split(" - ", 1)]
        else:
            return None

        # League/competition is the first term; site also tags every card
        # with a "ZZZ-Today's/Tomorrow's Tips" housekeeping term - skip that one.
        term_links = card.select(".pt-cv-specialp .terms a")
        league_tag = ""
        for a in term_links:
            text = a.get_text(strip=True)
            if not text.upper().startswith("ZZZ"):
                league_tag = text
                break

        def pct(ctf_number):
            raw = self._ctf_value(card, ctf_number)
            m = re.search(r"\d+", raw)
            return int(m.group()) if m else None

        home_prob = pct(16)
        draw_prob = pct(17)
        away_prob = pct(18)
        if home_prob is None or away_prob is None:
            return None

        return {
            "home_team": home_team,
            "away_team": away_team,
            "home_prob": home_prob,
            "draw_prob": draw_prob,
            "away_prob": away_prob,
            "league_tag": league_tag,
            "match_time": self._ctf_value(card, 2),
            "onextwo_pick": self._ctf_value(card, 8),     # "1" / "X" / "2"
            "over_under_pick": self._ctf_value(card, 9),  # "O" / "U"
            "btts_pick": self._ctf_value(card, 10),       # "Yes" / "No"
            "correct_score": self._ctf_value(card, 7),    # e.g. "2:1"
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
            # Extra fields this site uniquely offers, on top of the shared
            # pick shape - safe for scraper.py to ignore if not needed.
            "over_under_pick": card_data["over_under_pick"],
            "btts_pick": card_data["btts_pick"],
            "correct_score": card_data["correct_score"],
        }
