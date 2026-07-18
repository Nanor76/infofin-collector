# InfoFin

Outil Python 3.12 de veille des rapports financiers légaux pour une watchlist
Euronext. Les décisions de sélection reposent uniquement sur des règles et les
métadonnées des sources officielles.

## Fonctionnalités

- import d'un CSV `;` avec les colonnes `Name`, `ISIN`, `Symbol`, `Market`;
- validation des ISIN par `[A-Z]{2}[A-Z0-9]{9}[0-9]`;
- normalisation de Paris, Oslo, Amsterdam, Bruxelles, Lisbonne, Dublin et des marchés Milan, y compris
  `AMS`, `XAMS`, `Euronext Growth Amsterdam`, `MTA`, `AIM Italia` et
  `Borsa Italiana`, ainsi que `Brussels`, `BRU`, `XBRU` et
  `Alternext Brussels`, `Lisbon`, `LIS`, `Euronext Growth Lisbon`, `PSI`
  et `Bolsa de Lisboa`, ainsi que `Dublin`, `ISE`, `Irish Stock Exchange`,
  `Euronext Growth Dublin` et `Global Exchange Market`;
- stockage SQLite des émetteurs, documents et runs;
- watcher quotidien idempotent avec historique `watch_runs`;
- découverte source-first de liens de documents financiers par marché et par
  période, sans téléchargement serveur des fichiers;
- connecteur France Info-financière/OpenDataSoft Explore API 2.1;
- connecteur Oslo Euronext Live / NewsWeb avec découverte runtime;
- connecteur Italie EMARKET STORAGE avec 1INFO en fallback de découverte;
- connecteur Netherlands AFM Financial Reporting Register avec contrôle du
  Home Member State;
- connecteur Belgique FSMA STORI via l'API officielle utilisée par
  l'interface publique, avec fallback HTML BeautifulSoup;
- connecteur Portugal CMVM/SDI via les actions publiques du portail officiel,
  avec fallback HTML BeautifulSoup;
- connecteur Ireland Euronext Dublin OAM / Euronext Direct via les endpoints
  JSON publics utilisés par l'interface, avec fallback HTML BeautifulSoup;
- classification déterministe des rapports annuels, semestriels, DEU/URD et ESEF;
- téléchargement PDF, XHTML, XML et ZIP avec timeouts et retries;
- déduplication globale par SHA256;
- stockage sous
  `data/raw/{market}/{isin}/{date}_{type}_{hash8}.{ext}`, avec les documents
  italiens sous `data/raw/italy/{isin}/` et néerlandais sous
  `data/raw/netherlands/{isin}/`, les documents belges sous
  `data/raw/belgium/{isin}/` et les documents portugais sous
  `data/raw/portugal/{isin}/`, et les documents irlandais sous
  `data/raw/ireland/{isin}/`.

## Sources officielles

Le connecteur France utilise le portail officiel Info-financière /
OpenDataSoft. Le dataset par défaut `flux-amf-new-prod` n'est pas considéré
comme indisponible a priori. La racine du portail, les racines de repli et le
dataset sont configurables dans `.env`:

```dotenv
AMF_ODS_BASE_URL=https://www.info-financiere.gouv.fr
AMF_ODS_FALLBACK_BASE_URLS=https://www.info-financiere.gouv.fr;https://data.economie.gouv.fr
AMF_ODS_DATASET=flux-amf-new-prod
```

Pour chaque racine, le diagnostic teste:

```text
/api/explore/v2.1/catalog/datasets/{dataset}/records
/api/explore/v2.1/catalog/datasets/{dataset}/exports/json
/api/records/1.0/search/?dataset={dataset}
/api/explore/v2.1/catalog/datasets?where=search("flux-amf")
```

Le connecteur ne dépend pas de noms de colonnes exacts. Il normalise les
accents et la ponctuation pour détecter les rôles ISIN, société, titre de
fichier, type et sous-type d'information, URL de récupération et dates.

En cas d'erreur HTTP, réseau ou de réponse JSON invalide, le détail est
journalisé et le connecteur passe en état `degraded`. Le run est alors
`partial`, mais les autres marchés continuent d'être traités.

Le connecteur Oslo utilise comme source primaire la page publique Euronext
Live `Company regulated news`. Il découvre dans le HTML les actions de
formulaire, IDs de topics, pagination, réglages Drupal et endpoints exposés.
Les notices sont ensuite enrichies par le fragment HTML public associé à leur
`data-node-nid`; ce fragment fournit le texte, l'ISIN, l'URL canonique, le
lien NewsWeb et les attachments Euronext.

NewsWeb est consulté uniquement si son `robots.txt` est accessible et autorise
la page. Un refus, une erreur TLS ou un HTTP 403 n'est jamais contourné:
Euronext reste le fallback HTML officiel pour le texte et les attachments.

Configuration Oslo:

```dotenv
OSLO_EURONEXT_NEWS_URL=https://live.euronext.com/en/markets/oslo/equities/company-news
OSLO_NEWSWEB_BASE_URL=https://newsweb.oslobors.no
OSLO_RATE_LIMIT_SECONDS=0.5
OSLO_LOOKBACK_DAYS=400
```

Le connecteur Italie utilise en priorité les listings publics EMARKET STORAGE
`/it/documenti` et `/it/comunicati-finanziari`. Il parse les notices Drupal
avec BeautifulSoup, suit la pagination `page`, extrait le protocole, la date,
la société, le titre et les liens directs PDF/XHTML/ZIP/XBRI. Les catégories
`1.1`, `1.2` et `DOAG` sont interrogées globalement puis les notices sont
associées aux émetteurs de la watchlist par nom normalisé et symbole.

1INFO est exposé comme fallback de découverte. Son portail public étant une
application JavaScript sans API HTML exploitable vérifiée, son état est
`stub`; cet état ne dégrade pas EMARKET STORAGE. Borsa Italiana reste une
surface secondaire de découverte pour Euronext Growth Milan.

Configuration Italie:

```dotenv
ITALY_EMARKET_HOME_URL=https://www.emarketstorage.it/
ITALY_DOCUMENTS_URL=https://www.emarketstorage.it/it/documenti
ITALY_PRESS_RELEASES_URL=https://www.emarketstorage.it/it/comunicati-finanziari
ITALY_1INFO_URL=https://www.1info.it/PORTALE1INFO
ITALY_BORSA_COMPANY_BASE_URL=https://www.borsaitaliana.it/borsa/azioni/scheda
ITALY_RATE_LIMIT_SECONDS=0.5
ITALY_LOOKBACK_DAYS=400
ITALY_MAX_PAGES=2
ITALY_EMARKET_VERIFY_SSL=true
```

Le connecteur Netherlands utilise le registre officiel AFM `Financial
reporting`. L'export XML complet est prioritaire car il contient l'identifiant
AFM, la date de dépôt, l'émetteur, l'exercice, le type NL/EN et le nom de
fichier. Le CSV complet sert de secours. Si les exports ne sont pas
exploitables, le listing HTML et son endpoint officiel
`/api/sitecore/RegisterOverview/PagedRegisters` sont parsés avec
BeautifulSoup.

Le lien binaire est résolu uniquement pour les notices appariées en suivant
`details?id=...`. Le registre `Home member state` est utilisé pour enrichir
l'émetteur et écarter un résultat lorsqu'un autre État membre est
explicitement déclaré.

Configuration Netherlands:

```dotenv
NETHERLANDS_AFM_REGISTER_URL=https://www.afm.nl/en/sector/registers/meldingenregisters/financiele-verslaggeving
NETHERLANDS_AFM_EXPORT_TYPE=e8825b05-4004-4301-b736-651e8c61053d
NETHERLANDS_HOME_MEMBER_STATE_URL=https://www.afm.nl/en/sector/registers/meldingenregisters/home-member-state
NETHERLANDS_HOME_MEMBER_STATE_EXPORT_TYPE=6b365727-6220-452f-83b1-86a179d70d12
NETHERLANDS_RATE_LIMIT_SECONDS=0.2
NETHERLANDS_LOOKBACK_DAYS=900
```

Le connecteur Belgique utilise STORI, le mécanisme officiel FSMA de stockage
de l'information réglementée. L'interface publique expose l'API JSON
`https://webapi.fsma.be/api/v1/en/stori`: les recherches passent par
`POST /result`, avec pagination `startRowIndex` / `pageSize`, et les fichiers
par `GET /download?fileDataId=...`. Les réponses fournissent l'émetteur, le
type, les dates, le LEI, les ISIN, le marché et les fichiers PDF, XHTML, ZIP
ou XBRI.

Le parseur HTML BeautifulSoup reste disponible si le rendu STORI redevient
serveur. L'interface actuelle étant rendue côté client, l'API vérifiée est le
chemin primaire. Le connecteur Belgique ne retourne jamais `stub`.

Configuration Belgique:

```dotenv
BELGIUM_FSMA_STORI_BASE_URL=https://www.fsma.be/en/stori
BELGIUM_RATE_LIMIT_SECONDS=0.2
BELGIUM_LOOKBACK_DAYS=900
```

Le connecteur Portugal utilise le Sistema de Difusão de Informação de la
CMVM. Le portail actuel est une application OutSystems: le connecteur découvre
la version du module à l'exécution, appelle les actions JSON publiques des
listes `Contas anuais` et `Contas semestrais`, puis conserve un parseur HTML
tolérant en repli. Il ne suppose pas que ces actions constituent une API
publique stable.

Les notices exposent l'identifiant CMVM, la date, le titre, le nom de fichier,
le drapeau ESEF/ZIP et une URL chiffrée officielle. Les PDF, XHTML ESEF et ZIP
sont récupérés par les actions publiques utilisées par les visualiseurs CMVM,
sans login. L'ancienne surface `web3.cmvm.pt/sdi` n'est pas utilisée: elle
retourne actuellement HTTP 404.

Configuration Portugal:

```dotenv
PORTUGAL_CMVM_BASE_URL=https://www.cmvm.pt/PInstitucional
PORTUGAL_CMVM_SDI_URL=https://www.cmvm.pt/PInstitucional/Content?Input=BD77C8DEEB2702712300D99098915461C2A4F65FE4368A561E6AB83D1E580C4D
PORTUGAL_RATE_LIMIT_SECONDS=0.5
PORTUGAL_LOOKBACK_DAYS=900
```

Le connecteur Ireland utilise Euronext Direct, exploité par Euronext Dublin
comme mécanisme officiellement désigné de stockage de l'information
réglementée. La
[Central Bank of Ireland](https://www.centralbank.ie/docs/default-source/regulation/industry-market-sectors/securities-markets/transparency-regulation/regulatory-requirements-guidance/guidance-on-transparency-regulatory-framework---april-2022.pdf?sfvrsn=2)
est l'autorité compétente pour les Transparency Regulations et
[Euronext Dublin](https://www.euronext.com/en/about-euronext/markets/dublin)
opère l'OAM irlandais. L'interface publique expose actuellement deux
endpoints JSON sans authentification:

```text
POST https://direct.euronext.com/api/PublicAnnouncements/OAMs
POST https://direct.euronext.com/api/PublicAnnouncements/RIS
```

Le corps accepte `startDate`, `endDate`, `page`, `firstLetter` et
`companyName`. Les réponses exposent `records`, `totalItems`, `currentPage`,
`numberOfPages`, les dates, l'émetteur, le titre, la catégorie réglementaire
OAM et les documents. Les fichiers sont servis par les routes publiques
`/api/PublicAnnouncements/OAMDocument/...` et
`/api/PublicAnnouncements/RISDocument/...`.

Le JSON est prioritaire, sans supposer que son contrat restera stable. Un
parseur HTML BeautifulSoup tolérant est conservé pour les tableaux rendus
côté serveur. Le connecteur Ireland ne retourne jamais `stub`.

Configuration Ireland:

```dotenv
IRELAND_EURONEXT_DIRECT_BASE_URL=https://direct.euronext.com
IRELAND_EURONEXT_DUBLIN_URL=https://www.euronext.com/en/about-euronext/markets/dublin
IRELAND_RATE_LIMIT_SECONDS=0.5
IRELAND_LOOKBACK_DAYS=900
```

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Aucun secret n'est requis.

## Commandes

Importer le fichier fourni par Euronext:

```powershell
python main.py import-csv euronext_companies.csv
```

Le fichier peut contenir des lignes de métadonnées après l'en-tête: toute
ligne sans ISIN valide est ignorée.

## Recherche de liens sans téléchargement

Le mode `discover-market-documents` interroge les sources officielles en
source-first et exporte uniquement les liens vers les documents. Il ne
télécharge pas les PDF, XHTML, XML ou ZIP: l'utilisateur ouvre ou télécharge
ensuite le document depuis sa source officielle.

Recherche sur une place:

```powershell
python main.py discover-market-documents --market "Euronext Paris" --date-from 2026-01-01 --date-to 2026-06-30 --format csv
```

Recherche sur toutes les places supportées:

```powershell
python main.py discover-market-documents --all --date-from 2026-01-01 --date-to 2026-06-30 --format json --dedupe-url
```

Les exports sont écrits par défaut dans `exports/` avec les colonnes marché,
source, identifiant source, date de publication, période, exercice, type de
document, classification, titre, URL, émetteur, ISIN, LEI, catégorie source et
confiance de date.

La CLI et la webapp partagent le même service `DocumentSearchService` dans
`webapp/services/document_search.py`.

## Webapp de recherche

La webapp locale permet de lancer des recherches multi-marchés, de suivre la
progression par place, de filtrer les résultats et d'exporter des liens
officiels sans téléchargement serveur.

Lancer la webapp:

```powershell
python main.py serve
python main.py serve --host 127.0.0.1 --port 8000
```

Alternative directe avec Uvicorn:

```powershell
python -m uvicorn webapp.app:create_app --factory --reload --host 127.0.0.1 --port 8000
```

Variables d'environnement web (voir aussi `.env.example`):

```dotenv
INFOFIN_WEB_HOST=127.0.0.1
INFOFIN_WEB_PORT=8000
INFOFIN_WEB_WORKERS=2
INFOFIN_WEB_MAX_PERIOD_DAYS=370
INFOFIN_WEB_MAX_CANDIDATES=100000
```

Purge des recherches web anciennes:

```powershell
python main.py purge-web-searches --older-than-days 30
```

Documentation de conception:

- [`ARCHITECTURE_WEBAPP.md`](ARCHITECTURE_WEBAPP.md): architecture, modules,
  modèle de données, API, UI et critères MVP;
- [`PLAN_DEVELOPPEMENT_WEBAPP.md`](PLAN_DEVELOPPEMENT_WEBAPP.md): plan de
  développement détaillé, lots d'implémentation et tests;
- [`TESTING.md`](TESTING.md): execution des tests, integrations live et
  convention des `data-testid` Playwright.

La cible est une application locale FastAPI + Jinja2/HTMX qui lance des jobs
de recherche, affiche la progression par marché, permet de filtrer les liens
par type de document, période, source, texte libre, ISIN et format, puis
exporte les résultats en CSV ou JSON. Le téléchargement des documents reste à
la charge de l'utilisateur via les liens officiels affichés.

## Daily operations / troubleshooting

Vue opérationnelle quotidienne:

```powershell
python main.py status
```

La commande affiche les émetteurs et documents par marché, source et type, le
dernier watcher de chaque marché, les téléchargements et erreurs récents, la
taille de `data/raw` et les sources actuellement `degraded` ou `unavailable`.

Diagnostic consolidé de toutes les sources:

```powershell
python main.py healthcheck
```

Le rapport est écrit dans
`reports/healthcheck_YYYYMMDD_HHMMSS.md`. Une source `degraded` est signalée
sans rendre la commande fatale; une source critique `unavailable` produit un
code de sortie non nul.

Veille quotidienne consolidée avec une limite de 50 MiB:

```powershell
python main.py watch --all --max-download-mb 50 --dry-run
python main.py watch --all --max-download-mb 50
```

Le rapport unique est écrit dans
`reports/watch_all_YYYYMMDD_HHMMSS.md`. Un document dépassant la limite est
enregistré avec le statut `skipped_too_large`, sans interrompre les autres
téléchargements. Le mode quotidien interroge chaque source une fois par
fenêtre de dates, puis matche localement les notices avec la watchlist par
ISIN, nom et symbole. Il ne lance pas une recherche HTTP par émetteur.

Les limites quotidiennes par défaut sont:

- fenêtre récente de 7 jours avec `--lookback-days`;
- 1000 notices au plus par source avec `--max-candidates-per-source`;
- 100 documents traités au plus avec `--max-documents-per-run`;
- arrêt avant de dépasser 500 appels HTTP, sauf confirmation explicite avec
  `--confirm-large-run`.

Le rapport contient une section `Request efficiency` avec, pour chaque
source, le mode utilisé, les appels HTTP estimés et réels, les notices
scannées, les émetteurs matchés, les candidats, téléchargements, doublons et
la durée.

Exports du dernier jour de téléchargement disponible:

```powershell
python main.py export-latest --format csv
python main.py export-latest --format json
```

Les fichiers sont créés dans `exports/latest_documents_YYYYMMDD.csv` ou
`exports/latest_documents_YYYYMMDD.json`.

Préparer une notification sans envoyer d'email:

```powershell
python main.py watch --all --notify-email operations@example.com
```

Un fichier `.eml` est créé à côté du rapport Markdown. Il contient le résumé,
les nouveaux documents, les sources en erreur et le lien local vers le
rapport. Aucun serveur SMTP n'est contacté.

En cas d'incident, exécuter d'abord `status`, puis `healthcheck`. Utiliser
ensuite `diagnose-source <source>` pour le détail JSON d'une source précise.
Les erreurs de watcher et les documents trop gros sont conservés dans
`operational_events`; les états de source sont conservés dans
`source_states` et `source_health_checks`.

Vérifier uniquement Paris:

```powershell
python main.py check --market "Euronext Paris"
```

Vérifier tous les marchés importés:

```powershell
python main.py check --all
```

Lancer le watcher quotidien France:

```powershell
python main.py watch --market "Euronext Paris"
```

Lancer une veille consolidée France + Oslo + Italie + Netherlands + Belgique,
Portugal et Ireland:

```powershell
python main.py watch --all --dry-run
python main.py watch --all
```

Lancer le watcher Oslo:

```powershell
python main.py watch --market "Oslo Børs" --limit 5 --dry-run
python main.py watch --market "Oslo Børs" --limit 5
```

Lancer le watcher Italie:

```powershell
python main.py watch --market "Euronext Milan" --limit 10 --dry-run
python main.py watch --market "Euronext Milan" --limit 10
python main.py watch --market "Euronext Growth Milan" --limit 10 --dry-run
```

Lancer le watcher Ireland:

```powershell
python main.py watch --market "Euronext Dublin" --limit 10 --dry-run
python main.py watch --market "Euronext Dublin" --limit 10
```

Lancer le watcher Amsterdam:

```powershell
python main.py watch --market "Euronext Amsterdam" --limit 10 --dry-run
python main.py watch --market "Euronext Amsterdam" --limit 10
```

Lancer le watcher Bruxelles:

```powershell
python main.py watch --market "Euronext Brussels" --limit 10 --dry-run
python main.py watch --market "Euronext Brussels" --limit 10
python main.py watch --market "Euronext Growth Brussels" --limit 10 --dry-run
```

Lancer le watcher Lisbonne:

```powershell
python main.py watch --market "Euronext Lisbon" --limit 10 --dry-run
python main.py watch --market "Euronext Lisbon" --limit 10
```

Options disponibles:

```powershell
python main.py watch --market "Euronext Paris" `
  --lookback-days 7 `
  --max-candidates-per-source 500 `
  --max-documents-per-run 20 `
  --dry-run
```

- `--since YYYY-MM-DD` conserve uniquement les documents dont la date de
  publication est connue et supérieure ou égale à cette date;
- `--limit N` plafonne le nombre de documents traités pendant le run, tout en
  restant un alias compatible de `--max-documents-per-run`;
- `--dry-run` recherche et filtre les documents sans les télécharger ni les
  insérer en base;
- `--max-download-mb N` remplace la limite par défaut de 100 MiB pour le run;
- `--notify-email adresse` génère un `.eml` local sans envoi SMTP;
- `--lookback-days N` règle la fenêtre quotidienne, à 7 jours par défaut;
- `--max-candidates-per-source N` borne les notices chargées par source;
- `--max-documents-per-run N` borne les documents traités globalement;
- `--backfill` ou `--issuer-mode` active explicitement les recherches
  émetteur par émetteur pour un historique ou un diagnostic ciblé;
- `--confirm-large-run` autorise un run estimé ou observé à plus de 500
  appels HTTP.

Exemple de backfill lourd et volontaire:

```powershell
python main.py watch --market "Euronext Paris" `
  --backfill `
  --since 2025-01-01 `
  --confirm-large-run
```

Avec `--all`, seuls les marchés supportés présents dans la watchlist sont
traités. L'ordre est France, Oslo, les segments Milan, Amsterdam, Bruxelles
et Euronext Growth Brussels. La limite est globale à l'exécution, les
connecteurs sont regroupés par source officielle et une source dégradée
n'empêche pas les autres marchés de continuer. Un unique rapport agrège les
compteurs et les détails de tous les marchés.

Le watcher traite les rapports financiers annuels, semestriels et
intermédiaires, les documents d'enregistrement universels et les packages
ESEF. Une URL déjà connue est ignorée avant tout appel de téléchargement. Si
une nouvelle URL retourne un contenu dont le SHA256 existe déjà, elle est
enregistrée comme alias du document: les runs suivants ne la retéléchargent
plus.

Diagnostiquer la source France sans lancer de téléchargement:

```powershell
python main.py diagnose-source france
python main.py diagnose-source france --dataset flux-amf-new-prod
```

La sortie JSON contient la base et le dataset, chaque endpoint complet testé,
son statut HTTP, un extrait de réponse en cas d'erreur, l'endpoint retenu, le
nombre d'enregistrements, les champs observés et un exemple de record.

Rechercher les datasets AMF présents dans les catalogues configurés:

```powershell
python main.py discover-source france --query flux-amf
```

La liste contient l'identifiant, le titre, le nombre d'enregistrements et la
racine de portail de chaque candidat.

Diagnostiquer et découvrir la source Oslo:

```powershell
python main.py diagnose-source oslo
python main.py discover-source oslo --query "annual financial"
python main.py discover-issuer oslo --symbol 2020 --name "2020 BULKERS"
```

`discover-issuer` résout l'instrument XOSL via la recherche publique Euronext,
puis persiste `oslo_issuer_id`, `newsweb_url` et
`euronext_company_url` dans `issuers`.

Diagnostiquer et découvrir les sources Italie:

```powershell
python main.py diagnose-source italy
python main.py discover-source italy --query "relazione finanziaria annuale"
python main.py discover-issuer italy --symbol LR --name "LANDI RENZO"
```

L'émetteur doit déjà être importé pour que `discover-issuer italy` persiste
`italy_storage_provider`, `italy_emarket_url`, `italy_1info_url` et
`borsa_italiana_company_url`.

Diagnostiquer et découvrir la source Netherlands:

```powershell
python main.py diagnose-source netherlands
python main.py discover-source netherlands --query "annual financial"
python main.py discover-issuer netherlands `
  --symbol AALB `
  --name "AALBERTS NV"
```

L'émetteur doit déjà être importé pour persister
`netherlands_afm_issuer_url`, `netherlands_afm_detail_url`,
`netherlands_home_member_state` et `netherlands_afm_record_id`.

Diagnostiquer et découvrir la source Belgique:

```powershell
python main.py diagnose-source belgium
python main.py discover-source belgium --query "annual financial report"
python main.py discover-issuer belgium `
  --symbol ABI `
  --name "AB INBEV"
```

L'émetteur doit déjà être importé pour persister
`belgium_fsma_stori_url`, `belgium_fsma_detail_url`,
`belgium_home_member_state` et `belgium_fsma_record_id`.

Activer les logs de diagnostic:

```powershell
python main.py --log-level DEBUG check --all
```

## France source discovery / troubleshooting

Commencer par la découverte, puis diagnostiquer l'identifiant retenu:

```powershell
python main.py discover-source france --query flux-amf
python main.py diagnose-source france --dataset flux-amf-new-prod
```

Le contrôle manuel minimal dans PowerShell est:

```powershell
$base = "https://www.info-financiere.gouv.fr"
$dataset = "flux-amf-new-prod"
$uri = "$base/api/explore/v2.1/catalog/datasets/$dataset/records?limit=1"
$response = Invoke-RestMethod -Uri $uri -TimeoutSec 30
$response.total_count
$response.results[0] | Format-List
```

Une réponse conforme à Explore API 2.1 contient `total_count` et `results`.
Le catalogue peut être contrôlé séparément:

```powershell
$catalog = "$base/api/explore/v2.1/catalog/datasets" +
  '?limit=20&where=search("flux-amf")'
(Invoke-RestMethod -Uri $catalog -TimeoutSec 30).results |
  Select-Object dataset_id, @{n="title";e={$_.metas.default.title}},
    @{n="count";e={$_.metas.default.records_count}}
```

En cas d'échec, vérifier les valeurs réellement chargées depuis
`AMF_ODS_BASE_URL`, `AMF_ODS_FALLBACK_BASE_URLS` et `AMF_ODS_DATASET`.
Le connecteur journalise distinctement les erreurs réseau, HTTP et parsing,
puis passe en état `degraded` sans bloquer les autres marchés.

## Oslo source discovery / troubleshooting

Commencer par le diagnostic du listing marché:

```powershell
python main.py diagnose-source oslo
```

Une source exploitable retourne `ready` ou `degraded` avec `called_url`,
`http_status`, `detected_count`, `topics` et `example_notice`. Le listing
officiel attendu est:

```text
https://live.euronext.com/en/markets/oslo/equities/company-news
```

Inspecter ensuite les surfaces découvertes:

```powershell
python main.py discover-source oslo --query "annual financial"
```

La sortie liste le format détecté (`HTML`, `JSON` ou `HTML fragment`), la
pagination et les champs récupérables. Le endpoint Drupal Views JSON n'est
utilisé que s'il est réellement découvert et vérifié; le parseur HTML
BeautifulSoup reste le fallback.

Résoudre un émetteur avant la veille si les noms de la watchlist sont
ambigus:

```powershell
python main.py discover-issuer oslo --symbol 2020 --name "2020 BULKERS"
```

Contrôle manuel minimal:

```powershell
$url = "https://live.euronext.com/en/markets/oslo/equities/company-news"
$response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 30
$response.StatusCode
$response.Content | Select-String "Company press releases"
```

En cas d'échec, vérifier les quatre variables `OSLO_*`, puis relancer avec
`--log-level DEBUG`. Le connecteur distingue les erreurs réseau, HTTP,
robots et parsing. Une erreur sur un émetteur ne bloque pas les suivants.
Il n'effectue aucun login et ne contourne ni robots.txt, ni TLS, ni HTTP 403.

## Italy source discovery / troubleshooting

Commencer par le diagnostic EMARKET STORAGE:

```powershell
python main.py diagnose-source italy
```

Le diagnostic teste la page d'accueil, `/it/documenti`,
`/it/comunicati-finanziari`, la page suivante, un lien de document direct et
les catégories visibles. Une sortie exploitable contient un
`example_document` réel et les contrôles suivants:

```text
home_accessible
documents_accessible
press_releases_accessible
pagination
direct_document_link
direct_document_reachable
categories_visible
real_notices
```

Les états sont normalisés:

- `ready`: notices, catégories, pagination et liens directs exploitables;
- `degraded`: HTML accessible mais parsing ou capacité incomplet;
- `unavailable`: listings EMARKET STORAGE inaccessibles;
- `stub`: réservé à 1INFO tant qu'aucune API publique ou HTML serveur
  exploitable n'est disponible.

Vérifier ensuite la recherche et les exemples effectivement parsés:

```powershell
python main.py discover-source italy `
  --query "relazione finanziaria annuale"
```

La sortie distingue EMARKET STORAGE primaire, 1INFO secondaire et la surface
Borsa Italiana pour Euronext Growth Milan. Pour résoudre une société ambiguë:

```powershell
python main.py discover-issuer italy --symbol LR --name "LANDI RENZO"
```

Contrôle manuel minimal:

```powershell
$url = "https://www.emarketstorage.it/it/documenti"
$response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 30
$response.StatusCode
$response.Content | Select-String `
  "data-protocollo|sites/default/files/(comunicati|xbrl)"
```

Pour prouver l'idempotence, exécuter deux fois la même veille. Le premier run
doit écrire les fichiers sous `data/raw/italy/{isin}`; le second doit afficher
`Téléchargés: 0` et compter les mêmes URLs comme doublons. En cas d'échec,
relancer avec `--log-level DEBUG`, contrôler les variables `ITALY_*`, puis
vérifier que le HTML contient encore `.views-row`, `data-protocollo` ou des
liens sous `/sites/default/files/`.

## Netherlands source discovery / troubleshooting

Commencer par le diagnostic AFM:

```powershell
python main.py diagnose-source netherlands
```

Le diagnostic teste la page du registre financier, les exports CSV et XML,
la page et l'export XML Home Member State, une page détail réelle et un lien
de téléchargement. La sortie contient le nombre de records, les champs
détectés, un exemple de notice et les contrôles:

```text
financial_page
csv_export
xml_export
home_member_state_page
home_member_state_export
real_records
detail_page
automatic_download
```

Les états sont normalisés:

- `ready`: export ou listing exploitable, records réels et document
  téléchargeable automatiquement;
- `degraded`: registre accessible avec records, mais résolution ou
  téléchargement automatique incomplet;
- `unavailable`: page et exports AFM inaccessibles.

Le connecteur AFM ne retourne jamais `stub`. Rechercher ensuite des notices
réelles:

```powershell
python main.py discover-source netherlands --query "annual financial"
python main.py discover-issuer netherlands `
  --symbol AALB `
  --name "AALBERTS NV"
```

Contrôle manuel minimal des exports complets:

```powershell
$financialType = "e8825b05-4004-4301-b736-651e8c61053d"
$xml = "https://www.afm.nl/export.aspx?format=xml&type=$financialType"
$response = Invoke-WebRequest -Uri $xml -UseBasicParsing -TimeoutSec 60
$response.StatusCode
([xml]$response.Content).register.vermelding.Count
```

Pour prouver le parcours complet et l'idempotence:

```powershell
python main.py watch --market "Euronext Amsterdam" --limit 10 --dry-run
python main.py watch --market "Euronext Amsterdam" --limit 10
python main.py watch --market "Euronext Amsterdam" --limit 10
```

Le premier run télécharge sous `data/raw/netherlands/{isin}`. Le second doit
indiquer zéro téléchargement et compter les URL déjà connues comme doublons.
En cas d'échec, contrôler les variables `NETHERLANDS_*`, puis relancer avec
`--log-level DEBUG`.

## Belgium source discovery / troubleshooting

Commencer par le diagnostic STORI:

```powershell
python main.py diagnose-source belgium
```

Le diagnostic vérifie la page publique, la liste des émetteurs, les types de
documents, la recherche JSON, la pagination, des notices réelles et des liens
PDF/XHTML/ZIP. Il ouvre au moins un téléchargement direct sans enregistrer le
contenu. Une sortie exploitable contient `example_notice`, `total_count`,
`fields`, `formats` et les contrôles:

```text
stori_accessible
public_search
pagination
real_notices
download_links
automatic_download
```

Les états sont normalisés:

- `ready`: notices réelles et document téléchargeable;
- `degraded`: STORI accessible mais API, parsing ou téléchargement partiel;
- `unavailable`: page publique et recherche STORI inaccessibles.

Le connecteur Belgique ne retourne pas `stub`. Rechercher ensuite des notices
et résoudre un émetteur:

```powershell
python main.py discover-source belgium --query "annual financial report"
python main.py discover-issuer belgium --symbol ABI --name "AB INBEV"
```

Contrôle manuel minimal de l'API utilisée par l'interface officielle:

```powershell
$api = "https://webapi.fsma.be/api/v1/en/stori"
$body = @{
  startRowIndex = 0
  pageSize = 10
  sortDirection = "Descending"
  documentTypeId = "9813c451-9fd4-41ba-ba7d-4e0dda0d3051"
  isDocumentTypeGroup = $false
} | ConvertTo-Json
$result = Invoke-RestMethod `
  -Method Post `
  -Uri "$api/result" `
  -ContentType "application/json" `
  -Body $body
$result.resultCount
$result.storiResultItems[0]
```

Pour prouver le parcours complet et l'idempotence:

```powershell
python main.py watch --market "Euronext Brussels" --limit 10 --dry-run
python main.py watch --market "Euronext Brussels" --limit 10
python main.py watch --market "Euronext Brussels" --limit 10
```

Le premier run écrit sous `data/raw/belgium/{isin}`. Le second doit indiquer
zéro téléchargement et compter les URL connues comme doublons. En cas
d'échec, contrôler les variables `BELGIUM_*` et relancer avec
`--log-level DEBUG`.

## Portugal source discovery / troubleshooting

Commencer par le diagnostic CMVM/SDI:

```powershell
python main.py diagnose-source portugal
python main.py discover-source portugal --query "relatório financeiro anual"
python main.py discover-issuer portugal --symbol ALTR --name "Altri SGPS SA"
```

Le diagnostic vérifie le portail CMVM, la page SDI, les actions JSON
annuelles/semestrielles, la pagination `StartIndex`, les champs visibles, des
notices réelles et les formats PDF/XHTML/ZIP. Il ouvre un PDF officiel en
mémoire pour prouver que l'action de téléchargement est exploitable.

Les états sont normalisés:

- `ready`: notices réelles et document téléchargeable;
- `degraded`: portail SDI accessible mais API, parsing ou téléchargement
  partiel; le repli HTML est journalisé;
- `unavailable`: portail CMVM/SDI inaccessible.

Le connecteur Portugal ne retourne jamais `stub`. La version du module
OutSystems est relue via `moduleservices/moduleversioninfo`. Si la CMVM change
le contrat d'une action, le connecteur passe en `degraded`, tente le repli HTML
et journalise l'endpoint concerné. En cas d'échec, relancer avec
`--log-level DEBUG`, contrôler les variables `PORTUGAL_*` et vérifier que le
portail rend toujours les pages `Contas anuais` et `Contas semestrais`.

Pour prouver le parcours complet et l'idempotence:

```powershell
python main.py watch --market "Euronext Lisbon" --limit 10 --dry-run
python main.py watch --market "Euronext Lisbon" --limit 10
python main.py watch --market "Euronext Lisbon" --limit 10
```

Le premier run écrit sous `data/raw/portugal/{isin}`. Le second doit indiquer
zéro téléchargement et compter les URL déjà connues comme doublons.

## Ireland source discovery / troubleshooting

Commencer par le diagnostic Euronext Dublin OAM / Euronext Direct:

```powershell
python main.py diagnose-source ireland
python main.py discover-source ireland --query "annual report"
python main.py discover-issuer ireland `
  --symbol BIRG `
  --name "BANK OF IRELAND GP"
```

Le diagnostic vérifie les pages publiques Euronext Direct et Euronext Dublin,
les listings JSON OAM et RIS, la pagination `page`, les champs observés, des
notices réelles, les liens PDF/XHTML/XML/ZIP et un téléchargement public. Il
affiche un exemple réel de notice et toutes les tentatives d'endpoint.

Les états sont normalisés:

- `ready`: OAM/RIS fournit des notices réelles et au moins un document
  téléchargeable;
- `degraded`: Euronext Direct reste accessible mais l'API, le parsing HTML ou
  le téléchargement est partiel;
- `unavailable`: Euronext Direct et ses listings publics sont inaccessibles.

Le matching d'émetteur utilise l'ISIN lorsqu'il est exposé, puis le nom
normalisé et enfin le symbole. Les abréviations de la watchlist Euronext,
comme `GP`, `PROP`, `PERM` et `CONT`, sont développées avant comparaison.

Pour prouver le parcours complet et l'idempotence:

```powershell
python main.py watch --market "Euronext Dublin" --limit 10 --dry-run
python main.py watch --market "Euronext Dublin" --limit 10
```

Le premier run écrit sous `data/raw/ireland/{isin}` avec la source
`euronext_direct`. Le second doit indiquer zéro téléchargement et compter les
URL déjà connues comme doublons. En cas d'échec, contrôler les variables
`IRELAND_*`, relancer avec `--log-level DEBUG`, puis vérifier directement les
routes `OAMs`, `RIS`, `OAMDocument` et `RISDocument`.

## Règles documentaires

Les titres et URLs sont normalisés sans accents et comparés aux expressions:

- `rapport financier annuel`, `annual financial report`, `RFA`;
- `rapport financier semestriel`, `half-year financial report`, `RFS`;
- `half yearly financial report`, `Halvårsrapport`;
- `annual report`, `Årsrapport`;
- `semi-annual financial report`, `jaarverslag`,
  `halfjaarlijks financieel verslag`;
- `quarterly report`, `Q1` à `Q4`;
- `financial report`;
- `universal registration document`,
  `document d'enregistrement universel`, `URD`, `DEU`;
- `ESEF`, `XHTML`, `XML`, ou fichier `.xhtml` / `.xml` / `.zip` / `.xbri`.
- `relatório financeiro anual`, `relatório e contas`, `relatório anual`,
  `contas anuais`, `relatório semestral` et `contas semestrais`;
- `relazione finanziaria annuale`, `relazioni finanziarie annuali`,
  `bilancio d'esercizio`, `bilancio consolidato`;
- `relazione finanziaria semestrale`,
  `relazioni finanziarie semestrali`;
- catégories italiennes `1.1`, `1.2` et `DOAG`.

Seuls les contenus PDF, XHTML, XML et ZIP sont écrits sur disque. Une limite de
taille de 100 MiB par défaut, configurable par `MAX_DOWNLOAD_MB` ou
`--max-download-mb`, est appliquée avant et pendant le téléchargement.

## Base SQLite

- `issuers`: watchlist normalisée, ISIN unique, identifiants Oslo, URLs des
  stockages Italie, résolution AFM Netherlands, résolution FSMA STORI
  Belgique, résolution CMVM/SDI Portugal et résolution Euronext Direct
  Ireland;
- `documents`: métadonnées, chemin local et SHA256 unique;
- `document_urls`: URL principale et alias associés à un document dédupliqué;
- `download_runs`: périmètre, statut et compteurs de chaque exécution.
- `watch_runs`: début, fin, marché, compteurs, rapport et statut du watcher;
- `watch_run_markets`: compteurs et statut de chaque marché d'un watcher;
- `operational_events`: erreurs et documents `skipped_too_large`;
- `healthcheck_runs` et `source_health_checks`: diagnostics consolidés;
- `source_states`: dernier état opérationnel connu de chaque source.

Les écritures de fichiers passent par un fichier temporaire puis un renommage
atomique. Un contenu déjà présent est supprimé du temporaire et compté comme
doublon.

## Rapports du watcher

Chaque exécution crée un rapport:

```text
reports/watch_YYYYMMDD_HHMMSS.md
reports/watch_all_YYYYMMDD_HHMMSS.md
```

Il contient:

- le statut et les compteurs du run;
- le résumé consolidé par marché;
- les sociétés vérifiées;
- les documents téléchargés ou simulés en `dry-run`;
- les doublons URL ou SHA256;
- les erreurs détaillées par émetteur;
- les sources passées en état `degraded`.

Une erreur réseau ou de téléchargement sur un émetteur produit un run
`partial` mais n'empêche pas le traitement des émetteurs suivants. Un run sans
erreur est `success`; une erreur globale d'initialisation produit `failed`.

## Tests

Les commandes, les tests d'integration live et les conventions Playwright sont
regroupes dans [`TESTING.md`](TESTING.md).

## Déploiement Google Cloud

La webapp peut être déployée sur Cloud Run avec Firestore. Le mode performance
utilise Cloud Tasks pour exécuter les recherches dans le service déjà chaud et
éviter le démarrage à froid des Cloud Run Jobs, tout en conservant SQLite pour
le développement local. Le script configure une instance maximale, une
rétention de 30 jours et une purge quotidienne :

```powershell
.\deploy\google-cloud.ps1 -ProjectId "mon-projet-google"
```

Le mode worker chaud s'active avec `-Performance -WarmWorker` et la protection
HTTP documentée dans [`GOOGLE_CLOUD.md`](GOOGLE_CLOUD.md).

Pour une bêta multi-utilisateur, InfoFin prend également en charge les comptes
sur invitation, les sessions signées, l'isolation des recherches, un quota par
compte et un formulaire de retour. La création des secrets et le déploiement
sont décrits dans la section « Bêta privée avec comptes individuels » du guide.

Voir [`GOOGLE_CLOUD.md`](GOOGLE_CLOUD.md) pour les prérequis, l'architecture,
les permissions, la vérification et les limites de coût.

## Spain source discovery / troubleshooting

Le connecteur espagnol interroge les registres officiels de la CNMV (Comisión Nacional del Mercado de Valores) pour récupérer les rapports financiers annuels et semestriels. Il interroge optionnellement la liste des entreprises cotées de la BME (Bolsas y Mercados Españoles) pour enrichir la watchlist et mapper les URLs des sociétés.

### Configuration (.env)

Les variables suivantes peuvent être configurées :

* `SPAIN_CNMV_BASE_URL` : URL de base des registres de la CNMV.
* `SPAIN_CNMV_LOOKBACK_DAYS` : Nombre de jours de recherche de notices en mode quotidien.
* `SPAIN_RATE_LIMIT_SECONDS` : Temps d'attente minimal entre les requêtes pour respecter les serveurs cibles.
* `SPAIN_VERIFY_SSL` : Détermine si les certificats SSL CNMV doivent être vérifiés (utile en cas d'erreur de chaîne locale).
* `SPAIN_BME_LISTED_COMPANIES_URL` : URL de base de la liste des sociétés cotées sur la BME.

### Troubleshooting / Diagnostic

Si des requêtes échouent avec des statuts HTTP de type `403 Forbidden` ou des blocages de sécurité (la CNMV utilise des serveurs IIS/ASP.NET sensibles aux requêtes automatisées), le connecteur basculera automatiquement dans un statut `degraded` ou `unavailable` sans faire planter les autres marchés.

Pour tester le diagnostic et la récupération de données en ligne de commande :

```powershell
# Diagnostiquer l'état de la source CNMV
python main.py diagnose-source spain

# Rechercher des publications récentes sur la CNMV
python main.py discover-source spain --query "informe financiero"

# Résoudre un émetteur spécifique
python main.py discover-issuer spain --symbol SAN --name "Banco Santander, S.A." --isin ES0113900J37

# Lancer une veille en mode simulation (dry-run)
python main.py watch --market "Bolsa de Madrid" --limit 10 --dry-run
```

## Sweden source discovery / troubleshooting

Le connecteur suédois interroge la base de données officielle de la Finansinspektionen (FI - Stock Exchange Information Database) pour récupérer les rapports financiers (rapports annuels, semestriels, trimestriels, etc.). Il interroge optionnellement le Nasdaq Stockholm pour enrichir la watchlist avec des informations complémentaires (symboles, URLs de sociétés).

### Configuration (.env)

Les variables suivantes peuvent être configurées :

* `SWEDEN_FI_BASE_URL` : URL de base des registres de la Finansinspektionen.
* `SWEDEN_FI_LOOKBACK_DAYS` : Nombre de jours de recherche de notices en mode quotidien.
* `SWEDEN_RATE_LIMIT_SECONDS` : Temps d'attente minimal entre les requêtes.
* `SWEDEN_VERIFY_SSL` : Détermine si les certificats SSL de la FI doivent être vérifiés.
* `SWEDEN_NASDAQ_LISTED_COMPANIES_URL` : URL de base des sociétés cotées sur le Nasdaq Stockholm.

### Troubleshooting / Diagnostic

Pour tester le diagnostic et la récupération de données en ligne de commande :

```powershell
# Diagnostiquer l'état de la source suédoise
python main.py diagnose-source sweden

# Rechercher des publications récentes sur la FI
python main.py discover-source sweden --query "annual report"

# Résoudre un émetteur spécifique
python main.py discover-issuer sweden --symbol "ERIC B" --name "Ericsson, Telefonaktiebolaget LM" --isin SE0000108656

# Lancer une veille en mode simulation (dry-run)
python main.py watch --market "Nasdaq Stockholm" --limit 10 --dry-run
```

## Denmark source discovery / troubleshooting

Le connecteur danois utilise la base officielle DFSA OAM / Company
Announcements comme source réglementaire. La page publique charge actuellement
une application embarquée : le connecteur découvre dynamiquement son identifiant
de module, interroge le listing global par période, matche localement la
watchlist, puis ouvre uniquement les détails des notices matchées. Nasdaq
Copenhagen reste un enrichissement optionnel et ne bloque jamais la DFSA.

Les variables disponibles sont :

* `DENMARK_DFSA_BASE_URL`
* `DENMARK_DFSA_LOOKBACK_DAYS`
* `DENMARK_RATE_LIMIT_SECONDS`
* `DENMARK_VERIFY_SSL`
* `DENMARK_NASDAQ_LISTED_COMPANIES_URL`

Commandes de diagnostic :

```powershell
python main.py import-csv samples/watchlist_denmark.csv
python main.py diagnose-source denmark
python main.py discover-source denmark --query "annual report"
python main.py discover-issuer denmark --symbol MATAS --name "MATAS A/S" --isin DK0060497295
python main.py watch --market "Nasdaq Copenhagen" --limit 10 --dry-run
```

L'import CSV est multi-marchés. Il attend au minimum les colonnes `Name`,
`ISIN`, `Symbol` et `Market`; les alias Copenhagen sont normalisés vers
`Nasdaq Copenhagen`. La watchlist d'exemple contient les cinq grandes valeurs
demandées et Roblon, utilisé pour la validation quotidienne reproductible.

Le mode quotidien est toujours `source-first`. Les modes par émetteur sont
réservés à `--backfill` et `--issuer-mode`. Par défaut, seules les catégories
financières périodiques sont téléchargées ; `--include-regulatory-news` permet
explicitement d'inclure les autres annonces.

Le Danemark est marqué `eu_candidate` pour le contrôle géographique PEA. Cela ne
vaut jamais confirmation d'éligibilité : le domicile doit être confirmé depuis
les données émetteur DFSA et aucun `pea_eligible=true` n'est déduit de la place
de cotation.

## Bulgaria source discovery / troubleshooting

Le connecteur bulgare interroge d'abord le portail X3News courant avec les
filtres officiels de date et de catégorie financière. Il ouvre ensuite les
pièces jointes des seules périodicités demandées et conserve l'archive Apache
de la Bourse de Sofia comme repli historique.

Les variables disponibles sont :

* `BULGARIA_X3NEWS_BASE_URL`
* `BULGARIA_X3NEWS_VERIFY_SSL`
* `BULGARIA_X3NEWS_MAX_PAGES`
* `BULGARIA_BSE_X3NEWS_BASE_URL`
* `BULGARIA_BSE_X3NEWS_LOOKBACK_DAYS`
* `BULGARIA_BSE_X3NEWS_MAX_CANDIDATES_PER_SOURCE`

Le serveur X3News présente actuellement une chaîne de certificats que
`requests` ne valide pas dans l'environnement de référence. La vérification
est donc désactivée uniquement pour ce portail par défaut et un avertissement
explicite est journalisé. Réactiver `BULGARIA_X3NEWS_VERIFY_SSL=true` dès que la
chaîne TLS est corrigée ou qu'un magasin de certificats adapté est disponible.

```powershell
python main.py diagnose-source bulgaria
python main.py discover-market-documents --market "Bulgarian Stock Exchange" --date-from 2025-07-14 --date-to 2026-07-14 --format json
python main.py watch --market "Bulgarian Stock Exchange" --limit 10 --dry-run
```
