# Companies Demo — Maps Scraper

Small Flask UI that searches Google Maps via [Serper](https://serper.dev), lists local businesses (name, domain, rating, reviews, state, category, address), filters out manufacturers/directories via a blocklist, and looks up each company's leadership (founder / owner / president / MD) on demand via Perplexity.

## Setup

Requires Python 3 with `flask` and `requests`, plus two API keys in the macOS Keychain:

```
security add-generic-password -s serper-api-token     -a options2exit -w YOUR_SERPER_KEY
security add-generic-password -s perplexity-api-token -a options2exit -w YOUR_PPLX_KEY
```

## Run

```
python3 app.py
```

Open http://localhost:5055. Enter a query + state (defaults: windows & doors contractors in Florida), pick how many results (max 20 per search, 3 Serper credits), hit Search. Results are sorted by review count, most-reviewed first. Click **Check** on any row to find that company's owners/leadership with cited sources. **Download CSV** exports everything including leadership.

Blocked domains (manufacturer sites like Pella/Marvin/Andersen, directories like Yelp/BBB/Angi, social platforms) are hardcoded in `app.py`; add your own in `blocklist.txt` — one domain per line, re-read on every search.
