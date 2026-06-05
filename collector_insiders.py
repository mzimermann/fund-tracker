"""
collector_insiders.py — Collecte des transactions d'insiders (Form 4 SEC).

Source : OpenInsider (openinsider.com) — gratuit, sans clé API.
Récupère les ACHATS significatifs des dirigeants et administrateurs
des 7 derniers jours pour les intégrer comme signaux additionnels.

Robustesse : renvoie [] en cas d'échec (ne bloque jamais l'agent principal).
"""

from __future__ import annotations
import re, datetime as dt
try:
    import requests as _req
except ImportError:
    _req = None


# URL OpenInsider — achats (P) des 7 derniers jours, valeur ≥ $25k
_BASE_URL = (
    "https://openinsider.com/screener"
    "?s=&o=&pl=&ph=&ll=&lh=&fd=7&fdr=&td=0&tdr=&fdlyl=&fdlyh="
    "&daysago=7&xp=1&xs=0"      # xp=1 : achats seulement
    "&vl={min_k}&vh=&ocl=&och="
    "&sic1=-1&sicl=100&sich=9999&grp=0"
    "&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h="
    "&sortcol=0&cnt={count}&page=1"
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 SmartMoneyRadar/1.0 (github.com/fund-tracker)",
    "Accept": "text/html,application/xhtml+xml",
}


def _parse_value(s: str) -> float:
    """'$1,234,567' → 1234567.0"""
    return float(re.sub(r"[^\d.]", "", s) or 0)


def _extract_text(html_frag: str) -> str:
    return re.sub(r"<[^>]+>", "", html_frag).strip()


def _parse_table(html: str) -> list[dict]:
    """Extrait les lignes de la table 'tinytable' d'OpenInsider."""
    m = re.search(r'<table[^>]+class="[^"]*tinytable[^"]*"[^>]*>(.*?)</table>',
                  html, re.S | re.I)
    if not m:
        return []
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', m.group(1), re.S | re.I)
    if len(rows) < 2:
        return []

    results = []
    for row in rows[1:]:
        cells = [_extract_text(c) for c in
                 re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)]
        if len(cells) < 14:
            continue
        try:
            # Colonnes standard OpenInsider (layout août 2024) :
            # 0:Filing date, 1:Trade date, 2:Ticker, 3:Company,
            # 4:Insider, 5:Title, 6:Trade type, 7:Price, 8:Qty, 9:Owned,
            # 10:ΔOwn, 11:Value
            ticker     = cells[2].upper()
            company    = cells[3]
            insider    = cells[4]
            title      = cells[5]
            trade_type = cells[6]   # "P - Purchase" ou "S - Sale"
            price_str  = cells[7]
            value_str  = cells[11] if len(cells) > 11 else "0"
            trade_date = cells[1]

            if "P" not in trade_type:
                continue   # on ne garde que les achats

            value = _parse_value(value_str)
            price = _parse_value(price_str)

            if not ticker or not ticker.isalpha():
                continue

            results.append({
                "ticker": ticker,
                "name": company,
                "insider": insider,
                "title": title,
                "date": trade_date,
                "price": price,
                "value": value,
                "source": "OpenInsider",
            })
        except (ValueError, IndexError):
            continue
    return results


def fetch_insider_trades(days: int = 7, min_value: float = 50_000,
                         max_results: int = 40) -> list[dict]:
    """
    Récupère les achats d'insiders des `days` derniers jours.
    min_value : valeur minimale de la transaction en USD.
    Renvoie [] en cas d'échec (ne bloque jamais l'agent principal).
    """
    if _req is None:
        return []

    # min_k = valeur en milliers pour le filtre OpenInsider (&vl=)
    min_k = max(1, int(min_value / 1000))
    url = _BASE_URL.format(min_k=min_k, count=max_results)

    try:
        resp = _req.get(url, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        trades = _parse_table(resp.text)
        # Filtrer sur la valeur (le filtre URL n'est pas toujours exact)
        return [t for t in trades if t["value"] >= min_value]
    except Exception as e:
        print(f"   ⚠️  Collecte insiders échouée : {e}")
        return []


def to_global_signals(trades: list[dict]) -> list[dict]:
    """Convertit les trades en format global_signals pour le payload."""
    signals = []
    for t in trades:
        signals.append({
            "source": "Insider (Form 4)",
            "entity": f"{t['insider']} ({t['title']})",
            "type": "buy",
            "ticker": t["ticker"],
            "name": t["name"],
            "date": t["date"],
            "note": f"Achat de ${t['value']:,.0f} à ~${t['price']:.2f}",
        })
    return signals
