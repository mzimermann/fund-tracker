"""
deepseek_analyse.py — Analyse IA via l'API DeepSeek (ou compatible OpenAI).

Prend en entrée le payload enrichi (13F + insiders + marché),
appelle DeepSeek avec un prompt structuré, et retourne :
  - Un rapport narratif en français
  - Des propositions d'achat/vente scorées (0-100 = force du signal)
  - Les consensus thématiques

Score = force du signal agrégé (convergence, qualité source, contexte).
PAS un taux de réussite prédit. Signaux à analyser, pas des conseils.

Activation : variable d'environnement DEEPSEEK_API_KEY
             + deepseek.enabled: true dans config.yaml
"""

from __future__ import annotations
import os, json, datetime as dt, time
try:
    import requests as _req
except ImportError:
    _req = None

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
ALT_API_URL      = "https://api.openai.com/v1/chat/completions"   # repli si OPENAI_API_KEY

SYSTEM_PROMPT = """Tu es un analyste financier senior qui synthétise des signaux institutionnels.

Tu analyses :
1. Les mouvements 13F des meilleurs fonds (Berkshire, Pershing, Duquesne, Appaloosa, etc.)
2. Les achats d'insiders (dirigeants qui achètent leurs propres actions)
3. Le contexte macro-économique et sectoriel

Pour chaque signal tu calcules un SCORE DE SIGNAL (0-100) qui mesure la CONVERGENCE et la QUALITÉ du signal :
- 80-100 : convergence multi-sources, fond concentré, contexte favorable
- 60-79 : signal solide d'un fond concentré ou consensus 2-3 fonds
- 40-59 : signal intéressant mais isolé ou context neutre
- < 40  : signal faible (fond indiciel, position marginale, macro défavorable)

RÈGLE ABSOLUE : ces scores mesurent la force du signal — pas une probabilité de gain.
Tu inclus TOUJOURS la phrase : "Signal à analyser uniquement, pas un conseil en investissement."
Tu inclus TOUJOURS les risques pour chaque piste.
Tu mentionnes TOUJOURS le décalage 45 jours des 13F.

Réponds UNIQUEMENT avec du JSON valide (pas de Markdown, pas de balises).
Structure exacte :
{
  "rapport": "3-4 paragraphes narratifs en français résumant la situation",
  "propositions_achat": [
    {
      "ticker": "GOOGL",
      "nom": "Alphabet Inc.",
      "score": 82,
      "capitalisation": "large",
      "source": "Consensus Berkshire + Druckenmiller + 4 hedge funds",
      "raison": "Positionnement IA convergent, valorisation raisonnée vs croissance cloud",
      "risque": "Antitrust Google Search, compression marges IA",
      "drift_note": "Vérifier drift depuis date 13F avant toute décision"
    }
  ],
  "propositions_vente": [
    {
      "ticker": "CRM",
      "nom": "Salesforce",
      "score": 71,
      "capitalisation": "large",
      "source": "Réduction consensus HF T1 2026",
      "raison": "Dérating SaaS classique vers IA agentique, momentum en baisse",
      "risque": "Rebond possible si intégration IA accélérée"
    }
  ],
  "small_mid_caps": [
    {
      "ticker": "SNDK",
      "nom": "SanDisk",
      "score": 64,
      "capitalisation": "mid",
      "source": "Druckenmiller + Tepper T1 2026",
      "raison": "Cycle mémoire NAND en reprise, supercycle IA demande stockage",
      "risque": "Liquidité plus faible, volatilité élevée"
    }
  ],
  "secteurs_a_privilegier": ["IA / semi-conducteurs", "Énergie (géopolitique)"],
  "secteurs_a_eviter": ["SaaS décoté", "Exposition Chine non-couverte"],
  "avertissement": "Ces signaux proviennent de dépôts 13F avec ~45 jours de décalage et d'achats insiders publics. Ils reflètent les décisions passées de tiers. Signaux à analyser uniquement — pas des conseils en investissement personnalisés."
}"""


def _construire_prompt(payload: dict) -> str:
    """Construit le prompt utilisateur depuis le payload enrichi."""
    mkt    = payload.get("market", {})
    cons   = payload.get("consensus", {})
    alerts = payload.get("alerts", [])
    gsig   = payload.get("global_signals", [])

    snap = mkt.get("snapshot", [])
    def tile(i):
        return snap[i].get("value", "N/A") if len(snap) > i else "N/A"

    lignes = [
        "## CONTEXTE MARCHÉ",
        f"Régime : {mkt.get('regime_label', 'N/A')} — {mkt.get('regime_text', '')[:200]}",
        f"S&P 500 : {tile(0)} | Nasdaq : {tile(1)} | VIX : {tile(4)} | "
        f"Brent : {tile(5)} | Or : {tile(6)} | 10Y US : {tile(8)}",
        "",
        f"## SIGNAUX 13F (top {min(len(alerts), 20)})",
    ]
    for a in alerts[:20]:
        lignes.append(
            f"• {a.get('fund','?')} — {a.get('type','?')} {a.get('ticker','?')} "
            f"({a.get('name','?')}) | {a.get('prev_weight','?')}→{a.get('new_weight','?')} "
            f"| consensus: {a.get('consensus','—')}"
        )

    if gsig:
        lignes += ["", f"## SIGNAUX ADDITIONNELS ({len(gsig)} signaux)"]
        for s in gsig[:15]:
            lignes.append(
                f"• [{s.get('source','?')}] {s.get('entity','?')} : "
                f"{s.get('type','?')} {s.get('ticker','?')} — {s.get('note','')}"
            )

    lignes += [
        "",
        "## CONSENSUS THÉMATIQUES",
        f"Top achats : {', '.join(b['ticker'] for b in cons.get('top_buys', [])[:7])}",
        f"Top ventes : {', '.join(s['ticker'] for s in cons.get('top_sells', [])[:7])}",
        "",
        "## DEMANDE",
        "Produis le JSON structuré demandé : rapport narratif, propositions achat/vente scorées "
        "(dont 2-3 small/mid caps), secteurs, avertissement. Max 5 propositions achat, 5 vente, 3 small/mid.",
    ]
    return "\n".join(lignes)


def analyser(payload: dict, cfg_deepseek: dict | None = None) -> dict:
    """
    Appelle l'API DeepSeek et retourne le résultat structuré.
    payload : dict issu de run() (contient alerts, market, global_signals…)
    cfg_deepseek : section deepseek de config.yaml (optionnel)
    """
    if _req is None:
        return {"erreur": "requests non disponible"}

    # --- clé API (DeepSeek ou repli OpenAI) ---
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    api_url = DEEPSEEK_API_URL
    model   = (cfg_deepseek or {}).get("model", "deepseek-chat")
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY")
        api_url = ALT_API_URL
        model   = "gpt-4o-mini"
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY (ou OPENAI_API_KEY) manquante.")

    prompt = _construire_prompt(payload)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "temperature": 0.25,
        "max_tokens": 2800,
        "stream": False,
    }

    for attempt in range(3):
        try:
            r = _req.post(api_url, headers=headers, json=body, timeout=120)
            r.raise_for_status()
            texte = r.json()["choices"][0]["message"]["content"].strip()
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
            else:
                raise RuntimeError(f"Échec API DeepSeek après 3 tentatives : {e}")

    # --- tenter le parsing JSON structuré ---
    import re as _re
    # Retirer éventuels backticks Markdown
    clean = _re.sub(r"^```(?:json)?\s*|\s*```$", "", texte, flags=_re.M).strip()
    try:
        structured = json.loads(clean)
        structured["_raw"] = texte
        structured["timestamp"] = dt.datetime.utcnow().isoformat() + "Z"
        return structured
    except json.JSONDecodeError:
        # Repli : stocker le texte brut
        return {
            "rapport": texte,
            "propositions_achat": [],
            "propositions_vente": [],
            "small_mid_caps": [],
            "secteurs_a_privilegier": [],
            "secteurs_a_eviter": [],
            "avertissement": "Signaux à analyser uniquement — pas des conseils en investissement.",
            "_raw": texte,
            "_parse_error": True,
            "timestamp": dt.datetime.utcnow().isoformat() + "Z",
        }
