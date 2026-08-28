"""Serper Maps UI — search Google Maps via Serper, show name/domain/rating/count/state/category."""
import csv
import io
import json
import os
import re
import subprocess
import time
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request, Response

SERPER_URL = "https://google.serper.dev/maps"
# Router API (perplexity/kimi-k3) is invite-only preview — this account got
# 403s there, so we use the standard search-grounded endpoint instead.
PPLX_URL = "https://api.perplexity.ai/chat/completions"
PPLX_MODEL = "sonar"
PAGE_SIZE = 20  # Serper returns 20 places per maps call
BLOCKLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "blocklist.txt")

# Always-on blocklist, baked into the app. blocklist.txt adds to this —
# it can't remove entries from here.
HARDCODED_BLOCKLIST = frozenset({
    # window/door manufacturer brand sites (dealer locators, not local firms)
    "pella.com", "marvin.com", "andersenwindows.com", "renewalbyandersen.com",
    "jeld-wen.com", "milgard.com", "simonton.com", "plygem.com", "alside.com",
    "provia.com", "thermatru.com", "harveywindows.com", "kolbewindows.com",
    "weathershield.com", "windsorwindows.com", "sunrisewindows.com",
    "anlin.com", "okna.com", "windowworld.com", "championwindow.com",
    "westshorehome.com", "powerhrg.com",
    # directories / aggregators / review platforms
    "yelp.com", "angi.com", "angieslist.com", "homeadvisor.com",
    "thumbtack.com", "houzz.com", "porch.com", "buildzoom.com", "networx.com",
    "bbb.org", "mapquest.com", "yellowpages.com", "superpages.com",
    "manta.com", "chamberofcommerce.com", "expertise.com", "birdeye.com",
    "nextdoor.com", "alignable.com", "dexknows.com", "citysearch.com",
    "hotfrog.com", "trustpilot.com", "tripadvisor.com", "foursquare.com",
    "glassdoor.com", "indeed.com", "wikipedia.org", "cylex.us.com",
    "n49.com", "brownbook.net", "showmelocal.com", "merchantcircle.com",
    "localstack.com", "yellowbook.com", "ezlocal.com", "2findlocal.com",
    "opendi.us", "tupalo.co", "find-us-here.com", "salespider.com",
    "cybo.com", "iglobal.co",
    # social / generic platforms
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "pinterest.com", "tiktok.com", "business.site",
    "google.com", "godaddysites.com", "wixsite.com", "square.site",
    "weebly.com", "wordpress.com", "blogspot.com",
})

app = Flask(__name__)


# Keychain service name -> env var used on hosted deploys (Render etc.)
def load_dotenv():
    """Load .env beside this file into os.environ (no override)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


load_dotenv()

ENV_KEYS = {
    "serper-api-token": "SERPER_API_KEY",
    "perplexity-api-token": "PERPLEXITY_API_KEY",
    "findymail-api-token": "FINDYMAIL_API_KEY",
}


def keychain(service):
    """Env var first (Render/Linux), macOS Keychain as local fallback."""
    env_name = ENV_KEYS.get(service, "")
    val = os.environ.get(env_name, "").strip()
    if val:
        return val
    out = subprocess.run(
        ["security", "find-generic-password", "-s", service,
         "-a", "options2exit", "-w"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"Set the {env_name} env var "
                           f"({service} not found in Keychain either)")
    return out.stdout.strip()


def api_key():
    return keychain("serper-api-token")


def load_blocklist():
    """Hardcoded list plus blocklist.txt, re-read each search so edits apply
    without a restart."""
    out = set(HARDCODED_BLOCKLIST)
    try:
        with open(BLOCKLIST_PATH) as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        entry = line.split("#", 1)[0].strip().lower()
        if entry:
            out.add(entry[4:] if entry.startswith("www.") else entry)
    return out


def is_blocked(domain, blocked):
    """True if the domain matches a blocked entry, including subdomains."""
    if not domain:
        return False
    return any(domain == b or domain.endswith("." + b) for b in blocked)


def domain_of(website):
    if not website:
        return ""
    host = urlparse(website).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def state_of(address):
    """Pull the state/region out of a Google-formatted address."""
    if not address:
        return ""
    parts = [p.strip() for p in address.split(",")]
    # US style: "... , Austin, TX 78751, United States"
    for part in parts:
        m = re.fullmatch(r"([A-Z]{2})\s+\d{5}(?:-\d{4})?", part)
        if m:
            return m.group(1)
    # Fall back to the segment before the country, minus any postcode
    if len(parts) >= 3:
        return re.sub(r"\s*\d[\d\s-]*$", "", parts[-2]).strip()
    return ""


def flatten(place):
    return {
        "name": place.get("title", ""),
        "domain": domain_of(place.get("website")),
        "rating": place.get("rating"),
        "ratingCount": place.get("ratingCount"),
        "state": state_of(place.get("address")),
        "category": place.get("type", ""),
        "address": place.get("address", ""),
        "phone": place.get("phoneNumber", ""),
        "website": place.get("website", ""),
    }


def build_query(query, state):
    """Combine the term with a state so Serper geolocates the search."""
    if state and state.lower() not in query.lower():
        return f"{query} in {state}"
    return query


def search(query, limit):
    r = requests.post(
        SERPER_URL,
        json={"q": query},
        headers={"X-API-KEY": api_key(), "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    rows = [flatten(p) for p in data.get("places", [])]

    # Drop manufacturers/directories before trimming, so the limit is filled
    # with real local businesses rather than partly consumed by noise.
    blocked = load_blocklist()
    kept = [r for r in rows if not is_blocked(r["domain"], blocked)]
    removed = [r["domain"] for r in rows if is_blocked(r["domain"], blocked)]

    # Rank by review volume before trimming, so a limit of N returns the N
    # most-reviewed places rather than the first N in Google's own order.
    kept.sort(key=lambda r: r["ratingCount"] or 0, reverse=True)
    return kept[:limit], data.get("credits", 0), removed


@app.get("/")
def index():
    return render_template("index.html", page_size=PAGE_SIZE)


@app.post("/api/search")
def api_search():
    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
    state = (body.get("state") or "").strip()
    if not query:
        return jsonify({"error": "Enter a search query."}), 400
    full_query = build_query(query, state)
    try:
        limit = int(body.get("limit") or PAGE_SIZE)
    except (TypeError, ValueError):
        limit = PAGE_SIZE
    limit = max(1, min(limit, PAGE_SIZE))

    try:
        rows, credits, removed = search(full_query, limit)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except requests.HTTPError as e:
        return jsonify({"error": f"Serper returned {e.response.status_code}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Request failed: {e}"}), 502

    return jsonify({"results": rows, "credits": credits, "count": len(rows),
                    "query": full_query, "filtered": len(removed),
                    "filteredDomains": sorted(set(removed))})


@app.post("/api/export")
def api_export():
    rows = (request.get_json(silent=True) or {}).get("results", [])
    cols = ["name", "domain", "rating", "ratingCount", "state",
            "category", "address", "phone", "website", "leadership"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=maps_results.csv"},
    )




LEADERSHIP_PROMPT = """Find the leadership of this specific local business:

Business name: {name}
Domain: {domain}
Website: {website}
Address: {address}
Phone: {phone}
Category: {category}

I need the people who own or run it: founder, co-founder, owner, co-owner,
president, CEO, managing director, managing partner, or principal.

Search the company website (about/team pages), LinkedIn, state business
registries (Sunbiz, SOS filings), BBB profiles, and news. Make sure the
person belongs to THIS business at THIS address/domain — many businesses
share similar names. Only include real, verifiable people — never guess or
invent names. If you cannot find anyone, return an empty list.

For each person, cite where you found them (a URL if possible, otherwise
e.g. "LinkedIn profile" or "Florida Sunbiz filing").

Respond with ONLY a JSON object, no prose, in exactly this shape:
{{"people": [{{"name": "Full Name", "title": "Owner", "source": "https://..."}}],
  "source": "one-line summary of where the information came from"}}"""


def extract_json(text):
    """Parse the model reply, tolerating code fences or stray prose."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


@app.post("/api/leadership")
def api_leadership():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Missing company name."}), 400
    prompt = LEADERSHIP_PROMPT.format(
        name=name,
        domain=body.get("domain") or "unknown",
        website=body.get("website") or "unknown",
        address=body.get("address") or "unknown",
        phone=body.get("phone") or "unknown",
        category=body.get("category") or "unknown",
    )
    try:
        key = keychain("perplexity-api-token")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    try:
        # Low per-key rate limit: retry 429s with backoff instead of failing.
        for attempt in range(4):
            r = requests.post(
                PPLX_URL,
                json={
                    "model": PPLX_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                },
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                timeout=120,
            )
            if r.status_code == 429 and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            break
        r.raise_for_status()
        reply = r.json()["choices"][0]["message"]["content"]
    except requests.HTTPError as e:
        detail = e.response.text[:200]
        return jsonify({"error": f"Perplexity {e.response.status_code}: {detail}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Perplexity request failed: {e}"}), 502

    try:
        parsed = extract_json(reply)
        # Merge duplicate people (same person listed once per title).
        merged = {}
        for p in parsed.get("people", []):
            name = (p.get("name") or "").strip()
            if not name:
                continue
            title = (p.get("title") or "").strip()
            src = (p.get("source") or "").strip()
            key_ = name.lower()
            if key_ in merged:
                if title and title.lower() not in merged[key_]["title"].lower():
                    merged[key_]["title"] += f" / {title}"
                if src and not merged[key_]["source"]:
                    merged[key_]["source"] = src
            else:
                merged[key_] = {"name": name, "title": title, "source": src}
        people = list(merged.values())
    except (json.JSONDecodeError, AttributeError):
        return jsonify({"error": "Could not parse model reply.",
                        "raw": reply[:300]}), 502
    return jsonify({"people": people, "source": parsed.get("source", "")})


@app.post("/api/email")
def api_email():
    """Find a work email for one person via Findymail. Manual, per click —
    Findymail charges 1 credit per email found, so nothing here is automatic."""
    body = request.get_json(silent=True) or {}
    person = (body.get("person") or "").strip()
    domain = (body.get("domain") or "").strip()
    if not person or not domain:
        return jsonify({"error": "Need both a person name and a domain."}), 400
    try:
        key = keychain("findymail-api-token")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    try:
        r = requests.post(
            "https://app.findymail.com/api/search/name",
            json={"name": person, "domain": domain},
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            timeout=60,
        )
        if r.status_code == 402:
            return jsonify({"error": "Findymail: out of credits."}), 502
        r.raise_for_status()
        contact = r.json().get("contact") or {}
    except requests.HTTPError as e:
        return jsonify({"error": f"Findymail {e.response.status_code}: "
                                 f"{e.response.text[:150]}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Findymail request failed: {e}"}), 502
    return jsonify({"email": contact.get("email") or "",
                    "verified": bool(contact.get("verified", True))})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    app.run(host="0.0.0.0" if "PORT" in os.environ else "127.0.0.1",
            port=port, debug="PORT" not in os.environ)
