import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup

@@ -17,23 +18,48 @@


def fetch_html_flaresolverr(url):
    session_id = f"forebet_session_{int(time.time())}"
    
    # 1. Create FlareSolverr Session
    try:
        requests.post(
            FLARESOLVERR_URL,
            json={"cmd": "sessions.create", "session": session_id},
            timeout=30,
        )
    except Exception as e:
        print(f"Session creation warning: {e}")

    # 2. Execute GET Request through Session
payload = {
"cmd": "request.get",
"url": url,
        "maxTimeout": 60000
        "session": session_id,
        "maxTimeout": 120000,
}
headers = {"Content-Type": "application/json"}

print(f"Sending request to FlareSolverr for: {url}")
    response = requests.post(FLARESOLVERR_URL, json=payload, headers=headers, timeout=70)
    response = requests.post(FLARESOLVERR_URL, json=payload, headers=headers, timeout=130)
response.raise_for_status()

data = response.json()

    # 3. Destroy Session
    try:
        requests.post(
            FLARESOLVERR_URL,
            json={"cmd": "sessions.destroy", "session": session_id},
            timeout=15,
        )
    except Exception:
        pass

if data.get("status") == "ok":
print("FlareSolverr successfully bypassed Cloudflare.")
return data["solution"]["response"]
    
    raise RuntimeError(f"FlareSolverr returned error: {data.get('message')}")

    raise RuntimeError(f"FlareSolverr error: {data.get('message')}")


def numbers_in_text(text):
@@ -176,4 +202,4 @@ def send_telegram_message(message):
message = f"No matches found for {target_day.upper()} matching ≥ {MINIMUM_PROBABILITY}% probability criteria."

send_telegram_message(message)
    print(f"Telegram message sent. Matches selected: {len(picks)}")
    print(f"Telegram message sent. Total matches selected: {len(picks)}")
