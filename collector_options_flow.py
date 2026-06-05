"""
collector_options_flow.py — Ratio put/call et volumes d'options inhabituels.

Source : Yahoo Finance (yfinance), gratuit, sans clé.
Signal :
  - Ratio P/C très bas (< 0.4) → accumulation massive de calls → sentiment haussier
  - Ratio P/C très haut (> 2.0) → accumulation massive de puts → hedging/baissier
  - Volume call/put > 3× l'open interest moyen → activité inhabituelle

Note : avec les données gratuites Yahoo, on ne voit que les options listées
de la prochaine expiration, pas le flux intraday en temps réel. Signal
directionnel utile mais pas aussi précis que Unusual Whales (payant).
"""

from __future__ import annotations
import datetime as dt
try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore


_PC_BULL_THRESHOLD = 0.40   # < 0.40 → très haussier
_PC_BEAR_THRESHOLD = 2.00   # > 2.00 → très baissier
_MIN_TOTAL_VOLUME  = 500    # ignorer les titres illiquides en options


def fetch_options_flow(tickers: list[str], max_tickers: int = 25) -> list[dict]:
    """
    Calcule le ratio put/call pour les tickers donnés et retourne les signaux
    extrêmes (très haussiers ou très baissiers).
    """
    if yf is None or not tickers:
        return []
    today = dt.date.today().isoformat()
    results = []
    for tkr in tickers[:max_tickers]:
        try:
            stock = yf.Ticker(tkr)
            expirations = stock.options
            if not expirations:
                continue
            # Prendre la prochaine expiration (la plus liquide)
            chain = stock.option_chain(expirations[0])
            calls = chain.calls
            puts  = chain.puts
            call_vol = float(calls["volume"].sum()) if "volume" in calls.columns else 0
            put_vol  = float(puts["volume"].sum())  if "volume" in puts.columns  else 0
            total_vol = call_vol + put_vol
            if total_vol < _MIN_TOTAL_VOLUME:
                continue
            pc_ratio = put_vol / call_vol if call_vol > 0 else 99.0
            if _PC_BULL_THRESHOLD < pc_ratio < _PC_BEAR_THRESHOLD:
                continue   # ratio normal, pas de signal
            info = stock.info
            name = info.get("shortName") or info.get("longName") or tkr
            if pc_ratio <= _PC_BULL_THRESHOLD:
                signal_type = "options_bullish"
                note = (f"Ratio P/C = {pc_ratio:.2f} 🟢 accumulation de calls "
                        f"({int(call_vol):,} calls vs {int(put_vol):,} puts, "
                        f"exp. {expirations[0]})")
            else:
                signal_type = "options_bearish"
                note = (f"Ratio P/C = {pc_ratio:.2f} 🔴 accumulation de puts "
                        f"({int(put_vol):,} puts vs {int(call_vol):,} calls, "
                        f"exp. {expirations[0]})")
            results.append({
                "source": "Options Flow",
                "entity": "Marché des options",
                "type": signal_type,
                "ticker": tkr,
                "name": name,
                "date": today,
                "note": note,
                "pc_ratio": round(pc_ratio, 2),
            })
        except Exception:
            continue
    return results
