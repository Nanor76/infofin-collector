# Screener William Higgons - InfoFin

Ce module implémente un screener de présélection quantitative inspiré de la stratégie de William Higgons (gestionnaire de fonds de type "Value/Quality"). Son objectif est de filtrer l'univers d'investissement initial (les actions d'une bourse donnée) pour produire une liste restreinte de candidats qualitatifs, sans pour autant prendre de décision d'achat finale.

---

## 1. Sources de données

Le screener utilise :
* **La Watchlist locale** : Extraite de la base de données locale SQLite (`issuers`), contenant les émetteurs réglementés précédemment récupérés par le logiciel.
* **L'API EODHD** :
  * Liste des tickers : `/api/exchange-symbol-list/` pour récupérer et associer les ISINs/tickers officiels.
  * Historique EOD : `/api/eod/` pour le calcul de la liquidité 3 mois, de la performance 12 mois de l'action et de l'indice de référence.
  * Données fondamentales : `/api/fundamentals/` pour extraire les fiches annuelles d'Income Statement, Balance Sheet et Cash Flow.
  * Taux Forex : `/api/eod/` sur les paires `.FOREX` (ex: `NOKEUR.FOREX`) pour convertir toutes les valeurs financières en Euros (€).
* **Sécurité du Token** : Le token d'accès EODHD est lu à partir du fichier local et n'est **jamais** affiché en clair dans les logs ou les messages d'erreur. Un filtre de sécurité de logging intercepte et masque toute occurrence du token ou des paramètres sensibles (`api_token`, `token`, `apikey`) en les remplaçant par `[REDACTED_TOKEN]`.

---

## 2. Formules et Fallbacks

* **Capitalisation boursière (€)** : `MarketCapitalization` fournie par EODHD, ou calculée par `Dernier cours de clôture * Nombre d'actions en circulation`. Convertie en Euros.
* **Liquidité moyenne 3 mois (€)** : Moyenne quotidienne de `cours_cloture * volume_echange` calculée sur une fenêtre glissante de 90 jours calendaires avant la date d'analyse.
* **Performance 12 mois** : Performance calculée sur les cours de clôture ajustés (`adjusted_close`) à 12 mois d'intervalle (+/- 15 jours de tolérance).
* **Performance relative** : `performance_action_12m - performance_indice_12m`. Si aucun indice n'est fourni, une alerte est émise et le filtre n'est pas bloquant.
* **PER (Price-Earnings Ratio)** : Issu de l'API (`PERatio`/`TrailingPE`) ou calculé par `Capitalisation boursière / Résultat Net`.
* **P/CF (Price to Cash Flow)** :
  * *Formule principale* : `Capitalisation boursière / (Résultat Net + Amortissements & Dépréciations)` (source : `net_income_plus_dna`).
  * *Fallback* : Si les amortissements sont absents, `Capitalisation boursière / Operating Cash Flow` (source : `operating_cash_flow_fallback`, malus de -10 points sur le score de qualité).
* **Marge EBIT** : `EBIT / Chiffre d'affaires` (EBIT = Earnings Before Interest and Taxes).
* **ROE (Return on Equity)** : `Résultat Net / Capitaux Propres (Total Stockholder Equity)`.
* **ROCE (Return on Capital Employed)** :
  * *Formule principale* : `EBIT / (Capitaux Propres + Dette Nette)` (source : `equity_plus_net_debt`). Avec `Dette Nette = Dette Court Terme + Dette Long Terme - Trésorerie & Équivalents`. Si la dette nette est négative (position nette de trésorerie), elle est conservée négative (ce qui augmente le ROCE) et le champ `net_cash_position = true` est positionné.
  * *Fallback* : Si la dette nette ou les capitaux propres ne sont pas calculables, `EBIT / (Total Actifs - Passifs Courants)` (source : `assets_minus_current_liabilities`, malus de -10 points sur le score de qualité).
* **Dette Nette / EBITDA** : `Dette Nette / EBITDA`. Si la dette nette est négative, le critère est automatiquement validé. Si l'EBITDA est négatif ou nul (alors que la dette nette est positive), la société est rejetée.

---

## 3. Seuils de filtrage

Le screener applique les filtres séquentiellement :

| Niveau | Filtre | Seuil | Code de Rejet |
| :--- | :--- | :--- | :--- |
| **Niveau 0** | Type d'actif | Uniquement actions ordinaires (`Common Stock`) | `NOT_COMMON_STOCK` |
| | Historique de prix | $\ge 12$ mois complets | `INSUFFICIENT_PRICE_HISTORY` |
| | Données fondamentales | États financiers annuels complets disponibles | `MISSING_FUNDAMENTALS` / `MISSING_REQUIRED_FIELDS` |
| **Niveau 1** | Capitalisation boursière | $< 12$ milliards € | `MARKET_CAP_TOO_HIGH` |
| | Liquidité quotidienne | Moyenne sur 3 mois $\ge 50\ 000$ € / jour | `INSUFFICIENT_LIQUIDITY` |
| | Momentum relatif | Performance relative sur 12 mois $\ge -20\%$ | `RELATIVE_MOMENTUM_TOO_WEAK` |
| **Niveau 2** | PER | Strictement positif ($> 0$) et $< 12$ | `NEGATIVE_OR_INVALID_PE` / `PE_TOO_HIGH` |
| | P/CF | Strictement positif ($> 0$) et $< 10$ | `PCF_TOO_HIGH` |
| **Niveau 3** | Résultat Net | Strictement positif ($> 0$) | `NEGATIVE_NET_INCOME` |
| | Croissance CA | Croissance yoy positive ($> 0\%$) | `NEGATIVE_REVENUE_GROWTH` |
| | Marge EBIT | Marge EBIT $> 5\%$ | `EBIT_MARGIN_TOO_LOW` |
| | ROE | ROE $> 9\%$ | `ROE_TOO_LOW` |
| | ROCE | ROCE $> 10\%$ | `ROCE_TOO_LOW` |
| **Niveau 4** | Dette | Dette Nette / EBITDA $< 3.0$ (ou dette nette négative) | `NET_DEBT_EBITDA_TOO_HIGH` / `EBITDA_INVALID` |

---

## 4. Score de Qualité des Données

Chaque candidat commence avec un score de **100**. Les malus suivants s'appliquent :
* `-10` par champ fondamental manquant contourné par une formule de repli (fallback P/CF ou ROCE).
* `-20` si conversion monétaire avec un taux de secours (non daté du jour de l'analyse).
* `-20` si les données fondamentales datent de plus de 18 mois par rapport à la date d'analyse.
* `-30` si le momentum relatif n'a pas pu être calculé (absence d'historique de l'indice de référence).
* *Score minimum de 0.*

---

## 5. Comment lancer le screener

Exécutez le script principal via la CLI avec la commande `screen-higgons` :

```bash
# Analyse du marché parisien via l'API REST classique
python main.py screen-higgons --market paris --explain-rejections --index-symbol "^FCHI" --eodhd-backend rest

# Analyse via le protocole MCP (utile en cas de blocage réseau OpenDNS / Cisco Umbrella)
python main.py screen-higgons --market paris --explain-rejections --index-symbol "^FCHI" --eodhd-backend mcp

# Analyse avec bascule automatique (comportement par défaut)
python main.py screen-higgons --market paris --explain-rejections --index-symbol "^FCHI"
```

### Options CLI disponibles pour `screen-higgons` :
* `--market` (Obligatoire) : Place ou univers cible (ex: `paris`, `brussels`, `amsterdam`, `milan`, `oslo`, `lisbon`, `dublin`).
* `--exchange-code` : Code EODHD explicite pour surcharger la place financière.
* `--as-of-date` : Date d'analyse (format `YYYY-MM-DD`, défaut : aujourd'hui).
* `--force` : Ignore le cache local et recharge les données depuis l'API EODHD/MCP.
* `--limit` : Limite le nombre de tickers analysés (utile pour les phases de test).
* `--output` : Chemin du fichier CSV de sortie pour les candidats retenus (défaut : `data/screeners/higgons_candidates.csv`).
* `--json-output` : Chemin du fichier JSON pour un export complet et structuré.
* `--explain-rejections` : Produit également le fichier CSV des rejets.
* `--min-daily-traded-eur` : Modifie le seuil de liquidité minimale par défaut.
* `--index-symbol` : Indice de référence EODHD pour le calcul de la performance relative.
* `--eodhd-backend` : Backend de données EODHD à utiliser : `rest` (API REST directe), `mcp` (serveur MCP EODHD `mcpv2.eodhd.dev`), ou `auto` (teste REST, bascule vers MCP si bloqué par OpenDNS/Cisco Umbrella, défaut : `auto`).

---

## 6. Mode Préfiltrage Minimaliste (Higgons Prefilter)

Pour réduire rapidement l'univers des émetteurs Euronext à une liste courte de candidats sans appeler les fondamentaux lourds (ni parser les rapports financiers), vous pouvez utiliser la commande `prefilter-higgons` :

```bash
# Exemple de commande de préfiltrage sur le marché parisien avec le backend MCP
python main.py prefilter-higgons --market paris --limit 50 --eodhd-backend mcp --explain-rejections --json-output data/screeners/paris_prefilter_test.json
```

Cette commande applique uniquement les filtres suivants basés sur les données faciles à obtenir :
1. **Univers Euronext ciblé** : Sélectionne les sociétés du marché demandé et exclut les instruments non ordinaires (ETF, fonds, warrants, obligations, certificats, etc.).
2. **Données de prix suffisantes** : Rejette si moins de 180 jours de cotation sur les 12 derniers mois.
3. **Liquidité minimale** : Moyenne quotidienne de la valeur échangée sur 3 mois $\ge 50\ 000$ € (configurable via `--min-daily-traded-eur`).
4. **Momentum absolu** : Rejette si la performance absolue sur 12 mois est inférieure à $-40\%$.
5. **Momentum relatif (optionnel)** : Rejette si la performance relative sur 12 mois par rapport à un indice (fourni via `--index-symbol`) est inférieure à $-20\%$.
6. **Capitalisation maximale** : Rejette si la capitalisation boursière (si disponible) dépasse 12 milliards € (configurable via `--max-market-cap-eur`). Si la capitalisation est indisponible, un warning est émis sans bloquer.

### Options CLI disponibles pour `prefilter-higgons` :
* `--market` (Obligatoire) : Place ou univers cible (ex: `paris`, `brussels`, `amsterdam`, `milan`, `oslo`, `lisbon`, `dublin`).
* `--exchange-code` : Code EODHD explicite pour surcharger la place financière.
* `--as-of-date` : Date d'analyse (format `YYYY-MM-DD`, défaut : aujourd'hui).
* `--force` : Ignore le cache local et recharge les données depuis l'API EODHD.
* `--limit` : Limite le nombre de tickers analysés pour le test.
* `--output` : Chemin du fichier CSV de sortie pour les candidats (défaut : `data/screeners/prefilter_candidates_{market}_{date}.csv`).
* `--json-output` : Chemin du fichier JSON pour un export complet et structuré.
* `--explain-rejections` : Produit également le fichier CSV des rejets (sous le nom `prefilter_rejections_{market}_{date}.csv`).
* `--min-daily-traded-eur` : Seuil de liquidité quotidienne minimale en euros (défaut : 50 000).
* `--max-market-cap-eur` : Capitalisation maximale autorisée en euros (défaut : 12 000 000 000).
* `--index-symbol` : Ticker de l'indice de référence EODHD pour le momentum relatif.
* `--eodhd-backend` : Backend de données EODHD à utiliser : `rest`, `mcp`, ou `auto` (défaut : `auto`).

---

## 7. Commande de diagnostic réseau

Si vous rencontrez des problèmes de connexion avec l'API EODHD, vous pouvez lancer un diagnostic réseau complet :

```bash
python main.py diagnose-eodhd
```

Cette commande vérifie :
1. La bonne lecture du jeton EODHD local.
2. Le filtrage ou blocage éventuel par Cisco Umbrella / OpenDNS.
3. La validité du jeton avec des requêtes REST simulées (fictives et réelles) pour valider que le jeton n'apparaît **jamais** en clair dans les logs ou les traces d'exception.
4. L'accessibilité du serveur MCP EODHD.
5. La récupération des outils MCP, des cours historiques de test (`AAPL.US`), et des données fondamentales.

Elle conclut avec des statuts clairs comme `REST_BLOCKED_OPENDNS`, `MCP_OK`, `TOKEN_PROBABLY_VALID` ou `TOKEN_NOT_TESTABLE_BECAUSE_REST_BLOCKED`.

---

## 8. Interprétation des fichiers de sortie

### 1. Fichier Candidats (`higgons_candidates.csv`)
Ce fichier liste toutes les sociétés ayant passé **l'intégralité** des filtres de la stratégie William Higgons. Il contient l'ensemble des ratios calculés ainsi que les métadonnées (ISIN, pays, devises, score de qualité des données, date des sources). 
Il comporte également des colonnes vides d'analyse qualitative destinées à de futures étapes IA (actionnariat familial, holdings diversifiées, acquisitions récentes, etc.).

### 2. Fichier Rejets (`higgons_rejections.csv`)
Chaque entreprise rejetée y figure de manière transparente. Les colonnes précisent :
* `rejected_at_filter` : Le niveau et le nom du filtre qui a rejeté l'entreprise.
* `rejection_code` : Le code technique associé au rejet.
* `rejection_reason` : L'explication humaine détaillée de la cause de rejet.
* `missing_fields` : La liste des variables manquantes si le rejet est causé par une insuffisance de données.
* `raw_values_snapshot` : Un instantané structuré au format JSON des variables financières au moment du calcul.
