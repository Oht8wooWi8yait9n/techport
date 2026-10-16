#!/usr/bin/env python3
"""
NASA TechPort (techport.nasa.gov) Harvester, Static Mirror & Sitemap Generator
==============================================================================
Harvests authoritative NASA technology portfolio data from the public TechPort REST API:
  1. Active NASA Technology Projects (/projects/<projectId>) via bulk Solr search
     (POST /api/projects/search with fresh GET /api/nonce per page, all 34 schema fields)
  2. 2024 NASA Technology Taxonomy (/taxonomy/8817 & /taxonomy) — all 17 TX areas
     (TX01–TX17) and all 495 level-1/2/3 taxonomy nodes with descriptions & examples
  3. STMD Civil Space Shortfalls & Strategic Capabilities (/strategy) — all 187 shortfalls
     and 19 strategic capabilities with priority rankings and descriptions
  4. NASA Technology Programs Directory (/technologies) — all 98 NASA technology programs
     across STMD, ESDMD, ARMD, SMD, and SOMD
  5. NASA Technology Funding Opportunities (/opportunities) — all 36 funding opportunities
     with TRL ranges, solver roles, funding types, and solicitation links
  6. STMD Technology Infusion Impact Stories (/impactStories & /impactStories/<id>)

Generates:
  - Pre-rendered semantic HTML pages matching exact live TechPort routes for Onyx URL rewrite:
      * projects/<projectId>.html        -> https://techport.nasa.gov/projects/<projectId>
      * impactStories/<storyId>.html     -> https://techport.nasa.gov/impactStories/<storyId>
      * impactStories.html               -> https://techport.nasa.gov/impactStories
      * taxonomy/8817.html & taxonomy.html -> https://techport.nasa.gov/taxonomy/8817
      * strategy.html                    -> https://techport.nasa.gov/strategy
      * opportunities.html               -> https://techport.nasa.gov/opportunities
      * technologies.html                -> https://techport.nasa.gov/technologies
  - Primary XML Sitemap (sitemap.xml & techport_sitemap.xml) with GitHub Pages mirror URLs
  - Direct NASA XML Sitemap (techport_direct_sitemap.xml) with live techport.nasa.gov URLs
  - Plain text URL lists (techport_urls.txt & techport_direct_urls.txt)
  - Unified JSONL dataset (techport_records.jsonl) across all 6 TechPort knowledge domains
  - Interactive client-side searchable catalog (index.html)
"""

import os
import sys
import re
import json
import time
import socket
from html import escape
from datetime import datetime, timezone
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")
TAXONOMY_DIR = os.path.join(BASE_DIR, "taxonomy")
STORIES_DIR = os.path.join(BASE_DIR, "impactStories")
CACHE_DIR = os.path.join(BASE_DIR, ".techport_cache")
RAW_CACHE_DIR = os.path.join(CACHE_DIR, "raw")

JSONL_FILE = os.path.join(BASE_DIR, "techport_records.jsonl")
ACTIVE_JSONL_FILE = os.path.join(BASE_DIR, "techport_active_records.jsonl")
SITEMAP_FILE = os.path.join(BASE_DIR, "sitemap.xml")
SITEMAP_ALIAS_FILE = os.path.join(BASE_DIR, "techport_sitemap.xml")
DIRECT_SITEMAP_FILE = os.path.join(BASE_DIR, "techport_direct_sitemap.xml")
ACTIVE_SITEMAP_FILE = os.path.join(BASE_DIR, "techport_active_sitemap.xml")
ACTIVE_DIRECT_SITEMAP_FILE = os.path.join(BASE_DIR, "techport_active_direct_sitemap.xml")
URLS_FILE = os.path.join(BASE_DIR, "techport_urls.txt")
DIRECT_URLS_FILE = os.path.join(BASE_DIR, "techport_direct_urls.txt")
ACTIVE_URLS_FILE = os.path.join(BASE_DIR, "techport_active_urls.txt")
INDEX_HTML = os.path.join(BASE_DIR, "index.html")

TECHPORT_HOST = "https://techport.nasa.gov"
PUBLIC_ALB_HOST = "dualstack.vpc7lb-1240402781.us-gov-west-1.elb.amazonaws.com"
PUBLIC_ALB_IPS = ["3.31.216.201", "40.39.123.16"]

GH_PAGES_BASE = os.environ.get("GH_PAGES_BASE", "https://oht8woowi8yait9n.github.io/techport").rstrip("/")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

ALL_PROJECT_FIELDS = [
    "projectId", "title", "acronym", "status", "description", "benefits",
    "primaryImage", "program", "programId", "responsibleMd",
    "trlBegin", "trlCurrent", "trlEnd", "startMonth", "startYear",
    "endMonth", "endYear", "startDate", "endDate", "viewCount", "phase",
    "primaryTx", "additionalTxs", "projectContacts", "programContacts",
    "leadOrganization", "otherOrganizations", "states", "destinationTypes",
    "technologyOutcomes", "closeoutDocumentation", "libraryItems",
    "lastUpdated", "modifiedDate"
]


def configure_public_dns_override():
    """
    Ensure techport.nasa.gov resolves to the public AWS GovCloud ALB rather than
    the internal NASA split-horizon ALB (which redirects to Launchpad SAML).
    Works identically inside the NASA network and on public GitHub Actions runners.
    """
    orig_getaddrinfo = socket.getaddrinfo

    def custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host == "techport.nasa.gov":
            try:
                return orig_getaddrinfo(PUBLIC_ALB_HOST, port, family, type, proto, flags)
            except Exception:
                return orig_getaddrinfo(PUBLIC_ALB_IPS[0], port, family, type, proto, flags)
        return orig_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = custom_getaddrinfo


def create_session() -> requests.Session:
    """Create a requests Session configured for the public TechPort API."""
    configure_public_dns_override()
    session = requests.Session()
    # Ignore internal corporate proxy env vars so split-horizon DNS override is respected
    if os.environ.get("USE_SYSTEM_PROXY") != "1":
        session.trust_env = False
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    })
    return session


def api_get(session: requests.Session, path: str, cache_name: str = None, max_retries: int = 4):
    """GET JSON from TechPort API with local disk cache and exponential backoff."""
    if cache_name:
        cache_path = os.path.join(RAW_CACHE_DIR, cache_name)
        if os.path.exists(cache_path) and os.environ.get("FORCE_REFRESH") != "1":
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

    url = f"{TECHPORT_HOST}{path}"
    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if cache_name:
                os.makedirs(RAW_CACHE_DIR, exist_ok=True)
                with open(os.path.join(RAW_CACHE_DIR, cache_name), "w", encoding="utf-8") as f:
                    json.dump(data, f)
            return data
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"[!] GET {url} failed after {max_retries} attempts: {e}", file=sys.stderr)
                return {}
            time.sleep((2 ** attempt) * 0.8)
    return {}


def get_fresh_nonce(session: requests.Session) -> str:
    """Fetch a single-use nonce token required for POST requests to TechPort."""
    data = api_get(session, "/api/nonce", cache_name=None)
    return data.get("nonce", "")


def harvest_projects(session: requests.Session, include_completed: bool = False) -> list:
    """
    Harvest projects via bulk Solr endpoint POST /api/projects/search.
    Returns all 21,156 NASA TechPort projects with all 34 schema fields.
    """
    os.makedirs(RAW_CACHE_DIR, exist_ok=True)
    mode_tag = "all" if include_completed else "active"
    consolidated_cache = os.path.join(RAW_CACHE_DIR, f"projects_{mode_tag}.json")

    if os.path.exists(consolidated_cache) and os.environ.get("FORCE_REFRESH") != "1":
        try:
            with open(consolidated_cache, "r", encoding="utf-8") as f:
                cached = json.load(f)
                print(f"[+] Loaded {len(cached):,} cached projects from {consolidated_cache}")
                return cached
        except Exception:
            pass

    filters = []
    if not include_completed:
        filters = [{"field": "status", "name": "Active", "value": "Active"}]

    limit = 1000
    offset = 0
    all_projects = []
    seen_ids = set()

    print(f"[*] Harvesting NASA TechPort projects via POST /api/projects/search (limit={limit})...")
    while True:
        nonce = get_fresh_nonce(session)
        if not nonce:
            raise RuntimeError("Failed to obtain single-use nonce from /api/nonce")

        payload = {
            "nonce": nonce,
            "query": "",
            "queryType": "all",
            "fetchForListView": False,
            "fields": ALL_PROJECT_FIELDS,
            "filters": filters,
            "getStats": False,
            "limit": limit,
            "offset": offset,
            "paginationChange": False,
        }

        t0 = time.time()
        resp = session.post(f"{TECHPORT_HOST}/api/projects/search", json=payload, timeout=45)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
        total = data.get("total") or 0

        for proj in results:
            pid = proj.get("projectId")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                all_projects.append(proj)

        print(
            f"    Page offset={offset:,}: fetched {len(results):,} projects "
            f"(accumulated {len(all_projects):,}/{total:,} in {time.time() - t0:.1f}s)"
        )

        if not results or len(results) < limit or offset + limit >= total:
            break
        offset += limit

    all_projects.sort(key=lambda p: int(p.get("projectId") or 0), reverse=True)
    with open(consolidated_cache, "w", encoding="utf-8") as f:
        json.dump(all_projects, f)

    return all_projects


def harvest_taxonomy(session: requests.Session) -> dict:
    """
    Harvest the complete 2024 NASA Technology Taxonomy (taxonomyRootId = 8817):
    - Flat list of all 495 nodes (Levels 1, 2, and 3) via GET /api/taxonomies/nodes?rootId=8817
      containing full code, title, description, and exampleTechnologies.
    - Builds the complete 3-level nested hierarchy (TX01–TX17 -> TX01.x -> TX01.x.y) from parentNodeId.
    """
    print("[*] Harvesting 2024 NASA Technology Taxonomy (Root 8817, TX01-TX17, all 495 nodes)...")
    roots_resp = api_get(session, "/api/taxonomies", cache_name="taxonomy_roots.json")
    all_nodes_resp = api_get(session, "/api/taxonomies/nodes?rootId=8817", cache_name="taxonomy_8817_flat.json")
    flat_nodes = all_nodes_resp.get("taxonomyNodes") or []

    # Sort flat nodes by code so children appear in canonical order (TX01.1, TX01.2, ...)
    flat_nodes_sorted = sorted(flat_nodes, key=lambda n: n.get("code") or "")

    wrappers_by_id = {}
    flat_detailed = {}
    for node in flat_nodes_sorted:
        nid = node.get("taxonomyNodeId")
        if not nid:
            continue
        wrappers_by_id[nid] = {"content": node, "children": []}
        flat_detailed[str(nid)] = node

    trees = []
    for node in flat_nodes_sorted:
        nid = node.get("taxonomyNodeId")
        parent_id = node.get("parentNodeId")
        lvl = node.get("level")
        wrapper = wrappers_by_id.get(nid)
        if not wrapper:
            continue
        if lvl == 1 or not parent_id or parent_id not in wrappers_by_id:
            trees.append(wrapper)
        else:
            wrappers_by_id[parent_id]["children"].append(wrapper)

    result = {
        "roots": roots_resp.get("taxonomyRoots") or [],
        "trees": trees,
        "flat_nodes": flat_nodes_sorted,
        "detailed_by_id": flat_detailed,
    }
    return result


def harvest_impact_stories(session: requests.Session) -> list:
    """Harvest all STMD Technology Infusion Impact Stories and their full details."""
    cache_file = os.path.join(RAW_CACHE_DIR, "impact_stories_full.json")
    if os.path.exists(cache_file) and os.environ.get("FORCE_REFRESH") != "1":
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    print("[*] Harvesting STMD Technology Infusion Impact Stories...")
    index_data = api_get(session, "/api/impactStories", cache_name="impact_stories_index.json")
    stories_summary = index_data.get("impactStories") or []
    detailed_stories = []
    for s in stories_summary:
        sid = s.get("impactStoryId")
        if not sid:
            continue
        detail_resp = api_get(session, f"/api/impactStories/{sid}", cache_name=f"impact_story_{sid}.json")
        story_obj = detail_resp.get("impactStory") or s
        detailed_stories.append(story_obj)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(detailed_stories, f)
    return detailed_stories


def clean_html_text(raw_html: str) -> str:
    """Strip HTML tags for plain-text excerpts while preserving whitespace."""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(raw_html))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def format_date_iso(dt_str: str, fallback: str) -> str:
    """Extract YYYY-MM-DD from an ISO timestamp string."""
    if dt_str and isinstance(dt_str, str) and len(dt_str) >= 10:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", dt_str)
        if m:
            return m.group(1)
    return fallback


BASE_CSS = """
    :root {
      --primary: #0b3d91;
      --primary-dark: #061f4a;
      --accent: #fc3d21;
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --text: #0f172a;
      --text-muted: #475569;
      --border: #e2e8f0;
      --badge-bg: #e0f2fe;
      --badge-text: #0369a1;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.65;
      padding: 24px 16px;
    }
    .container { max-width: 1000px; margin: 0 auto; }
    .breadcrumb {
      font-size: 0.875rem;
      color: var(--text-muted);
      margin-bottom: 16px;
    }
    .breadcrumb a { color: var(--primary); text-decoration: none; font-weight: 500; }
    .breadcrumb a:hover { text-decoration: underline; }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
      overflow: hidden;
      margin-bottom: 24px;
    }
    .header {
      background: linear-gradient(135deg, var(--primary-dark) 0%, var(--primary) 100%);
      color: #ffffff;
      padding: 28px 32px;
    }
    .badge-row { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
    .badge {
      display: inline-block;
      background: rgba(255, 255, 255, 0.18);
      color: #ffffff;
      padding: 4px 12px;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .badge-active { background: #10b981; color: #ffffff; }
    .header h1 { font-size: 1.65rem; font-weight: 700; line-height: 1.3; margin-bottom: 8px; }
    .meta-id { font-size: 0.9rem; opacity: 0.9; font-family: monospace; }
    .content { padding: 32px; }
    .action-banner {
      background: #f0f9ff;
      border: 1px solid #bae6fd;
      border-radius: 8px;
      padding: 16px 20px;
      margin-bottom: 28px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
    }
    .action-banner-text { font-size: 0.9rem; color: #0c4a6e; }
    .btn {
      display: inline-block;
      background: var(--primary);
      color: #ffffff;
      text-decoration: none;
      padding: 8px 16px;
      border-radius: 6px;
      font-weight: 600;
      font-size: 0.875rem;
    }
    .btn:hover { background: var(--primary-dark); }
    h2 {
      font-size: 1.2rem;
      color: var(--primary-dark);
      border-bottom: 2px solid var(--border);
      padding-bottom: 8px;
      margin: 28px 0 14px 0;
    }
    h2:first-of-type { margin-top: 0; }
    h3 { font-size: 1.05rem; color: var(--text); margin: 18px 0 8px 0; }
    .prose {
      background: #f8fafc;
      border-left: 4px solid var(--primary);
      padding: 16px 20px;
      border-radius: 0 8px 8px 0;
      margin-bottom: 20px;
      color: #1e293b;
    }
    .prose p { margin-bottom: 10px; }
    .prose p:last-child { margin-bottom: 0; }
    table { width: 100%; border-collapse: collapse; margin-top: 8px; margin-bottom: 20px; }
    th, td {
      text-align: left;
      padding: 11px 14px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
      font-size: 0.925rem;
    }
    th { width: 28%; color: var(--text-muted); font-weight: 600; background: #f8fafc; }
    ul.item-list { padding-left: 20px; margin-bottom: 16px; }
    ul.item-list li { margin-bottom: 6px; }
    .subcard {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px 20px;
      margin-bottom: 14px;
      background: #ffffff;
    }
    .subcard-header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 8px;
    }
    .code-pill {
      font-family: monospace;
      font-weight: 700;
      background: var(--badge-bg);
      color: var(--badge-text);
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 0.85rem;
    }
    .footer {
      text-align: center;
      font-size: 0.8rem;
      color: var(--text-muted);
      margin-top: 28px;
      padding-top: 16px;
      border-top: 1px solid var(--border);
    }
    .footer a { color: var(--primary); text-decoration: none; }
"""


def format_org(org: dict) -> str:
    if not org or not isinstance(org, dict):
        return ""
    name = org.get("organization_name") or org.get("organizationName") or ""
    acr = org.get("acronym") or ""
    city = org.get("city") or ""
    state = org.get("state_name") or org.get("stateName") or ""
    country = org.get("country_name") or org.get("countryName") or ""
    label = f"{name} ({acr})" if acr and acr not in name else name
    loc = ", ".join(x for x in [city, state, country] if x and x != "United States")
    if city and state and not loc:
        loc = f"{city}, {state}"
    elif state and not loc:
        loc = state
    return f"{label} — {loc}" if loc else label


def render_project_html(proj: dict) -> str:
    """Render a full semantic HTML page for a single NASA TechPort project."""
    pid = proj.get("projectId")
    title = proj.get("title") or f"NASA Technology Project {pid}"
    acronym = proj.get("acronym") or ""
    status = proj.get("status") or "Active"
    live_url = f"{TECHPORT_HOST}/projects/{pid}"

    program = proj.get("program") or {}
    prog_title = program.get("title") or ""
    prog_acr = program.get("acronym") or ""
    prog_str = f"{prog_title} ({prog_acr})" if prog_acr else prog_title

    resp_md = proj.get("responsibleMd") or {}
    md_name = resp_md.get("organization_name") or ""
    md_acr = resp_md.get("acronym") or ""
    md_str = f"{md_name} ({md_acr})" if md_acr else md_name

    primary_tx = proj.get("primaryTx") or {}
    ptx_str = f"{primary_tx.get('code', '')}: {primary_tx.get('title', '')}".strip(": ")

    add_txs = proj.get("additionalTxs") or []
    add_tx_strs = [
        f"{tx.get('code', '')}: {tx.get('title', '')}".strip(": ")
        for tx in add_txs if isinstance(tx, dict) and (tx.get("code") or tx.get("title"))
    ]

    trl_begin = proj.get("trlBegin")
    trl_curr = proj.get("trlCurrent")
    trl_end = proj.get("trlEnd")
    trl_parts = []
    if trl_begin is not None:
        trl_parts.append(f"Start TRL: {trl_begin}")
    if trl_curr is not None:
        trl_parts.append(f"Current TRL: {trl_curr}")
    if trl_end is not None:
        trl_parts.append(f"Target End TRL: {trl_end}")
    trl_str = " | ".join(trl_parts)

    start_date = (proj.get("startDate") or "")[:10]
    end_date = (proj.get("endDate") or "")[:10]
    phase = proj.get("phase")

    lead_org = format_org(proj.get("leadOrganization") or {})
    other_orgs = [format_org(o) for o in (proj.get("otherOrganizations") or []) if format_org(o)]
    states = [
        f"{s.get('name')} ({s.get('abbreviation')})" if s.get("abbreviation") else s.get("name", "")
        for s in (proj.get("states") or []) if isinstance(s, dict) and s.get("name")
    ]
    destinations = [str(d) for d in (proj.get("destinationTypes") or []) if d]

    proj_contacts = []
    for c in (proj.get("projectContacts") or []):
        if isinstance(c, dict) and c.get("full_name"):
            role = c.get("project_contact_role") or "Contact"
            email = f" <{c.get('email')}>" if c.get("email") else ""
            proj_contacts.append(f"{c['full_name']} ({role}){email}")

    prog_contacts = []
    for c in (proj.get("programContacts") or []):
        if isinstance(c, dict) and c.get("full_name"):
            role = c.get("program_contact_role") or "Program Contact"
            email = f" <{c.get('email')}>" if c.get("email") else ""
            prog_contacts.append(f"{c['full_name']} ({role}){email}")

    rows = [
        ("Project ID", str(pid)),
        ("Acronym", acronym),
        ("Project Status", status),
        ("Responsible Mission Directorate", md_str),
        ("NASA Program", prog_str),
        ("Primary Technology Taxonomy (TX)", ptx_str),
        ("Additional Taxonomy Areas", "; ".join(add_tx_strs)),
        ("Technology Readiness Level (TRL)", trl_str),
        ("Project Start Date", start_date),
        ("Project End Date", end_date),
        ("Project Phase", str(phase) if phase is not None else ""),
        ("Lead Organization / Center", lead_org),
        ("Supporting Organizations / Partners", "; ".join(other_orgs)),
        ("Work Locations (U.S. States)", ", ".join(states)),
        ("Exploration Destination Types", ", ".join(destinations)),
        ("Project Leadership & Investigators", "; ".join(proj_contacts)),
        ("Program Management Contacts", "; ".join(prog_contacts)),
        ("Last Updated", (proj.get("modifiedDate") or proj.get("lastUpdated") or "")[:10]),
    ]

    table_rows_html = "\n".join(
        f"<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>"
        for k, v in rows if v
    )

    desc_raw = proj.get("description") or "<p>No description provided.</p>"
    benefits_raw = proj.get("benefits") or ""

    benefits_section = ""
    if benefits_raw:
        benefits_section = f"""
        <h2>Anticipated Benefits &amp; Mission Applications</h2>
        <div class="prose">
          {benefits_raw}
        </div>
        """

    outcomes = proj.get("technologyOutcomes") or []
    outcomes_section = ""
    if outcomes:
        items_li = []
        for o in outcomes:
            if not isinstance(o, dict):
                continue
            ptype = o.get("path_type") or "Outcome"
            org_n = o.get("organization_name") or ""
            details = clean_html_text(o.get("details") or "")
            line = f"<strong>{escape(ptype)}</strong>"
            if org_n:
                line += f" ({escape(org_n)})"
            if details:
                line += f": {escape(details)}"
            items_li.append(f"<li>{line}</li>")
        if items_li:
            outcomes_section = f"<h2>Technology Transition &amp; Outcomes</h2><ul class='item-list'>{''.join(items_li)}</ul>"

    lib_items = proj.get("libraryItems") or []
    closeout_items = proj.get("closeoutDocumentation") or []
    docs_li = []
    for doc in closeout_items:
        if isinstance(doc, dict) and doc.get("title"):
            ctype = doc.get("closeout_type") or "Closeout Document"
            docs_li.append(f"<li><strong>[{escape(ctype)}]</strong> {escape(doc.get('title'))}</li>")
    for item in lib_items:
        if isinstance(item, dict) and (item.get("title") or item.get("url")):
            t = item.get("title") or item.get("url")
            u = item.get("url") or ""
            desc = clean_html_text(item.get("description") or "")
            entry = f"<strong>{escape(t)}</strong>"
            if u and u.startswith("http"):
                entry = f'<a href="{escape(u)}" target="_blank" rel="noopener noreferrer">{escape(t)}</a>'
            if desc:
                entry += f" — {escape(desc)}"
            docs_li.append(f"<li>{entry}</li>")

    docs_section = ""
    if docs_li:
        docs_section = f"<h2>Publications, References &amp; Library Items</h2><ul class='item-list'>{''.join(docs_li)}</ul>"

    full_heading = f"{title} ({acronym})" if acronym and acronym not in title else title

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape(full_heading)} | NASA TechPort Project {pid}</title>
  <meta name="description" content="{escape(clean_html_text(desc_raw)[:260])}">
  <link rel="canonical" href="{escape(live_url)}">
  <style>{BASE_CSS}</style>
</head>
<body>
  <div class="container">
    <div class="breadcrumb">
      <a href="../index.html">NASA TechPort Catalog</a> /
      <a href="../technologies.html">Technology Projects</a> /
      <span>Project {pid}</span>
    </div>

    <div class="card">
      <div class="header">
        <div class="badge-row">
          <span class="badge badge-active">{escape(status)}</span>
          {f'<span class="badge">{escape(md_acr or md_name)}</span>' if (md_acr or md_name) else ''}
          {f'<span class="badge">{escape(prog_acr or prog_title)}</span>' if (prog_acr or prog_title) else ''}
          {f'<span class="badge">{escape(primary_tx.get("code", ""))}</span>' if primary_tx.get("code") else ''}
        </div>
        <h1>{escape(full_heading)}</h1>
        <div class="meta-id">NASA TechPort Project ID: {pid}</div>
      </div>

      <div class="content">
        <div class="action-banner">
          <div class="action-banner-text">
            <strong>Official NASA Source:</strong> Space Technology Mission Directorate (STMD) TechPort Portfolio
          </div>
          <a href="{escape(live_url)}" target="_blank" rel="noopener noreferrer" class="btn">
            View Official Project on NASA TechPort &rarr;
          </a>
        </div>

        <h2>Project Description</h2>
        <div class="prose">
          {desc_raw}
        </div>

        {benefits_section}

        <h2>Technical Metadata &amp; Portfolio Classification</h2>
        <table>
          <tbody>
            {table_rows_html}
          </tbody>
        </table>

        {outcomes_section}
        {docs_section}
      </div>
    </div>

    <div class="footer">
      NASA TechPort &bull; National Aeronautics and Space Administration
      <br>
      Canonical Source URL: <a href="{escape(live_url)}">{escape(live_url)}</a>
    </div>
  </div>
</body>
</html>
"""


def render_taxonomy_html(taxonomy_data: dict, rel_root: str = ".") -> str:
    """Render the complete 2024 NASA Technology Taxonomy (TX01–TX17, all 495 nodes)."""
    live_url = f"{TECHPORT_HOST}/taxonomy/8817"
    trees = taxonomy_data.get("trees") or []

    sections_html = []
    total_nodes = 0

    def render_node(node_wrapper, depth=1):
        nonlocal total_nodes
        if not node_wrapper or not isinstance(node_wrapper, dict):
            return ""
        content = node_wrapper.get("content") or {}
        code = content.get("code") or ""
        title = content.get("title") or ""
        desc = content.get("description") or ""
        examples = content.get("exampleTechnologies") or ""
        nid = content.get("taxonomyNodeId") or ""
        total_nodes += 1

        children_html = "".join(render_node(c, depth + 1) for c in (node_wrapper.get("children") or []))

        if depth == 1:
            return f"""
            <section id="{escape(code)}" style="margin-bottom: 32px;">
              <h2>{escape(code)}: {escape(title)}</h2>
              <div class="subcard">
                {f'<p><strong>Overview:</strong> {escape(desc)}</p>' if desc else ''}
                {f'<p style="margin-top:6px;"><strong>Example Technologies:</strong> {escape(examples)}</p>' if examples else ''}
              </div>
              {children_html}
            </section>
            """
        else:
            indent = (depth - 2) * 18
            return f"""
            <div class="subcard" id="{escape(code)}" style="margin-left: {indent}px;">
              <div class="subcard-header">
                <h3><span class="code-pill">{escape(code)}</span> {escape(title)}</h3>
                <span style="font-size:0.78rem; color:#64748b;">Node ID: {nid} | Level {depth}</span>
              </div>
              {f'<p><strong>Definition &amp; Scope:</strong> {escape(desc)}</p>' if desc else ''}
              {f'<p style="margin-top:6px; color:#334155;"><strong>Example Technologies:</strong> {escape(examples)}</p>' if examples else ''}
            </div>
            {children_html}
            """

    for tree in trees:
        sections_html.append(render_node(tree, depth=1))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>2024 NASA Technology Taxonomy (TX01–TX17 Complete Reference) | NASA TechPort</title>
  <meta name="description" content="Complete 2024 NASA Technology Taxonomy (TX01 through TX17, 495 technology areas, definitions, and example technologies) from NASA TechPort.">
  <link rel="canonical" href="{escape(live_url)}">
  <style>{BASE_CSS}</style>
</head>
<body>
  <div class="container">
    <div class="breadcrumb">
      <a href="{rel_root}/index.html">NASA TechPort Catalog</a> /
      <span>2024 NASA Technology Taxonomy (TX01–TX17)</span>
    </div>

    <div class="card">
      <div class="header">
        <div class="badge-row">
          <span class="badge badge-active">Released Standard</span>
          <span class="badge">Root ID: 8817</span>
          <span class="badge">17 TX Disciplines &bull; {total_nodes} Taxonomy Nodes</span>
        </div>
        <h1>2024 NASA Technology Taxonomy (TX01–TX17)</h1>
        <div class="meta-id">Authoritative NASA Technology Classification Structure</div>
      </div>

      <div class="content">
        <div class="action-banner">
          <div class="action-banner-text">
            <strong>Official NASA Taxonomy:</strong> Complete hierarchy of {total_nodes} technology areas (Levels 1, 2, and 3) with definitions and example technologies.
          </div>
          <a href="{escape(live_url)}" target="_blank" rel="noopener noreferrer" class="btn">
            Open Interactive Taxonomy on NASA TechPort &rarr;
          </a>
        </div>

        {''.join(sections_html)}
      </div>
    </div>

    <div class="footer">
      NASA TechPort &bull; 2024 NASA Technology Taxonomy
      <br>
      Canonical Source URL: <a href="{escape(live_url)}">{escape(live_url)}</a>
    </div>
  </div>
</body>
</html>
"""


def render_strategy_html(strategy_data: dict) -> str:
    """Render STMD Civil Space Shortfalls (187) & Strategic Capabilities (19) (/strategy)."""
    live_url = f"{TECHPORT_HOST}/strategy"
    shortfalls = strategy_data.get("shortfalls") or []
    capabilities = strategy_data.get("capabilities") or []

    cap_sections = []
    for cap in capabilities:
        cname = cap.get("title") or cap.get("capability") or "Capability"
        cdesc = cap.get("description") or ""
        cap_sections.append(f"""
        <div class="subcard">
          <h3>{escape(cname)}</h3>
          {f'<p>{escape(cdesc)}</p>' if cdesc else ''}
        </div>
        """)

    # Group shortfalls by capability while also ordering by priority
    shortfalls_sorted = sorted(shortfalls, key=lambda s: (s.get("priority") or 9999, s.get("shortfallId") or 0))
    sf_cards = []
    for sf in shortfalls_sorted:
        sid = sf.get("shortfallId")
        stitle = sf.get("title") or ""
        scap = sf.get("capability") or "General"
        sdesc = sf.get("description") or ""
        sprio = sf.get("priority")
        sf_cards.append(f"""
        <div class="subcard" id="shortfall-{sid}">
          <div class="subcard-header">
            <h3><span class="code-pill">Shortfall ID #{sid}</span> {escape(stitle)}</h3>
            <span style="font-size:0.82rem; font-weight:600; color:#0369a1;">
              Capability: {escape(scap)} {f'| Priority Rank #{sprio}' if sprio is not None else ''}
            </span>
          </div>
          <p>{escape(sdesc)}</p>
        </div>
        """)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NASA STMD Civil Space Shortfalls &amp; Strategic Capabilities | NASA TechPort</title>
  <meta name="description" content="Complete catalog of {len(shortfalls)} NASA STMD Civil Space Shortfalls and {len(capabilities)} Strategic Capabilities prioritized to guide space technology development.">
  <link rel="canonical" href="{escape(live_url)}">
  <style>{BASE_CSS}</style>
</head>
<body>
  <div class="container">
    <div class="breadcrumb">
      <a href="index.html">NASA TechPort Catalog</a> /
      <span>STMD Civil Space Shortfalls &amp; Strategy</span>
    </div>

    <div class="card">
      <div class="header">
        <div class="badge-row">
          <span class="badge badge-active">STMD Strategic Framework</span>
          <span class="badge">{len(shortfalls)} Civil Space Shortfalls</span>
          <span class="badge">{len(capabilities)} Strategic Capabilities</span>
        </div>
        <h1>NASA STMD Civil Space Shortfalls &amp; Strategic Framework</h1>
        <div class="meta-id">Space Technology Mission Directorate (STMD) Integrated Prioritization</div>
      </div>

      <div class="content">
        <div class="action-banner">
          <div class="action-banner-text">
            <strong>Official NASA Strategy:</strong> STMD's Civil Space Shortfalls identify high-priority pervasive technology gaps for Moon, Mars, and civil space missions.
          </div>
          <a href="{escape(live_url)}" target="_blank" rel="noopener noreferrer" class="btn">
            View Official Strategy Page on NASA TechPort &rarr;
          </a>
        </div>

        <h2>Strategic Capabilities ({len(capabilities)})</h2>
        {''.join(cap_sections)}

        <h2>Prioritized Civil Space Shortfalls ({len(shortfalls)})</h2>
        {''.join(sf_cards)}
      </div>
    </div>

    <div class="footer">
      NASA TechPort &bull; STMD Civil Space Shortfalls
      <br>
      Canonical Source URL: <a href="{escape(live_url)}">{escape(live_url)}</a>
    </div>
  </div>
</body>
</html>
"""


def render_opportunities_html(opportunities: list) -> str:
    """Render NASA Technology Funding Opportunities (/opportunities)."""
    live_url = f"{TECHPORT_HOST}/opportunities"
    opp_cards = []
    for opp in opportunities:
        oid = opp.get("opportunityId")
        name = opp.get("name") or f"Opportunity {oid}"
        desc = opp.get("description") or ""
        sol_url = opp.get("solicitationUrl") or ""
        web_url = opp.get("websiteUrl") or ""
        roles = ", ".join(opp.get("roles") or [])
        trls = ", ".join(str(t) for t in (opp.get("trlValues") or []))
        ftype = opp.get("fundingType") or ""

        links_html = []
        if web_url:
            links_html.append(f'<a href="{escape(web_url)}" target="_blank" rel="noopener noreferrer">Program Website &nearr;</a>')
        if sol_url:
            links_html.append(f'<a href="{escape(sol_url)}" target="_blank" rel="noopener noreferrer">Solicitation / NSPIRES &nearr;</a>')

        opp_cards.append(f"""
        <div class="subcard" id="opportunity-{oid}">
          <div class="subcard-header">
            <h3>{escape(name)}</h3>
            {f'<span class="code-pill">{escape(ftype)}</span>' if ftype else ''}
          </div>
          <p style="margin-bottom:10px;">{escape(clean_html_text(desc))}</p>
          <p style="font-size:0.86rem; color:#475569;">
            {f'<strong>Applicable TRLs:</strong> {escape(trls)} &bull; ' if trls else ''}
            {f'<strong>Eligible Organizations / Roles:</strong> {escape(roles)}' if roles else ''}
          </p>
          {f'<p style="margin-top:8px; font-size:0.88rem;">{" &nbsp;|&nbsp; ".join(links_html)}</p>' if links_html else ''}
        </div>
        """)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NASA Technology Funding Opportunities | NASA TechPort</title>
  <meta name="description" content="Directory of {len(opportunities)} NASA technology funding opportunities, grants, prizes, SBIR/STTR, NIAC, and Tipping Point solicitations.">
  <link rel="canonical" href="{escape(live_url)}">
  <style>{BASE_CSS}</style>
</head>
<body>
  <div class="container">
    <div class="breadcrumb">
      <a href="index.html">NASA TechPort Catalog</a> /
      <span>Funding Opportunities</span>
    </div>

    <div class="card">
      <div class="header">
        <div class="badge-row">
          <span class="badge badge-active">Active Solicitations &amp; Programs</span>
          <span class="badge">{len(opportunities)} Funding Mechanisms</span>
        </div>
        <h1>NASA Technology Funding Opportunities</h1>
        <div class="meta-id">Grants, Cooperative Agreements, SBIR/STTR, Prizes &amp; Flight Opportunities</div>
      </div>

      <div class="content">
        <div class="action-banner">
          <div class="action-banner-text">
            <strong>Official NASA Resource:</strong> Identify NASA technology programs and solicitations matching your organization's capabilities and TRL maturity.
          </div>
          <a href="{escape(live_url)}" target="_blank" rel="noopener noreferrer" class="btn">
            Explore Funding Opportunities on NASA TechPort &rarr;
          </a>
        </div>

        <h2>All NASA Technology Funding Opportunities ({len(opportunities)})</h2>
        {''.join(opp_cards)}
      </div>
    </div>

    <div class="footer">
      NASA TechPort &bull; Funding Opportunities
      <br>
      Canonical Source URL: <a href="{escape(live_url)}">{escape(live_url)}</a>
    </div>
  </div>
</body>
</html>
"""


def render_programs_html(programs: list, active_project_count: int) -> str:
    """Render NASA Technology Programs Directory (/technologies)."""
    live_url = f"{TECHPORT_HOST}/technologies"
    prog_cards = []
    for p in sorted(programs, key=lambda x: (not x.get("isActive", False), x.get("title") or "")):
        pid = p.get("programId")
        title = p.get("title") or ""
        acr = p.get("acronym") or ""
        is_active = p.get("isActive", False)
        desc = p.get("description") or ""
        resp_md = p.get("responsibleMd") or {}
        md_str = resp_md.get("acronym") or resp_md.get("organizationName") or ""
        parent = p.get("parentProgram") or {}
        parent_str = parent.get("title") or ""

        heading = f"{title} ({acr})" if acr and acr not in title else title
        prog_cards.append(f"""
        <div class="subcard" id="program-{pid}">
          <div class="subcard-header">
            <h3>{escape(heading)}</h3>
            <div>
              {f'<span class="code-pill">{escape(md_str)}</span>' if md_str else ''}
              <span class="code-pill" style="background:{'#dcfce7' if is_active else '#f1f5f9'}; color:{'#166534' if is_active else '#475569'};">
                {'Active Program' if is_active else 'Historical Program'}
              </span>
            </div>
          </div>
          {f'<p style="font-size:0.84rem; color:#64748b; margin-bottom:6px;">Parent Program: {escape(parent_str)}</p>' if parent_str else ''}
          <div class="prose" style="margin-top:8px; margin-bottom:0;">
            {desc if desc else '<p>NASA technology development program tracked in TechPort.</p>'}
          </div>
        </div>
        """)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NASA Technology Programs &amp; Portfolio Directory | NASA TechPort</title>
  <meta name="description" content="Complete directory of {len(programs)} NASA Technology Programs and {active_project_count:,} active technology projects across STMD, ESDMD, ARMD, SMD, and SOMD.">
  <link rel="canonical" href="{escape(live_url)}">
  <style>{BASE_CSS}</style>
</head>
<body>
  <div class="container">
    <div class="breadcrumb">
      <a href="index.html">NASA TechPort Catalog</a> /
      <span>NASA Technology Programs &amp; Portfolio</span>
    </div>

    <div class="card">
      <div class="header">
        <div class="badge-row">
          <span class="badge badge-active">{len(programs)} NASA Technology Programs</span>
          <span class="badge">{active_project_count:,} Active Projects</span>
        </div>
        <h1>NASA Technology Programs &amp; Portfolio Directory</h1>
        <div class="meta-id">Authoritative Directory of NASA Mission Directorate Technology Programs</div>
      </div>

      <div class="content">
        <div class="action-banner">
          <div class="action-banner-text">
            <strong>Official NASA Portfolio:</strong> Browse all {len(programs)} NASA technology programs across STMD, ARMD, ESDMD, SMD, and SOMD.
          </div>
          <a href="{escape(live_url)}" target="_blank" rel="noopener noreferrer" class="btn">
            Search All Projects on NASA TechPort &rarr;
          </a>
        </div>

        <h2>NASA Technology Programs ({len(programs)})</h2>
        {''.join(prog_cards)}
      </div>
    </div>

    <div class="footer">
      NASA TechPort &bull; Technology Programs Directory
      <br>
      Canonical Source URL: <a href="{escape(live_url)}">{escape(live_url)}</a>
    </div>
  </div>
</body>
</html>
"""


def render_impact_story_html(story: dict) -> str:
    """Render an individual STMD Technology Infusion Impact Story (/impactStories/<id>)."""
    sid = story.get("impactStoryId")
    title = story.get("title") or f"Impact Story {sid}"
    desc = story.get("description") or ""
    date_str = story.get("dateString") or ""
    live_url = f"{TECHPORT_HOST}/impactStories/{sid}"

    sections_html = []
    for sec in (story.get("content") or []):
        if not isinstance(sec, dict):
            continue
        sh = sec.get("heading") or ""
        sb = sec.get("body") or ""
        sq = sec.get("quote") or ""
        sqa = sec.get("quoteAttribution") or ""
        block = ""
        if sh:
            block += f"<h2>{escape(sh)}</h2>"
        if sb:
            block += f"<div class='prose'>{sb}</div>"
        if sq:
            block += f"<blockquote class='subcard'><em>&ldquo;{escape(sq)}&rdquo;</em>{f'<br><strong>— {escape(sqa)}</strong>' if sqa else ''}</blockquote>"
        sections_html.append(block)

    linked = story.get("linkedProjects") or []
    linked_li = []
    for lp in linked:
        if isinstance(lp, dict) and lp.get("projectId"):
            lpid = lp["projectId"]
            lptitle = lp.get("title") or f"Project {lpid}"
            linked_li.append(
                f'<li><a href="../projects/{lpid}.html">{escape(lptitle)} (Project #{lpid})</a> '
                f'&bull; <a href="{TECHPORT_HOST}/projects/{lpid}" target="_blank" rel="noopener noreferrer">Live on TechPort &nearr;</a></li>'
            )

    linked_section = ""
    if linked_li:
        linked_section = f"<h2>Linked NASA Technology Projects</h2><ul class='item-list'>{''.join(linked_li)}</ul>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape(title)} | NASA TechPort Impact Story</title>
  <meta name="description" content="{escape(clean_html_text(desc)[:260])}">
  <link rel="canonical" href="{escape(live_url)}">
  <style>{BASE_CSS}</style>
</head>
<body>
  <div class="container">
    <div class="breadcrumb">
      <a href="../index.html">NASA TechPort Catalog</a> /
      <a href="../impactStories.html">Impact Stories</a> /
      <span>{escape(title)}</span>
    </div>

    <div class="card">
      <div class="header">
        <div class="badge-row">
          <span class="badge badge-active">STMD Technology Infusion Impact Story</span>
          {f'<span class="badge">{escape(date_str)}</span>' if date_str else ''}
        </div>
        <h1>{escape(title)}</h1>
        <div class="meta-id">Impact Story ID: {sid}</div>
      </div>

      <div class="content">
        <div class="action-banner">
          <div class="action-banner-text">
            <strong>Official NASA Impact Story:</strong> Following STMD-funded technologies from concept to flight infusion and commercialization.
          </div>
          <a href="{escape(live_url)}" target="_blank" rel="noopener noreferrer" class="btn">
            Read Official Impact Story on NASA TechPort &rarr;
          </a>
        </div>

        <div class="prose">
          <p><strong>Summary:</strong> {escape(desc)}</p>
        </div>

        {''.join(sections_html)}
        {linked_section}
      </div>
    </div>

    <div class="footer">
      NASA TechPort &bull; STMD Impact Stories
      <br>
      Canonical Source URL: <a href="{escape(live_url)}">{escape(live_url)}</a>
    </div>
  </div>
</body>
</html>
"""


def render_impact_stories_index_html(stories: list) -> str:
    """Render the Impact Stories overview page (/impactStories)."""
    live_url = f"{TECHPORT_HOST}/impactStories"
    cards = []
    for s in stories:
        sid = s.get("impactStoryId")
        title = s.get("title") or f"Impact Story {sid}"
        desc = s.get("description") or ""
        date_str = s.get("dateString") or ""
        cards.append(f"""
        <div class="subcard">
          <div class="subcard-header">
            <h3><a href="impactStories/{sid}.html" style="color:var(--primary); text-decoration:none;">{escape(title)}</a></h3>
            {f'<span class="code-pill">{escape(date_str)}</span>' if date_str else ''}
          </div>
          <p style="margin-bottom:10px;">{escape(desc)}</p>
          <p style="font-size:0.88rem;">
            <a href="impactStories/{sid}.html">Read Full Story Mirror &rarr;</a> &nbsp;|&nbsp;
            <a href="{TECHPORT_HOST}/impactStories/{sid}" target="_blank" rel="noopener noreferrer">Official NASA Page &nearr;</a>
          </p>
        </div>
        """)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NASA STMD Technology Infusion Impact Stories | NASA TechPort</title>
  <meta name="description" content="STMD Technology Infusion Impact Stories showcasing how NASA technology investments transition into flight missions and commercial breakthroughs.">
  <link rel="canonical" href="{escape(live_url)}">
  <style>{BASE_CSS}</style>
</head>
<body>
  <div class="container">
    <div class="breadcrumb">
      <a href="index.html">NASA TechPort Catalog</a> /
      <span>Impact Stories</span>
    </div>

    <div class="card">
      <div class="header">
        <div class="badge-row">
          <span class="badge badge-active">Technology Infusion</span>
          <span class="badge">{len(stories)} Featured Impact Stories</span>
        </div>
        <h1>NASA STMD Technology Infusion Impact Stories</h1>
        <div class="meta-id">From Early-Stage Concept to Spaceflight Infusion &amp; Commercialization</div>
      </div>

      <div class="content">
        <div class="action-banner">
          <div class="action-banner-text">
            <strong>Official NASA Impact Stories:</strong> Explore how NASA drives innovation, development, demonstration, and commercialization of space technologies.
          </div>
          <a href="{escape(live_url)}" target="_blank" rel="noopener noreferrer" class="btn">
            View Impact Stories on NASA TechPort &rarr;
          </a>
        </div>

        {''.join(cards)}
      </div>
    </div>
  </div>
</body>
</html>
"""


def render_index_html(stats: dict, catalog_items: list) -> str:
    """Render the interactive client-side searchable catalog index.html."""
    total_records = sum(stats.values())
    json_catalog = json.dumps(catalog_items, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NASA TechPort Portfolio Mirror &amp; Searchable Catalog</title>
  <style>
    :root {{
      --primary: #0b3d91;
      --primary-dark: #061f4a;
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --text: #0f172a;
      --text-muted: #64748b;
      --border: #e2e8f0;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    header {{
      background: linear-gradient(135deg, var(--primary-dark) 0%, var(--primary) 100%);
      color: #ffffff;
      padding: 40px 20px;
      text-align: center;
    }}
    header h1 {{ font-size: 2rem; font-weight: 800; margin-bottom: 10px; }}
    header p {{ max-width: 780px; margin: 0 auto 20px auto; opacity: 0.92; font-size: 1rem; }}
    .stats-bar {{
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 10px;
      max-width: 1050px;
      margin: 0 auto;
    }}
    .stat-badge {{
      background: rgba(255, 255, 255, 0.14);
      border: 1px solid rgba(255, 255, 255, 0.25);
      padding: 6px 14px;
      border-radius: 999px;
      font-size: 0.82rem;
      font-weight: 600;
    }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 28px 16px; }}
    .controls {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 20px;
      box-shadow: 0 2px 4px rgba(0,0,0,0.03);
    }}
    .search-box {{
      width: 100%;
      padding: 12px 16px;
      font-size: 1rem;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      margin-bottom: 14px;
    }}
    .filter-pills {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .filter-pill {{
      border: 1px solid var(--border);
      background: #f1f5f9;
      color: var(--text);
      padding: 6px 14px;
      border-radius: 999px;
      font-size: 0.84rem;
      font-weight: 600;
      cursor: pointer;
    }}
    .filter-pill.active {{ background: var(--primary); color: #ffffff; border-color: var(--primary); }}
    .results-count {{ font-size: 0.9rem; color: var(--text-muted); margin-bottom: 14px; font-weight: 500; }}
    .cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
      gap: 16px;
      margin-bottom: 32px;
    }}
    .item-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 18px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      box-shadow: 0 1px 3px rgba(0,0,0,0.03);
    }}
    .item-type {{
      display: inline-block;
      color: #ffffff;
      font-size: 0.7rem;
      font-weight: 700;
      text-transform: uppercase;
      padding: 3px 9px;
      border-radius: 999px;
      margin-bottom: 8px;
    }}
    .item-title {{ font-size: 1rem; font-weight: 700; margin-bottom: 6px; color: var(--text); }}
    .item-meta {{ font-size: 0.82rem; color: var(--text-muted); margin-bottom: 12px; }}
    .item-links {{
      display: flex;
      justify-content: space-between;
      border-top: 1px solid var(--border);
      padding-top: 10px;
      font-size: 0.84rem;
      font-weight: 600;
    }}
    .item-link {{ color: var(--primary); text-decoration: none; }}
    .item-link:hover {{ text-decoration: underline; }}
    .sitemaps-box {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px;
    }}
    .sitemaps-box h3 {{ margin-bottom: 12px; color: var(--primary-dark); }}
    .sitemaps-box ul {{ padding-left: 20px; }}
    .sitemaps-box li {{ margin-bottom: 6px; font-size: 0.92rem; }}
    .sitemaps-box a {{ color: var(--primary); text-decoration: none; font-weight: 600; }}
  </style>
</head>
<body>
  <header>
    <h1>NASA TechPort Portfolio Mirror &amp; Catalog</h1>
    <p>Authoritative mirror and machine-readable knowledge base for NASA's active technology portfolio, 2024 NASA Technology Taxonomy (TX01–TX17), STMD Civil Space Shortfalls, Programs, and Funding Opportunities.</p>
    <div class="stats-bar">
      <span class="stat-badge">Active Projects: {stats.get('active_project', 0):,}</span>
      <span class="stat-badge">Total Mirrored Projects: {stats.get('total_project', 0):,}</span>
      <span class="stat-badge">2024 Taxonomy Nodes: {stats.get('taxonomy', 0):,}</span>
      <span class="stat-badge">Civil Space Shortfalls: {stats.get('shortfall', 0):,}</span>
      <span class="stat-badge">NASA Programs: {stats.get('program', 0):,}</span>
      <span class="stat-badge">Funding Opportunities: {stats.get('opportunity', 0):,}</span>
      <span class="stat-badge">Impact Stories: {stats.get('impact_story', 0):,}</span>
    </div>
  </header>

  <div class="container">
    <div class="controls">
      <input type="text" id="searchInput" class="search-box" placeholder="Search by project title, Project ID (e.g. 176542), TX code (e.g. TX01.1.1), TRL, NASA center, shortfall, program...">
      <div class="filter-pills" id="filterPills">
        <button class="filter-pill active" data-type="all">Featured &amp; Active Catalog ({len(catalog_items):,})</button>
        <button class="filter-pill" data-type="project">Active Projects ({stats.get('active_project', 0):,})</button>
        <button class="filter-pill" data-type="completed_project">Recent Completed Projects ({stats.get('completed_sample', 0):,})</button>
        <button class="filter-pill" data-type="taxonomy">2024 Taxonomy ({stats.get('taxonomy', 0):,})</button>
        <button class="filter-pill" data-type="shortfall">Civil Space Shortfalls ({stats.get('shortfall', 0):,})</button>
        <button class="filter-pill" data-type="program">NASA Programs ({stats.get('program', 0):,})</button>
        <button class="filter-pill" data-type="opportunity">Funding Opportunities ({stats.get('opportunity', 0):,})</button>
        <button class="filter-pill" data-type="impact_story">Impact Stories ({stats.get('impact_story', 0):,})</button>
      </div>
    </div>

    <div class="results-count" id="resultsCount">Loading catalog...</div>
    <div class="cards-grid" id="cardsGrid"></div>

    <div class="sitemaps-box">
      <h3>Machine-Readable Endpoints &amp; Sitemaps</h3>
      <ul>
        <li><strong>Active Portfolio XML Sitemap (Recommended for Onyx Web Connector — {stats.get('active_urls', 0):,} URLs):</strong> <a href="techport_active_sitemap.xml">techport_active_sitemap.xml</a> (<a href="techport_active_direct_sitemap.xml">Direct NASA URLs</a> &bull; <a href="techport_active_urls.txt">techport_active_urls.txt</a>)</li>
        <li><strong>Complete Historical Archive XML Sitemap ({stats.get('total_urls', 0):,} URLs):</strong> <a href="sitemap.xml">sitemap.xml</a> / <a href="techport_sitemap.xml">techport_sitemap.xml</a> (<a href="techport_direct_sitemap.xml">Direct NASA URLs</a> &bull; <a href="techport_urls.txt">techport_urls.txt</a>)</li>
        <li><strong>Unified JSONL Datasets:</strong> <a href="techport_active_records.jsonl">techport_active_records.jsonl</a> (Active Portfolio) &bull; <a href="techport_records.jsonl">techport_records.jsonl</a> (Complete Archive)</li>
        <li><strong>Reference Pages:</strong> <a href="taxonomy/8817.html">2024 NASA Technology Taxonomy (495 Nodes)</a> &bull; <a href="strategy.html">STMD Civil Space Shortfalls (187)</a> &bull; <a href="opportunities.html">Funding Opportunities (36)</a> &bull; <a href="technologies.html">NASA Programs (98)</a> &bull; <a href="impactStories.html">Impact Stories</a></li>
      </ul>
    </div>
  </div>

  <script>
    const CATALOG = {json_catalog};
    const COLORS = {{
      project: "#0284c7",
      completed_project: "#475569",
      taxonomy: "#7c3aed",
      shortfall: "#dc2626",
      program: "#059669",
      opportunity: "#d97706",
      impact_story: "#db2777"
    }};

    let currentFilter = "all";
    let searchQuery = "";

    const searchInput = document.getElementById("searchInput");
    const filterPills = document.getElementById("filterPills");
    const cardsGrid = document.getElementById("cardsGrid");
    const resultsCount = document.getElementById("resultsCount");

    function renderCards() {{
      const q = searchQuery.toLowerCase().trim();
      const filtered = CATALOG.filter(item => {{
        const matchesType = (currentFilter === "all" || item.type === currentFilter);
        const matchesSearch = !q ||
          item.title.toLowerCase().includes(q) ||
          (item.meta && item.meta.toLowerCase().includes(q)) ||
          (item.summary && item.summary.toLowerCase().includes(q));
        return matchesType && matchesSearch;
      }});

      resultsCount.textContent = `Showing ${{Math.min(filtered.length, 120).toLocaleString()}} of ${{filtered.length.toLocaleString()}} matching catalog records (out of {stats.get('total_project', 0):,} total mirrored projects)`;

      if (filtered.length === 0) {{
        const numMatch = q.match(/^\\d+$/);
        const directLookup = numMatch
          ? `<div style="margin-top:12px;"><a href="projects/${{q}}.html" style="color:#0b3d91;font-weight:700;">Open Mirrored Project #${{q}} Directly &rarr;</a> &nbsp;|&nbsp; <a href="https://techport.nasa.gov/projects/${{q}}" target="_blank" rel="noopener noreferrer" style="color:#0b3d91;font-weight:700;">View Project #${{q}} on NASA TechPort &nearr;</a></div>`
          : '';
        cardsGrid.innerHTML = `<div style="grid-column: 1/-1; padding: 40px; text-align: center; color: #64748b;">No inline catalog records match your filter.${{directLookup}}</div>`;
        return;
      }}

      cardsGrid.innerHTML = filtered.slice(0, 120).map(item => `
        <div class="item-card">
          <div>
            <span class="item-type" style="background: ${{COLORS[item.type] || '#0284c7'}}">${{item.label}}</span>
            <h3 class="item-title">${{item.title}}</h3>
            <div class="item-meta">${{item.meta || ''}}</div>
          </div>
          <div class="item-links">
            <a href="${{item.local_href}}" class="item-link">Mirror Page &rarr;</a>
            <a href="${{item.live_url}}" target="_blank" rel="noopener noreferrer" class="item-link">NASA TechPort &nearr;</a>
          </div>
        </div>
      `).join("");
    }}

    searchInput.addEventListener("input", e => {{
      searchQuery = e.target.value;
      renderCards();
    }});

    filterPills.addEventListener("click", e => {{
      const btn = e.target.closest(".filter-pill");
      if (!btn) return;
      document.querySelectorAll(".filter-pill").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      currentFilter = btn.dataset.type;
      renderCards();
    }});

    renderCards();
  </script>
</body>
</html>
"""


def write_sitemap_files(pages: list, xml_path: str, direct_xml_path: str, urls_path: str, direct_urls_path: str = None, alias_xml_path: str = None):
    """Write XML sitemaps and plain-text URL lists for a given page set."""
    mirror_entries = []
    direct_entries = []
    mirror_urls = []
    direct_urls = []

    for page in pages:
        m_url = page["mirror_url"]
        d_url = page["live_url"]
        lmod = page["lastmod"]
        mirror_urls.append(m_url)
        direct_urls.append(d_url)
        mirror_entries.append(
            f"  <url>\n    <loc>{escape(m_url)}</loc>\n    <lastmod>{lmod}</lastmod>\n    <changefreq>weekly</changefreq>\n  </url>"
        )
        direct_entries.append(
            f"  <url>\n    <loc>{escape(d_url)}</loc>\n    <lastmod>{lmod}</lastmod>\n    <changefreq>weekly</changefreq>\n  </url>"
        )

    sitemap_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(mirror_entries)}
</urlset>
"""
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(sitemap_xml)
    if alias_xml_path:
        with open(alias_xml_path, "w", encoding="utf-8") as f:
            f.write(sitemap_xml)

    direct_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(direct_entries)}
</urlset>
"""
    with open(direct_xml_path, "w", encoding="utf-8") as f:
        f.write(direct_xml)

    with open(urls_path, "w", encoding="utf-8") as f:
        f.write("\n".join(mirror_urls) + "\n")
    if direct_urls_path:
        with open(direct_urls_path, "w", encoding="utf-8") as f:
            f.write("\n".join(direct_urls) + "\n")


def main():
    print("=" * 80)
    print(" NASA TechPort (techport.nasa.gov) Harvester, Mirror & Sitemap Generator")
    print("=" * 80)

    include_completed = os.environ.get("INCLUDE_COMPLETED") == "1" or "--all-statuses" in sys.argv
    session = create_session()
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. Harvest all domains
    projects = harvest_projects(session, include_completed=include_completed)
    taxonomy_data = harvest_taxonomy(session)
    strategy_data = api_get(session, "/api/strategy", cache_name="strategy.json")
    programs_data = api_get(session, "/api/programs", cache_name="programs.json")
    opportunities_data = api_get(session, "/api/opportunities", cache_name="opportunities.json")
    impact_stories = harvest_impact_stories(session)

    programs = programs_data.get("programs") or []
    opportunities = opportunities_data.get("opportunities") or []
    shortfalls = strategy_data.get("shortfalls") or []
    detailed_tx = taxonomy_data.get("detailed_by_id") or {}

    os.makedirs(PROJECTS_DIR, exist_ok=True)
    os.makedirs(TAXONOMY_DIR, exist_ok=True)
    os.makedirs(STORIES_DIR, exist_ok=True)

    sitemap_pages = []
    active_sitemap_pages = []
    jsonl_records = []
    active_jsonl_records = []
    catalog_summary = []

    active_project_count = 0
    completed_sample_count = 0
    MAX_COMPLETED_IN_CATALOG = 750

    # 2. Render Project HTML pages
    print(f"\n[*] Processing {len(projects):,} project HTML pages in {PROJECTS_DIR}...")
    force_render = os.environ.get("FORCE_RENDER_HTML") == "1"
    for proj in projects:
        pid = proj.get("projectId")
        if not pid:
            continue
        status = proj.get("status") or "Active"
        is_active = (status == "Active")
        if is_active:
            active_project_count += 1

        out_html_path = os.path.join(PROJECTS_DIR, f"{pid}.html")
        if force_render or not os.path.exists(out_html_path):
            html_str = render_project_html(proj)
            with open(out_html_path, "w", encoding="utf-8") as f:
                f.write(html_str)

        mirror_url = f"{GH_PAGES_BASE}/projects/{pid}"
        live_url = f"{TECHPORT_HOST}/projects/{pid}"
        lastmod = format_date_iso(proj.get("modifiedDate") or proj.get("lastUpdated"), today_iso)
        page_entry = {"mirror_url": mirror_url, "live_url": live_url, "lastmod": lastmod}
        sitemap_pages.append(page_entry)
        if is_active:
            active_sitemap_pages.append(page_entry)

        prog = (proj.get("program") or {}).get("acronym") or (proj.get("program") or {}).get("title") or ""
        md = (proj.get("responsibleMd") or {}).get("acronym") or ""
        tx = (proj.get("primaryTx") or {}).get("code") or ""
        lead = format_org(proj.get("leadOrganization") or {})
        trl = proj.get("trlCurrent")
        meta_parts = [x for x in [f"ID: {pid}", md, prog, tx, f"TRL {trl}" if trl else "", lead] if x]
        meta_str = " | ".join(meta_parts)

        title_full = proj.get("title") or f"Project {pid}"
        if proj.get("acronym") and proj["acronym"] not in title_full:
            title_full = f"{title_full} ({proj['acronym']})"

        desc_clean = clean_html_text(proj.get("description") or "")
        benefits_clean = clean_html_text(proj.get("benefits") or "")

        if is_active:
            catalog_summary.append({
                "type": "project",
                "label": "Project (Active)",
                "title": title_full,
                "meta": meta_str,
                "summary": desc_clean[:220],
                "local_href": f"projects/{pid}.html",
                "live_url": live_url,
            })
        elif completed_sample_count < MAX_COMPLETED_IN_CATALOG:
            completed_sample_count += 1
            catalog_summary.append({
                "type": "completed_project",
                "label": f"Project ({status})",
                "title": title_full,
                "meta": meta_str,
                "summary": desc_clean[:200],
                "local_href": f"projects/{pid}.html",
                "live_url": live_url,
            })

        rec = {
            "record_type": "project",
            "id": str(pid),
            "title": title_full,
            "status": status,
            "responsible_md": md,
            "program": prog,
            "primary_tx": tx,
            "trl_begin": proj.get("trlBegin"),
            "trl_current": proj.get("trlCurrent"),
            "trl_end": proj.get("trlEnd"),
            "lead_organization": lead,
            "start_date": (proj.get("startDate") or "")[:10],
            "end_date": (proj.get("endDate") or "")[:10],
            "description": desc_clean if is_active else desc_clean[:1200],
            "benefits": benefits_clean if is_active else benefits_clean[:800],
            "mirror_url": mirror_url,
            "live_url": live_url,
        }
        jsonl_records.append(rec)
        if is_active:
            active_jsonl_records.append(rec)

    # 3. Render Impact Stories
    print(f"[*] Rendering {len(impact_stories)} Impact Story pages...")
    for story in impact_stories:
        sid = story.get("impactStoryId")
        if not sid:
            continue
        with open(os.path.join(STORIES_DIR, f"{sid}.html"), "w", encoding="utf-8") as f:
            f.write(render_impact_story_html(story))
        mirror_url = f"{GH_PAGES_BASE}/impactStories/{sid}"
        live_url = f"{TECHPORT_HOST}/impactStories/{sid}"
        page_entry = {"mirror_url": mirror_url, "live_url": live_url, "lastmod": today_iso}
        sitemap_pages.append(page_entry)
        active_sitemap_pages.append(page_entry)

        catalog_summary.append({
            "type": "impact_story",
            "label": "Impact Story",
            "title": story.get("title") or f"Impact Story {sid}",
            "meta": f"Story #{sid} | {story.get('dateString', 'STMD Infusion')}",
            "summary": clean_html_text(story.get("description") or ""),
            "local_href": f"impactStories/{sid}.html",
            "live_url": live_url,
        })
        rec = {
            "record_type": "impact_story",
            "id": str(sid),
            "title": story.get("title"),
            "description": story.get("description"),
            "date": story.get("dateString"),
            "mirror_url": mirror_url,
            "live_url": live_url,
        }
        jsonl_records.append(rec)
        active_jsonl_records.append(rec)

    with open(os.path.join(BASE_DIR, "impactStories.html"), "w", encoding="utf-8") as f:
        f.write(render_impact_stories_index_html(impact_stories))
    stories_idx_entry = {
        "mirror_url": f"{GH_PAGES_BASE}/impactStories",
        "live_url": f"{TECHPORT_HOST}/impactStories",
        "lastmod": today_iso,
    }
    sitemap_pages.append(stories_idx_entry)
    active_sitemap_pages.append(stories_idx_entry)

    # 4. Render 2024 NASA Technology Taxonomy (/taxonomy/8817 & /taxonomy)
    print(f"[*] Rendering 2024 NASA Technology Taxonomy ({len(detailed_tx)} nodes)...")
    with open(os.path.join(TAXONOMY_DIR, "8817.html"), "w", encoding="utf-8") as f:
        f.write(render_taxonomy_html(taxonomy_data, rel_root=".."))
    with open(os.path.join(BASE_DIR, "taxonomy.html"), "w", encoding="utf-8") as f:
        f.write(render_taxonomy_html(taxonomy_data, rel_root="."))

    tx_entry = {
        "mirror_url": f"{GH_PAGES_BASE}/taxonomy/8817",
        "live_url": f"{TECHPORT_HOST}/taxonomy/8817",
        "lastmod": today_iso,
    }
    sitemap_pages.append(tx_entry)
    active_sitemap_pages.append(tx_entry)

    for nid, tx_node in sorted(detailed_tx.items(), key=lambda kv: kv[1].get("code", "")):
        code = tx_node.get("code") or ""
        ttitle = tx_node.get("title") or ""
        tdesc = tx_node.get("description") or ""
        tex = tx_node.get("exampleTechnologies") or ""
        catalog_summary.append({
            "type": "taxonomy",
            "label": f"Taxonomy Level {tx_node.get('level', '')}",
            "title": f"{code}: {ttitle}",
            "meta": f"2024 NASA Technology Taxonomy | Node ID: {nid}",
            "summary": f"{tdesc} {tex}".strip()[:240],
            "local_href": f"taxonomy/8817.html#{code}",
            "live_url": f"{TECHPORT_HOST}/taxonomy/8817",
        })
        rec = {
            "record_type": "taxonomy_node",
            "id": str(nid),
            "code": code,
            "title": ttitle,
            "level": tx_node.get("level"),
            "description": tdesc,
            "example_technologies": tex,
            "mirror_url": f"{GH_PAGES_BASE}/taxonomy/8817",
            "live_url": f"{TECHPORT_HOST}/taxonomy/8817",
        }
        jsonl_records.append(rec)
        active_jsonl_records.append(rec)

    # 5. Render STMD Civil Space Shortfalls & Strategy (/strategy)
    print(f"[*] Rendering STMD Strategy & {len(shortfalls)} Civil Space Shortfalls...")
    with open(os.path.join(BASE_DIR, "strategy.html"), "w", encoding="utf-8") as f:
        f.write(render_strategy_html(strategy_data))
    strat_entry = {
        "mirror_url": f"{GH_PAGES_BASE}/strategy",
        "live_url": f"{TECHPORT_HOST}/strategy",
        "lastmod": today_iso,
    }
    sitemap_pages.append(strat_entry)
    active_sitemap_pages.append(strat_entry)

    for sf in shortfalls:
        sid = sf.get("shortfallId")
        stitle = sf.get("title") or ""
        scap = sf.get("capability") or ""
        sprio = sf.get("priority")
        sdesc = sf.get("description") or ""
        catalog_summary.append({
            "type": "shortfall",
            "label": "Civil Space Shortfall",
            "title": f"Shortfall #{sid}: {stitle}",
            "meta": f"Capability: {scap} | Priority Rank: #{sprio}",
            "summary": sdesc[:240],
            "local_href": f"strategy.html#shortfall-{sid}",
            "live_url": f"{TECHPORT_HOST}/strategy",
        })
        rec = {
            "record_type": "civil_space_shortfall",
            "id": str(sid),
            "title": stitle,
            "capability": scap,
            "priority": sprio,
            "description": sdesc,
            "mirror_url": f"{GH_PAGES_BASE}/strategy",
            "live_url": f"{TECHPORT_HOST}/strategy",
        }
        jsonl_records.append(rec)
        active_jsonl_records.append(rec)

    # 6. Render Funding Opportunities (/opportunities)
    print(f"[*] Rendering {len(opportunities)} NASA Funding Opportunities...")
    with open(os.path.join(BASE_DIR, "opportunities.html"), "w", encoding="utf-8") as f:
        f.write(render_opportunities_html(opportunities))
    opp_entry = {
        "mirror_url": f"{GH_PAGES_BASE}/opportunities",
        "live_url": f"{TECHPORT_HOST}/opportunities",
        "lastmod": today_iso,
    }
    sitemap_pages.append(opp_entry)
    active_sitemap_pages.append(opp_entry)

    for opp in opportunities:
        oid = opp.get("opportunityId")
        oname = opp.get("name") or ""
        odesc = clean_html_text(opp.get("description") or "")
        ftype = opp.get("fundingType") or ""
        catalog_summary.append({
            "type": "opportunity",
            "label": "Funding Opportunity",
            "title": oname,
            "meta": f"{ftype} | Roles: {', '.join(opp.get('roles') or [])}",
            "summary": odesc[:240],
            "local_href": f"opportunities.html#opportunity-{oid}",
            "live_url": f"{TECHPORT_HOST}/opportunities",
        })
        rec = {
            "record_type": "funding_opportunity",
            "id": str(oid),
            "title": oname,
            "funding_type": ftype,
            "roles": opp.get("roles"),
            "trl_values": opp.get("trlValues"),
            "website_url": opp.get("websiteUrl"),
            "solicitation_url": opp.get("solicitationUrl"),
            "description": odesc,
            "mirror_url": f"{GH_PAGES_BASE}/opportunities",
            "live_url": f"{TECHPORT_HOST}/opportunities",
        }
        jsonl_records.append(rec)
        active_jsonl_records.append(rec)

    # 7. Render NASA Programs Directory (/technologies)
    print(f"[*] Rendering {len(programs)} NASA Technology Programs...")
    with open(os.path.join(BASE_DIR, "technologies.html"), "w", encoding="utf-8") as f:
        f.write(render_programs_html(programs, active_project_count))
    prog_entry = {
        "mirror_url": f"{GH_PAGES_BASE}/technologies",
        "live_url": f"{TECHPORT_HOST}/technologies",
        "lastmod": today_iso,
    }
    sitemap_pages.append(prog_entry)
    active_sitemap_pages.append(prog_entry)

    for prog in programs:
        prid = prog.get("programId")
        ptitle = prog.get("title") or ""
        pacr = prog.get("acronym") or ""
        pfull = f"{ptitle} ({pacr})" if pacr and pacr not in ptitle else ptitle
        pmd = (prog.get("responsibleMd") or {}).get("acronym") or ""
        pdesc = clean_html_text(prog.get("description") or "")
        catalog_summary.append({
            "type": "program",
            "label": "NASA Program",
            "title": pfull,
            "meta": f"{pmd} | {'Active' if prog.get('isActive') else 'Historical'}",
            "summary": pdesc[:240],
            "local_href": f"technologies.html#program-{prid}",
            "live_url": f"{TECHPORT_HOST}/technologies",
        })
        rec = {
            "record_type": "program",
            "id": str(prid),
            "title": pfull,
            "acronym": pacr,
            "responsible_md": pmd,
            "is_active": prog.get("isActive"),
            "description": pdesc,
            "mirror_url": f"{GH_PAGES_BASE}/technologies",
            "live_url": f"{TECHPORT_HOST}/technologies",
        }
        jsonl_records.append(rec)
        active_jsonl_records.append(rec)

    # 8. Write XML Sitemaps & URL lists (both Active Portfolio and Complete Archive)
    print(f"\n[*] Writing Active Portfolio XML sitemaps ({len(active_sitemap_pages):,} URLs)...")
    write_sitemap_files(
        pages=active_sitemap_pages,
        xml_path=ACTIVE_SITEMAP_FILE,
        direct_xml_path=ACTIVE_DIRECT_SITEMAP_FILE,
        urls_path=ACTIVE_URLS_FILE,
    )

    print(f"[*] Writing Complete Archive XML sitemaps ({len(sitemap_pages):,} URLs)...")
    write_sitemap_files(
        pages=sitemap_pages,
        xml_path=SITEMAP_FILE,
        direct_xml_path=DIRECT_SITEMAP_FILE,
        urls_path=URLS_FILE,
        direct_urls_path=DIRECT_URLS_FILE,
        alias_xml_path=SITEMAP_ALIAS_FILE,
    )

    # 9. Write JSONL datasets
    print(f"[*] Writing {len(active_jsonl_records):,} active records to {ACTIVE_JSONL_FILE}...")
    with open(ACTIVE_JSONL_FILE, "w", encoding="utf-8") as f:
        for rec in active_jsonl_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"[*] Writing {len(jsonl_records):,} total records to {JSONL_FILE}...")
    with open(JSONL_FILE, "w", encoding="utf-8") as f:
        for rec in jsonl_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 10. Write interactive index.html
    stats = {
        "active_project": active_project_count,
        "total_project": len(projects),
        "completed_sample": completed_sample_count,
        "taxonomy": len(detailed_tx),
        "shortfall": len(shortfalls),
        "program": len(programs),
        "opportunity": len(opportunities),
        "impact_story": len(impact_stories),
        "active_urls": len(active_sitemap_pages),
        "total_urls": len(sitemap_pages),
    }
    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(render_index_html(stats, catalog_summary))

    print("\n" + "=" * 80)
    print(" NASA TECHPORT HARVEST & MIRROR SUMMARY")
    print("=" * 80)
    print(f"  - Active Technology Projects : {active_project_count:>6,}")
    print(f"  - Total Mirrored Projects    : {len(projects):>6,}")
    print(f"  - 2024 Taxonomy Nodes (TX)   : {len(detailed_tx):>6,}")
    print(f"  - STMD Civil Space Shortfalls: {len(shortfalls):>6,}")
    print(f"  - NASA Technology Programs   : {len(programs):>6,}")
    print(f"  - Funding Opportunities      : {len(opportunities):>6,}")
    print(f"  - STMD Impact Stories        : {len(impact_stories):>6,}")
    print(f"  ----------------------------------------------")
    print(f"  - Active Portfolio URLs      : {len(active_sitemap_pages):>6,}")
    print(f"  - Complete Archive URLs      : {len(sitemap_pages):>6,}")
    print(f"  - Total Structured Records   : {len(jsonl_records):>6,}")
    print("=" * 80)


if __name__ == "__main__":
    main()
