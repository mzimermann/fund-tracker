"""
collector_13dg.py — Déclarations SC 13D/G (franchissements de seuil >5 %).

Source : flux RSS officiel SEC EDGAR (gratuit, sans clé).
Délai légal : 10 jours après la transaction.

Signal : un investisseur activiste ou institutionnel vient de prendre
une participation importante dans une société → souvent précurseur
d'une campagne activiste, d'une OPA, ou d'un retournement stratégique.
"""

from __future__ import annotations
import re, datetime as dt
try:
    import requests as _req
except ImportError:
    _req = None
try:
    from xml.etree import ElementTree as ET
except ImportError:
    ET = None  # type: ignore

# Délai max de freshness
_MAX_DAYS = 10

_HEADERS = {"User-Agent": "SmartMoneyRadar mzimermannpro@gmail.com"}

# Activistes et investisseurs connus — leurs noms apparaîtront dans le signal
_KNOWN_FILERS = {
    "icahn": "Carl Icahn",
    "pershing": "Bill Ackman (Pershing)",
    "elliott": "Elliott Management",
    "starboard": "Starboard Value",
    "valueact": "ValueAct Capital",
    "trian": "Nelson Peltz (Trian)",
    "third point": "Dan Loeb (Third Point)",
    "jana": "Jana Partners",
    "ancora": "Ancora Holdings",
    "legion": "Legion Partners",
    "sachem": "Sachem Head",
    "corvex": "Corvex Management",
    "greenlight": "David Einhorn (Greenlight)",
    "appaloosa": "David Tepper (Appaloosa)",
    "duquesne": "Stan Druckenmiller",
    "berkshire": "Berkshire Hathaway (Warren Buffett)",
}

# URL RSS EDGAR pour les dépôts récents 13D et 13G
_RSS_URLS = {
    "SC 13D": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D&dateb=&owner=include&count=40&output=atom",
    "SC 13G": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13G&dateb=&owner=include&count=40&output=atom",
}
_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _identify_filer(filer_raw: str) -> str:
    """Retourne le nom connu si le filer est reconnu, sinon le nom brut."""
    low = filer_raw.lower()
    for key, display in _KNOWN_FILERS.items():
        if key in low:
            return display
    return filer_raw.strip()


def _parse_rss(form_type: str, xml_text: str, max_days: int) -> list[dict]:
    """Parse le flux Atom EDGAR et extrait les entrées récentes."""
    if not ET:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    cutoff = dt.date.today() - dt.timedelta(days=max_days)
    results = []
    for entry in root.findall("atom:entry", _NS):
        # Titre format : "SC 13D - TARGET_COMPANY (CIK) - FILER_NAME"
        title_el = entry.find("atom:title", _NS)
        if title_el is None or not title_el.text:
            continue
        title = title_el.text.strip()
        # Extraire la date de mise à jour
        updated_el = entry.find("atom:updated", _NS)
        date_str = ""
        if updated_el is not None and updated_el.text:
            try:
                date_str = updated_el.text[:10]  # "YYYY-MM-DD"
                filing_date = dt.date.fromisoformat(date_str)
                if filing_date < cutoff:
                    continue
            except ValueError:
                pass
        # Extraire target et filer du titre
        # Pattern: "FORM - TARGET (CIK) - FILER" ou "FORM - TARGET - FILER"
        parts = [p.strip() for p in title.split(" - ")]
        if len(parts) >= 3:
            target_raw = parts[1]
            filer_raw  = parts[-1]
        elif len(parts) == 2:
            target_raw = parts[1]
            filer_raw  = "Inconnu"
        else:
            target_raw = title
            filer_raw  = "Inconnu"
        # Nettoyer le CIK entre parenthèses dans le target
        target = re.sub(r'\(\d+\)', '', target_raw).strip()
        filer  = _identify_filer(filer_raw)
        # URL du dépôt
        link_el = entry.find("atom:link", _NS)
        url = link_el.get("href", "") if link_el is not None else ""
        note = f"Déclaration {form_type} : {filer} détient >5% de {target}"
        is_activist = any(k in filer_raw.lower() for k in _KNOWN_FILERS)
        results.append({
            "source": "SEC 13D/G",
            "entity": filer,
            "type": "buy",
            "ticker": "",          # pas extrait directement (complexe)
            "name": target,
            "date": date_str,
            "note": note,
            "is_activist": is_activist,
            "form_type": form_type,
            "url": url,
        })
    return results


def fetch_recent_13dg(days: int = 10) -> list[dict]:
    """Récupère les 13D et 13G récents depuis EDGAR. Renvoie [] en cas d'échec."""
    if _req is None:
        return []
    results = []
    for form_type, url in _RSS_URLS.items():
        try:
            r = _req.get(url, headers=_HEADERS, timeout=30)
            r.raise_for_status()
            entries = _parse_rss(form_type, r.text, days)
            results.extend(entries)
            print(f"   13D/G — {form_type} : {len(entries)} dépôt(s) récent(s).")
        except Exception as e:
            print(f"   ⚠️  13D/G {form_type} échoué : {e}")
    # Trier par pertinence : activistes d'abord
    results.sort(key=lambda x: (not x.get("is_activist", False), x.get("date", "")))
    return results
