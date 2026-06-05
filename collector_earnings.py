"""
collector_earnings.py — Dates de publication des résultats + surprises récentes.

Source : Yahoo Finance (yfinance), gratuit, sans clé.
Signal :
  - Un titre en alerte 13F qui publie ses résultats dans les 7 jours → contexte
    critique pour valider ou invalider la thèse du fonds.
  - Une forte surprise positive/négative récente → catalyseur de continuation
    ou de retournement selon le consensus.
"""

from __future__ import annotations
import datetime as dt
try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore


def _parse_earnings_dates(ticker_obj) -> list[dt.date]:
    """Extrait les dates de résultats depuis le calendrier yfinance (multi-format)."""
    dates = []
    try:
        cal = ticker_obj.calendar
        if cal is None:
            return []
        # Format nouveau (dict) : {"Earnings Date": [timestamp, ...], ...}
        if isinstance(cal, dict):
            raw = cal.get("Earnings Date", [])
            for d in raw:
                try:
                    if isinstance(d, (int, float)):
                        dates.append(dt.datetime.fromtimestamp(d).date())
                    elif hasattr(d, "date"):
                        dates.append(d.date())
                    elif isinstance(d, str):
                        dates.append(dt.date.fromisoformat(d[:10]))
                except Exception:
                    pass
        # Format ancien (DataFrame) : index = ["Earnings Date", ...]
        else:
            try:
                row = cal.loc["Earnings Date"]
                for d in row:
                    if hasattr(d, "date"):
                        dates.append(d.date())
            except Exception:
                pass
    except Exception:
        pass
    return dates


def fetch_upcoming_earnings(tickers: list[str],
                             within_days: int = 7,
                             max_tickers: int = 30) -> list[dict]:
    """
    Retourne les tickers qui publient leurs résultats dans `within_days` jours.
    """
    if yf is None or not tickers:
        return []
    today = dt.date.today()
    results = []
    for tkr in tickers[:max_tickers]:
        try:
            stock = yf.Ticker(tkr)
            earnings_dates = _parse_earnings_dates(stock)
            for edate in earnings_dates:
                delta = (edate - today).days
                if 0 <= delta <= within_days:
                    info = stock.info
                    name = info.get("shortName") or info.get("longName") or tkr
                    # Récupérer la surprise du dernier trimestre si dispo
                    surprise_note = ""
                    try:
                        earnings_hist = stock.earnings_history
                        if earnings_hist is not None and not earnings_hist.empty:
                            last = earnings_hist.iloc[-1]
                            surprise_pct = last.get("surprisePercent", 0) * 100
                            if abs(surprise_pct) >= 5:
                                sense = "surprise positive" if surprise_pct > 0 else "déception"
                                surprise_note = f" · Dernier T : {sense} {abs(surprise_pct):.0f}%"
                    except Exception:
                        pass
                    urgency = "critique" if delta <= 2 else "prochaine"
                    results.append({
                        "source": "Earnings Calendar",
                        "entity": "Publication résultats",
                        "type": "event",
                        "ticker": tkr,
                        "name": name,
                        "date": edate.isoformat(),
                        "note": f"Résultats {urgency} dans {delta} jour(s) ({edate.strftime('%d/%m')}){surprise_note}",
                        "days_ahead": delta,
                    })
                    break  # ne prendre que la prochaine date
        except Exception:
            continue
    results.sort(key=lambda x: x.get("days_ahead", 99))
    return results
