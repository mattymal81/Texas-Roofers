#!/usr/bin/env python3
"""
IGA Lead Scraper - Roofers in Texas (v1)
------------------------------------------------
Scrapes roofing companies within a radius of a center point (default: The
Woodlands, TX, 50 miles) using the Google Places API, optionally enriches
each company with Apollo.io firmographic data, scores each lead, and writes
out a de-duplicated CSV (and appends to a master CSV used as your running
master list).

WHY THIS RUNS LOCALLY, NOT INSIDE THE CLAUDE SESSION:
Anthropic's cloud sandbox that Claude works in only has network access to an
allowlisted set of domains, and maps.googleapis.com is not on it. So this
script needs to run on your own machine (or any server of yours) that has
normal internet access. Claude can maintain/update this script for you, but
can't execute the live API calls itself.

SETUP
-----
1. Python 3.9+
2. pip install requests
3. Set environment variables (or edit the CONFIG section below):
     export GOOGLE_PLACES_API_KEY="your-key-here"
     export APOLLO_API_KEY="your-apollo-key-here"   # optional, enables enrichment
4. Run:
     python3 roofer_scraper.py

OUTPUT
------
- roofer_leads_YYYY-MM-DD.csv  -> this run's leads
- roofer_leads_master.csv      -> running de-duplicated master list (appended each run)

Both are plain CSV, ready to import into GHL. Import roofer_leads_master.csv
into your Google Sheet (File > Import > Replace/Append) to keep a single
running master sheet, or ask Claude to push it there for you via the Google
Drive connector once you've shared the CSV back.
"""

import csv
import math
import os
import sys
import time
from datetime import date

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")

# Center point: The Woodlands, TX
CENTER_LAT = 30.1658
CENTER_LNG = -95.4613
RADIUS_MILES = 50

# Search terms to try (Places Text Search) - keeps recall high across how
# roofers describe themselves.
SEARCH_TERMS = [
    "roofing contractor",
    "roofing company",
    "roof repair",
    "residential roofing",
    "commercial roofing",
]

# Google Places Nearby/Text Search radius cap is 50,000 meters (~31 miles),
# so we tile the 50-mile circle with a grid of overlapping search circles.
GRID_STEP_MILES = 20  # spacing between grid centers
SUB_RADIUS_METERS = 30000  # ~18.6 miles per sub-search, overlapping for coverage

SCORE_WEIGHT_WEB = 0.60
SCORE_WEIGHT_SIZE = 0.40

MASTER_CSV = "roofer_leads_master.csv"
TODAY = date.today().isoformat()
RUN_CSV = f"roofer_leads_{TODAY}.csv"

FIELDNAMES = [
    "place_id",
    "business_name",
    "phone",
    "address",
    "city",
    "website",
    "google_rating",
    "google_review_count",
    "business_status",
    "apollo_matched",
    "employee_count",
    "founded_year",
    "linkedin_url",
    "contact_name",
    "contact_title",
    "contact_email",
    "score_web_opportunity",
    "score_size_opportunity",
    "opportunity_score",
    "source",
    "date_scraped",
]

PLACES_TEXTSEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------

def miles_to_grid_points(center_lat, center_lng, radius_miles, step_miles):
    """Generate a grid of (lat, lng) points covering a circle of radius_miles
    around the center, spaced step_miles apart."""
    points = [(center_lat, center_lng)]
    lat_deg_per_mile = 1 / 69.0
    n_steps = int(radius_miles // step_miles) + 1
    for i in range(1, n_steps + 1):
        r = i * step_miles
        if r > radius_miles:
            break
        # number of points around this ring, spaced ~step_miles apart
        circumference = 2 * math.pi * r
        n_points = max(6, int(circumference / step_miles))
        for j in range(n_points):
            angle = 2 * math.pi * j / n_points
            dlat = (r * math.cos(angle)) * lat_deg_per_mile
            lng_deg_per_mile = 1 / (69.0 * math.cos(math.radians(center_lat)))
            dlng = (r * math.sin(angle)) * lng_deg_per_mile
            points.append((center_lat + dlat, center_lng + dlng))
    return points


def haversine_miles(lat1, lng1, lat2, lng2):
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Google Places
# ---------------------------------------------------------------------------

def places_text_search(query, lat, lng, radius_m):
    results = []
    params = {
        "query": query,
        "location": f"{lat},{lng}",
        "radius": radius_m,
        "key": GOOGLE_PLACES_API_KEY,
    }
    page = 0
    while True:
        page += 1
        # A next_page_token isn't valid the instant Google issues it - it
        # needs a moment to "warm up" server-side. Retry a few times with
        # backoff instead of giving up on the first INVALID_REQUEST, which is
        # the normal transient response while the token isn't ready yet.
        data = None
        for attempt in range(5):
            resp = requests.get(PLACES_TEXTSEARCH_URL, params=params, timeout=20)
            data = resp.json()
            status = data.get("status")
            if status in ("OK", "ZERO_RESULTS"):
                break
            if status == "INVALID_REQUEST" and "pagetoken" in params and attempt < 4:
                time.sleep(2 + attempt * 1.5)  # 2s, 3.5s, 5s, 6.5s
                continue
            print(f"  ! Places API error on page {page}: {status} - {data.get('error_message', '')}",
                  file=sys.stderr, flush=True)
            break
        if not data or data.get("status") not in ("OK", "ZERO_RESULTS"):
            break
        results.extend(data.get("results", []))
        next_page_token = data.get("next_page_token")
        if not next_page_token:
            break
        # Google requires a short delay before a fresh token is valid at all
        time.sleep(3)
        params = {"pagetoken": next_page_token, "key": GOOGLE_PLACES_API_KEY}
    return results


def place_details(place_id):
    params = {
        "place_id": place_id,
        "fields": "name,formatted_phone_number,formatted_address,website,"
                  "rating,user_ratings_total,business_status,geometry",
        "key": GOOGLE_PLACES_API_KEY,
    }
    resp = requests.get(PLACES_DETAILS_URL, params=params, timeout=20)
    data = resp.json()
    if data.get("status") != "OK":
        return None
    return data.get("result", {})


def scrape_places():
    if not GOOGLE_PLACES_API_KEY:
        print("ERROR: GOOGLE_PLACES_API_KEY is not set.", file=sys.stderr, flush=True)
        sys.exit(1)

    grid_points = miles_to_grid_points(CENTER_LAT, CENTER_LNG, RADIUS_MILES, GRID_STEP_MILES)
    print(f"Searching {len(grid_points)} grid points x {len(SEARCH_TERMS)} search terms...", flush=True)

    seen_place_ids = {}
    for gi, (lat, lng) in enumerate(grid_points):
        for term in SEARCH_TERMS:
            results = places_text_search(term, lat, lng, SUB_RADIUS_METERS)
            for r in results:
                pid = r.get("place_id")
                if not pid or pid in seen_place_ids:
                    continue
                loc = r.get("geometry", {}).get("location", {})
                dist = haversine_miles(CENTER_LAT, CENTER_LNG, loc.get("lat", CENTER_LAT), loc.get("lng", CENTER_LNG))
                if dist > RADIUS_MILES + 2:  # small buffer for grid overlap slop
                    continue
                seen_place_ids[pid] = r
        print(f"  grid point {gi + 1}/{len(grid_points)} done, {len(seen_place_ids)} unique so far", flush=True)

    print(f"Found {len(seen_place_ids)} unique candidate businesses. Fetching details...", flush=True)

    leads = []
    for pid, basic in seen_place_ids.items():
        details = place_details(pid)
        time.sleep(0.05)  # gentle pacing
        if not details:
            continue
        leads.append({
            "place_id": pid,
            "business_name": details.get("name", basic.get("name", "")),
            "phone": details.get("formatted_phone_number", ""),
            "address": details.get("formatted_address", basic.get("formatted_address", "")),
            "website": details.get("website", ""),
            "google_rating": details.get("rating", ""),
            "google_review_count": details.get("user_ratings_total", ""),
            "business_status": details.get("business_status", ""),
        })
    return leads


# ---------------------------------------------------------------------------
# Apollo enrichment (optional)
# ---------------------------------------------------------------------------

_apollo_diagnostic_printed = False


def apollo_enrich(lead):
    """Enrich a lead with Apollo org data by domain, if an API key is set."""
    global _apollo_diagnostic_printed
    enriched = {
        "apollo_matched": False,
        "employee_count": "",
        "founded_year": "",
        "linkedin_url": "",
        "contact_name": "",
        "contact_title": "",
        "contact_email": "",
    }
    if not APOLLO_API_KEY or not lead.get("website"):
        return enriched

    domain = lead["website"].split("//")[-1].split("/")[0].replace("www.", "")
    try:
        resp = requests.get(
            "https://api.apollo.io/api/v1/organizations/enrich",
            headers={"Cache-Control": "no-cache", "X-Api-Key": APOLLO_API_KEY},
            params={"domain": domain},
            timeout=20,
        )
        # Print full diagnostics for the FIRST call only, so a systemic
        # problem (bad key, wrong plan, rate limit) is visible in the log
        # without spamming it once per lead.
        if not _apollo_diagnostic_printed:
            print(f"  [apollo diag] first enrich call -> status {resp.status_code}, "
                  f"body: {resp.text[:500]}", file=sys.stderr, flush=True)
            _apollo_diagnostic_printed = True

        if resp.status_code == 401:
            print("  ! Apollo enrich: 401 Unauthorized - the API key is invalid or missing "
                  "the required scope/plan for organization enrichment.", file=sys.stderr, flush=True)
            return enriched
        if resp.status_code == 429:
            print("  ! Apollo enrich: 429 rate limited / out of credits.", file=sys.stderr, flush=True)
            return enriched
        if resp.status_code != 200:
            print(f"  ! Apollo enrich: unexpected status {resp.status_code} for {domain}: "
                  f"{resp.text[:300]}", file=sys.stderr, flush=True)
            return enriched

        data = resp.json()
        org = data.get("organization")
        if org:
            enriched["apollo_matched"] = True
            enriched["employee_count"] = org.get("estimated_num_employees", "")
            enriched["founded_year"] = org.get("founded_year", "")
            enriched["linkedin_url"] = org.get("linkedin_url", "")
    except requests.RequestException as e:
        print(f"  ! Apollo enrich failed for {domain}: {e}", file=sys.stderr, flush=True)
    return enriched


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_web_opportunity(lead):
    """Higher score = bigger opportunity for a marketing agency, i.e. WEAKER
    existing web presence. A roofer with no website, a low rating, or few
    reviews needs help (and is worth calling); one with a great site and 500
    five-star reviews almost certainly already has marketing in place."""
    score = 0

    # No website at all is the single biggest opportunity signal.
    if not lead.get("website"):
        score += 40
    else:
        score += 10  # has a site, but it may still be outdated/unoptimized - can't tell from Places data alone

    rating = lead.get("google_rating") or 0
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        rating = 0
    if rating == 0:
        score += 25  # no rating at all - essentially invisible online
    elif rating < 4.0:
        score += 25
    elif rating < 4.5:
        score += 12
    elif rating < 4.8:
        score += 5
    # 4.8+ rating: no points, reputation is already excellent

    reviews = lead.get("google_review_count") or 0
    try:
        reviews = int(reviews)
    except (TypeError, ValueError):
        reviews = 0
    if reviews == 0:
        score += 20
    elif reviews < 10:
        score += 15
    elif reviews < 25:
        score += 8
    elif reviews < 100:
        score += 3
    # 100+ reviews: no points, well-established review volume already

    if not lead.get("phone"):
        score -= 10  # can't cold call or voicemail-drop without a number - real penalty, not just fewer points

    return round(max(0, min(score, 100)), 1)


def score_size_opportunity(lead):
    """Optional signal from Apollo (only populated if Apollo enrichment is
    on and working). A small, newer shop is a more approachable prospect for
    an agency - established with a big team is more likely to already have
    marketing handled or a bigger budget to be picky about vendors. This
    contributes 0 for every lead if Apollo is disabled or unmatched, which
    just means score_total collapses to the web-opportunity score alone."""
    if not lead.get("apollo_matched"):
        return 0.0

    score = 0
    emp = lead.get("employee_count")
    try:
        emp = int(emp)
    except (TypeError, ValueError):
        emp = 0
    if emp == 0:
        score += 20  # unknown/unlisted headcount - small enough Apollo has little on them
    elif emp < 5:
        score += 50
    elif emp < 15:
        score += 30
    elif emp < 50:
        score += 10
    # 50+ employees: no points, likely has marketing resources already

    founded = lead.get("founded_year")
    try:
        founded = int(founded)
        years_in_business = date.today().year - founded
        if years_in_business <= 3:
            score += 50  # newer business, more likely actively looking to grow
        elif years_in_business <= 8:
            score += 25
    except (TypeError, ValueError):
        pass

    return round(min(score, 100), 1)


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def load_existing_place_ids(path):
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row["place_id"] for row in reader if row.get("place_id")}


def write_csv(path, rows, append=False):
    file_exists = os.path.exists(path)
    mode = "a" if append and file_exists else "w"
    with open(path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if mode == "w" or not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    raw_leads = scrape_places()

    existing_ids = load_existing_place_ids(MASTER_CSV)
    print(f"{len(existing_ids)} leads already in master list; will skip duplicates on append.", flush=True)

    final_rows = []
    for lead in raw_leads:
        if lead.get("business_status") not in ("OPERATIONAL", ""):
            continue  # skip permanently/temporarily closed businesses

        apollo_data = apollo_enrich(lead)
        lead.update(apollo_data)

        web_score = score_web_opportunity(lead)
        size_score = score_size_opportunity(lead)
        total_score = round(web_score * SCORE_WEIGHT_WEB + size_score * SCORE_WEIGHT_SIZE, 1)

        row = {
            "place_id": lead["place_id"],
            "business_name": lead.get("business_name", ""),
            "phone": lead.get("phone", ""),
            "address": lead.get("address", ""),
            "city": "",
            "website": lead.get("website", ""),
            "google_rating": lead.get("google_rating", ""),
            "google_review_count": lead.get("google_review_count", ""),
            "business_status": lead.get("business_status", ""),
            "apollo_matched": lead.get("apollo_matched", False),
            "employee_count": lead.get("employee_count", ""),
            "founded_year": lead.get("founded_year", ""),
            "linkedin_url": lead.get("linkedin_url", ""),
            "contact_name": lead.get("contact_name", ""),
            "contact_title": lead.get("contact_title", ""),
            "contact_email": lead.get("contact_email", ""),
            "score_web_opportunity": web_score,
            "score_size_opportunity": size_score,
            "opportunity_score": total_score,
            "source": "google_places" + ("+apollo" if lead.get("apollo_matched") else ""),
            "date_scraped": TODAY,
        }
        final_rows.append(row)

    final_rows.sort(key=lambda r: r["opportunity_score"], reverse=True)

    write_csv(RUN_CSV, final_rows, append=False)
    print(f"Wrote {len(final_rows)} leads to {RUN_CSV}", flush=True)

    new_for_master = [r for r in final_rows if r["place_id"] not in existing_ids]
    write_csv(MASTER_CSV, new_for_master, append=True)
    print(f"Appended {len(new_for_master)} new leads to {MASTER_CSV} "
          f"({len(existing_ids) + len(new_for_master)} total in master)", flush=True)


if __name__ == "__main__":
    main()
