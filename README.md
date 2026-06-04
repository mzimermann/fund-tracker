# 📈 Smart Money Radar

Agent qui surveille les dépôts **13F** des plus grands fonds (Berkshire, BlackRock, Vanguard, Pershing Square, Appaloosa, Duquesne…), détecte leurs gros mouvements de portefeuille, les replace dans le **contexte de marché du jour**, puis :

- 📧 vous envoie un **rapport e-mail** à chaque nouveau dépôt (et un point régulier entre-temps) ;
- 🖥️ met à jour un **tableau de bord web** (`index.html`) hébergeable gratuitement sur GitHub Pages.

Le tout tourne **tout seul, une fois par jour**, via GitHub Actions — sans serveur, gratuitement.

---

## ⚠️ À lire avant tout

1. **Les 13F sont trimestriels et décalés de ~45 jours.** Un fonds publie au 15 mai ce qu'il détenait au 31 mars. Quand vous voyez le mouvement, le prix a déjà bougé. L'agent calcule donc le **« drift »** (variation du titre depuis la date du portefeuille) pour vous le rappeler.
2. **« Une fois par jour » ≠ nouvelles positions chaque jour.** L'agent **vérifie chaque jour** s'il existe un nouveau dépôt et **rafraîchit le contexte de marché** quotidiennement ; les positions, elles, ne changent qu'aux dépôts trimestriels (~mi-février, mi-mai, mi-août, mi-novembre).
3. **Ce sont des signaux, pas un conseil.** Cet outil n'émet aucune recommandation personnalisée. Les « pistes » affichées sont des invitations à analyser, à confronter à votre propre stratégie, horizon et tolérance au risque.
4. **Passif vs actif.** Les variations 13F de BlackRock / Vanguard / State Street reflètent surtout du rééquilibrage d'indices → **signal faible** (l'agent ne remonte que leurs changements de top 5). Le vrai signal vient des fonds **concentrés** (Berkshire, Pershing, Appaloosa, Duquesne).

---

## 🏗️ Architecture

```
fund-tracker/
├─ index.html         ← tableau de bord (lit data.json) — servi par GitHub Pages
├─ data.json          ← données régénérées par l'agent (commitées chaque jour)
├─ agent.py           ← orchestrateur : EDGAR → diff → alertes → e-mail → data.json
├─ market.py          ← contexte marché : prix, macro, actualité, drift
├─ config.yaml        ← fonds suivis (CIK), seuils, sources, e-mail
├─ requirements.txt
└─ .github/workflows/
   └─ daily.yml        ← cron quotidien (GitHub Actions)
```

**Flux quotidien :** Actions lance `agent.py` → l'agent interroge EDGAR, calcule les écarts, récupère le marché, écrit `data.json`, envoie l'e-mail → Actions commit `data.json` → Pages sert le site à jour.

---

## 🚀 Déploiement en 3 étapes

### 1) Forkez / créez le dépôt
Mettez ces fichiers dans un dépôt GitHub (public = Pages gratuit).

### 2) Ajoutez les secrets
`Settings → Secrets and variables → Actions → New repository secret` :

| Secret | Obligatoire | Rôle |
|---|---|---|
| `SMTP_HOST` | ✅ | Serveur SMTP (ex. `smtp.gmail.com`) |
| `SMTP_PORT` | ✅ | `465` (SSL) ou `587` (STARTTLS) |
| `SMTP_USER` | ✅ | Identifiant SMTP |
| `SMTP_PASS` | ✅ | Mot de passe **d'application** (Gmail : créez-en un, pas votre mot de passe principal) |
| `MAIL_FROM` | ✅ | Adresse expéditrice |
| `MAIL_TO` | ✅ | Destinataire(s), séparés par des virgules |
| `SEC_USER_AGENT` | ⭐ recommandé | `Prénom Nom email@exemple.com` — la SEC l'exige formellement |
| `OPENFIGI_API_KEY` | optionnel | Augmente le quota CUSIP→ticker ([openfigi.com](https://www.openfigi.com/api)) |
| `TE_API_KEY` | optionnel | Données macro complètes ([tradingeconomics.com](https://tradingeconomics.com/api)) |
| `ANTHROPIC_API_KEY` | optionnel | Active la lecture marché rédigée par IA (`llm_commentary: true`) |
| `NTFY_TOPIC` | optionnel | Notification **push mobile** via l'app ntfy (voir « Application mobile ») |

### 3) Activez GitHub Pages
`Settings → Pages → Source : Deploy from a branch → Branch : main / root`.
Copiez l'URL obtenue dans `config.yaml → site.dashboard_url` (elle apparaît dans l'e-mail).

➡️ Puis `Actions → Smart Money Radar → Run workflow` pour un premier run immédiat (sinon, attendez le cron).

---

## 📱 Application mobile (PWA)

Le tableau de bord est une **PWA** : depuis le navigateur de votre téléphone, ajoutez-le à l'écran d'accueil et il se comporte comme une vraie app — **icône dédiée, plein écran, hors-ligne, mise à jour automatique** — sans App Store, sans compte développeur, gratuitement.

**Installer :**
- **iPhone (Safari)** : ouvrez l'URL Pages → bouton **Partager** → *Sur l'écran d'accueil*.
- **Android (Chrome)** : ouvrez l'URL → une invite **« Installer »** apparaît (ou menu ⋮ → *Ajouter à l'écran d'accueil*).

Une fois installée, l'app lit `data.json` à chaque ouverture : comme l'agent le réécrit chaque jour, vos données restent à jour automatiquement. Hors-ligne, elle affiche la dernière version mise en cache.

> Fichiers PWA : `manifest.webmanifest`, `sw.js` (service worker) et les icônes `icon-192/512`, `apple-touch-icon`. Régénérez les icônes avec `python make_icons.py`.

### 🔔 Notifications push (optionnel, sans serveur)
Pour être **alerté instantanément** dès qu'un fonds dépose un nouveau 13F (en plus de l'e-mail) :
1. Installez l'app **ntfy** ([iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)) — gratuite, open-source.
2. Choisissez un **sujet privé** difficile à deviner (ex. `smr-9f3k-prive`) et abonnez-vous-y dans l'app.
3. Ajoutez le secret `NTFY_TOPIC` (= ce même sujet) dans GitHub.

L'agent enverra une notification (titre + mouvements + lien vers le tableau de bord) uniquement quand il y a du nouveau — pas de spam. `NTFY_SERVER` permet un serveur auto-hébergé.

> **App native (Swift/Kotlin) ?** Possible, mais cela demande un Mac + Xcode (iOS), un compte développeur Apple (~99 $/an pour l'installer durablement) et une base de code séparée à maintenir — disproportionné pour un usage perso. La PWA + ntfy couvre l'icône, le plein écran, le hors-ligne **et** les notifications, gratuitement.

---

## 🔌 Sources de données

| Donnée | Source | Accès | Remarque |
|---|---|---|---|
| Positions 13F | **SEC EDGAR** | API publique `data.sec.gov` | Officiel, gratuit. User-Agent obligatoire. |
| CUSIP → ticker | **OpenFIGI** | API (clé optionnelle) | Sans clé : 10 codes/requête. Résultats mis en cache. |
| Prix en direct | **Yahoo Finance** (`yfinance`) | gratuit, sans clé | Indices, pétrole, or, taux, BTC + drift depuis le 13F. |
| Macro / calendrier | **TradingEconomics** | `guest:guest` (limité) ou `TE_API_KEY` | Calendrier US, importance ≥ moyenne. |
| Actualité | **Investing.com** + **FinancialJuice** (RSS) | flux RSS | ⚠️ Ces sites bloquent parfois les robots ; repli automatique sur Google News. Pour FinancialJuice, un flux RSS personnel (compte gratuit) est plus fiable. |

> Investing.com et FinancialJuice n'offrent pas d'API publique officielle. L'agent tente leurs flux RSS puis bascule sur un repli si rien ne remonte, afin de ne jamais planter.

---

## 🎯 Seuils de détection (modifiables dans `config.yaml`)

- **Nouvelle position** : poids ≥ 0,5 %
- **Sortie totale** : la ligne pesait ≥ 0,5 %
- **Gros mouvement** : variation du nombre de titres > 20 % **et** poids ≥ 1 %
- **Top 5** : toute entrée/sortie du top 5
- **Consensus** : même mouvement (même titre, même sens) chez ≥ 2 fonds

---

## 🧪 Tester sans réseau

```bash
pip install -r requirements.txt
python agent.py --selftest   # valide le parseur XML 13F + le moteur de diff
python agent.py --force      # force le re-téléchargement complet (ignore le cache d'état)
```

---

## ✅ Vérifier les CIK

Les fonds sont identifiés par leur **CIK** SEC dans `config.yaml`. Quelques-uns sont marqués `# verify: true` : confirmez-les sur
👉 [sec.gov/cgi-bin/browse-edgar](https://www.sec.gov/cgi-bin/browse-edgar) (recherche par nom, type = `13F`).
Un CIK erroné = ce fonds est simplement ignoré (l'agent ne plante pas).

---

*Données fournies à titre informatif. Aucune recommandation d'investissement. Faites vos propres recherches.*
