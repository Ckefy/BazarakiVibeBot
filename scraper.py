"""Fetch a Bazaraki search page and extract listing links.

Bazaraki sits behind Cloudflare's "managed challenge", so a plain HTTP request
returns 403 ("Just a moment..."). We use cloudscraper to solve the JS challenge.
As a fallback (useful when running from datacenter IPs such as GitHub Actions,
which Cloudflare blocks more aggressively) a scraping-API can be used by setting
the SCRAPER_API_KEY environment variable.
"""
import os
import re
import urllib.parse

import requests

BASE = "https://www.bazaraki.com"

# Matches hrefs like: /adv/6426567_3-bedroom-apartment-to-rent/
LISTING_RE = re.compile(r"/adv/(\d+)_[^\"'?#<>\s]*")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_listings(url):
    """Return a list of (listing_id:int, full_url:str) in page order (newest first)."""
    html_text = _get_html(url)
    if _looks_like_challenge(html_text):
        raise RuntimeError("Cloudflare challenge was not solved (blocked).")

    seen = set()
    listings = []
    for match in LISTING_RE.finditer(html_text):
        listing_id = int(match.group(1))
        if listing_id in seen:
            continue
        seen.add(listing_id)
        path = match.group(0)
        full = BASE + path if path.startswith("/") else path
        listings.append((listing_id, full))
    return listings


# --- private helpers ---------------------------------------------------------

def _get_html(url):
    api_key = os.environ.get("SCRAPER_API_KEY", "").strip()
    if api_key:
        return _get_via_scraper_api(url, api_key)
    return _get_via_cloudscraper(url)


def _get_via_cloudscraper(url):
    import cloudscraper

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    response = scraper.get(url, timeout=60)
    response.raise_for_status()
    return response.text


def _get_via_scraper_api(url, api_key):
    # ScraperAPI-compatible endpoint: renders the page and bypasses Cloudflare.
    api_url = "http://api.scraperapi.com/?" + urllib.parse.urlencode(
        {"api_key": api_key, "url": url, "render": "true"}
    )
    response = requests.get(api_url, headers={"User-Agent": _UA}, timeout=90)
    response.raise_for_status()
    return response.text


def _looks_like_challenge(html_text):
    head = html_text[:2000].lower()
    return "just a moment" in head or "cf-chl" in head or "enable javascript and cookies" in head
