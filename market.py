"""
market.py — Couche "contexte de marché" de l'agent Smart Money Radar.

Objectif : replacer les mouvements 13F (trimestriels, décalés de 45 j) dans le
contexte du marché vivant, afin d'éclairer la lecture des signaux.

Sources :
  - Prix en direct        : Yahoo Finance (via yfinance) — indices, pétrole, or, taux, BTC
  - Macro / calendrier     : TradingEconomics (clé gratuite "guest:guest" limitée, ou TE_API_KEY)
  - Actualité              : flux RSS Investing.com + FinancialJuice, repli Google News
  - Drift depuis le 13F    : Yahoo Finance (prix à la date de portefeuille vs prix actuel)

Toutes les fonctions sont défensives : en cas d'échec réseau, elles renvoient une
valeur de repli plutôt que de planter l'agent.
"""

from __future__ import annotations
import os, datetime as dt, html, re
from dateutil import parser as dtparse

# Imports optionnels (l'agent fonctionne en mode dégradé s'ils manquent)
try:
    import requests
except Exception:
    requests = None
try:
    import feedparser
except Exception:
    feedparser = None
try:
    import yfinance as yf
except Exception:
    yf = None

UA = {"User-Agent": "Mozilla/5.0 (SmartMoneyRadar; contact: you@example.com)"}


# ----------------------------------------------------------------------
#  PRIX & BANDEAU DE MARCHÉ
# ----------------------------------------------------------------------
def _fmt_price(label, v, chg_pct):
    """Formate une tuile selon le type d'instrument."""
    if v is None:
        return {"label": label, "value": "—", "change": "", "dir": "flat", "sub": ""}
    if label in ("Brent", "WTI", "Or"):
        val = f"{v:,.1f} $".replace(",", " ")
    elif label == "10 ans US":
        val = f"{v:.2f} %"
    elif label == "Bitcoin":
        val = f"{v:,.0f} $".replace(",", " ")
    elif label == "VIX":
        val = f"{v:,.1f}".replace(",", " ")
    else:
        val = f"{v:,.0f}".replace(",", " ")
    direction = "up" if (chg_pct or 0) > 0.05 else "down" if (chg_pct or 0) < -0.05 else "flat"
    chg = "" if chg_pct is None else (f"{chg_pct:+.2f} %".replace(".", ","))
    return {"label": label, "value": val, "change": chg, "dir": direction, "sub": ""}


def build_snapshot(snapshot_tickers: dict) -> list[dict]:
    """Renvoie la liste des tuiles (indices, matières premières, taux, BTC)."""
    tiles = []
    if yf is None:
        return [_fmt_price(lbl, None, None) for lbl in snapshot_tickers]
    for label, ticker in snapshot_tickers.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if hist.empty:
                tiles.append(_fmt_price(label, None, None)); continue
            last = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else last
            chg = (last - prev) / prev * 100 if prev else 0.0
            # ^TNX est exprimé en dixièmes de point (ex. 43.3 = 4,33 %)
            if ticker == "^TNX":
                last = last / 10.0
            tiles.append(_fmt_price(label, last, chg))
        except Exception:
            tiles.append(_fmt_price(label, None, None))
    return tiles


def regime_from_tiles(tiles: list[dict]) -> tuple[str, str]:
    """Déduit un libellé + une phrase de régime à partir du bandeau (repli si LLM off)."""
    by = {t["label"]: t for t in tiles}
    def num(lbl):
        try:
            return float(re.sub(r"[^\d,.-]", "", by.get(lbl, {}).get("value", "")).replace(" ", "").replace(",", "."))
        except Exception:
            return None
    vix = num("VIX")
    risk = "calme" if (vix or 99) < 16 else "nerveux" if (vix or 0) > 22 else "neutre"
    label = "Risque-on" if (vix or 99) < 18 else "Risque-off" if (vix or 0) > 24 else "Mitigé"
    parts = [f"Régime {risk} (VIX {by.get('VIX',{}).get('value','—')})."]
    if by.get("S&P 500", {}).get("value", "—") != "—":
        parts.append(f"S&P 500 à {by['S&P 500']['value']}.")
    if by.get("Brent", {}).get("value", "—") != "—":
        parts.append(f"Brent {by['Brent']['value']}.")
    if by.get("10 ans US", {}).get("value", "—") != "—":
        parts.append(f"10 ans US à {by['10 ans US']['value']}.")
    return label, " ".join(parts)


# ----------------------------------------------------------------------
#  DRIFT DEPUIS LE 13F  (le prix a-t-il bougé depuis la date du portefeuille ?)
# ----------------------------------------------------------------------
def price_drift(tickers: list[str], portfolio_date: str) -> dict:
    """
    Pour chaque ticker, renvoie le % de variation entre le cours à la date du
    portefeuille (fin de trimestre) et le cours actuel. Crucial : un 13F a 45 j
    de retard, donc un achat 'signalé' peut déjà avoir beaucoup monté.
    """
    out = {}
    if yf is None or not tickers:
        return out
    try:
        start = dtparse.parse(portfolio_date).date()
        end = dt.date.today()
        data = yf.download(tickers, start=start.isoformat(), end=(end + dt.timedelta(days=1)).isoformat(),
                           progress=False, group_by="ticker", threads=True)
        for tk in tickers:
            try:
                col = data[tk]["Close"].dropna() if len(tickers) > 1 else data["Close"].dropna()
                if len(col) >= 2:
                    drift = (float(col.iloc[-1]) - float(col.iloc[0])) / float(col.iloc[0]) * 100
                    out[tk] = round(drift, 1)
            except Exception:
                continue
    except Exception:
        pass
    return out


# ----------------------------------------------------------------------
#  MACRO / CALENDRIER  (TradingEconomics)
# ----------------------------------------------------------------------
def get_macro(cfg: dict) -> tuple[list[dict], list[dict]]:
    """
    Renvoie (macro_indicators, events) depuis TradingEconomics.
    Clé via secret TE_API_KEY, sinon "guest:guest" (données d'exemple limitées).
    En cas d'échec, renvoie des listes vides (le bandeau reste affiché sans macro).
    """
    macro, events = [], []
    if requests is None:
        return macro, events
    key = os.environ.get("TE_API_KEY", "guest:guest")
    te = cfg.get("market", {}).get("tradingeconomics", {})
    days = te.get("calendar_days_ahead", 7)
    min_imp = te.get("min_importance", 2)
    try:
        d1 = dt.date.today().isoformat()
        d2 = (dt.date.today() + dt.timedelta(days=days)).isoformat()
        url = f"https://api.tradingeconomics.com/calendar/country/united%20states/{d1}/{d2}?c={key}&f=json"
        r = requests.get(url, headers=UA, timeout=20)
        if r.ok:
            for ev in r.json():
                if int(ev.get("Importance", 0)) >= min_imp:
                    date = (ev.get("Date") or "")[:10]
                    events.append({
                        "date": _fr_date(date),
                        "label": ev.get("Event", "").strip(),
                        "weight": "haute" if int(ev.get("Importance", 0)) >= 3 else "moyenne",
                    })
        events = events[:8]
    except Exception:
        pass
    return macro, events


# ----------------------------------------------------------------------
#  ACTUALITÉ  (RSS Investing.com / FinancialJuice + repli Google News)
# ----------------------------------------------------------------------
def get_news(cfg: dict, limit: int = 6) -> list[dict]:
    """
    Agrège des titres depuis les flux RSS configurés. Investing.com et
    FinancialJuice bloquent parfois les robots : on tente d'abord leurs flux,
    puis on bascule sur un repli (Google News) si rien ne remonte.
    Les titres sont raccourcis et nettoyés (pas de reproduction longue).
    """
    if feedparser is None:
        return []
    feeds = cfg.get("market", {}).get("rss_feeds", [])
    fallback = cfg.get("market", {}).get("rss_fallback", [])
    items = _parse_feeds(feeds, limit)
    if len(items) < 3:
        items += _parse_feeds(fallback, limit)
    # dédoublonnage par titre
    seen, out = set(), []
    for it in items:
        k = it["title"][:60].lower()
        if k in seen:
            continue
        seen.add(k); out.append(it)
    return out[:limit]


def _parse_feeds(urls: list[str], limit: int) -> list[dict]:
    out = []
    for url in urls:
        try:
            # feedparser gère le téléchargement ; on force un UA via request_headers
            feed = feedparser.parse(url, request_headers=UA)
            src = (feed.feed.get("title") or _domain(url)).split(" - ")[0][:24]
            for e in feed.entries[:limit]:
                title = html.unescape(re.sub(r"<[^>]+>", "", e.get("title", "")).strip())
                if not title:
                    continue
                when = ""
                if e.get("published_parsed"):
                    when = dt.datetime(*e.published_parsed[:6]).strftime("%d/%m")
                out.append({"src": src, "time": when, "title": title[:150]})
        except Exception:
            continue
    return out


# ----------------------------------------------------------------------
#  COMMENTAIRE LLM OPTIONNEL  (API Anthropic)
# ----------------------------------------------------------------------
def llm_market_read(tiles, events, news, alerts) -> str | None:
    """
    Rédige une 'lecture de marché' de 3-4 phrases reliant la macro aux signaux 13F.
    Optionnel : nécessite ANTHROPIC_API_KEY et market.llm_commentary = true.
    Le prompt insiste : signaux à analyser, PAS de conseil personnalisé.
    """
    if requests is None or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    snap = "; ".join(f"{t['label']} {t['value']} ({t['change']})" for t in tiles if t["value"] != "—")
    ev = "; ".join(f"{e['date']} {e['label']}" for e in events[:5])
    nw = " | ".join(n["title"] for n in news[:5])
    mv = "; ".join(f"{a['fund']}: {a['type']} {a['ticker']}" for a in alerts[:10])
    prompt = (
        "Tu es analyste marché. En 3-4 phrases en français, relie le contexte macro "
        "aux mouvements de fonds ci-dessous. Reste factuel et prudent : ce sont des "
        "SIGNAUX à analyser, jamais un conseil d'investissement personnalisé. "
        f"\n\nMarché: {snap}\nÉvénements: {ev}\nActu: {nw}\nMouvements 13F: {mv}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 400,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40,
        )
        if r.ok:
            return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------
#  utilitaires
# ----------------------------------------------------------------------
_FR_MONTHS = ["", "janv.", "févr.", "mars", "avr.", "mai", "juin",
              "juil.", "août", "sept.", "oct.", "nov.", "déc."]

def _fr_date(iso: str) -> str:
    try:
        d = dtparse.parse(iso).date()
        return f"{d.day} {_FR_MONTHS[d.month]}"
    except Exception:
        return iso

def _domain(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url)
    return (m.group(1) if m else url).replace("www.", "")
