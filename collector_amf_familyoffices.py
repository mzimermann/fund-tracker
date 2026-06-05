"""
collector_amf_familyoffices.py — Déclarations de franchissement de seuil (Europe).

Surveille les déclarations de franchissement de seuil obligatoires publiées par :
  - AMF (France) — amf-france.org
  - BaFin (Allemagne) — bafin.de
  - (optionnel) CONSOB (Italie), CNMV (Espagne)

Quand un investisseur (family office, fonds, activiste) franchit 5 %, 10 %, 15 %...
du capital d'une société cotée, il doit le déclarer. Ces déclarations sont publiques
et constituent un signal d'accumulation discrète.

Sources : sites publics des régulateurs + flux RSS si disponibles.
BeautifulSoup est optionnel — feedparser suffit si flux RSS disponible.
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
try:
    from xml.etree import ElementTree as ET
except ImportError:
    ET = None  # type: ignore

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SmartMoneyRadar/1.0)",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# Régulateurs et leurs flux de déclarations
_REGULATORS = [
    {
        "name": "AMF (France)",
        "type": "rss",
        "url": "https://www.amf-france.org/fr/rss/actualites-publications.rss",
        "filter_kw": ["franchissement", "seuil", "5%", "10%", "15%", "participation"],
    },
    {
        "name": "BaFin (Allemagne)",
        "type": "rss",
        "url": "https://www.bafin.de/SiteGlobals/Functions/RSSFeed/DE/RSSNewsfeed/RSS_Hauptmeldungen.xml",
        "filter_kw": ["stimmrechte", "aktien", "voting rights", "shares", "threshold", "5%"],
    },
    {
        "name": "AMF (BDIF — recherche générale)",
        "type": "html",
        "url": "https://bdif.amf-france.org/apex/f?p=206:2:0::NO:RP,2:P2_DATE_FROM:TODAY",
        "filter_kw": ["franchissement"],
    },
]


def _extract_ticker(text: str) -> str:
    """Tente d'extraire un ticker entre parenthèses, ex: SOCIÉTÉ GÉNÉRALE (GLE)."""
    m = re.search(r'\(([A-Z]{2,6})\)', text)
    return m.group(1) if m else ""


def _extract_entity(text: str) -> str:
    """Extrait l'entité déclarante si présente après ' - ' ou 'par '."""
    m = re.search(r'(?:par|by|von)\s+(.+?)(?:\s*[:-]|$)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()[:60]
    return text[:60]


def _parse_feed(url: str, filter_kw: list[str]) -> list[dict]:
    """Parse un flux RSS/Atom et filtre par mots-clés."""
    entries = []
    if _HAS_FP:
        try:
            feed = _fp.parse(url)
            for e in (feed.entries or [])[:20]:
                title = e.get("title", "") + " " + e.get("summary", "")
                date  = e.get("published", dt.date.today().isoformat())[:10]
                if any(kw.lower() in title.lower() for kw in filter_kw):
                    entries.append({"title": e.get("title", ""), "date": date})
            return entries
        except Exception:
            pass
    if _req is None or ET is None:
        return []
    try:
        r = _req.get(url, headers=_HEADERS, timeout=20)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        items = (root.findall(".//item") or root.findall("atom:entry", ns))
        for item in items[:20]:
            t = (item.findtext("title") or
                 item.findtext("atom:title", namespaces=ns) or "")
            d = (item.findtext("pubDate") or
                 item.findtext("atom:updated", namespaces=ns) or
                 dt.date.today().isoformat())
            if any(kw.lower() in t.lower() for kw in filter_kw):
                entries.append({"title": t, "date": d[:10]})
    except Exception:
        pass
    return entries


def _scrape_html(url: str, filter_kw: list[str]) -> list[dict]:
    """Scrape HTML avec BeautifulSoup en dernier recours."""
    if not _HAS_BS4 or _req is None:
        return []
    entries = []
    try:
        r = _req.get(url, headers=_HEADERS, timeout=25)
        r.raise_for_status()
        soup = _BS(r.text, "html.parser")
        for item in soup.select("article, li, tr, .item")[:30]:
            text = item.get_text(separator=" ", strip=True)
            if len(text) < 15:
                continue
            if any(kw.lower() in text.lower() for kw in filter_kw):
                ttag = item.select_one("a, h3, h4, strong")
                title = ttag.get_text(strip=True) if ttag else text[:100]
                dtag  = item.select_one("time, .date, .published")
                date  = dtag.get_text(strip=True)[:10] if dtag else dt.date.today().isoformat()
                entries.append({"title": title, "date": date})
    except Exception as e:
        print(f"   ⚠️  Scraping AMF/BaFin : {e}")
    return entries


def fetch_threshold_signals() -> list[dict]:
    """Retourne les déclarations de franchissement de seuil récentes."""
    results = []
    today = dt.date.today().isoformat()
    for reg in _REGULATORS:
        try:
            if reg["type"] == "rss":
                entries = _parse_feed(reg["url"], reg["filter_kw"])
            else:
                entries = _scrape_html(reg["url"], reg["filter_kw"])
            for e in entries[:10]:
                ticker = _extract_ticker(e["title"])
                entity = _extract_entity(e["title"])
                results.append({
                    "source": reg["name"],
                    "entity": entity,
                    "type": "buy",
                    "ticker": ticker,
                    "name": e["title"][:90],
                    "date": e.get("date", today),
                    "note": e["title"],
                })
            if entries:
                print(f"   {reg['name']} : {len(entries)} déclaration(s) de seuil.")
        except Exception as e:
            print(f"   ⚠️  {reg['name']} : {e}")
    return results
