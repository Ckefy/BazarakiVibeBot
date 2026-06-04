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
from bs4 import BeautifulSoup

BASE = "https://www.bazaraki.com"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def normalize_search_url(url):
    """Force newest-first first page so we never miss new listings on later pages.

    - adds ordering=newest only if the user didn't set an ordering themselves;
    - drops any `page` param so we always read page 1 (the freshest results);
    - preserves all other params, including empty ones (lat=&lng=&radius=).
    """
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k != "page"]
    if not any(k == "ordering" for k, _ in query):
        query.append(("ordering", "newest"))
    new_query = urllib.parse.urlencode(query)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def fetch_listings(url):
    """Return a list of (listing_id:int, full_url:str) in page order (newest first).

    Only the actual search results are returned. Bazaraki pages also contain
    "similar"/"recommended" carousels with unrelated ads (e.g. studios in a
    3-bedroom search); those live OUTSIDE the results container and are ignored.
    """
    html_text = _get_html(normalize_search_url(url))
    if _looks_like_challenge(html_text):
        raise RuntimeError("Cloudflare challenge was not solved (blocked).")
    return _parse_listings(html_text)


def _parse_listings(html_text):
    soup = BeautifulSoup(html_text, "html.parser")

    # Real results live in <div id="listing" class="items-listing">. Restrict to it
    # so recommendation/VIP blocks elsewhere on the page are excluded.
    container = soup.select_one("#listing") or soup.select_one("div.items-listing") or soup

    seen = set()
    listings = []
    for card in container.select("div.advert.js-item-listing[data-id]"):
        data_id = card.get("data-id", "")
        if not data_id.isdigit():
            continue
        listing_id = int(data_id)
        if listing_id in seen:
            continue
        seen.add(listing_id)
        listings.append((listing_id, _card_url(card, data_id)))
    return listings


def _card_url(card, data_id):
    """Pick the canonical /adv/ link (with slug) from a result card."""
    pattern = re.compile(r"/adv/{}[_/]".format(data_id))
    for a in card.select("a[href*='/adv/']"):
        href = a.get("href", "")
        if pattern.search(href):
            return BASE + href if href.startswith("/") else href
    # Fallback: canonical URL without the slug (Bazaraki redirects to the full one).
    return "{}/adv/{}/".format(BASE, data_id)


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
        # datacenter is ~25x cheaper in credits and currently passes Bazaraki's
        # Cloudflare; switch to "residential" via SCRAPER_PROXY_TYPE if it ever 403s.
        proxy_type = (os.environ.get("SCRAPER_PROXY_TYPE") or "datacenter")
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
