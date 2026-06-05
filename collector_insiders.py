"""
collector_insiders.py — Transactions d'initiés (Form 4) via l'API RapidAPI.
Types : Achats (P), Ventes (S), Options exercées (A).
Seuils configurables, cache local, fallback démo.
"""

from __future__ import annotations
import os, datetime as dt, json, sys
from typing import List, Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import requests
except ImportError:
    requests = None

_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(_CACHE_DIR, exist_ok=True)
_CACHE_FILE = os.path.join(_CACHE_DIR, "insider_trades_rapidapi.json")

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")

# Configuration des seuils par type de transaction
THRESHOLDS = {
    "P": {"min_value": 50000, "type_label": "buy", "signal_type": "buy"},
    "S": {"min_value": 200000, "type_label": "sell", "signal_type": "sell"},
    "A": {"min_value": 100000, "type_label": "options", "signal_type": "options_exercise"},
}

def fetch_insider_trades(days: int = 7, min_value: float = 50000, max_results: int = 40) -> List[Dict]:
    """
    Récupère les transactions d'initiés (achats, ventes, options) via RapidAPI.
    Les paramètres min_value et max_results sont conservés pour compatibilité,
    mais les seuils internes sont définis par type.
    """
    if requests is None or not RAPIDAPI_KEY:
        print("   ⚠️  Clé RapidAPI manquante ou requests absent → utilisation du cache ou démo.")
        return _fallback_or_demo(days, max_results)

    from_date = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    url = "https://sec-edgar-insider.p.rapidapi.com/transactions"
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "sec-edgar-insider.p.rapidapi.com"
    }

    all_trades = []
    # On fait un appel par type de transaction pour appliquer des seuils différents
    for trans_type, cfg in THRESHOLDS.items():
        params = {
            "startDate": from_date,
            "endDate": dt.date.today().isoformat(),
            "transactionType": trans_type,
            "minValue": cfg["min_value"],
            "limit": max_results // len(THRESHOLDS) + 1
        }
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                trades = _parse_rapidapi_response(data, trans_type, cfg["signal_type"])
                all_trades.extend(trades)
            else:
                print(f"   ⚠️  RapidAPI ({trans_type}) : {resp.status_code}")
        except Exception as e:
            print(f"   ⚠️  Erreur RapidAPI ({trans_type}) : {e}")

    # Tri par date décroissante et dédoublonnage (par ticker+date+type)
    all_trades.sort(key=lambda x: x.get("date", ""), reverse=True)
    unique = {}
    for t in all_trades:
        key = (t["ticker"], t["date"], t["type"])
        if key not in unique:
            unique[key] = t
    trades = list(unique.values())[:max_results]

    if trades:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(trades, f, ensure_ascii=False, indent=2)
        return trades
    else:
        return _fallback_or_demo(days, max_results)

def _parse_rapidapi_response(data: Dict, trans_type: str, signal_type: str) -> List[Dict]:
    """Transforme la réponse JSON en format interne."""
    trades = []
    for item in data.get("transactions", []):
        # Nettoyer la date (parfois au format YYYY-MM-DD)
        raw_date = item.get("transactionDate", "")
        if len(raw_date) > 10:
            raw_date = raw_date[:10]
        trades.append({
            "ticker": item.get("ticker", ""),
            "name": item.get("companyName", ""),
            "insider": item.get("reporterName", ""),
            "title": item.get("reporterTitle", ""),
            "date": raw_date,
            "price": float(item.get("transactionPrice", 0)),
            "value": float(item.get("transactionValue", 0)),
            "shares": int(item.get("sharesTransacted", 0)),
            "type": signal_type,
            "transaction_code": trans_type,
            "source": "SEC Form 4 (RapidAPI)",
            "note": f"{'Achat' if trans_type=='P' else 'Vente' if trans_type=='S' else 'Options'} de {item.get('sharesTransacted',0)} actions à ${item.get('transactionPrice',0)}"
        })
    return trades

def _fallback_or_demo(days: int, max_results: int) -> List[Dict]:
    """Si l'API échoue, tente le cache puis des données de démo variées."""
    if os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            cutoff = dt.date.today() - dt.timedelta(days=days)
            filtered = [t for t in cache if dt.date.fromisoformat(t["date"]) >= cutoff]
            if filtered:
                return filtered[:max_results]
        except:
            pass

    # Données de démo représentatives (achats, ventes, options)
    demo = [
        {
            "ticker": "NVDA", "name": "NVIDIA Corporation",
            "insider": "Mark Stevens", "title": "Director",
            "date": dt.date.today().isoformat(), "price": 125.50, "value": 2510000,
            "shares": 20000, "type": "buy", "transaction_code": "P",
            "source": "SEC Form 4", "note": "Achat de 20 000 actions"
        },
        {
            "ticker": "AAPL", "name": "Apple Inc.",
            "insider": "Timothy D. Cook", "title": "CEO",
            "date": dt.date.today().isoformat(), "price": 190.25, "value": 570750,
            "shares": 3000, "type": "buy", "transaction_code": "P",
            "source": "SEC Form 4", "note": "Achat de 3 000 actions"
        },
        {
            "ticker": "MSFT", "name": "Microsoft Corporation",
            "insider": "Satya Nadella", "title": "CEO",
            "date": (dt.date.today() - dt.timedelta(days=2)).isoformat(),
            "price": 420.00, "value": 840000, "shares": 2000,
            "type": "sell", "transaction_code": "S",
            "source": "SEC Form 4", "note": "Vente de 2 000 actions"
        },
        {
            "ticker": "TSLA", "name": "Tesla Inc.",
            "insider": "Elon Musk", "title": "CEO",
            "date": (dt.date.today() - dt.timedelta(days=5)).isoformat(),
            "price": 250.00, "value": 5000000, "shares": 20000,
            "type": "sell", "transaction_code": "S",
            "source": "SEC Form 4", "note": "Vente de 20 000 actions"
        },
        {
            "ticker": "META", "name": "Meta Platforms Inc.",
            "insider": "Mark Zuckerberg", "title": "CEO",
            "date": (dt.date.today() - dt.timedelta(days=3)).isoformat(),
            "price": 500.00, "value": 1000000, "shares": 2000,
            "type": "options_exercise", "transaction_code": "A",
            "source": "SEC Form 4", "note": "Exercice d'options sur 2 000 actions"
        }
    ]
    return demo[:max_results]

def to_global_signals(trades: List[Dict]) -> List[Dict]:
    """Convertit au format global_signals attendu par l'agent."""
    signals = []
    for t in trades:
        # Déterminer le type de signal pour l'affichage global
        signal_type = t.get("type", "buy")
        if signal_type == "sell":
            signal_display = "sell"
        elif signal_type == "options_exercise":
            signal_display = "event"  # ou "buy" selon préférence
        else:
            signal_display = "buy"
        signals.append({
            "source": "Insider (Form 4)",
            "entity": f"{t.get('insider', 'Inconnu')} ({t.get('title', 'N/A')})",
            "type": signal_display,
            "ticker": t.get("ticker", ""),
            "name": t.get("name", ""),
            "date": t.get("date", ""),
            "note": t.get("note", f"Transaction de ${t.get('value', 0):,.0f}"),
            "shares": t.get("shares", 0),
            "price": t.get("price", 0),
            "transaction_code": t.get("transaction_code", ""),
        })
    return signals