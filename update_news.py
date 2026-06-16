#!/usr/bin/env python3
"""
FuelWired — News Updater
Fetches latest oil & gas news from free RSS feeds,
generates article HTML pages, and regenerates index.html.

Dependencies: Python stdlib only (no pip installs required)
"""

import urllib.request
import xml.etree.ElementTree as ET
import json
import os
import re
import html
import datetime
import hashlib
import sys
import time

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = SCRIPT_DIR          # HTML files go in site root

ARTICLES_JSON = os.path.join(SCRIPT_DIR, "articles.json")
MAX_ARTICLES  = 9                # How many to show on the homepage grid
MAX_PER_FEED  = 15               # Max items to pull per feed
FETCH_TIMEOUT = 15               # Seconds before giving up on a feed

RSS_FEEDS = [
    {
        "name": "OilPrice.com",
        "url":  "https://feeds.feedburner.com/oilprice/dQgE",
        "default_category": "Markets",
    },
    {
        "name": "EIA Press Releases",
        "url":  "https://www.eia.gov/rss/press_releases.xml",
        "default_category": "EIA Report",
    },
    {
        "name": "Rigzone Latest",
        "url":  "https://www.rigzone.com/news/rss/rigzone_latest.aspx",
        "default_category": "Upstream",
    },
]

# Commodity prices — static placeholders; replace with live API if desired.
PRICES = {
    "brent": {"label": "Brent Crude",      "unit": "USD/bbl",     "value": "84.72", "change": "+1.23", "pct": "1.47%",  "dir": "up"},
    "wti":   {"label": "WTI Crude",        "unit": "USD/bbl",     "value": "81.15", "change": "+0.89", "pct": "1.11%",  "dir": "up"},
    "hh":    {"label": "Henry Hub Gas",    "unit": "USD/MMBtu",   "value": "2.64",  "change": "-0.07", "pct": "2.58%",  "dir": "down"},
    "opec":  {"label": "OPEC Basket",      "unit": "USD/bbl",     "value": "85.30", "change": "+0.96", "pct": "1.14%",  "dir": "up"},
    "ttf":   {"label": "EU Natural Gas",   "unit": "EUR/MWh TTF", "value": "35.80", "change": "+0.45", "pct": "1.27%",  "dir": "up"},
}

# Category CSS class mapping
CAT_CLASS = {
    "upstream":    "cat-upstream",
    "downstream":  "cat-downstream",
    "lng":         "cat-lng",
    "markets":     "cat-markets",
    "eia report":  "cat-policy",
    "policy":      "cat-policy",
    "technology":  "cat-tech",
    "tech":        "cat-tech",
}

# Image placeholder emoji + CSS class by category
CAT_PLACEHOLDER = {
    "upstream":    ("🛢️",  "upstream"),
    "downstream":  ("🏭",  "downstream"),
    "lng":         ("🌊",  "lng"),
    "markets":     ("📈",  "markets"),
    "eia report":  ("⚡",  "policy"),
    "policy":      ("⚡",  "policy"),
    "technology":  ("🤖",  "tech"),
    "tech":        ("🤖",  "tech"),
}


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert a title to a URL-safe slug."""
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = text.strip("-")
    return text[:80]


def strip_tags(text: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text or "")


def truncate(text: str, length: int = 200) -> str:
    """Truncate text to roughly `length` characters, breaking at word boundary."""
    text = text.strip()
    if len(text) <= length:
        return text
    cut = text[:length].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "…"


def estimate_read_time(text: str) -> int:
    """Estimate reading time in minutes (200 wpm)."""
    words = len(re.findall(r"\w+", text))
    return max(1, round(words / 200))


def parse_date(raw: str) -> datetime.datetime:
    """Parse an RSS pubDate string into a datetime (best-effort)."""
    raw = (raw or "").strip()
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%a, %d %b %Y %H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(raw, fmt)
        except (ValueError, TypeError):
            continue
    return datetime.datetime.utcnow()


def format_date_display(dt: datetime.datetime) -> str:
    """Format a datetime for display on the site."""
    return dt.strftime("%b %d, %Y")


def article_id(title: str, link: str) -> str:
    """Create a stable short ID for deduplication."""
    key = (title + link).encode("utf-8")
    return hashlib.md5(key).hexdigest()[:12]


def detect_category(title: str, description: str, source_name: str) -> str:
    """Heuristically detect a category from title/description."""
    combined = (title + " " + description).lower()
    rules = [
        (["lng", "liquefied natural gas", "regasif"], "LNG"),
        (["refin", "downstream", "petrochemic", "crack spread", "gasoline", "diesel", "fuel"], "Downstream"),
        (["upstream", "drilling", "rig count", "wellbore", "exploration", "deepwater", "permian", "shale", "fractur"], "Upstream"),
        (["eia", "energy information"], "EIA Report"),
        (["opec", "iea ", "market", "price", "brent", "wti", "crude"], "Markets"),
        (["pipeline", "policy", "regulation", "sanction", "tariff"], "Policy"),
        (["ai ", "artificial intel", "digital", "technolog", "robot", "software"], "Technology"),
    ]
    for keywords, cat in rules:
        if any(kw in combined for kw in keywords):
            return cat
    # Fallback to source default
    for feed in RSS_FEEDS:
        if feed["name"] == source_name:
            return feed["default_category"]
    return "Markets"


# ──────────────────────────────────────────────────────────────────────────────
# RSS FETCHING
# ──────────────────────────────────────────────────────────────────────────────

def fetch_feed(feed: dict) -> list:
    """Fetch and parse a single RSS feed. Returns list of article dicts."""
    url  = feed["url"]
    name = feed["name"]
    articles = []

    try:
        print(f"  Fetching {name} … ", end="", flush=True)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "FuelWired/1.0 (https://fuelwired.com; news-bot)",
                "Accept":     "application/rss+xml, application/xml, text/xml",
            },
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            raw_xml = resp.read()

        root = ET.fromstring(raw_xml)

        # Handle both RSS 2.0 and Atom (basic)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)

        for item in items[:MAX_PER_FEED]:
            def get(tag: str, fallback: str = "") -> str:
                el = item.find(tag)
                return (el.text or "").strip() if el is not None and el.text else fallback

            title       = html.unescape(strip_tags(get("title")))
            link        = get("link") or get("guid")
            description = html.unescape(strip_tags(get("description") or get("summary")))
            pub_date    = get("pubDate") or get("published") or get("updated")

            if not title or not link:
                continue

            dt       = parse_date(pub_date)
            category = detect_category(title, description, name)
            slug     = slugify(title)
            uid      = article_id(title, link)
            excerpt  = truncate(description, 220)
            read_min = estimate_read_time(description)

            articles.append({
                "id":          uid,
                "slug":        slug,
                "title":       title,
                "link":        link,
                "description": description,
                "excerpt":     excerpt,
                "pub_date":    dt.isoformat(),
                "date_display": format_date_display(dt),
                "category":    category,
                "source":      name,
                "read_min":    read_min,
                "filename":    f"article-{slug}.html",
            })

        print(f"OK ({len(articles)} items)")

    except urllib.error.URLError as e:
        print(f"FAILED (URLError: {e.reason})")
    except ET.ParseError as e:
        print(f"FAILED (XML parse error: {e})")
    except Exception as e:
        print(f"FAILED ({type(e).__name__}: {e})")

    return articles


def fetch_all_feeds() -> list:
    """Fetch all configured RSS feeds; deduplicate and sort by date."""
    seen_ids  = set()
    all_items = []

    for feed in RSS_FEEDS:
        items = fetch_feed(feed)
        for item in items:
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                all_items.append(item)

    # Sort newest-first
    all_items.sort(key=lambda x: x["pub_date"], reverse=True)
    return all_items


# ──────────────────────────────────────────────────────────────────────────────
# HTML GENERATION HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _cat_class(category: str) -> str:
    return CAT_CLASS.get(category.lower(), "cat-markets")


def _cat_placeholder(category: str) -> tuple:
    return CAT_PLACEHOLDER.get(category.lower(), ("📰", "markets"))


def _price_row(p: dict, symbol: str) -> str:
    sign = "+" if p["dir"] == "up" else ""
    return f"""
            <div class="price-row">
              <div>
                <div class="price-commodity">{html.escape(p['label'])}</div>
                <div class="price-unit">{html.escape(p['unit'])}</div>
              </div>
              <div class="price-right">
                <div class="price-value">${p['value']}</div>
                <div class="price-change {p['dir']}">{p['pct']}</div>
              </div>
            </div>"""


def _ticker_items() -> str:
    items = []
    ticker_data = [
        ("BRENT",        f"${PRICES['brent']['value']}", PRICES['brent']['change'], "up"   if PRICES['brent']['dir'] == "up" else "down"),
        ("WTI",          f"${PRICES['wti']['value']}",   PRICES['wti']['change'],   "up"   if PRICES['wti']['dir'] == "up" else "down"),
        ("HENRY HUB",    f"${PRICES['hh']['value']}",    PRICES['hh']['change'],    "up"   if PRICES['hh']['dir'] == "up" else "down"),
        ("OPEC BASKET",  f"${PRICES['opec']['value']}",  PRICES['opec']['change'],  "up"   if PRICES['opec']['dir'] == "up" else "down"),
        ("TTF GAS",      f"€{PRICES['ttf']['value']}",   PRICES['ttf']['change'],   "up"   if PRICES['ttf']['dir'] == "up" else "down"),
    ]
    for symbol, price, chg, direction in ticker_data:
        items.append(f'<div class="ticker-item"><span class="ticker-symbol">{symbol}</span>'
                     f'<span class="ticker-price">{price}</span>'
                     f'<span class="ticker-change {direction}">{chg}</span></div>')
    # Duplicate for seamless scroll
    return "\n    ".join(items * 2)


def _article_card_html(article: dict, delay_class: str = "") -> str:
    emoji, bg_class = _cat_placeholder(article["category"])
    cat_cls         = _cat_class(article["category"])
    safe_title      = html.escape(article["title"])
    safe_excerpt    = html.escape(article["excerpt"])
    safe_date       = html.escape(article["date_display"])
    safe_cat        = html.escape(article["category"])
    filename        = article["filename"]
    read_min        = article["read_min"]

    return f"""
            <article class="article-card {delay_class}" onclick="location.href='{filename}'">
              <div class="card-image">
                <div class="card-image-placeholder {bg_class}" aria-label="{safe_cat} article">{emoji}</div>
              </div>
              <div class="card-body">
                <span class="card-category {cat_cls}">{safe_cat}</span>
                <h3 class="card-title">{safe_title}</h3>
                <p class="card-excerpt">{safe_excerpt}</p>
                <div class="card-footer">
                  <span class="card-date">
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
                    {safe_date}
                  </span>
                  <span class="card-read-time">
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                    {read_min} min
                  </span>
                </div>
              </div>
            </article>"""


def _price_rows_html() -> str:
    rows = ""
    for key, p in PRICES.items():
        rows += _price_row(p, key)
    return rows


def _markets_grid_html() -> str:
    cells = []
    market_data = [
        ("Brent Crude",   f"${PRICES['brent']['value']}", f"+{PRICES['brent']['change']} ({PRICES['brent']['pct']})" if PRICES['brent']['dir']=='up' else f"{PRICES['brent']['change']} ({PRICES['brent']['pct']})", PRICES['brent']['dir']),
        ("WTI Crude",     f"${PRICES['wti']['value']}",   f"+{PRICES['wti']['change']} ({PRICES['wti']['pct']})" if PRICES['wti']['dir']=='up' else f"{PRICES['wti']['change']} ({PRICES['wti']['pct']})",   PRICES['wti']['dir']),
        ("Henry Hub",     f"${PRICES['hh']['value']}",    f"+{PRICES['hh']['change']} ({PRICES['hh']['pct']})" if PRICES['hh']['dir']=='up' else f"{PRICES['hh']['change']} ({PRICES['hh']['pct']})",     PRICES['hh']['dir']),
        ("OPEC Basket",   f"${PRICES['opec']['value']}",  f"+{PRICES['opec']['change']} ({PRICES['opec']['pct']})" if PRICES['opec']['dir']=='up' else f"{PRICES['opec']['change']} ({PRICES['opec']['pct']})", PRICES['opec']['dir']),
        ("EU Nat. Gas",   f"€{PRICES['ttf']['value']}",   f"+{PRICES['ttf']['change']} ({PRICES['ttf']['pct']})" if PRICES['ttf']['dir']=='up' else f"{PRICES['ttf']['change']} ({PRICES['ttf']['pct']})",   PRICES['ttf']['dir']),
    ]
    for name, price, chg, direction in market_data:
        cells.append(f"""            <div class="market-cell">
              <div class="market-name">{html.escape(name)}</div>
              <div class="market-price">{html.escape(price)}</div>
              <div class="market-change {direction}">{html.escape(chg)}</div>
            </div>""")
    return "\n".join(cells)


# ──────────────────────────────────────────────────────────────────────────────
# ARTICLE PAGE GENERATOR
# ──────────────────────────────────────────────────────────────────────────────

def generate_article_page(article: dict) -> None:
    """Write a standalone article-*.html page for a single article."""
    cat_cls  = _cat_class(article["category"])
    safe_title   = html.escape(article["title"])
    safe_excerpt = html.escape(article["excerpt"])
    safe_date    = html.escape(article["date_display"])
    safe_cat     = html.escape(article["category"])
    safe_source  = html.escape(article["source"])
    safe_link    = html.escape(article["link"])
    read_min     = article["read_min"]

    # Build body paragraphs from description; split on newlines or sentences
    body_text = article.get("description", "")
    # Attempt to create multi-paragraph content
    paragraphs = [p.strip() for p in re.split(r"\n{2,}|\. {2}", body_text) if p.strip()]
    if len(paragraphs) <= 1 and ". " in body_text:
        sentences = re.split(r"(?<=[.!?])\s+", body_text)
        chunk_size = max(3, len(sentences) // 3)
        paragraphs = [
            " ".join(sentences[i:i+chunk_size])
            for i in range(0, len(sentences), chunk_size)
        ]
    if not paragraphs:
        paragraphs = [body_text]

    body_html = "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs if p)

    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{safe_title} — FuelWired</title>
  <meta name="description" content="{safe_excerpt}" />
  <meta property="og:type" content="article" />
  <meta property="og:title" content="{safe_title}" />
  <meta property="og:description" content="{safe_excerpt}" />
  <meta property="og:url" content="https://fuelwired.com/{html.escape(article['filename'])}" />
  <meta name="twitter:card" content="summary" />
  <link rel="canonical" href="https://fuelwired.com/{html.escape(article['filename'])}" />
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%230d1117'/><polygon points='18,4 10,18 16,18 14,28 22,14 16,14' fill='%23f0a500'/></svg>" />
  <link rel="stylesheet" href="style.css" />
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "NewsArticle",
    "headline": "{article['title'].replace('"', '\\"')}",
    "datePublished": "{article['pub_date']}",
    "publisher": {{
      "@type": "Organization",
      "name": "FuelWired",
      "url": "https://fuelwired.com"
    }},
    "url": "https://fuelwired.com/{article['filename']}"
  }}
  </script>
</head>
<body>

<!-- Ticker -->
<div class="ticker-banner" aria-label="Live commodity prices">
  <div class="ticker-track">
    {_ticker_items()}
  </div>
</div>

<!-- Header -->
<header class="site-header" role="banner">
  <div class="container">
    <div class="header-inner">
      <a class="logo" href="/" aria-label="FuelWired Home">
        <div class="logo-icon">
          <svg viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <rect width="40" height="40" rx="8" fill="#1c2333"/>
            <polygon points="22,5 12,22 20,22 18,36 28,18 20,18" fill="url(#bolt-art)"/>
            <defs><linearGradient id="bolt-art" x1="12" y1="5" x2="28" y2="36" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#fbbf24"/><stop offset="100%" stop-color="#d97706"/></linearGradient></defs>
          </svg>
        </div>
        <div>
          <div class="logo-text">FuelWired</div>
          <div class="logo-tagline">Oil &amp; Gas Intelligence</div>
        </div>
      </a>
      <nav class="main-nav" role="navigation" aria-label="Main navigation">
        <a class="nav-link" href="/">Home</a>
        <a class="nav-link" href="/#markets">Markets</a>
        <a class="nav-link" href="/#upstream">Upstream</a>
        <a class="nav-link" href="/#downstream">Downstream</a>
        <a class="nav-link" href="/#lng">LNG</a>
        <a class="nav-link" href="about.html">About</a>
      </nav>
      <div class="header-actions">
        <a class="btn-subscribe" href="/#newsletter">Subscribe Free</a>
        <button class="hamburger" aria-label="Open menu" aria-expanded="false">
          <span></span><span></span><span></span>
        </button>
      </div>
    </div>
  </div>
</header>

<!-- Article Hero -->
<div class="article-hero">
  <div class="container">
    <div class="article-breadcrumb">
      <a href="/">Home</a> / <a href="/#latest">{safe_cat}</a> / Article
    </div>
    <span class="card-category {cat_cls}" style="margin-bottom:14px;display:inline-block;">{safe_cat}</span>
    <h1 class="article-hero-title">{safe_title}</h1>
    <div class="article-hero-meta">
      <span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        {safe_date}
      </span>
      <span class="dot"></span>
      <span>{read_min} min read</span>
      <span class="dot"></span>
      <span>Source: {safe_source}</span>
    </div>
  </div>
</div>

<!-- Article Body -->
<main class="page-main">
  <div class="container">
    <div class="article-body-layout">
      <article class="article-content">
        {body_html}
        <a class="source-link" href="{safe_link}" target="_blank" rel="noopener noreferrer">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          Read original article at {safe_source}
        </a>
      </article>

      <!-- Sidebar -->
      <aside class="sidebar" aria-label="Market data">
        <div class="sidebar-widget">
          <div class="widget-header">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
            Live Prices
          </div>
          <div class="widget-body">
            {_price_rows_html()}
            <div class="price-timestamp">Updated: {datetime.datetime.utcnow().strftime('%b %d, %Y · %H:%M UTC')}</div>
          </div>
        </div>

        <div class="sidebar-widget newsletter-widget">
          <div class="widget-body">
            <div class="newsletter-title">Stay Wired In</div>
            <p class="newsletter-sub">Daily oil &amp; gas briefing delivered at 7 AM.</p>
            <form class="newsletter-form" onsubmit="handleSubscribe(event)">
              <input class="newsletter-input" type="email" placeholder="your@email.com" required aria-label="Email address"/>
              <button class="newsletter-btn" type="submit">Get Daily Briefing →</button>
            </form>
          </div>
        </div>
      </aside>
    </div>
  </div>
</main>

<!-- Footer -->
<footer class="site-footer" role="contentinfo">
  <div class="container">
    <div class="footer-bottom" style="border-top:0;padding-top:0;">
      <span>© {datetime.datetime.utcnow().year} FuelWired. All rights reserved.</span>
      <div class="footer-bottom-links">
        <a href="/">Home</a>
        <a href="about.html">About</a>
        <a href="#">Privacy</a>
      </div>
    </div>
  </div>
</footer>

<script>
function handleSubscribe(e) {{
  e.preventDefault();
  const btn = e.target.querySelector('.newsletter-btn');
  btn.textContent = '✓ Subscribed!';
  btn.style.background = 'var(--green-accent)';
  btn.disabled = true;
}}
document.querySelector('.hamburger').addEventListener('click', function() {{
  const nav = document.querySelector('.main-nav');
  const open = nav.style.display === 'flex';
  nav.style.display = open ? '' : 'flex';
  nav.style.flexDirection = 'column';
  nav.style.position = 'absolute';
  nav.style.top = '64px';
  nav.style.left = '0';
  nav.style.right = '0';
  nav.style.background = 'var(--bg-secondary)';
  nav.style.padding = '12px 20px';
  nav.style.borderBottom = '1px solid var(--border)';
  nav.style.zIndex = '99';
}});
</script>
</body>
</html>
"""
    path = os.path.join(OUTPUT_DIR, article["filename"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ──────────────────────────────────────────────────────────────────────────────
# INDEX.HTML REGENERATOR
# ──────────────────────────────────────────────────────────────────────────────

def regenerate_index(articles: list) -> None:
    """Rewrite index.html with the latest articles."""
    if not articles:
        print("  No articles — skipping index.html regeneration.")
        return

    display = articles[:MAX_ARTICLES]
    hero    = display[0]
    grid    = display[1:7]  # 6 cards in the grid
    more    = display[7:9]  # extra if available

    # Hero fields
    hero_title   = html.escape(hero["title"])
    hero_excerpt = html.escape(hero["excerpt"])
    hero_date    = html.escape(hero["date_display"])
    hero_cat     = html.escape(hero["category"])
    hero_file    = hero["filename"]
    hero_read    = hero["read_min"]

    # Build grid cards HTML
    delay_classes = ["fade-in-1", "fade-in-2", "fade-in-3"] * 3
    grid_html = ""
    for i, art in enumerate(grid):
        grid_html += _article_card_html(art, delay_classes[i])

    # Extra secondary stories
    secondary_html = ""
    for idx, art in enumerate(more, start=len(grid)+1):
        secondary_html += f"""
            <article class="secondary-item" onclick="location.href='{html.escape(art['filename'])}'">
              <div class="secondary-num">0{idx}</div>
              <div class="secondary-body">
                <div class="secondary-title">{html.escape(art['title'])}</div>
                <div class="secondary-meta">{html.escape(art['category'])} · {html.escape(art['date_display'])} · {art['read_min']} min read</div>
              </div>
            </article>"""

    updated_ts = datetime.datetime.utcnow().strftime("%b %d, %Y · %H:%M UTC")

    # Price rows for sidebar
    price_rows_html = _price_rows_html()

    # Markets grid
    markets_html = _markets_grid_html()

    # Ticker
    ticker_html = _ticker_items()

    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>FuelWired — Oil &amp; Gas Intelligence</title>
  <meta name="description" content="FuelWired delivers real-time oil and gas industry news, energy market analysis, upstream &amp; downstream coverage, and LNG market intelligence." />
  <meta name="keywords" content="oil gas news, energy markets, crude oil, Brent, WTI, LNG, upstream, downstream, petroleum, refinery, OPEC" />
  <meta name="author" content="FuelWired" />
  <meta property="og:type" content="website" />
  <meta property="og:title" content="FuelWired — Oil &amp; Gas Intelligence" />
  <meta property="og:description" content="Real-time oil and gas industry news, energy market analysis, and LNG market intelligence." />
  <meta property="og:url" content="https://fuelwired.com" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="theme-color" content="#0d1117" />
  <link rel="canonical" href="https://fuelwired.com/" />
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%230d1117'/><polygon points='18,4 10,18 16,18 14,28 22,14 16,14' fill='%23f0a500'/></svg>" />
  <link rel="stylesheet" href="style.css" />
  <!-- Last updated: {updated_ts} -->
</head>
<body>

<!-- Ticker -->
<div class="ticker-banner" aria-label="Live commodity prices">
  <div class="ticker-track">
    {ticker_html}
  </div>
</div>

<!-- Header -->
<header class="site-header" role="banner">
  <div class="container">
    <div class="header-inner">
      <a class="logo" href="/" aria-label="FuelWired Home">
        <div class="logo-icon">
          <svg viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <rect width="40" height="40" rx="8" fill="#1c2333"/>
            <polygon points="22,5 12,22 20,22 18,36 28,18 20,18" fill="url(#bolt-grad)"/>
            <defs><linearGradient id="bolt-grad" x1="12" y1="5" x2="28" y2="36" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#fbbf24"/><stop offset="100%" stop-color="#d97706"/></linearGradient></defs>
          </svg>
        </div>
        <div>
          <div class="logo-text">FuelWired</div>
          <div class="logo-tagline">Oil &amp; Gas Intelligence</div>
        </div>
      </a>
      <nav class="main-nav" role="navigation" aria-label="Main navigation">
        <a class="nav-link active" href="/">Home</a>
        <a class="nav-link" href="#markets">Markets</a>
        <a class="nav-link" href="#upstream">Upstream</a>
        <a class="nav-link" href="#downstream">Downstream</a>
        <a class="nav-link" href="#lng">LNG</a>
        <a class="nav-link" href="about.html">About</a>
      </nav>
      <div class="header-actions">
        <a class="btn-subscribe" href="#newsletter">Subscribe Free</a>
        <button class="hamburger" aria-label="Open menu" aria-expanded="false">
          <span></span><span></span><span></span>
        </button>
      </div>
    </div>
  </div>
</header>

<!-- Breaking Bar -->
<div class="breaking-bar" role="complementary" aria-label="Breaking news">
  <div class="container">
    <div class="breaking-inner">
      <span class="breaking-label">Breaking</span>
      <span class="breaking-text">
        {hero_title} &nbsp;•&nbsp;
        Brent crude at ${PRICES['brent']['value']}/bbl &nbsp;•&nbsp;
        WTI at ${PRICES['wti']['value']}/bbl &nbsp;•&nbsp;
        Henry Hub at ${PRICES['hh']['value']}/MMBtu
      </span>
    </div>
  </div>
</div>

<main class="page-main" id="main-content">
  <div class="container">

    <!-- Hero -->
    <section class="hero-section fade-in" aria-labelledby="hero-title">
      <div class="section-heading">
        <h2 class="section-title">Top Story</h2>
        <a class="section-more" href="#latest">View All Stories →</a>
      </div>
      <article class="hero-card" onclick="location.href='{html.escape(hero_file)}'" role="article">
        <div class="hero-bg">
          <div class="hero-bg-pattern"></div>
          <div class="hero-bg-orb"></div>
          <div class="hero-bg-orb2"></div>
        </div>
        <div class="hero-image-area">
          <div class="hero-image-placeholder">
            <svg viewBox="0 0 200 200" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
              <rect x="90" y="20" width="20" height="120" fill="rgba(240,165,0,0.15)" rx="2"/>
              <polygon points="60,140 100,20 140,140" fill="none" stroke="rgba(240,165,0,0.2)" stroke-width="2"/>
              <rect x="50" y="140" width="100" height="8" fill="rgba(240,165,0,0.2)" rx="2"/>
              <rect x="80" y="152" width="40" height="30" fill="rgba(240,165,0,0.08)" rx="2"/>
            </svg>
          </div>
          <div class="hero-image-overlay"></div>
        </div>
        <div class="hero-content">
          <span class="hero-category">{hero_cat}</span>
          <h1 class="hero-title" id="hero-title">{hero_title}</h1>
          <p class="hero-excerpt">{hero_excerpt}</p>
          <div class="hero-meta">
            <span>{hero_date}</span>
            <span class="dot"></span>
            <span>{hero_read} min read</span>
          </div>
          <a class="btn-read-more" href="{html.escape(hero_file)}">
            Read Full Story
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
          </a>
        </div>
      </article>
    </section>

    <!-- Content + Sidebar -->
    <div class="content-layout">
      <div class="main-column">

        <!-- News Grid -->
        <section id="latest" aria-labelledby="latest-title">
          <div class="section-heading">
            <h2 class="section-title" id="latest-title">Latest News</h2>
            <a class="section-more" href="#">More Stories →</a>
          </div>
          <div class="news-grid" id="news-grid">
            {grid_html}
          </div>
        </section>

        <!-- Markets Strip -->
        <div class="markets-strip" id="markets">
          <div class="section-heading" style="margin-bottom:18px;">
            <h2 class="section-title">Commodity Prices</h2>
            <span style="font-size:11px;color:var(--text-muted);">Updated: {updated_ts}</span>
          </div>
          <div class="markets-grid">
            {markets_html}
          </div>
        </div>

        <!-- Secondary Stories -->
        {f'''<section aria-labelledby="more-stories-title">
          <div class="section-heading">
            <h2 class="section-title" id="more-stories-title">More Stories</h2>
          </div>
          <div class="secondary-grid">{secondary_html}</div>
        </section>''' if secondary_html else ''}

      </div><!-- /main-column -->

      <!-- Sidebar -->
      <aside class="sidebar" aria-label="Market data and tools">
        <div class="sidebar-widget">
          <div class="widget-header">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
            Live Prices
          </div>
          <div class="widget-body">
            {price_rows_html}
            <div class="price-timestamp">Updated: {updated_ts}</div>
          </div>
        </div>

        <div class="sidebar-widget newsletter-widget" id="newsletter">
          <div class="widget-body">
            <div class="newsletter-title">Stay Wired In</div>
            <p class="newsletter-sub">Daily oil &amp; gas briefing delivered to your inbox at 7 AM.</p>
            <form class="newsletter-form" onsubmit="handleSubscribe(event)">
              <input class="newsletter-input" type="email" placeholder="your@email.com" required aria-label="Email address"/>
              <button class="newsletter-btn" type="submit">Get Daily Briefing →</button>
            </form>
          </div>
        </div>

        <div class="sidebar-widget">
          <div class="widget-header">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>
            Trending Topics
          </div>
          <div class="widget-body">
            <div class="trending-item"><div class="trending-rank">1</div><div class="trending-info"><div class="trending-topic">OPEC+ Output Cuts</div><div class="trending-count">Top story this week</div></div><div class="trending-arrow">→</div></div>
            <div class="trending-item"><div class="trending-rank">2</div><div class="trending-info"><div class="trending-topic">US LNG Exports</div><div class="trending-count">Rising coverage</div></div><div class="trending-arrow">→</div></div>
            <div class="trending-item"><div class="trending-rank">3</div><div class="trending-info"><div class="trending-topic">Brent Price Outlook</div><div class="trending-count">Analyst focus</div></div><div class="trending-arrow">→</div></div>
          </div>
        </div>

        <div class="ad-placeholder" aria-label="Advertisement space">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
          Advertisement
        </div>
      </aside>
    </div>

  </div>
</main>

<!-- Footer -->
<footer class="site-footer" role="contentinfo">
  <div class="container">
    <div class="footer-grid">
      <div class="footer-brand">
        <a class="logo" href="/" aria-label="FuelWired Home">
          <div class="logo-icon">
            <svg viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
              <rect width="40" height="40" rx="8" fill="#1c2333"/>
              <polygon points="22,5 12,22 20,22 18,36 28,18 20,18" fill="url(#bolt-grad2)"/>
              <defs><linearGradient id="bolt-grad2" x1="12" y1="5" x2="28" y2="36" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#fbbf24"/><stop offset="100%" stop-color="#d97706"/></linearGradient></defs>
            </svg>
          </div>
          <div><div class="logo-text">FuelWired</div><div class="logo-tagline">Oil &amp; Gas Intelligence</div></div>
        </a>
        <p class="footer-desc">Independent oil and gas industry intelligence — from wellhead to pump.</p>
        <div class="footer-social">
          <a class="social-btn" href="#" aria-label="X (Twitter)"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.744l7.73-8.835L1.254 2.25H8.08l4.259 5.63 5.905-5.63zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg></a>
          <a class="social-btn" href="#" aria-label="LinkedIn"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg></a>
          <a class="social-btn" href="#" aria-label="RSS"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M6.18 15.64a2.18 2.18 0 0 1 2.18 2.18C8.36 19.01 7.38 20 6.18 20C4.98 20 4 19.01 4 17.82a2.18 2.18 0 0 1 2.18-2.18M4 4.44A15.56 15.56 0 0 1 19.56 20h-2.83A12.73 12.73 0 0 0 4 7.27V4.44m0 5.66a9.9 9.9 0 0 1 9.9 9.9h-2.83A7.07 7.07 0 0 0 4 12.93V10.1z"/></svg></a>
        </div>
      </div>
      <div>
        <div class="footer-col-title">Coverage</div>
        <ul class="footer-links">
          <li><a href="#">Upstream</a></li><li><a href="#">Downstream</a></li>
          <li><a href="#">LNG Markets</a></li><li><a href="#">Oil Markets</a></li>
          <li><a href="#">Natural Gas</a></li><li><a href="#">Energy Transition</a></li>
        </ul>
      </div>
      <div>
        <div class="footer-col-title">Resources</div>
        <ul class="footer-links">
          <li><a href="#">Price Dashboard</a></li><li><a href="#">EIA Data</a></li>
          <li><a href="#">OPEC Reports</a></li><li><a href="#">Rig Counts</a></li>
          <li><a href="#">LNG Tracker</a></li>
        </ul>
      </div>
      <div>
        <div class="footer-col-title">Company</div>
        <ul class="footer-links">
          <li><a href="about.html">About</a></li><li><a href="#">Advertise</a></li>
          <li><a href="#">Contact</a></li><li><a href="#">Privacy Policy</a></li>
          <li><a href="#">Terms of Use</a></li>
        </ul>
      </div>
    </div>
    <div class="footer-bottom">
      <span>© {datetime.datetime.utcnow().year} FuelWired. All rights reserved.</span>
      <div class="footer-bottom-links">
        <a href="#">Privacy</a><a href="#">Terms</a><a href="#">Accessibility</a>
      </div>
    </div>
  </div>
</footer>

<script>
function handleSubscribe(e) {{
  e.preventDefault();
  const btn = e.target.querySelector('.newsletter-btn');
  const input = e.target.querySelector('.newsletter-input');
  btn.textContent = '✓ You\\'re subscribed!';
  btn.style.background = 'var(--green-accent)';
  input.value = '';
  input.disabled = true;
  btn.disabled = true;
}}
document.querySelector('.hamburger').addEventListener('click', function() {{
  const nav = document.querySelector('.main-nav');
  const open = nav.style.display === 'flex';
  nav.style.display = open ? '' : 'flex';
  nav.style.flexDirection = 'column';
  nav.style.position = 'absolute';
  nav.style.top = '64px';
  nav.style.left = '0';
  nav.style.right = '0';
  nav.style.background = 'var(--bg-secondary)';
  nav.style.padding = '12px 20px';
  nav.style.borderBottom = '1px solid var(--border)';
  nav.style.zIndex = '99';
}});
const observer = new IntersectionObserver((entries) => {{
  entries.forEach(e => {{
    if (e.isIntersecting) {{
      e.target.style.opacity = '1';
      e.target.style.transform = 'translateY(0)';
    }}
  }});
}}, {{ threshold: 0.1 }});
document.querySelectorAll('.article-card').forEach(card => {{
  card.style.opacity = '0';
  card.style.transform = 'translateY(16px)';
  card.style.transition = 'opacity .4s ease, transform .4s ease';
  card.setAttribute('tabindex', '0');
  card.addEventListener('keydown', e => {{ if (e.key === 'Enter') card.click(); }});
  observer.observe(card);
}});
</script>
</body>
</html>
"""

    path = os.path.join(OUTPUT_DIR, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  ✓ Regenerated index.html ({len(display)} articles)")


# ──────────────────────────────────────────────────────────────────────────────
# ARTICLES.JSON MANIFEST
# ──────────────────────────────────────────────────────────────────────────────

def load_existing_articles() -> list:
    """Load existing articles.json (for deduplication/merging)."""
    if not os.path.exists(ARTICLES_JSON):
        return []
    try:
        with open(ARTICLES_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def save_articles(articles: list) -> None:
    """Persist articles to articles.json."""
    with open(ARTICLES_JSON, "w", encoding="utf-8") as f:
        json.dump(articles, f, indent=2, ensure_ascii=False)
    print(f"  ✓ Saved articles.json ({len(articles)} total articles)")


def merge_articles(existing: list, fresh: list) -> list:
    """Merge fresh articles into existing, deduplicate, keep newest MAX_ARTICLES*3."""
    existing_ids = {a["id"] for a in existing}
    new_items    = [a for a in fresh if a["id"] not in existing_ids]
    merged       = new_items + existing
    merged.sort(key=lambda x: x["pub_date"], reverse=True)
    return merged[:MAX_ARTICLES * 10]  # Keep a generous archive


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    start = time.time()
    print("=" * 60)
    print(" FuelWired — News Updater")
    print(f" {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    print("\n[1/4] Fetching RSS feeds…")
    fresh_articles = fetch_all_feeds()
    print(f"  → {len(fresh_articles)} unique items fetched across all feeds")

    print("\n[2/4] Merging with existing archive…")
    existing   = load_existing_articles()
    print(f"  → {len(existing)} existing articles in archive")
    all_articles = merge_articles(existing, fresh_articles)
    print(f"  → {len(all_articles)} total after merge")

    if not all_articles:
        print("\n  ⚠ No articles available. Exiting without changes.")
        sys.exit(0)

    print("\n[3/4] Generating article pages…")
    generated_count = 0
    for article in all_articles[:MAX_ARTICLES * 3]:  # Generate pages for recent articles
        try:
            generate_article_page(article)
            generated_count += 1
        except Exception as e:
            print(f"  ⚠ Failed to generate page for '{article.get('title','?')[:50]}': {e}")
    print(f"  ✓ Generated {generated_count} article pages")

    print("\n[4/4] Regenerating index.html and saving manifest…")
    regenerate_index(all_articles)
    save_articles(all_articles)

    elapsed = time.time() - start
    print(f"\n✅ Update complete in {elapsed:.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
