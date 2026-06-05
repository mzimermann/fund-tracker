"""
collector_short_interest.py — Taux de ventes à découvert (short interest).

Source : Yahoo Finance (yfinance), gratuit, sans clé.
Signal : un fort short interest sur un titre déjà en alerte 13F peut signaler
soit une divergence de conviction (les shorts misent contre les fonds), soit
un potentiel short squeeze si une bonne nouvelle survient.
"""

from __future__ import annotations
import datetime as dt
try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore


def fetch_short_interest(tickers: list[str],
                          min_short_pct: float = 15.0,
                          max_tickers: int = 30) -> list[dict]:
    """
    Renvoie les tickers dont le short interest dépasse min_short_pct%.
    Ne traite pas plus de max_tickers pour éviter les timeouts.
    """
    if yf is None or not tickers:
        return []
    results = []
    today = dt.date.today().isoformat()
    for tkr in tickers[:max_tickers]:
        try:
            info = yf.Ticker(tkr).info
            # shortPercentOfFloat est un ratio (0.0 à 1.0)
            raw = info.get("shortPercentOfFloat") or info.get("sharesPercentSharesOut")
            if raw is None:
                continue
            pct = float(raw) * 100
            if pct < min_short_pct:
                continue
            short_ratio = info.get("shortRatio")   # jours pour couvrir
            name = info.get("shortName") or info.get("longName") or tkr
            note = f"Short interest : {pct:.1f}% du flottant"
            if short_ratio:
                note += f" · {short_ratio:.1f} jours pour couvrir"
            signal_type = "squeeze_risk" if pct >= 25 else "short_elevated"
            results.append({
                "source": "Short Interest",
                "entity": "Vendeurs à découvert",
                "type": signal_type,
                "ticker": tkr,
                "name": name,
                "date": today,
                "note": note,
                "short_pct": round(pct, 1),
            })
        except Exception:
            continue
    results.sort(key=lambda x: x.get("short_pct", 0), reverse=True)
    return results
