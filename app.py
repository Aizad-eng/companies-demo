"""Serper Maps UI — search Google Maps via Serper, show name/domain/rating/count/state/category."""
import csv
import io
import json
import os
import re
import subprocess
import time
import uuid
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
# Templates change rarely in prod and constantly in dev; always re-check mtime
# so edits show up without a server restart regardless of how we were launched.
app.config["TEMPLATES_AUTO_RELOAD"] = True


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
    "firecrawl-api-token": "FIRECRAWL_API_KEY",
}

# Mobile-finder pipeline: we push a person (with LinkedIn URL) straight to
# the Clay table's webhook source; Clay finds the mobile and writes the
# result into Supabase, which we poll.
CLAY_WEBHOOK_URL = os.environ.get(
    "CLAY_WEBHOOK_URL",
    "https://api.clay.com/v3/sources/webhook/pull-in-data-from-a-webhook-e7fbbcec-4f04-43a4-9bb7-58641b403d74")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
MOBILE_TABLE = "mobile_results"


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
            "category", "address", "phone", "website", "leadership", "acquired"]
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


ACQUISITION_PROMPT = """Determine whether this specific local business has been ACQUIRED BY another company:

Business name: {name}
Domain: {domain}
Website: {website}
Address: {address}
Phone: {phone}
Category: {category}

IMPORTANT — direction matters. I only care whether THIS company was bought,
acquired, merged into, or taken over by someone else (private equity, a
platform/roll-up, a national brand, or any other buyer), i.e. a change in
its ownership. I do NOT care about companies that THIS business acquired —
its own acquisitions of others are irrelevant and must be ignored.

EVIDENCE RULES — precision is critical, a wrong "acquired" ruins decisions:
1. The source must EXPLICITLY name this business (or its exact brand/domain).
   Anonymous broker or M&A announcements describing an unnamed business
   ("a founder-owned window company in Florida", "a 35-year-old contractor")
   are NOT evidence, no matter how closely the description resembles this
   company. Inferring identity from such descriptions is forbidden.
2. The match must be THIS business at THIS domain/address, not a similarly
   named company elsewhere.
3. Provide the URL of the page that names the company, and quote the exact
   sentence that names it as acquired.
4. If no source explicitly names this company as acquired, answer false.
   Never guess.

Search news, press releases, PE/M&A announcements (PR Newswire, BusinessWire),
the company's own site, and industry trade press.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{{"acquired": true or false,
  "acquirer": "Buyer Name or empty string",
  "when": "year or date or empty string",
  "detail": "one short sentence, empty if not acquired",
  "quote": "the exact sentence from the source naming this company as acquired, empty if none",
  "source": "URL of the page that names the company, or where you checked"}}"""


def normalize_text(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def company_name_variants(name, domain):
    """Normalized forms that count as 'the page names this company'."""
    base = re.sub(r"\b(inc|llc|corp|co|ltd|pllc|pa|p a)\b", "", normalize_text(name)).strip()
    variants = {normalize_text(name), base}
    if domain:
        variants.add(normalize_text(domain.split(".")[0]))
    return {v for v in variants if len(v) >= 5}


def identify_company_in_page(page, name, domain, address, phone):
    """Return how the page identifies this company, or '' if it doesn't.
    Domain / phone / street address are strong anchors; a name match alone
    is weaker (generic names collide with descriptive text)."""
    norm_page = normalize_text(page)
    raw_page = page.lower()
    if domain and domain.lower() in raw_page:
        return "domain"
    if phone:
        digits = re.sub(r"\D", "", phone)[-10:]
        if len(digits) == 10 and digits in re.sub(r"\D", "", page):
            return "phone number"
    if address:
        street = normalize_text(address.split(",")[0])
        if len(street) >= 8 and street in norm_page:
            return "street address"
    if any(v in norm_page for v in company_name_variants(name, domain)):
        return "company name"
    return ""


def fetch_page_text(url):
    """Fetch a page's text for verification: Firecrawl first, plain GET fallback."""
    try:
        key = keychain("firecrawl-api-token")
        r = requests.post(
            "https://api.firecrawl.dev/v2/scrape",
            json={"url": url, "formats": ["markdown"]},
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            timeout=60,
        )
        if r.ok:
            md = (r.json().get("data") or {}).get("markdown") or ""
            if md.strip():
                return md
    except (RuntimeError, requests.RequestException):
        pass
    try:
        r = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
        if r.ok:
            return re.sub(r"<[^>]+>", " ", r.text)
    except requests.RequestException:
        pass
    return ""


def ask_perplexity(prompt):
    """One sonar call with 429 backoff; returns the reply text."""
    key = keychain("perplexity-api-token")
    for attempt in range(4):
        r = requests.post(
            PPLX_URL,
            json={"model": PPLX_MODEL,
                  "messages": [{"role": "user", "content": prompt}]},
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            timeout=120,
        )
        if r.status_code == 429 and attempt < 3:
            time.sleep(2 * (attempt + 1))
            continue
        break
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


@app.post("/api/acquisition")
def api_acquisition():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Missing company name."}), 400
    prompt = ACQUISITION_PROMPT.format(
        name=name,
        domain=body.get("domain") or "unknown",
        website=body.get("website") or "unknown",
        address=body.get("address") or "unknown",
        phone=body.get("phone") or "unknown",
        category=body.get("category") or "unknown",
    )
    try:
        reply = ask_perplexity(prompt)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except requests.HTTPError as e:
        return jsonify({"error": f"Perplexity {e.response.status_code}: "
                                 f"{e.response.text[:200]}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Perplexity request failed: {e}"}), 502
    try:
        parsed = extract_json(reply)
    except (json.JSONDecodeError, AttributeError):
        return jsonify({"error": "Could not parse model reply.",
                        "raw": reply[:300]}), 502
    acquired = bool(parsed.get("acquired"))
    source = (parsed.get("source") or "").strip()
    status = "independent"
    verification = ""

    if acquired:
        # Trust nothing: fetch the cited page ourselves and require it to
        # explicitly name this company. Anonymous releases stay unconfirmed.
        status = "unconfirmed"
        m = re.search(r"https?://\S+", source)
        if not m:
            verification = "No source URL was provided for the claim."
        else:
            page = fetch_page_text(m.group(0).rstrip(").,;"))
            if not page:
                verification = "Could not fetch the cited source to verify."
            else:
                matched = identify_company_in_page(
                    page, name,
                    body.get("domain") or "",
                    body.get("address") or "",
                    body.get("phone") or "")
                if matched:
                    status = "acquired"
                    verification = (f"Source page fetched; identified this "
                                    f"company by {matched}.")
                else:
                    verification = ("Cited source does not identify this "
                                    "company (no name, domain, address, or "
                                    "phone match) — likely an anonymous or "
                                    "unrelated announcement.")

    return jsonify({
        "status": status,
        "acquired": status == "acquired",
        "acquirer": (parsed.get("acquirer") or "").strip(),
        "when": str(parsed.get("when") or "").strip(),
        "detail": (parsed.get("detail") or "").strip(),
        "quote": (parsed.get("quote") or "").strip(),
        "source": source,
        "verification": verification,
    })


LINKEDIN_PROMPT = """Find the LinkedIn profile URL of this specific person:

Person: {person}
Their role: {title}
Company: {company}
Company domain: {domain}
Company location: {address}

Rules — accuracy is critical:
1. The profile MUST belong to this exact person at THIS company (or clearly
   this company's owner/leader). Verify the profile's company, role, or
   location matches before answering. Many people share the same name.
2. Return the canonical URL only, like https://www.linkedin.com/in/username
   — no tracking parameters, no search-result URLs, no company pages.
3. If you cannot find a profile you are CONFIDENT belongs to this person at
   this company, return an empty url. An empty answer is always better than
   a wrong profile. Never guess or construct a URL that you have not seen.
4. NEVER substitute someone else: do not return a profile of a different
   person at the same company, even its owner. If the named person has no
   findable profile, the url must be empty.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{{"url": "https://www.linkedin.com/in/... or empty string",
  "confidence": "high or medium or empty",
  "evidence": "one short sentence: what on the profile matched (company, title, location)"}}"""


@app.post("/api/linkedin")
def api_linkedin():
    body = request.get_json(silent=True) or {}
    person = (body.get("person") or "").strip()
    company = (body.get("company") or "").strip()
    if not person or not company:
        return jsonify({"error": "Need a person and a company."}), 400
    prompt = LINKEDIN_PROMPT.format(
        person=person,
        title=body.get("title") or "unknown",
        company=company,
        domain=body.get("domain") or "unknown",
        address=body.get("address") or "unknown",
    )
    try:
        reply = ask_perplexity(prompt)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except requests.HTTPError as e:
        return jsonify({"error": f"Perplexity {e.response.status_code}: "
                                 f"{e.response.text[:200]}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Perplexity request failed: {e}"}), 502
    try:
        parsed = extract_json(reply)
    except (json.JSONDecodeError, AttributeError):
        return jsonify({"error": "Could not parse model reply.",
                        "raw": reply[:300]}), 502
    url = (parsed.get("url") or "").strip()
    # accept only real profile URLs; anything else counts as not found
    if url and not re.match(r"^https://([a-z]{2,3}\.)?linkedin\.com/in/[^?\s]+$", url):
        url = ""
    # hard guard: the profile must plausibly be the requested person, not a
    # substitute (the model sometimes falls back to the company's owner).
    if url:
        tokens = [t for t in re.split(r"[^a-z]+", person.lower()) if len(t) > 1]
        slug = url.split("/in/", 1)[-1].lower()
        if tokens and not any(t in slug for t in tokens):
            url = ""
    return jsonify({"url": url.rstrip("/"),
                    "confidence": (parsed.get("confidence") or "").strip(),
                    "evidence": (parsed.get("evidence") or "").strip()})


def supabase_headers():
    return {"apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


@app.post("/api/mobile")
def api_mobile():
    """Kick off a mobile lookup: requires a LinkedIn profile (Clay needs it),
    pushes to the Clay webhook, returns a request_id the client polls."""
    body = request.get_json(silent=True) or {}
    linkedin = (body.get("linkedin") or "").strip()
    person = (body.get("person") or "").strip()
    if not linkedin:
        return jsonify({"error": "A LinkedIn profile is required first."}), 400
    if not person:
        return jsonify({"error": "Missing person name."}), 400
    if not (SUPABASE_URL and SUPABASE_KEY):
        return jsonify({"error": "Supabase is not configured "
                                 "(SUPABASE_URL / SUPABASE_SERVICE_KEY)."}), 500
    request_id = uuid.uuid4().hex
    payload = {
        "request_id": request_id,
        "name": person,
        "title": body.get("title") or "",
        "linkedin_url": linkedin,
        "company": body.get("company") or "",
        "domain": body.get("domain") or "",
    }
    try:
        r = requests.post(CLAY_WEBHOOK_URL, json=payload, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"Webhook push failed: {e}"}), 502
    return jsonify({"request_id": request_id})


@app.get("/api/mobile/<request_id>")
def api_mobile_result(request_id):
    """Poll Supabase for the Clay result for one request."""
    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        return jsonify({"error": "Bad request id."}), 400
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{MOBILE_TABLE}",
            params={"request_id": f"eq.{request_id}",
                    "select": "mobile,status,name,linkedin_url",
                    "limit": "1"},
            headers=supabase_headers(), timeout=30)
        r.raise_for_status()
        rows = r.json()
    except requests.RequestException as e:
        return jsonify({"error": f"Supabase query failed: {e}"}), 502
    if not rows:
        return jsonify({"status": "pending"})
    row = rows[0]
    return jsonify({"status": row.get("status") or ("found" if row.get("mobile") else "not_found"),
                    "mobile": row.get("mobile") or ""})


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
