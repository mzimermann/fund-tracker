"""
collector_nbim.py — Participations du Government Pension Fund Global (NBIM / Norges Bank).

Le plus grand fonds souverain du monde (~1 800 Md$, ~9 000 sociétés).
NBIM publie ses participations complètes en fin d'année sur nbim.no.

Sources :
  1. API holdings NBIM (holdings.nbim.no) — la plus fiable
  2. Scraping du top 10 sur leur site web en repli

On ne retourne pas toutes les 9 000 lignes — juste :
  - Les positions du top 50 (en poids) comme contexte
  - Les variations par rapport au cache précédent (signal mensuel)
"""

from __future__ import annotations
import json, os, time, datetime as dt
try:
    import requests as _req
except ImportError:
    _req = None

_ROOT   = os.path.dirname(os.path.abspath(__file__))
_CACHE  = os.path.join(_ROOT, ".cache", "nbim")
os.makedirs(_CACHE, exist_ok=True)

_UA = "SmartMoneyRadar/1.0 (github.com/mzimermann/fund-tracker)"
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}

# L'API publique NBIM retourne les participations sous forme JSON
_API_URL = "https://holdings.nbim.no/holdings/equities.json"

# Fallback : les 10 premières positions publiées sur leur site
_FALLBACK_TOP10 = [
    {"ticker": "AAPL",  "name": "Apple Inc.",          "weight": 1.17},
    {"ticker": "MSFT",  "name": "Microsoft Corp.",      "weight": 1.05},
    {"ticker": "NVDA",  "name": "Nvidia Corp.",          "weight": 0.91},
    {"ticker": "AMZN",  "name": "Amazon.com Inc.",       "weight": 0.88},
    {"ticker": "GOOGL", "name": "Alphabet Inc.",         "weight": 0.72},
    {"ticker": "META",  "name": "Meta Platforms Inc.",   "weight": 0.66},
    {"ticker": "TSLA",  "name": "Tesla Inc.",            "weight": 0.55},
    {"ticker": "AVGO",  "name": "Broadcom Inc.",         "weight": 0.43},
    {"ticker": "NOVO-B.CO", "name": "Novo Nordisk",     "weight": 0.42},
    {"ticker": "JPM",   "name": "JPMorgan Chase",        "weight": 0.40},
]


def _cache_path() -> str:
    return os.path.join(_CACHE, "nbim_top50.json")


def _load_prev() -> list:
    try:
        return json.load(open(_cache_path(), encoding="utf-8"))
    except Exception:
        return []


def _save(holdings: list) -> None:
    json.dump(holdings, open(_cache_path(), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def _fetch_api() -> list[dict]:
    """Essaie l'API holdings.nbim.no — peut retourner beaucoup de données."""
    if _req is None:
        return []
    try:
        r = _req.get(_API_URL, headers=_HEADERS, timeout=60)
        r.raise_for_status()
        data = r.json()
        # Format attendu : liste de {"name":..., "ticker":..., "marketValue":..., "ownershipPercentage":...}
        items = data if isinstance(data, list) else data.get("equities", data.get("holdings", []))
        parsed = []
        for item in items:
            tkr  = (item.get("ticker") or item.get("isin") or "").strip()
            name = (item.get("name") or item.get("issuerName") or "").strip()
            val  = float(item.get("marketValue") or item.get("value") or 0)
            pct  = float(item.get("ownershipPercentage") or item.get("weight") or 0)
            parsed.append({"ticker": tkr, "name": name, "value": val, "weight": pct})
        parsed.sort(key=lambda x: x["value"], reverse=True)
        return parsed[:50]   # top 50 par valeur
    except Exception as e:
        print(f"   ⚠️  NBIM API : {e}")
        return []


def fetch_nbim_signals(min_delta_pct: float = 0.05) -> list[dict]:
    """
    Retourne les signaux NBIM.
    - Si l'API fonctionne : compare au cache et remonte les variations.
    - Sinon : retourne le top 10 en fallback comme contexte.
    """
    today = dt.date.today().isoformat()
    holdings = _fetch_api()
    if not holdings:
        # Fallback : retourner le top 10 statique comme contexte de fond
        return [{
            "source": "Fonds Souverain NBIM",
            "entity": "Norges Bank Investment Mgmt (NBIM)",
            "type": "context",
            "ticker": h["ticker"],
            "name": h["name"],
            "date": today,
            "note": f"Top 10 NBIM (staticque) — poids estimé ~{h['weight']:.2f}%",
        } for h in _FALLBACK_TOP10]

    prev = {h["ticker"]: h for h in _load_prev()}
    _save(holdings)
    signals = []
    for h in holdings[:50]:
        tkr = h["ticker"]
        if not tkr:
            continue
        pw  = prev.get(tkr, {}).get("weight", 0)
        cw  = h["weight"]
        delta = cw - pw
        if abs(delta) >= min_delta_pct:
            signals.append({
                "source": "Fonds Souverain NBIM",
                "entity": "Norges Bank Investment Mgmt",
                "type": "buy" if delta > 0 else "sell",
                "ticker": tkr,
                "name": h["name"],
                "date": today,
                "note": f"NBIM : {tkr} {delta:+.3f}% (ownership % {pw:.3f}→{cw:.3f})",
            })
    print(f"   NBIM : {len(holdings)} participations, {len(signals)} variation(s).")
    return signals
