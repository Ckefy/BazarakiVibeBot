#!/usr/bin/env python3
"""Quick sanity check for a scraping service before wiring it into Actions.

Usage:
    export SCRAPER_API_KEY="your-key"
    export SCRAPER_PROVIDER="scrapingant"   # or scraperapi / scrapingbee
    python check_service.py
"""
import os
import sys

from scraper import fetch_listings

URL = ("https://www.bazaraki.com/real-estate-to-rent/apartments-flats/"
       "number-of-bedrooms---3/?ordering=newest&price_max=2000")

if not os.environ.get("SCRAPER_API_KEY"):
    print("Set SCRAPER_API_KEY (and optionally SCRAPER_PROVIDER) first.", file=sys.stderr)
    sys.exit(1)

print("Provider:", os.environ.get("SCRAPER_PROVIDER", "scrapingant"))
print("Fetching Bazaraki through the service...")
try:
    items = fetch_listings(URL)
except Exception as exc:  # noqa: BLE001
    print("FAILED:", type(exc).__name__, exc)
    sys.exit(2)

print("OK — parsed {} listings.".format(len(items)))
for lid, link in items[:5]:
    print(" ", lid, link)
if not items:
    print("WARNING: 0 listings — page loaded but selectors found nothing.")
