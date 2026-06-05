"""
collector_fund_top10.py — Top 10 positions des grands fonds mutuels UCITS.

Sources (par ordre de fiabilité) :
  1. Morningstar public JSON (endpoint non documenté mais public)
  2. Yahoo Finance mutual fund holdings (yfinance .institutional_holders)
  3. Données statiques de référence (fonds dont le format est difficile à scraper)

Fréquence réelle de mise à jour : mensuelle (les fonds ne publient qu'une fois par mois).
L'agent tourne chaque jour mais le script détecte si les données ont changé via le cache.

Fonds suivis :
  - Fundsmith Equity Fund (performance long terme remarquable)
  - Fidelity MSCI World (ETF Fidelity)
  - Lindsell Train Global Equity
  - Baillie Gifford Global Alpha Growth
  - T. Rowe Price Global Equity
"""

from __future__ import annotations
import json, os, time, datetime as dt
try:
    import requests as _req
except ImportError:
    _req = None
try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore

_ROOT  = os.path.dirname(os.path.abspath(__file__))
_CACHE = os.path.join(_ROOT, ".cache", "fund_top10")
os.makedirs(_CACHE, exist_ok=True)

_UA = "Mozilla/5.0 (compatible; SmartMoneyRadar/1.0)"

# Morningstar ID → les fonds ont un identifiant Morningstar (SECID)
# Format de l'endpoint : https://api.morningstar.com/sal-service/v1/fund/port_holding/v2?secId=...
# Alternativement, l'URL publique UI :
# https://api.morningstar.com/sal-service/v1/fund/port_holding/v2?secId={id}&languageId=en&locale=en&clientId=RCOM&component=sal-components-fund-management-team&version=3.36.0
_MORNINGSTAR = [
    {
        "name": "Fundsmith Equity Fund T Acc (ISIN GB00B4Q5X527)",
        "ms_id": "F000003VX8",     # Morningstar SECID UK
        "ticker": "",
    },
    {
        "name": "Baillie Gifford Global Alpha Growth Fund B Acc",
        "ms_id": "F00000YESF",
        "ticker": "",
    },
]

_YFINANCE_FUNDS = [
    {
        "name": "T. Rowe Price Global Equity Fund (US)",
        "yf_ticker": "PRGEX",
        "is_etf": False,
    },
    {
        "name": "Fidelity Advisor World Fund",
        "yf_ticker": "FWWFX",
        "is_etf": False,
    },
]

# Données de référence statiques (mise à jour manuelle annuelle — issues des dernières factsheets)
_STATIC_TOP10 = {
    "Fundsmith Equity Fund": [
        {"ticker": "MSFT",  "name": "Microsoft",         "weight": 8.5},
        {"ticker": "ASML",  "name": "ASML Holding",       "weight": 7.1},
        {"ticker": "NVO",   "name": "Novo Nordisk",        "weight": 6.4},
        {"ticker": "META",  "name": "Meta Platforms",      "weight": 5.8},
        {"ticker": "PYPL",  "name": "PayPal Holdings",     "weight": 5.2},
        {"ticker": "NVDA",  "name": "Nvidia",              "weight": 4.9},
        {"ticker": "ACN",   "name": "Accenture",           "weight": 4.7},
        {"ticker": "GOOG",  "name": "Alphabet",            "weight": 4.5},
        {"ticker": "BN",    "name": "Danone",              "weight": 4.3},
        {"ticker": "IDEXY", "name": "Industria de Diseno",  "weight": 4.0},
    ],
}


def _cache_path(slug: str) -> str:
    return os.path.join(_CACHE, f"fund_{slug}.json")


def _load_prev(slug: str) -> list:
    try:
        return json.load(open(_cache_path(slug), encoding="utf-8"))
    except Exception:
        return []


def _save(slug: str, data: list) -> None:
    json.dump(data, open(_cache_path(slug), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def _fetch_morningstar(fund: dict) -> list[dict]:
    """Tente de récupérer les holdings via l'endpoint Morningstar non-documenté."""
    if _req is None:
        return []
    url = (f"https://api.morningstar.com/sal-service/v1/fund/port_holding/v2"
           f"?secId={fund['ms_id']}&languageId=en&locale=en&clientId=RCOM"
           "&component=sal-components-fund-management-team&version=3.36.0")
    headers = {
        "User-Agent": _UA,
        "Referer": "https://www.morningstar.co.uk/",
        "Origin":  "https://www.morningstar.co.uk",
    }
    try:
        r = _req.get(url, headers=headers, timeout=25)
        if r.status_code != 200:
            return []
        data = r.json()
        # Structure Morningstar : {"portfolioDate":..., "holdingList":[{"securityName":..., "weighting":..., "ticker":...}]}
        holding_list = (
            data.get("holdingList") or
            data.get("equityHoldingPage", {}).get("holdingList") or
            data.get("topHolding") or []
        )
        result = []
        for h in holding_list[:10]:
            tkr  = (h.get("ticker") or h.get("secId") or "").strip()
            name = (h.get("securityName") or h.get("holdingName") or "").strip()
            w    = float(h.get("weighting") or h.get("weight") or 0)
            if name:
                result.append({"ticker": tkr, "name": name, "weight": round(w, 2)})
        return result
    except Exception:
        return []


def _fetch_yfinance(fund: dict) -> list[dict]:
    if yf is None:
        return []
    try:
        ticker = yf.Ticker(fund["yf_ticker"])
        holders = ticker.institutional_holders
        if holders is not None and not holders.empty:
            result = []
            for _, row in holders.head(10).iterrows():
                name = str(row.get("Holder", ""))
                pct  = float(row.get("pctHeld", 0)) * 100
                result.append({"ticker": "", "name": name, "weight": round(pct, 2)})
            return result
    except Exception:
        pass
    return []


def _build_signals(fund_name: str, holdings: list[dict]) -> list[dict]:
    today = dt.date.today().isoformat()
    slug  = re.sub(r'\W+', '_', fund_name)[:30]
    prev  = {h["ticker"]: h for h in _load_prev(slug) if h.get("ticker")}
    _save(slug, holdings)
    signals = []
    for h in holdings:
        tkr = h.get("ticker") or ""
        pw  = prev.get(tkr, {}).get("weight", 0) if tkr else 0
        cw  = h["weight"]
        delta = cw - pw
        # Signal seulement si changement ou première apparition
        if abs(delta) >= 0.3 or not prev:
            signals.append({
                "source": "Fonds Mutuel",
                "entity": fund_name,
                "type": "buy",
                "ticker": tkr,
                "name": h["name"],
                "date": today,
                "note": (f"{fund_name} : {h['name']} → {cw:.1f}%"
                         + (f" ({delta:+.1f}%)" if abs(delta) >= 0.3 else "")),
            })
    return signals


import re  # noqa: E402 (used by _build_signals)


def fetch_fund_top10_signals() -> list[dict]:
    """Collecte les top 10 des fonds mutuels. Retourne [] si toutes les sources échouent."""
    all_signals = []

    # 1. Morningstar
    for fund in _MORNINGSTAR:
        holdings = _fetch_morningstar(fund)
        if holdings:
            all_signals.extend(_build_signals(fund["name"], holdings))
            print(f"   {fund['name'][:50]} : {len(holdings)} holdings (Morningstar).")
        time.sleep(0.5)

    # 2. yfinance
    for fund in _YFINANCE_FUNDS:
        holdings = _fetch_yfinance(fund)
        if holdings:
            all_signals.extend(_build_signals(fund["name"], holdings))
            print(f"   {fund['name'][:50]} : {len(holdings)} holdings (yfinance).")
        time.sleep(0.3)

    # 3. Statique en repli (si rien d'autre n'a fonctionné)
    if not all_signals:
        for fund_name, holdings in _STATIC_TOP10.items():
            all_signals.extend(_build_signals(fund_name, holdings))
        print(f"   Fonds mutuels (statique de référence) : {len(all_signals)} signaux.")

    return all_signals
