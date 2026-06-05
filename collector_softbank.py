"""
collector_softbank.py — Annonces d'investissement des grands conglomérats.

Surveille les communiqués de presse de :
  - SoftBank Group (Vision Fund I & II)
  - Berkshire Hathaway (communiqués hors 13F — OPA, fusions)
  - Sequoia Capital (annonces publiques)

Sources : flux RSS/Atom publics + scraping défensif.
BeautifulSoup est optionnel — repli RSS si bs4 non installé.
"""

from __future__ import annotations
import re, datetime as dt
try:
    import requests as _req
except ImportError:
    _req = None
try:
    from bs4 import BeautifulSoup as _BS
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False
try:
    import feedparser as _fp
    _HAS_FP = True
except ImportError:
    _HAS_FP = False

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SmartMoneyRadar/1.0)",
    "Accept": "application/rss+xml, application/atom+xml, text/html;q=0.8",
}

# Mots-clés signalant une prise de participation ou un investissement
_INVEST_KW = [
    "stake", "acqui", "invest", "fund", "portfolio", "participat",
    "backs", "leads round", "series", "capital", "prise de participation",
    "investit", "acquisition", "prend une participation", "vision fund",
]

# Sources de nouvelles
_SOURCES = [
    {
        "name": "SoftBank Group",
        "type": "rss",
        "url": "https://group.softbank/en/news/rss",
    },
    {
        "name": "SoftBank (PR Newswire)",
        "type": "rss",
        "url": "https://www.prnewswire.com/rss/news-releases-list.rss?queryId=softbank",
    },
    {
        "name": "Berkshire Hathaway (SEC 8-K)",
        "type": "sec_rss",
        "url": ("https://www.sec.gov/cgi-bin/browse-edgar"
                "?action=getcurrent&CIK=0001067983&type=8-K"
                "&dateb=&owner=include&count=10&output=atom"),
    },
]


def _is_investment_news(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in _INVEST_KW)


def _parse_rss(source: dict) -> list[dict]:
    """Parse un flux RSS/Atom avec feedparser ou requests fallback."""
    entries = []
    url = source["url"]
    if _HAS_FP:
        try:
            feed = _fp.parse(url)
            for e in (feed.entries or [])[:10]:
                title = e.get("title", "")
                date  = e.get("published", dt.date.today().isoformat())[:10]
                link  = e.get("link", "")
                entries.append({"title": title, "date": date, "link": link})
        except Exception:
            pass
    if not entries and _req is not None:
        try:
            from xml.etree import ElementTree as ET
            r = _req.get(url, headers=_HEADERS, timeout=20)
            if r.status_code == 200:
                root = ET.fromstring(r.text)
                ns   = {"atom": "http://www.w3.org/2005/Atom"}
                for item in (root.findall(".//item") or root.findall("atom:entry", ns))[:10]:
                    t = (item.findtext("title") or item.findtext("atom:title", namespaces=ns) or "")
                    d = (item.findtext("pubDate") or item.findtext("atom:updated", namespaces=ns) or "")
                    entries.append({"title": t, "date": d[:10], "link": ""})
        except Exception:
            pass
    return entries


def _scrape_html(source: dict) -> list[dict]:
    """Scrape HTML avec BeautifulSoup (si disponible)."""
    if not _HAS_BS4 or _req is None:
        return []
    entries = []
    try:
        r = _req.get(source["url"], headers=_HEADERS, timeout=25)
        r.raise_for_status()
        soup = _BS(r.text, "html.parser")
        for item in soup.select("article, .news-item, .press-release, li.news")[:12]:
            ttag = item.select_one("h2, h3, a.title, .headline, a")
            if not ttag:
                continue
            title = ttag.get_text(strip=True)
            dtag  = item.select_one("time, .date, .published")
            date  = dtag.get_text(strip=True)[:10] if dtag else dt.date.today().isoformat()
            entries.append({"title": title, "date": date, "link": ""})
    except Exception as e:
        print(f"   ⚠️  Scraping {source['name']} : {e}")
    return entries


def fetch_conglomerate_signals() -> list[dict]:
    """Collecte les annonces d'investissement des grands conglomérats."""
    results = []
    today = dt.date.today().isoformat()
    for src in _SOURCES:
        try:
            if src["type"] in ("rss", "sec_rss"):
                entries = _parse_rss(src)
            else:
                entries = _scrape_html(src)
            hits = [e for e in entries if _is_investment_news(e["title"])]
            for h in hits:
                results.append({
                    "source": "Conglomérat",
                    "entity": src["name"],
                    "type": "buy",
                    "ticker": "",
                    "name": h["title"][:90],
                    "date": h.get("date", today),
                    "note": h["title"],
                })
            if hits:
                print(f"   {src['name']} : {len(hits)} annonce(s) d'investissement.")
        except Exception as e:
            print(f"   ⚠️  {src['name']} : {e}")
    return results
