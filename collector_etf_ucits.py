"""
collector_etf_ucits.py — Positions quotidiennes des grands ETF UCITS.

Sources :
  - iShares API CSV (CSPX, SWDA, IWDA, EIMI)
  - Amundi ETF (CSV public)
  - yfinance en repli pour les ETF cotés aux US (IVV, VTI, QQQ)

Logique : compare les poids de la veille (cache JSON) et remonte
les variations > seuil comme signaux. Utile pour voir les arbitrages
quotidiens des plus gros ETF mondiaux.
"""

from __future__ import annotations
import csv, io, json, os, re, time, datetime as dt
try:
    import requests as _req
except ImportError:
    _req = None
try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore

_ROOT   = os.path.dirname(os.path.abspath(__file__))
_CACHE  = os.path.join(_ROOT, ".cache", "etf")
os.makedirs(_CACHE, exist_ok=True)

_UA = "Mozilla/5.0 (compatible; SmartMoneyRadar/1.0)"
_HEADERS = {
    "User-Agent": _UA,
    "Accept-Language": "en-US,en;q=0.9",
}

# --- Catalogue des ETF suivis ---
# Format iShares : l'API CSV intègre plusieurs lignes de métadonnées en début de fichier
# avant le vrai tableau — il faut les ignorer.
ETF_CATALOGUE = [
    {
        "slug": "CSPX",
        "name": "iShares Core S&P 500 UCITS ETF (Acc)",
        "url": "https://www.ishares.com/uk/individual/en/products/253743/CSPX/1506572306608.ajax"
               "?fileType=csv&fileName=CSPX_holdings&dataType=fund",
        "referer": "https://www.ishares.com/uk/individual/en/products/253743/",
        "type": "ishares_csv",
        "weight_col": "Weight (%)",
        "ticker_col": "Ticker",
        "name_col": "Name",
    },
    {
        "slug": "IWDA",
        "name": "iShares Core MSCI World UCITS ETF (Acc)",
        "url": "https://www.ishares.com/uk/individual/en/products/251882/SWDA/1506572306608.ajax"
               "?fileType=csv&fileName=SWDA_holdings&dataType=fund",
        "referer": "https://www.ishares.com/uk/individual/en/products/251882/",
        "type": "ishares_csv",
        "weight_col": "Weight (%)",
        "ticker_col": "Ticker",
        "name_col": "Name",
    },
    # Repli yfinance pour les ETF liquides US
    {
        "slug": "IVV",
        "name": "iShares Core S&P 500 ETF (US)",
        "url": "",
        "type": "yfinance",
        "yf_ticker": "IVV",
    },
    {
        "slug": "QQQ",
        "name": "Invesco QQQ Trust (Nasdaq 100)",
        "url": "",
        "type": "yfinance",
        "yf_ticker": "QQQ",
    },
]


def _cache_path(slug: str) -> str:
    return os.path.join(_CACHE, f"etf_{slug}.json")


def _load_prev(slug: str) -> dict:
    try:
        return json.load(open(_cache_path(slug), encoding="utf-8"))
    except Exception:
        return {}


def _save(slug: str, holdings: dict) -> None:
    json.dump(holdings, open(_cache_path(slug), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def _parse_ishares_csv(text: str, weight_col: str, ticker_col: str, name_col: str) -> dict:
    """Parse le CSV iShares (ignore les lignes d'en-tête non-CSV)."""
    # Trouver la vraie ligne d'en-tête (contient "Name" ou "Ticker")
    lines = text.splitlines()
    header_idx = -1
    for i, line in enumerate(lines):
        if ticker_col.lower() in line.lower() and name_col.lower() in line.lower():
            header_idx = i
            break
    if header_idx == -1:
        return {}
    csv_text = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(csv_text))
    holdings = {}
    for row in reader:
        tkr  = (row.get(ticker_col) or "").strip()
        nme  = (row.get(name_col) or "").strip()
        wstr = (row.get(weight_col) or "0").strip().replace(",", ".")
        if not tkr or tkr == "-":
            continue
        try:
            w = float(wstr)
        except ValueError:
            continue
        holdings[tkr] = {"ticker": tkr, "name": nme, "weight": w}
    return holdings


def _fetch_ishares(etf: dict) -> dict:
    if _req is None:
        return {}
    hdrs = dict(_HEADERS)
    hdrs["Referer"] = etf.get("referer", "https://www.ishares.com/")
    r = _req.get(etf["url"], headers=hdrs, timeout=60)
    r.raise_for_status()
    return _parse_ishares_csv(
        r.text, etf["weight_col"], etf["ticker_col"], etf["name_col"]
    )


def _fetch_yfinance(etf: dict) -> dict:
    if yf is None:
        return {}
    stock = yf.Ticker(etf["yf_ticker"])
    # top_holdings disponible pour certains ETF
    try:
        holders = stock.institutional_holders
        if holders is None or holders.empty:
            return {}
        result = {}
        total_pct = float(holders["pctHeld"].sum()) * 100 or 1
        for _, row in holders.iterrows():
            tkr  = str(row.get("Holder", "")).strip()
            pct  = float(row.get("pctHeld", 0)) * 100
            result[tkr] = {"ticker": tkr, "name": tkr, "weight": round(pct, 4)}
        return result
    except Exception:
        return {}


def fetch_etf_signals(min_delta_pct: float = 0.20) -> list[dict]:
    """Compare les poids du jour avec la veille et retourne les variations notables."""
    results = []
    today = dt.date.today().isoformat()
    for etf in ETF_CATALOGUE:
        slug = etf["slug"]
        try:
            if etf["type"] == "ishares_csv":
                current = _fetch_ishares(etf)
            elif etf["type"] == "yfinance":
                current = _fetch_yfinance(etf)
            else:
                current = {}
            if not current:
                print(f"   ⚠️  ETF {slug} : aucune donnée.")
                continue
            prev = _load_prev(slug)
            _save(slug, current)
            time.sleep(0.5)
            for tkr, cur in current.items():
                pw = prev.get(tkr, {}).get("weight", 0)
                cw = cur["weight"]
                delta = cw - pw
                if abs(delta) < min_delta_pct:
                    continue
                results.append({
                    "source": "ETF UCITS",
                    "entity": etf["name"],
                    "type": "buy" if delta > 0 else "sell",
                    "ticker": tkr,
                    "name": cur.get("name", tkr),
                    "date": today,
                    "note": (f"ETF {slug} : {tkr} {delta:+.2f}% "
                             f"(poids {pw:.2f}%→{cw:.2f}%)"),
                })
            print(f"   ETF {slug} : {len(current)} lignes, {sum(1 for r in results if etf['name'] in r['entity'])} variation(s) notables.")
        except Exception as e:
            print(f"   ⚠️  ETF {slug} : {e}")
    return results
