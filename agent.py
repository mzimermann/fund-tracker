#!/usr/bin/env python3
"""
agent.py — Smart Money Radar
Agent de suivi des 13F des plus grands fonds + contexte de marché.

Pipeline quotidien :
  1. Pour chaque fonds, vérifier sur SEC EDGAR s'il existe un NOUVEAU 13F
     depuis le dernier run (les 13F ne changent qu'une fois par trimestre).
  2. Si nouveau dépôt : télécharger la table d'information (XML), mapper
     les CUSIP en tickers (OpenFIGI), comparer au trimestre précédent et
     générer les alertes selon les seuils du cahier des charges.
  3. Rafraîchir le contexte de marché (prix, macro, actualité) — tous les jours.
  4. Écrire data.json (lu par le tableau de bord) et envoyer le rapport e-mail.

Usage :
  python agent.py            # run normal
  python agent.py --selftest # teste le parseur XML + moteur de diff (hors-ligne)
  python agent.py --force    # force le re-téléchargement même sans nouveau 13F
"""

from __future__ import annotations
import os, sys, json, time, html, smtplib, datetime as dt
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import yaml
try:
    import requests
except Exception:
    requests = None

# S'assurer que le dossier du script est dans le chemin Python
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import market  # module local

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, ".cache")
os.makedirs(CACHE, exist_ok=True)

SEC_UA = {"User-Agent": os.environ.get("SEC_USER_AGENT", "SmartMoneyRadar you@example.com")}
TYPE_LABEL = {"new": "Nouvelle position", "exit": "Sortie totale",
              "increase": "Augmentation", "reduction": "Réduction"}


# ======================================================================
#  SEC EDGAR
# ======================================================================
def sec_get(url, as_json=False, retries=3):
    """GET poli vers EDGAR (User-Agent obligatoire, throttle ~0.2s)."""
    if requests is None:
        raise RuntimeError("requests indisponible")
    for i in range(retries):
        try:
            r = requests.get(url, headers=SEC_UA, timeout=30)
            time.sleep(0.2)  # SEC : rester sous 10 req/s
            if r.status_code == 200:
                return r.json() if as_json else r.text
        except Exception:
            time.sleep(1 + i)
    return None


def latest_13f_filings(cik: str, n: int = 2):
    """Renvoie les n derniers dépôts 13F-HR : [{accession, report_date, filing_date}]."""
    cik10 = cik.zfill(10)
    data = sec_get(f"https://data.sec.gov/submissions/CIK{cik10}.json", as_json=True)
    if not data:
        return []
    rec = data.get("filings", {}).get("recent", {})
    forms = rec.get("form", [])
    out = []
    for i, form in enumerate(forms):
        if form in ("13F-HR", "13F-HR/A"):
            out.append({
                "accession": rec["accessionNumber"][i],
                "report_date": rec["reportDate"][i],
                "filing_date": rec["filingDate"][i],
            })
    # uniques par report_date (garder le plus récent dépôt par trimestre), triés desc
    seen, dedup = set(), []
    for f in sorted(out, key=lambda x: x["filing_date"], reverse=True):
        if f["report_date"] in seen:
            continue
        seen.add(f["report_date"]); dedup.append(f)
    return dedup[:n]


def fetch_info_table(cik: str, accession: str):
    """Télécharge et parse la table d'information d'un 13F. Renvoie une liste de holdings."""
    cik_int = str(int(cik))
    accn = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn}"
    # 1) lister les fichiers du dépôt
    idx = sec_get(f"{base}/index.json", as_json=True)
    candidates = []
    if idx:
        for item in idx.get("directory", {}).get("item", []):
            name = item.get("name", "")
            if name.lower().endswith(".xml") and name.lower() != "primary_doc.xml":
                candidates.append(name)
    # repli : noms usuels
    if not candidates:
        candidates = ["informationtable.xml", "infotable.xml", "form13fInfoTable.xml"]
    # 2) trouver le XML contenant la table
    for name in candidates:
        xml = sec_get(f"{base}/{name}")
        if xml and ("infoTable" in xml or "informationTable" in xml):
            try:
                return parse_info_table(xml)
            except ET.ParseError as e:
                print(f"   ⚠️  XML mal formé ({name}) : {e} — tentative suivante.")
                continue
    return []


def parse_info_table(xml_text: str):
    """Parse le XML 13F (namespace-agnostique) -> liste de positions agrégées par CUSIP."""
    # retirer les namespaces pour simplifier les requêtes
    xml_text = _strip_ns(xml_text)
    root = ET.fromstring(xml_text)
    holdings = {}
    for it in root.iter("infoTable"):
        def txt(tag, parent=it):
            e = parent.find(tag)
            return e.text.strip() if e is not None and e.text else ""
        cusip = txt("cusip").upper()
        if not cusip:
            continue
        issuer = txt("nameOfIssuer")
        try:
            value = float(txt("value") or 0)
        except ValueError:
            value = 0.0
        shrs_el = it.find("shrsOrPrnAmt")
        shares = 0.0
        put_call = ""
        if shrs_el is not None:
            try:
                shares = float((shrs_el.find("sshPrnamt").text or 0))
            except Exception:
                shares = 0.0
        pc = it.find("putCall")
        if pc is not None and pc.text:
            put_call = pc.text.strip()
        key = (cusip, put_call)  # séparer puts/calls de l'action sous-jacente
        if key not in holdings:
            holdings[key] = {"cusip": cusip, "issuer": issuer, "value": 0.0,
                             "shares": 0.0, "put_call": put_call}
        holdings[key]["value"] += value
        holdings[key]["shares"] += shares
    return list(holdings.values())


def _strip_ns(xml_text: str) -> str:
    import re
    # Retirer les déclarations de namespace (xmlns et xmlns:prefix)
    xml_text = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', xml_text)
    # Retirer les préfixes des noms d'éléments (<ns1:tag> → <tag>)
    xml_text = re.sub(r'<(/?)[\w.\-]+:', r'<\1', xml_text)
    # Retirer les préfixes des noms d'attributs (xsi:type="..." → type="...")
    xml_text = re.sub(r'(?<![:/"\w])([\w]+):([\w]+)(\s*=)', r'\2\3', xml_text)
    return xml_text


# ======================================================================
#  CUSIP -> TICKER  (OpenFIGI, avec cache)
# ======================================================================
def cusip_to_ticker(cusips: list[str]) -> dict:
    """Mappe une liste de CUSIP en tickers via OpenFIGI. Cache local pour éviter les rappels."""
    cache_path = os.path.join(CACHE, "cusip_map.json")
    cache = _load_json(cache_path, {})
    todo = [c for c in set(cusips) if c and c not in cache]
    if todo and requests is not None:
        key = os.environ.get("OPENFIGI_API_KEY")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-OPENFIGI-APIKEY"] = key
        batch = 100 if key else 10
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            body = [{"idType": "ID_CUSIP", "idValue": c} for c in chunk]
            try:
                r = requests.post("https://api.openfigi.com/v3/mapping",
                                  headers=headers, json=body, timeout=30)
                if r.status_code == 200:
                    for c, res in zip(chunk, r.json()):
                        data = (res or {}).get("data") or []
                        cache[c] = data[0].get("ticker", "") if data else ""
                time.sleep(0.3 if key else 2.5)  # respecter les quotas
            except Exception:
                continue
        _save_json(cache_path, cache)
    return {c: cache.get(c, "") for c in cusips}


# ======================================================================
#  PORTEFEUILLE & MOTEUR DE DIFF
# ======================================================================
def build_portfolio(holdings: list[dict], ticker_map: dict) -> dict:
    """Indexe par CUSIP avec poids %, ticker, nom. (Le scaling de 'value' s'annule dans le %.)"""
    total = sum(h["value"] for h in holdings) or 1.0
    port = {}
    for h in holdings:
        c = h["cusip"]
        if c not in port:
            port[c] = {"cusip": c, "ticker": ticker_map.get(c, "") or c[:6],
                       "name": _title(h["issuer"]), "value": 0.0, "shares": 0.0}
        port[c]["value"] += h["value"]
        port[c]["shares"] += h["shares"]
    for c, p in port.items():
        p["weight"] = p["value"] / total * 100.0
    return port


def diff(cur: dict, prev: dict, th: dict, tier: str):
    """Compare deux portefeuilles et renvoie les alertes selon les seuils + top-5."""
    alerts = []
    top_n = th["top_n"]
    cur_top = _top_set(cur, top_n)
    prev_top = _top_set(prev, top_n)

    # fonds indiciels : on ne remonte QUE les changements de top 5 (sinon bruit)
    only_top5 = (tier == "faible-signal")

    for c, p in cur.items():
        w = p["weight"]
        if c not in prev:
            if w >= th["new_position_min_weight_pct"] and not only_top5:
                alerts.append(_mk(p, "new", 0, w, "+∞", tier))
            continue
        pv = prev[c]
        dw, dpw = w, pv["weight"]
        if only_top5:
            continue
        if pv["shares"] > 0:
            chg = (p["shares"] - pv["shares"]) / pv["shares"] * 100
            if chg > th["share_change_pct"] and w >= th["big_move_min_weight_pct"]:
                alerts.append(_mk(p, "increase", dpw, dw, f"+{chg:.0f}%", tier))
            elif chg < -th["share_change_pct"] and dpw >= th["big_move_min_weight_pct"]:
                alerts.append(_mk(p, "reduction", dpw, dw, f"{chg:.0f}%", tier))

    for c, pv in prev.items():
        if c not in cur and pv["weight"] >= th["exit_min_prev_weight_pct"] and not only_top5:
            alerts.append(_mk(pv, "exit", pv["weight"], 0, "-100%", tier))

    # changements du top 5 (tous tiers confondus)
    for c in cur_top - prev_top:
        p = cur[c]
        if not any(a["_cusip"] == c for a in alerts):
            alerts.append(_mk(p, "increase", prev.get(c, {}).get("weight", 0), p["weight"],
                              "entrée top 5", tier, top5=True))
    return alerts


def _cusip_to_isin(cusip: str) -> str:
    """Calcule l'ISIN depuis un CUSIP américain (US + 9 chiffres + check digit Luhn)."""
    if not cusip or len(cusip) != 9:
        return ""
    raw = "US" + cusip.upper()
    digs = ""
    for c in raw:
        digs += c if c.isdigit() else str(ord(c) - ord("A") + 10)
    total = 0
    for i, d in enumerate(reversed(digs)):
        n = int(d)
        if i % 2 == 0:   # le chiffre le plus à droite est doublé (convention ISIN ISO 6166)
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return raw + str((10 - (total % 10)) % 10)


def _mk(p, typ, pw, nw, shares_change, tier, top5=False):
    sev = _severity(typ, pw, nw)
    isin = _cusip_to_isin(p.get("cusip", ""))
    return {
        "_cusip": p["cusip"], "_dir": "buy" if typ in ("new", "increase") else "sell",
        "ticker": p["ticker"], "name": p["name"], "type": typ,
        "cusip": p["cusip"],   # conservé pour le calcul ISIN côté JS
        "isin": isin,          # pré-calculé côté serveur aussi
        "prev_weight": f"{pw:.1f}%".replace(".", ","),
        "new_weight": f"{nw:.1f}%".replace(".", ",") if nw else "0 %",
        "shares_change": shares_change, "weight": f"{nw:.1f}%".replace(".", ","),
        "severity": sev, "_top5": top5,
    }


def _severity(typ, pw, nw):
    ref = nw if typ in ("new", "increase") else pw
    return "high" if ref >= 3 else "medium" if ref >= 1.0 else "low"


def _top_set(port, n):
    return {c for c, _ in sorted(port.items(), key=lambda kv: kv[1]["weight"], reverse=True)[:n]}


# ======================================================================
#  CONSENSUS + HABILLAGE DES ALERTES
# ======================================================================
def annotate(all_alerts: list[dict], min_funds: int, drift: dict):
    """Détecte les consensus (même ticker+direction chez ≥ N fonds) et rédige contexte/pistes."""
    from collections import defaultdict
    groups = defaultdict(set)
    for a in all_alerts:
        groups[(a["ticker"], a["_dir"])].add(a["fund"])

    for a in all_alerts:
        peers = groups[(a["ticker"], a["_dir"])] - {a["fund"]}
        a["consensus"] = (f"Aussi chez : {', '.join(sorted(peers))}."
                          if len(groups[(a["ticker"], a["_dir"])]) >= min_funds else "—")
        # contexte générique
        verb = {"new": "ouvre une position sur", "exit": "sort totalement de",
                "increase": "renforce", "reduction": "allège"}[a["type"]]
        a["context"] = f"{a['fund']} {verb} {a['name']} ({a['ticker']})."
        # lecture marché : drift depuis le 13F si dispo
        d = drift.get(a["ticker"])
        if d is not None:
            sense = "monté" if d >= 0 else "reculé"
            a["cross_read"] = (f"Depuis la date du portefeuille, {a['ticker']} a {sense} de "
                               f"{abs(d):.1f} %. À intégrer avant toute décision (effet du décalage 45 j).")
        else:
            a["cross_read"] = "À recouper avec le contexte de marché ci-dessus."
        # piste prudente, non-personnalisée
        if a["consensus"] != "—":
            a["suggestion"] = "Signal convergent entre plusieurs fonds — à analyser au regard de votre stratégie. Pas un conseil."
        elif a["_dir"] == "buy":
            a["suggestion"] = "Signal d'achat isolé — surveiller et comprendre la thèse avant d'envisager quoi que ce soit."
        else:
            a["suggestion"] = "Signal de vente isolé — à pondérer, ne pas copier mécaniquement."
        for k in ("_cusip", "_dir", "_top5"):
            a.pop(k, None)
    return all_alerts


def consensus_lists(all_alerts: list[dict], min_funds: int):
    """Construit les classements top achats / top ventes collectifs pour le tableau de bord."""
    from collections import defaultdict
    buys, sells, names = defaultdict(set), defaultdict(set), {}
    for a in all_alerts:
        names[a["ticker"]] = a["name"]
        (buys if a["type"] in ("new", "increase") else sells)[a["ticker"]].add(a["fund"])

    def fmt(d):
        rows = sorted(d.items(), key=lambda kv: len(kv[1]), reverse=True)
        return [{"ticker": tk, "name": names.get(tk, tk),
                 "funds": f"{len(fn)} fonds", "note": ", ".join(sorted(fn))}
                for tk, fn in rows[:7] if len(fn) >= 1]
    return fmt(buys), fmt(sells)


# ======================================================================
#  ÉTAT (détecter les nouveaux dépôts)
# ======================================================================
def load_state():
    return _load_json(os.path.join(CACHE, "state.json"), {})

def save_state(s):
    _save_json(os.path.join(CACHE, "state.json"), s)


# ======================================================================
#  E-MAIL
# ======================================================================
def render_email_html(payload: dict) -> str:
    """Génère un e-mail clair, lisible, sans jargon technique."""
    m    = payload["meta"]
    mkt  = payload["market"]
    alerts = payload["alerts"]
    ai   = payload.get("ai_analysis", {})
    url  = payload.get("_dashboard_url", "#")
    today = dt.date.today().strftime("%d %B %Y").lstrip("0")

    # --- Résumé marché ---
    snap  = mkt.get("snapshot", [])
    def tile(lbl):
        t = next((x for x in snap if x.get("label") == lbl), {})
        return f"{t.get('value','—')} {t.get('change','')}"

    # --- Signaux en langage humain ---
    TYPE_FR = {
        "new":       "a acheté pour la première fois",
        "increase":  "a renforcé sa position sur",
        "exit":      "a vendu TOUTES ses actions",
        "reduction": "a réduit sa position sur",
    }
    BUY_COLOR  = "#00d68f"
    SELL_COLOR = "#ff4757"

    def sig_block(a):
        action  = a.get("type", "")
        is_buy  = action in ("new", "increase")
        color   = BUY_COLOR if is_buy else SELL_COLOR
        label   = "ACHETER" if is_buy else "VENDRE"
        fund    = a.get("fund", "Un grand fonds")
        name    = a.get("name", a.get("ticker", ""))
        ticker  = a.get("ticker", "")
        verb    = TYPE_FR.get(action, "a modifié sa position sur")
        reason  = a.get("suggestion") or a.get("context") or ""
        consensus = a.get("consensus", "—")
        isin    = a.get("isin", "")
        return f"""
<div style="background:#111320;border-radius:12px;margin-bottom:12px;overflow:hidden;border-left:4px solid {color}">
  <div style="padding:14px 18px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <div>
        <span style="font-family:monospace;font-weight:700;font-size:1.1rem;color:#e8eaf2">{html.escape(ticker)}</span>
        <span style="color:#6b7490;font-size:.85rem;margin-left:8px">{html.escape(name)}</span>
      </div>
      <span style="background:{color}22;color:{color};border:1px solid {color}44;padding:3px 10px;border-radius:5px;font-size:.72rem;font-weight:800;text-transform:uppercase;letter-spacing:.06em">{label}</span>
    </div>
    <div style="color:#9aa0bc;font-size:.88rem;line-height:1.55;margin-bottom:8px">
      <strong style="color:#e8eaf2">{html.escape(fund)}</strong> {verb} <strong style="color:#e8eaf2">{html.escape(name)}</strong>.
      {f'<br>{html.escape(reason)}' if reason else ''}
    </div>
    {f'<div style="color:#6b7490;font-size:.78rem">👥 {html.escape(consensus)}</div>' if consensus != "—" else ""}
    {f'<div style="margin-top:8px;background:#08090f;border-radius:5px;padding:7px 10px;font-family:monospace;font-size:.82rem;color:#e8eaf2">ISIN : {html.escape(isin)}</div>' if isin else ""}
  </div>
</div>"""

    # Trier : achats d'abord
    buys  = [a for a in alerts if a.get("type") in ("new", "increase")]
    sells = [a for a in alerts if a.get("type") in ("exit", "reduction")]
    top_signals = buys[:5] + sells[:3]

    signals_html = "".join(sig_block(a) for a in top_signals) if top_signals else \
        '<p style="color:#6b7490;padding:14px;background:#111320;border-radius:8px">Aucun mouvement au-dessus des seuils depuis le dernier rapport. Le contexte de marché est mis à jour.</p>'

    # --- Analyse DeepSeek ---
    ai_html = ""
    if ai.get("rapport"):
        ts = ai.get("timestamp", "")
        ts_fmt = ""
        try:
            ts_fmt = f" · {dt.datetime.fromisoformat(ts.replace('Z','')):%d/%m %H:%M}"
        except Exception:
            pass
        rapport_txt = ai["rapport"].replace("\n", "<br>")
        ai_html = f"""
<div style="margin-top:24px;background:rgba(61,123,255,.06);border:1px solid rgba(61,123,255,.2);border-radius:12px;overflow:hidden">
  <div style="background:rgba(61,123,255,.1);padding:12px 18px;border-bottom:1px solid rgba(61,123,255,.15)">
    <span style="color:#3d7bff;font-weight:700">🧠 Analyse DeepSeek{ts_fmt}</span>
  </div>
  <div style="padding:14px 18px;color:#9aa0bc;font-size:.88rem;line-height:1.65">
    {rapport_txt}
  </div>
</div>"""

    # --- Insiders & signaux additionnels ---
    gs       = payload.get("global_signals", [])
    insiders = [g for g in gs if "Insider" in g.get("source", "") or "Form 4" in g.get("source", "")]
    gs_other = [g for g in gs if g not in insiders][:3]
    insider_html = ""
    if insiders:
        rows = "".join(
            f'<div style="padding:8px 0;border-bottom:1px solid #1e2235;color:#9aa0bc;font-size:.84rem">'
            f'👤 <strong style="color:#e8eaf2">{html.escape(g.get("ticker","—"))}</strong> — '
            f'{html.escape(g.get("note","Achat insider"))}</div>'
            for g in insiders[:5]
        )
        insider_html = f"""
<div style="margin-top:20px">
  <div style="font-size:.72rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#00d68f;margin-bottom:8px">👤 Achats de dirigeants cette semaine</div>
  <div style="background:#111320;border-radius:10px;padding:0 14px">{rows}</div>
</div>"""

    nb_total = len(alerts) + len(gs)

    return f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#08090f;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<div style="max-width:620px;margin:0 auto;padding:20px 14px 40px">

  <!-- Header -->
  <div style="text-align:center;padding:24px 0 20px;border-bottom:1px solid #1e2235;margin-bottom:20px">
    <div style="font-weight:800;font-size:1.4rem;color:#e8eaf2">Smart<span style="color:#00d68f">Money</span> Radar</div>
    <div style="color:#6b7490;font-size:.82rem;margin-top:4px">{today} · {nb_total} signal(s) disponible(s)</div>
  </div>

  <!-- Marché -->
  <div style="background:#111320;border-radius:12px;padding:14px 18px;margin-bottom:20px">
    <div style="font-size:.7rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#6b7490;margin-bottom:10px">📊 Marchés aujourd'hui</div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;text-align:center">
      <div><div style="font-weight:700;color:#e8eaf2">{tile("S&P 500")}</div><div style="font-size:.68rem;color:#6b7490">S&P 500</div></div>
      <div><div style="font-weight:700;color:#e8eaf2">{tile("VIX")}</div><div style="font-size:.68rem;color:#6b7490">VIX</div></div>
      <div><div style="font-weight:700;color:#e8eaf2">{tile("Brent")}</div><div style="font-size:.68rem;color:#6b7490">Pétrole</div></div>
    </div>
  </div>

  <!-- Signaux -->
  <div style="font-size:.72rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#6b7490;margin-bottom:10px">
    {f"🟢 {len(buys)} achat(s) · 🔴 {len(sells)} vente(s)" if top_signals else "📋 Signaux du jour"}
  </div>
  {signals_html}

  {insider_html}
  {ai_html}

  <!-- CTA -->
  <div style="text-align:center;margin:28px 0 20px">
    <a href="{url}" style="display:inline-block;background:#00d68f;color:#00160c;text-decoration:none;padding:13px 28px;border-radius:10px;font-weight:700;font-size:.9rem">📱 Ouvrir l'application →</a>
  </div>

  <!-- Footer -->
  <div style="border-top:1px solid #1e2235;padding-top:14px;font-size:.72rem;color:#4a5568;line-height:1.7;text-align:center">
    Portefeuilles 13F au {m.get("portfolio_date","?")} · Décalage ~45 jours<br>
    Signaux à analyser — pas un conseil en investissement personnalisé.
  </div>

</div>
</body>
</html>"""
    m, mk = payload["meta"], payload["market"]
    alerts = payload["alerts"]
    url = payload.get("_dashboard_url", "#")
    tiles = "".join(
        f'<td style="padding:6px 10px;border:1px solid #2c3641;border-radius:8px;font-family:monospace">'
        f'<div style="font-size:10px;color:#8b95a2">{t["label"]}</div>'
        f'<div style="font-size:15px;color:#ece6d9">{t["value"]} '
        f'<span style="color:{"#4ed6a1" if t["dir"]=="up" else "#e0706e" if t["dir"]=="down" else "#8b95a2"}">{t["change"]}</span></div></td>'
        for t in mk.get("snapshot", [])[:6])
    cards = ""
    for a in alerts[:14]:
        col = {"new": "#e3b35c", "exit": "#e0706e", "increase": "#4ed6a1", "reduction": "#e0706e"}.get(a["type"], "#888")
        cons = f'<div style="color:#e3b35c;font-size:12px;margin-top:4px">★ {html.escape(a["consensus"])}</div>' if a["consensus"] != "—" else ""
        cards += (
            f'<div style="border-left:3px solid {col};background:#14191f;border:1px solid #232b34;'
            f'border-radius:8px;padding:12px 14px;margin:8px 0">'
            f'<div style="font-family:monospace"><b style="color:#ece6d9">{a["ticker"]}</b> '
            f'<span style="color:#8b95a2">{html.escape(a["name"])}</span> '
            f'<span style="float:right;color:{col};font-size:11px;text-transform:uppercase">{TYPE_LABEL.get(a["type"],a["type"])}</span></div>'
            f'<div style="color:#ece6d9;font-size:14px;margin:5px 0 3px">{html.escape(a["fund"])}</div>'
            f'<div style="color:#cbd2da;font-size:13px">{html.escape(a["context"])} '
            f'<span style="color:#8b95a2">({a["prev_weight"]} → {a["new_weight"]}, {a["shares_change"]})</span></div>'
            f'<div style="color:#6aa3e0;font-size:12px;margin-top:4px">{html.escape(a["cross_read"])}</div>'
            f'{cons}</div>')
    if not alerts:
        cards = '<p style="color:#8b95a2">Aucun mouvement au-dessus des seuils depuis le dernier rapport. Contexte de marché ci-dessus mis à jour.</p>'

    return f"""<!DOCTYPE html><html><body style="margin:0;background:#0b0e12;color:#ece6d9;font-family:Helvetica,Arial,sans-serif;padding:0">
<div style="max-width:680px;margin:0 auto;padding:24px">
  <div style="border-bottom:1px solid #232b34;padding-bottom:14px;margin-bottom:18px">
    <div style="font-family:monospace;font-size:11px;letter-spacing:.3em;color:#e3b35c;text-transform:uppercase">Smart Money Radar</div>
    <h1 style="font-size:24px;margin:6px 0 2px;color:#ece6d9">Rapport — {m['quarter']}</h1>
    <div style="font-size:12px;color:#8b95a2">Portefeuilles au {m['portfolio_date']} · {len(alerts)} alerte(s)</div>
  </div>
  <div style="background:#14191f;border:1px solid #232b34;border-radius:10px;padding:14px 16px;margin-bottom:18px">
    <div style="font-size:12px;color:#e3b35c;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Contexte de marché — {mk.get('as_of','')}</div>
    <div style="color:#cbd2da;font-size:13px;line-height:1.5">{html.escape(mk.get('regime_text',''))}</div>
    <table style="border-collapse:separate;border-spacing:6px;margin-top:8px"><tr>{tiles}</tr></table>
  </div>
  <h2 style="font-size:16px;color:#ece6d9;border-bottom:1px solid #232b34;padding-bottom:8px">Alertes de mouvement</h2>
  {cards}
  <div style="text-align:center;margin:22px 0">
    <a href="{url}" style="background:#ece6d9;color:#0b0e12;text-decoration:none;padding:11px 22px;border-radius:8px;font-weight:bold;font-size:14px">Ouvrir le tableau de bord →</a>
  </div>
  <div style="border-top:1px solid #232b34;padding-top:12px;font-size:11px;color:#5e6873;line-height:1.5">
    {html.escape(m.get('disclaimer',''))}<br>Les 13F sont publiés ~45 j après la fin du trimestre. Signaux à analyser, pas un conseil en investissement.
  </div>
</div></body></html>"""


def send_email(subject: str, html_body: str):
    host = os.environ.get("SMTP_HOST"); port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER"); pwd = os.environ.get("SMTP_PASS")
    mail_from = os.environ.get("MAIL_FROM", user); mail_to = os.environ.get("MAIL_TO")
    if not all([host, user, pwd, mail_to]):
        print("⚠️  Identifiants SMTP/MAIL_TO manquants — e-mail non envoyé (run local ?).")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject; msg["From"] = mail_from; msg["To"] = mail_to
    msg.attach(MIMEText("Version HTML requise.", "plain"))
    msg.attach(MIMEText(html_body, "html"))
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, pwd); s.sendmail(mail_from, mail_to.split(","), msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(); s.login(user, pwd); s.sendmail(mail_from, mail_to.split(","), msg.as_string())
        print(f"✓ E-mail envoyé à {mail_to}")
    except Exception as e:
        print(f"⚠️  Échec de l'envoi e-mail : {e}")


def push_ntfy(alerts: list[dict], dashboard_url: str, quarter_label: str = ""):
    """Notification push mobile OPTIONNELLE via ntfy.sh — sans serveur, gratuit.

    Activation : définissez le secret NTFY_TOPIC (ex. 'smr-9f3k-prive', un nom
    long et privé qui vous est propre). Installez l'app « ntfy » (iOS/Android),
    abonnez-vous à ce même sujet, et vous recevrez une alerte dès qu'un fonds
    dépose un nouveau 13F. NTFY_SERVER (optionnel) pour un serveur auto-hébergé.
    Ne pousse rien s'il n'y a pas de nouveau mouvement (pas de spam)."""
    if requests is None:
        return
    topic = os.environ.get("NTFY_TOPIC")
    if not topic or not alerts:
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    verb = {"new": "nouvelle position", "exit": "sortie totale",
            "increase": "renforce", "reduction": "allège"}
    lines = [f"{a.get('fund', '?')} — {verb.get(a.get('type'), 'mouvement')} "
             f"{a.get('ticker', '')}".strip() for a in alerts[:5]]
    more = len(alerts) - 5
    if more > 0:
        lines.append(f"… +{more} autre(s)")
    body = "\n".join(lines).encode("utf-8")
    # En-têtes HTTP en ASCII uniquement (le corps, lui, est en UTF-8)
    headers = {"Title": f"Smart Money : {len(alerts)} mouvement(s)",
               "Tags": "chart_with_upwards_trend", "Priority": "default"}
    if dashboard_url and dashboard_url not in ("#", ""):
        headers["Click"] = dashboard_url
    try:
        r = requests.post(f"{server}/{topic}", data=body, headers=headers, timeout=20)
        print(f"✓ Notification push envoyée (ntfy : {topic})." if r.ok
              else f"⚠️  ntfy a répondu {r.status_code}.")
    except Exception as e:
        print(f"⚠️  Échec de la notification ntfy : {e}")


# ======================================================================
#  MAIN
# ======================================================================
def run(force=False):
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    th = cfg["thresholds"]; min_funds = th["consensus_min_funds"]
    state = load_state()
    today = dt.date.today().isoformat()

    all_alerts, fund_blocks = [], []
    quarter_label, portfolio_date, filing_deadline = "", "", ""
    all_tickers = set()

    for f in cfg["funds"]:
        slug, cik, tier = f["slug"], f["cik"], f.get("tier", "contexte")
        print(f"→ {f['name']} (CIK {cik})")
        try:
            filings = latest_13f_filings(cik, n=2)
            if not filings:
                print("   pas de 13F trouvé."); fund_blocks.append(_passive_block(f)); continue

            cur_f = filings[0]
            prev_f = filings[1] if len(filings) > 1 else None
            if not quarter_label:
                portfolio_date = cur_f["report_date"]; filing_deadline = cur_f["filing_date"]
                quarter_label = _quarter(portfolio_date)

            new_filing = state.get(slug, {}).get("last_accession") != cur_f["accession"]
            if not (new_filing or force):
                print("   pas de nouveau dépôt."); fund_blocks.append(_passive_block(f)); continue

            cur_h = fetch_info_table(cik, cur_f["accession"])
            prev_h = fetch_info_table(cik, prev_f["accession"]) if prev_f else []
            if not cur_h:
                print("   table d'information illisible."); fund_blocks.append(_passive_block(f)); continue

            tmap = cusip_to_ticker([h["cusip"] for h in cur_h + prev_h])
            cur_p = build_portfolio(cur_h, tmap)
            prev_p = build_portfolio(prev_h, tmap) if prev_h else {}

            a = diff(cur_p, prev_p, th, tier)
            for x in a:
                x["fund"] = f["name"]; x["fund_slug"] = slug
                all_tickers.add(x["ticker"])
            all_alerts += a
            fund_blocks.append(_active_block(f, cur_p, a))
            state[slug] = {"last_accession": cur_f["accession"], "report_date": cur_f["report_date"]}
            print(f"   {len(a)} alerte(s).")
        except Exception as e:
            print(f"   ⚠️  Erreur pour {f['name']} : {e} — ignoré, on continue.")
            fund_blocks.append(_passive_block(f))

    # ---- Si aucune nouvelle alerte 13F : conserver les dernières connues ----
    if not all_alerts:
        try:
            last = _load_json(os.path.join(ROOT, "data.json"), {})
            kept = last.get("alerts", [])
            if kept:
                all_alerts = kept
                if not quarter_label:
                    quarter_label = last.get("meta", {}).get("quarter", "")
                if not portfolio_date:
                    portfolio_date = last.get("meta", {}).get("portfolio_date", "")
                print(f"   Pas de nouveau 13F — {len(all_alerts)} alerte(s) conservées du dernier dépôt.")
            if not fund_blocks:
                fund_blocks = last.get("funds", [])
        except Exception:
            pass

    # ---- contexte de marché (tous les jours) ----
    print("→ Contexte de marché…")
    tiles = market.build_snapshot(cfg["market"]["snapshot_tickers"])
    _, events = market.get_macro(cfg)
    news = market.get_news(cfg)
    drift = market.price_drift(sorted(all_tickers), portfolio_date) if portfolio_date else {}
    label, regime_text = market.regime_from_tiles(tiles)
    if cfg["market"].get("llm_commentary"):
        llm = market.llm_market_read(tiles, events, news, all_alerts)
        if llm:
            regime_text = llm

    # ---- annotations (consensus, lecture, pistes) ----
    all_alerts = annotate(all_alerts, min_funds, drift)
    all_alerts.sort(key=lambda a: {"high": 0, "medium": 1, "low": 2}[a["severity"]])
    cbuys, csells = consensus_lists(all_alerts, min_funds)

    payload = {
        "meta": {
            "generated_at": dt.datetime.utcnow().isoformat() + "Z",
            "is_demo": False,
            "quarter": quarter_label or "—",
            "portfolio_date": portfolio_date, "filing_deadline": filing_deadline,
            "lag_days": 45,
            "next_filing_window": _next_window(),
            "sources": ["SEC EDGAR (13F-HR officiels)", "OpenFIGI (CUSIP→ticker)",
                        "TradingEconomics", "Investing.com / FinancialJuice (RSS)", "Yahoo Finance"],
            "disclaimer": cfg.get("meta", {}).get("disclaimer",
                "Les 13F sont publiés ~45 jours après la fin du trimestre : les prix ont déjà évolué. "
                "Signaux à analyser, pas un conseil en investissement personnalisé."),
        },
        "market": {
            "as_of": dt.date.today().strftime("%d/%m/%Y"),
            "regime_label": label, "regime_text": regime_text,
            "snapshot": tiles, "macro": [], "events": events, "news": news,
            "providers_note": "Prix : Yahoo Finance · Macro : TradingEconomics · Actualité : RSS Investing.com/FinancialJuice (repli Google News).",
        },
        "consensus": {"theme": _theme(cbuys, csells), "top_buys": cbuys, "top_sells": csells},
        "alerts": all_alerts,
        "funds": fund_blocks,
        "_dashboard_url": cfg.get("site", {}).get("dashboard_url", "#"),
    }

    # L'état de suivi est sauvegardé tôt pour ne pas retraiter si le run plante
    save_state(state)
    # data.json sera écrit APRÈS les collecteurs (insiders, 13D/G, etc.)
    # afin d'inclure TOUS les signaux dans le fichier final.

    # ---- signaux insiders (Form 4 — optionnel, ne bloque pas) ----
    global_signals = []
    if cfg.get("insiders", {}).get("enabled", True):
        print("→ Collecte des achats insiders (OpenInsider)…")
        try:
            from collector_insiders import fetch_insider_trades, to_global_signals
            trades = fetch_insider_trades(
                days=cfg.get("insiders", {}).get("days", 7),
                min_value=cfg.get("insiders", {}).get("min_value_usd", 50_000),
            )
            gs = to_global_signals(trades)
            global_signals.extend(gs)
            print(f"   {len(gs)} achat(s) insiders trouvé(s)." if gs else "   Aucun achat insider significatif.")
        except Exception as e:
            print(f"   ⚠️  Collecte insiders échouée : {e}")

    # ---- 13D/G : activistes et franchissements de seuil >5 % ----
    coll = cfg.get("collectors", {})
    if coll.get("dg13", True):
        print("→ Collecte des 13D/G (activistes >5 %)…")
        try:
            from collector_13dg import fetch_recent_13dg
            dg = fetch_recent_13dg(days=coll.get("dg13_days", 10))
            global_signals.extend(dg)
        except Exception as e:
            print(f"   ⚠️  13D/G échoué : {e}")

    # ---- Signaux complémentaires sur les tickers déjà en alerte ----
    tickers_alerte = sorted(set(a["ticker"] for a in all_alerts if a.get("ticker")))
    if not tickers_alerte:
        # Si pas d'alerte 13F (run quotidien), utiliser les tickers du dernier data.json
        try:
            last = _load_json(os.path.join(ROOT, "data.json"), {})
            tickers_alerte = list(set(a.get("ticker", "") for a in last.get("alerts", []) if a.get("ticker")))[:30]
        except Exception:
            pass

    if tickers_alerte:
        if coll.get("short_interest", True):
            print(f"→ Short interest ({len(tickers_alerte)} tickers)…")
            try:
                from collector_short_interest import fetch_short_interest
                shorts = fetch_short_interest(
                    tickers_alerte,
                    min_short_pct=coll.get("short_interest_min_pct", 15.0),
                )
                global_signals.extend(shorts)
                if shorts:
                    print(f"   {len(shorts)} signal(s) de short interest élevé.")
            except Exception as e:
                print(f"   ⚠️  Short interest échoué : {e}")

        if coll.get("earnings", True):
            print(f"→ Calendrier résultats ({len(tickers_alerte)} tickers)…")
            try:
                from collector_earnings import fetch_upcoming_earnings
                earn = fetch_upcoming_earnings(
                    tickers_alerte,
                    within_days=coll.get("earnings_days_ahead", 7),
                )
                global_signals.extend(earn)
                if earn:
                    print(f"   {len(earn)} publication(s) de résultats imminente(s).")
            except Exception as e:
                print(f"   ⚠️  Earnings échoué : {e}")

        if coll.get("options_flow", False):   # désactivé par défaut (lent)
            print(f"→ Options flow ({len(tickers_alerte)} tickers)…")
            try:
                from collector_options_flow import fetch_options_flow
                opts = fetch_options_flow(tickers_alerte)
                global_signals.extend(opts)
                if opts:
                    print(f"   {len(opts)} signal(s) options inhabituels.")
            except Exception as e:
                print(f"   ⚠️  Options flow échoué : {e}")

    if global_signals:
        payload["global_signals"] = global_signals
        print(f"✓ {len(global_signals)} signal(s) complémentaire(s) au total.")

    # ---- ETF UCITS (positions quotidiennes) ----
    if coll.get("etf_ucits", True):
        print("→ ETF UCITS (iShares, Vanguard)…")
        try:
            from collector_etf_ucits import fetch_etf_signals
            etf = fetch_etf_signals(min_delta_pct=coll.get("etf_min_delta", 0.20))
            payload.setdefault("global_signals", []).extend(etf)
        except Exception as e:
            print(f"   ⚠️  ETF UCITS échoué : {e}")

    # ---- Fonds mutuels top 10 ----
    if coll.get("fund_top10", True):
        print("→ Fonds mutuels top 10 (Fundsmith, Baillie Gifford…)…")
        try:
            from collector_fund_top10 import fetch_fund_top10_signals
            mf = fetch_fund_top10_signals()
            payload.setdefault("global_signals", []).extend(mf)
        except Exception as e:
            print(f"   ⚠️  Fonds mutuels échoué : {e}")

    # ---- Fonds souverain NBIM ----
    if coll.get("nbim", True):
        print("→ Fonds souverain NBIM (Norges Bank)…")
        try:
            from collector_nbim import fetch_nbim_signals
            nb = fetch_nbim_signals()
            payload.setdefault("global_signals", []).extend(nb)
        except Exception as e:
            print(f"   ⚠️  NBIM échoué : {e}")

    # ---- SoftBank + grands conglomérats ----
    if coll.get("conglomerates", True):
        print("→ Conglomérats (SoftBank, Berkshire 8-K…)…")
        try:
            from collector_softbank import fetch_conglomerate_signals
            sb = fetch_conglomerate_signals()
            payload.setdefault("global_signals", []).extend(sb)
        except Exception as e:
            print(f"   ⚠️  Conglomérats échoué : {e}")

    # ---- AMF / BaFin / Family offices ----
    if coll.get("amf_bafin", True):
        print("→ Régulateurs AMF / BaFin (franchissements de seuil)…")
        try:
            from collector_amf_familyoffices import fetch_threshold_signals
            amf = fetch_threshold_signals()
            payload.setdefault("global_signals", []).extend(amf)
        except Exception as e:
            print(f"   ⚠️  AMF/BaFin échoué : {e}")

    total_gs = len(payload.get("global_signals", []))
    if total_gs:
        print(f"✓ Total signaux complémentaires (toutes sources) : {total_gs}")

    # ---- Sauvegarde data.json avec TOUS les signaux (13F + collecteurs) ----
    _save_json(os.path.join(ROOT, "data.json"),
               {k: v for k, v in payload.items() if not k.startswith("_")})
    print(f"✓ data.json écrit — {len(all_alerts)} alerte(s) 13F + {total_gs} signal(s) complémentaire(s).")

    # ---- analyse DeepSeek (optionnel — activé par deepseek.enabled: true) ----
    if cfg.get("deepseek", {}).get("enabled", False):
        print("→ Analyse IA DeepSeek…")
        try:
            from deepseek_analyse import analyser
            ds_result = analyser(payload, cfg.get("deepseek", {}))
            payload["ai_analysis"] = {
                "rapport":                ds_result.get("rapport", ""),
                "propositions_achat":     ds_result.get("propositions_achat", []),
                "propositions_vente":     ds_result.get("propositions_vente", []),
                "small_mid_caps":         ds_result.get("small_mid_caps", []),
                "secteurs_a_privilegier": ds_result.get("secteurs_a_privilegier", []),
                "secteurs_a_eviter":      ds_result.get("secteurs_a_eviter", []),
                "avertissement":          ds_result.get("avertissement", ""),
                "timestamp":              ds_result.get("timestamp", ""),
            }
            # Re-sauvegarde avec l'analyse IA
            _save_json(os.path.join(ROOT, "data.json"),
                       {k: v for k, v in payload.items() if not k.startswith("_")})
            print("✓ Analyse IA intégrée à data.json.")
        except Exception as e:
            print(f"   ⚠️  Analyse DeepSeek échouée : {e}")

    if all_alerts or cfg.get("email", {}).get("send_when_no_alert", True):
        subj = f"{cfg['email']['subject_prefix']} — {quarter_label or today} · {len(all_alerts)} alerte(s)"
        send_email(subj, render_email_html(payload))
        push_ntfy(all_alerts, payload["_dashboard_url"], quarter_label)


# ---- helpers de blocs "fonds" pour data.json ----
def _active_block(f, port, alerts):
    top = sorted(port.values(), key=lambda p: p["weight"], reverse=True)[:5]
    buys = [a["ticker"] for a in alerts if a["type"] in ("new", "increase")][:6]
    exits = [a["ticker"] for a in alerts if a["type"] in ("exit", "reduction")][:8]
    return {
        "name": f["name"], "slug": f["slug"], "manager": f.get("manager", ""),
        "style": f.get("style", ""), "tier": f.get("tier", "contexte"),
        "positions": str(len(port)), "is_passive": f.get("tier") == "faible-signal",
        "featured": f.get("featured", False),
        "top5_weight": f"{sum(p['weight'] for p in top):.0f} %",
        "top_holdings": [{"ticker": p["ticker"], "name": p["name"], "weight": round(p["weight"], 1)} for p in top],
        "new_buys": buys, "exits": exits,
        "notable": f"{len(alerts)} mouvement(s) au-dessus des seuils ce trimestre.",
    }

def _passive_block(f):
    return {
        "name": f["name"], "slug": f["slug"], "manager": f.get("manager", ""),
        "style": f.get("style", ""), "tier": f.get("tier", "faible-signal"),
        "is_passive": f.get("tier") == "faible-signal", "positions": "—",
        "top_holdings": [], "new_buys": [], "exits": [],
        "notable": "Pas de nouveau dépôt / signal faible ce cycle.",
    }


# ---- divers ----
def _quarter(iso):
    try:
        d = dt.date.fromisoformat(iso); return f"T{(d.month - 1)//3 + 1} {d.year}"
    except Exception:
        return "—"

def _next_window():
    m = dt.date.today().month
    nxt = {1: "Février", 2: "Février", 3: "Mai", 4: "Mai", 5: "Mai",
           6: "Août", 7: "Août", 8: "Août", 9: "Novembre",
           10: "Novembre", 11: "Novembre", 12: "Février"}[m]
    return f"{nxt} (prochains 13F)"

def _theme(buys, sells):
    b = ", ".join(x["ticker"] for x in buys[:3]) or "—"
    s = ", ".join(x["ticker"] for x in sells[:3]) or "—"
    return f"Achats les plus convergents : {b}. Ventes les plus convergentes : {s}."

def _title(s):
    return s.title() if s and s.isupper() else s

def _load_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default

def _save_json(path, obj):
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ======================================================================
#  SELF-TEST (hors-ligne : valide parseur XML + moteur de diff)
# ======================================================================
def selftest():
    sample = """<?xml version="1.0"?>
    <informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
      <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip>
        <value>50000000</value><shrsOrPrnAmt><sshPrnamt>200000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
      <infoTable><nameOfIssuer>VISA INC</nameOfIssuer><cusip>92826C839</cusip>
        <value>10000000</value><shrsOrPrnAmt><sshPrnamt>40000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
    </informationTable>"""
    h = parse_info_table(sample)
    assert len(h) == 2, h
    assert any(x["cusip"] == "037833100" and x["shares"] == 200000 for x in h)
    print("✓ parseur XML : 2 lignes lues correctement")

    tmap = {"037833100": "AAPL", "92826C839": "V", "02079K305": "GOOGL"}
    prev = build_portfolio(
        [{"cusip": "037833100", "issuer": "APPLE INC", "value": 50, "shares": 200000},
         {"cusip": "92826C839", "issuer": "VISA INC", "value": 10, "shares": 40000}], tmap)
    cur = build_portfolio(
        [{"cusip": "037833100", "issuer": "APPLE INC", "value": 50, "shares": 260000},   # +30% titres
         {"cusip": "02079K305", "issuer": "ALPHABET INC", "value": 20, "shares": 90000}], tmap)  # nouvelle + Visa sortie
    th = {"new_position_min_weight_pct": 0.5, "exit_min_prev_weight_pct": 0.5,
          "share_change_pct": 20, "big_move_min_weight_pct": 1.0, "top_n": 5}
    a = diff(cur, prev, th, "actionnable")
    types = sorted(x["type"] for x in a)
    print("  alertes générées :", [(x["ticker"], x["type"]) for x in a])
    assert "new" in types and "exit" in types and "increase" in types, types
    a2 = annotate([dict(x, fund="Fonds A", fund_slug="a") for x in a], 2, {"AAPL": 12.3})
    assert all("suggestion" in x and "context" in x for x in a2)
    print("✓ moteur de diff + annotations : nouvelle / sortie / augmentation détectées")
    print("✓ self-test OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run(force="--force" in sys.argv)
