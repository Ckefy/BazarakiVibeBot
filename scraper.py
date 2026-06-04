"""Fetch a Bazaraki search page and extract listing links.

Bazaraki sits behind Cloudflare's "managed challenge", so a plain HTTP request
returns 403 ("Just a moment..."). Two fetch strategies are supported:

1. Local / residential IP: cloudscraper solves the JS challenge directly.
   Used automatically when SCRAPER_API_KEY is NOT set.

2. Datacenter IP (e.g. GitHub Actions, which Cloudflare blocks): route the
   request through a scraping service that provides clean proxies + a real
   browser. Activated by setting SCRAPER_API_KEY. Pick the service with
   SCRAPER_PROVIDER (default: scrapingant). Supported:
     - scrapingant  (https://scrapingant.com)
     - scraperapi   (https://scraperapi.com)
     - scrapingbee  (https://scrapingbee.com)
     - custom       (provide SCRAPER_URL_TEMPLATE with {key} and {url})
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
        return _get_via_service(url, api_key)
    return _get_via_cloudscraper(url)


def _get_via_cloudscraper(url):
    import cloudscraper

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    response = scraper.get(url, timeout=60)
    response.raise_for_status()
    return response.text


def _get_via_service(url, api_key):
    provider = (os.environ.get("SCRAPER_PROVIDER") or "scrapingant").strip().lower()
    endpoint = _build_service_url(provider, url, api_key)
    response = requests.get(endpoint, headers={"User-Agent": _UA}, timeout=120)
    response.raise_for_status()
    return response.text


def _build_service_url(provider, url, api_key):
    enc = lambda v: urllib.parse.quote(v, safe="")

    if provider == "scrapingant":
        # browser=true renders JS; residential proxy is needed to pass Cloudflare.
        proxy_type = (os.environ.get("SCRAPER_PROXY_TYPE") or "residential")
        return "https://api.scrapingant.com/v2/general?" + urllib.parse.urlencode({
            "url": url, "x-api-key": api_key, "browser": "true", "proxy_type": proxy_type,
        })

    if provider == "scraperapi":
        return "https://api.scraperapi.com/?" + urllib.parse.urlencode({
            "api_key": api_key, "url": url, "render": "true", "ultra_premium": "true",
        })

    if provider == "scrapingbee":
        return "https://app.scrapingbee.com/api/v1/?" + urllib.parse.urlencode({
            "api_key": api_key, "url": url, "render_js": "true", "stealth_proxy": "true",
        })

    if provider == "custom":
        template = os.environ.get("SCRAPER_URL_TEMPLATE", "")
        if not template:
            raise RuntimeError("SCRAPER_PROVIDER=custom requires SCRAPER_URL_TEMPLATE")
        return template.format(key=enc(api_key), url=enc(url))

    raise RuntimeError("Unknown SCRAPER_PROVIDER: {}".format(provider))


def _looks_like_challenge(html_text):
    head = html_text[:2000].lower()
    return "just a moment" in head or "cf-chl" in head or "enable javascript and cookies" in head
