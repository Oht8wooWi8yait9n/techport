# NASA TechPort (`techport.nasa.gov`) Harvester, Mirror & Sitemap Pipeline

[![Update NASA TechPort Catalog & Sitemap](https://github.com/Oht8wooWi8yait9n/techport/actions/workflows/update-sitemap.yml/badge.svg)](https://github.com/Oht8wooWi8yait9n/techport/actions/workflows/update-sitemap.yml)

Automated REST API harvester, pre-rendered static HTML mirror, client-side searchable catalog, JSONL dataset, and dual XML sitemaps for **NASA TechPort** ([https://techport.nasa.gov](https://techport.nasa.gov)) — NASA's authoritative Space Technology Portfolio, 2024 NASA Technology Taxonomy (`TX01`–`TX17`), and STMD Civil Space Shortfalls knowledge base.

---

## 1. Why a Static Mirror + URL Rewrite Is Required

1. **Client-Side React SPA + Split-Horizon DNS**:
   - `https://techport.nasa.gov` is a client-side React single-page application (SPA) whose initial HTML shell contains an empty `<div id="root"></div>`.
   - Inside NASA's internal network (`*.nasa.gov`), split-horizon DNS resolves `techport.nasa.gov` to an internal load balancer (`vpc5LB-1607924795.us-gov-west-1.elb.amazonaws.com`) that redirects unauthenticated crawlers (`302` / `401`) to NASA Launchpad SAML (`auth.launchpad.nasa.gov`), whereas the public internet load balancer (`dualstack.vpc7lb-1240402781.us-gov-west-1.elb.amazonaws.com`) exposes the public REST API without authentication.
2. **High-Speed Bulk Solr API Harvesting**:
   - Instead of making thousands of slow single-project REST calls, [`generate_techport_sitemap.py`](generate_techport_sitemap.py) acquires a fresh single-use token from `GET /api/nonce` and queries TechPort's bulk Solr search endpoint (`POST /api/projects/search`) for all 34 project schema fields (`1,000` full projects per request), alongside `GET /api/taxonomies/8817`, `GET /api/taxonomies/nodes/{id}`, `GET /api/strategy`, `GET /api/programs`, `GET /api/opportunities`, and `GET /api/impactStories`.
3. **1-to-1 Canonical URL Rewrite for Onyx**:
   - Every pre-rendered page on GitHub Pages mirrors the exact React Router path structure on `https://techport.nasa.gov/`:
     - `https://oht8woowi8yait9n.github.io/techport/projects/<projectId>` &rarr; `https://techport.nasa.gov/projects/<projectId>`
     - `https://oht8woowi8yait9n.github.io/techport/taxonomy/8817` &rarr; `https://techport.nasa.gov/taxonomy/8817`
     - `https://oht8woowi8yait9n.github.io/techport/strategy` &rarr; `https://techport.nasa.gov/strategy`
     - `https://oht8woowi8yait9n.github.io/techport/opportunities` &rarr; `https://techport.nasa.gov/opportunities`
     - `https://oht8woowi8yait9n.github.io/techport/technologies` &rarr; `https://techport.nasa.gov/technologies`
     - `https://oht8woowi8yait9n.github.io/techport/impactStories/<id>` &rarr; `https://techport.nasa.gov/impactStories/<id>`

---

## 2. Portfolio Coverage & Knowledge Domains

| Domain | API Source | Active Portfolio | Complete Archive | Mirror Route |
| :--- | :--- | :--- | :--- | :--- |
| **NASA Technology Projects** | `POST /api/projects/search` (all 34 schema fields) | **1,013** (Active) | **21,156** (All Statuses) | `/projects/<projectId>` |
| **2024 NASA Technology Taxonomy (`TX01`–`TX17`)** | `GET /api/taxonomies/nodes?rootId=8817` (Levels 1, 2, 3) | **495** | **495** | `/taxonomy/8817` |
| **STMD Civil Space Shortfalls & Capabilities** | `GET /api/strategy` (`187` shortfalls & `19` strategic capabilities) | **187** | **187** | `/strategy` |
| **NASA Technology Programs** | `GET /api/programs` | **98** | **98** | `/technologies` |
| **Funding Opportunities** | `GET /api/opportunities` | **36** | **36** | `/opportunities` |
| **STMD Impact Stories** | `GET /api/impactStories/{id}` | **4** | **4** | `/impactStories/<id>` |
| **TOTAL STRUCTURED RECORDS** | **All 6 TechPort Knowledge Domains** | **1,833 Records (1,022 URLs)** | **21,976 Records (21,165 URLs)** | — |

---

## 3. Live Endpoints & Sitemaps

| Resource | URL |
| :--- | :--- |
| **Interactive Searchable Catalog** | [https://oht8woowi8yait9n.github.io/techport/](https://oht8woowi8yait9n.github.io/techport/) |
| **Active Portfolio XML Sitemap (Recommended for Onyx — 1,022 URLs)** | `https://oht8woowi8yait9n.github.io/techport/techport_active_sitemap.xml` |
| **Complete Historical Archive XML Sitemap (21,165 URLs)** | `https://oht8woowi8yait9n.github.io/techport/sitemap.xml` |
| **Alias Complete XML Sitemap (Raw GitHub)** | `https://raw.githubusercontent.com/Oht8wooWi8yait9n/techport/main/techport_sitemap.xml` |
| **Direct NASA XML Sitemap (`techport.nasa.gov` — Active)** | `https://raw.githubusercontent.com/Oht8wooWi8yait9n/techport/main/techport_active_direct_sitemap.xml` |
| **Direct NASA XML Sitemap (`techport.nasa.gov` — Complete)** | `https://raw.githubusercontent.com/Oht8wooWi8yait9n/techport/main/techport_direct_sitemap.xml` |
| **Plain Text Mirror URLs** | [`techport_active_urls.txt`](techport_active_urls.txt) &bull; [`techport_urls.txt`](techport_urls.txt) |
| **Unified JSONL Datasets** | [`techport_active_records.jsonl`](techport_active_records.jsonl) &bull; [`techport_records.jsonl`](techport_records.jsonl) |

---

## 4. Onyx Web Connector & URL Rewrite Configuration

1. **Onyx Web Connector**:
   - **Connector Name**: `NASA TechPort`
   - **Indexing Mode**: Sitemap
   - **Sitemap URL (Active Portfolio — Recommended)**:
     ```text
     https://oht8woowi8yait9n.github.io/techport/techport_active_sitemap.xml
     ```
   - **Sitemap URL (Complete Historical Archive — 21,165 URLs)**:
     ```text
     https://oht8woowi8yait9n.github.io/techport/sitemap.xml
     ```
2. **Onyx URL Rewrite Rule**:
   - **Source Prefix**:
     ```text
     https://oht8woowi8yait9n.github.io/techport/
     ```
   - **Target Prefix**:
     ```text
     https://techport.nasa.gov/
     ```
