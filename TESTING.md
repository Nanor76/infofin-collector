# Tests

Ce document regroupe les commandes de test du projet, les precautions pour les
integrations live et le contrat de ciblage de l'interface web avec Playwright.

## Tests standards

La suite standard n'effectue aucun appel reseau :

```powershell
python -m pytest -q
```

Pour limiter l'execution aux composants de la webapp :

```powershell
python -m pytest tests/test_web_api.py tests/test_web_cloud.py tests/test_web_document_search.py tests/test_web_filters.py tests/test_web_jobs.py tests/test_web_repository.py tests/test_test_catalog.py -q
```

## Maintenance obligatoire a chaque evolution

La base de cas de tests fait partie du produit. Toute evolution de
l'application doit modifier ou ajouter les tests qui decrivent le comportement
touche. Une modification de code applicatif sans cas de test associe n'est pas
terminee.

La vue de routage optimisee de la base existante se trouve dans
`tests/TEST_CATALOG.md`. Elle indique, par zone de code et risque fonctionnel,
les fichiers et cas a consulter. Elle doit etre lue avant toute evolution et
mise a jour dans la meme modification lorsqu'un fichier, un cas ou une
responsabilite de test est ajoute, renomme, deplace ou supprime.

Avant de coder :

1. identifier le comportement observable qui change et les regressions
   possibles ;
2. retrouver les cas existants les plus proches avec `rg` dans `tests/` ;
3. choisir le niveau de test le plus bas capable de prouver le comportement ;
4. pour un bug, faire reproduire la regression par le nouveau test avant de
   valider le correctif.

Matrice d'impact minimale :

| Evolution | Tests a mettre a jour | Verification complementaire |
| --- | --- | --- |
| Route, validation ou schema API | `tests/test_web_api.py` | codes, payloads et erreurs metier |
| Service de recherche ou filtres | `tests/test_web_document_search.py`, `tests/test_web_filters.py` | cas nominal, limites et absence de resultat |
| Jobs ou repository | `tests/test_web_jobs.py`, `tests/test_web_repository.py` | transitions d'etat, persistance et erreurs |
| Configuration de déploiement Google Cloud | `tests/test_web_cloud.py` | Cloud Tasks, worker chaud, profils économique/performance, ressources, secrets et garde-fous de coût |
| Template ou controle HTML | `tests/test_web_api.py` et `tests/e2e/*.spec.ts` | contrat `data-testid` et parcours utilisateur |
| JavaScript, carte ou soumission | `tests/e2e/*.spec.ts` | payload, navigation, synchronisation et erreur visible |
| HTMX, tri, filtre ou pagination | pytest sur le partial et Playwright | contenu remplace, compteurs et conservation des filtres |
| Export | pytest du service/API et Playwright | nom, format et contenu telecharge |
| Connecteur officiel | `tests/test_<pays>_connector.py`, puis `tests/test_<pays>_watch.py` | parsing, pagination, limites et isolation hors réseau |
| Correction de bug | test de regression au niveau concerne | le test doit echouer sans le correctif |
| Suppression ou renommage | cas, fixtures et registre de couverture | aucun selecteur ou scenario orphelin |

Le tableau de couverture Playwright de la section suivante constitue le
registre des parcours essentiels. Le mettre a jour des qu'un parcours, un etat
ou une responsabilite est ajoute, modifie, deplace ou supprime. Les noms de cas
dans `tests/e2e/essential-flows.spec.ts` doivent rester lisibles comme des
exigences fonctionnelles.

Avant livraison, verifier que :

- chaque nouveau comportement possede au moins une assertion sur son resultat
  metier ou son etat visible ;
- les cas nominal, vide, invalide ou erreur pertinents sont couverts ;
- les fixtures restent isolees et sans reseau ;
- aucun test n'est desactive ou rendu moins strict pour faire passer la suite ;
- les commandes de validation ont ete executees et leurs resultats sont notes
  dans le compte rendu.

## Strategie Playwright

La suite Playwright valide les parcours essentiels dans un vrai Chromium, en
complement des tests pytest de routes, de services et de rendu HTML. Elle vise
les risques d'integration que pytest seul ne couvre pas : JavaScript, carte SVG,
soumission du formulaire, navigation, mises a jour HTMX, tri, pagination et
telechargement.

La strategie repose sur quatre principes :

- les selecteurs fonctionnels utilisent exclusivement le contrat
  `data-testid` documente ci-dessous ;
- `tests/e2e/server.py` lance une application isolee avec une base SQLite
  temporaire et un gestionnaire de jobs synchrone ;
- les documents de test sont deterministes et aucun connecteur officiel n'est
  appele ;
- HTMX, Lucide et les fontes externes sont interceptes par la fixture
  Playwright. HTMX est servi depuis la dependance npm verrouillee, les autres
  ressources sont neutralisees, donc l'execution ne depend pas d'un CDN.

La couverture essentielle est repartie ainsi :

| Parcours | Verifications principales |
| --- | --- |
| Authentification | refus sans session, connexion individuelle à la bêta, isolation des recherches, quota 24 h, déconnexion et jeton worker distinct |
| Recherche | sante locale SQLite/local, chargement de la carte, selection d'un marche, dates, types periodiques annuel/semestriel/trimestriel, payload API, navigation, resultats et exclusion stricte des autres periodicites |
| Statut initial | etat technique `queued` expose comme `running`, puis transition vers « Terminée » par le worker interne chaud |
| Selection | boutons Tous/Aucun, synchronisation carte-liste et validation sans marche |
| Filtres | absence du filtre ISIN redondant, mise a jour HTMX par type et texte, ciblage d'une ligne et etat vide |
| Resultats | tri, pagination aller-retour, telechargement CSV et stabilité du défilement horizontal mobile après l'arrêt du polling |
| Confidentialite | champs techniques de collecte absents des pages, de l'API et des exports ; ouverture directe de l'adresse officielle du document |
| Retours bêta | formulaire attribué au compte connecté, lié à la recherche et confirmation visible sans rechargement |

### Installation et execution

Pre-requis : Python 3.12 avec `requirements.txt`, Node.js 20 et npm.

```powershell
npm ci
npx playwright install chromium
npm run test:e2e
```

Afficher uniquement l'inventaire des cas Playwright :

```powershell
npm run test:e2e:list
```

Le serveur de test ecoute par defaut sur `127.0.0.1:8766`, est demarre et arrete
par Playwright, et ne touche pas a `data/infofin.sqlite3`. Pour viser une
application deja lancee, definir `INFOFIN_E2E_BASE_URL`; dans ce mode,
Playwright ne demarre pas le serveur de fixtures :

```powershell
$env:INFOFIN_E2E_BASE_URL = "http://127.0.0.1:8765"
npm run test:e2e
Remove-Item Env:INFOFIN_E2E_BASE_URL
```

Commandes utiles pour l'investigation locale :

```powershell
npm run test:e2e:headed
npm run test:e2e:debug
npm run test:e2e:report
```

En cas d'echec, une capture, une video et une trace sont conservees dans
`test-results/`; le rapport HTML est genere dans `playwright-report/`. Ces deux
repertoires sont ignores par Git. En CI, la suite utilise un seul worker, refuse
les `test.only`, effectue deux nouvelles tentatives et doit publier ces
repertoires comme artefacts uniquement en cas d'echec.

La commande de validation web minimale avant livraison est :

```powershell
python -m pytest tests/test_web_api.py tests/test_web_cloud.py tests/test_web_document_search.py tests/test_web_filters.py tests/test_web_jobs.py tests/test_web_repository.py tests/test_test_catalog.py -q
npm run test:e2e
```

Sur Windows, si le repertoire temporaire global de `pytest` n'est pas
accessible, utiliser un repertoire local au projet :

```powershell
python -m pytest -q --basetemp=pytest_tmp/local-run
```

## Audit live de la categorisation

`classification_audit.py` compare la categorie produite par chaque connecteur
avec les indices independants du titre et des metadonnees officielles. La
commande `all` couvre tous les marches declares dans
`connectors.SUPPORTED_WATCH_MARKETS`, y compris les marches d'Europe centrale
et orientale :

```powershell
python classification_audit.py --batch all --since 2026-01-01 --limit 300
```

Limiter l'audit a un ou plusieurs marches :

```powershell
python classification_audit.py --market "Bucharest Stock Exchange" --market "Nasdaq Stockholm" --since 2026-01-01
```

Cet audit appelle les sources officielles et reste donc hors de la suite
standard sans reseau. Les fichiers JSON et CSV produits dans `reports/`
distinguent `MATCH`, `CONFLICT` et `NO_TITLE_SIGNAL`. Un conflit doit etre
examine ; l'absence d'indice dans le titre n'est pas a elle seule une erreur
lorsqu'une categorie source exacte fait autorite.

## Tests d'integration live

Les tests live sont explicitement opt-in. Selon le connecteur, ils interrogent
une source officielle, peuvent telecharger un document, calculer son SHA256,
l'ecrire localement et verifier son insertion SQLite. Ils ne doivent pas etre
inclus dans une execution hors ligne ou dans une CI sans acces reseau maitrise.

Activer les tests live dans le terminal courant :

```powershell
$env:RUN_LIVE_TESTS = "1"
```

Executer un connecteur precis :

```powershell
python -m pytest tests/test_france_live.py -q
python -m pytest tests/test_oslo_live.py -q
python -m pytest tests/test_italy_live.py -q
python -m pytest tests/test_netherlands_live.py -q
python -m pytest tests/test_belgium_live.py -q
python -m pytest tests/test_portugal_live.py -q
python -m pytest tests/test_ireland_live.py -q
python -m pytest tests/test_spain_watch.py -k "live" -q
python -m pytest tests/test_sweden_watch.py -k "live" -q
python -m pytest tests/test_denmark_connector.py -k "live" -q
python -m pytest tests/test_austria_connector.py -k "live" -q
python -m pytest tests/test_estonia_connector.py -k "live" -q
python -m pytest tests/test_latvia_connector.py -k "live" -q
python -m pytest tests/test_lithuania_connector.py -k "live" -q
python -m pytest tests/test_slovenia_connector.py -k "live" -q
```

La Bulgarie combine le portail X3News courant et l'archive historique BSE.
Le diagnostic live verifie en priorite le portail courant et ses pieces jointes :

```powershell
python main.py diagnose-source bulgaria
```

Desactiver ensuite le mode live si le terminal reste ouvert :

```powershell
Remove-Item Env:RUN_LIVE_TESTS
```

## Contrat des `data-testid`

Les tests UI Playwright ciblent les controles et les points d'observation avec
`getByTestId`. Ces identifiants constituent un contrat de test : ils ne doivent
pas reprendre une classe CSS, un texte traduit ou une position visuelle
susceptible de changer sans modification fonctionnelle.

Le format general est :

```text
<page>-<zone>-<cible>-<type>
```

- les segments sont en minuscules et en kebab-case ;
- `page` vaut actuellement `layout`, `search` ou `results` ;
- `zone` decrit le domaine fonctionnel, par exemple `market`, `date`, `filter`,
  `sort`, `status`, `document` ou `pagination` ;
- `type` precise la nature du controle ou de la valeur lorsque cela est utile :
  `button`, `link`, `input`, `select`, `checkbox`, `section`, `region`, `count`
  ou `state` ;
- les noms de classes CSS, les libelles visibles et les index de ligne ou de
  page sont exclus.

Exemples :

```text
layout-home-link
search-date-from-input
search-document-type-checkbox-annual-financial-report
results-job-state
results-filter-document-type-select
results-pagination-next-button
```

## Collections et valeurs dynamiques

Un element unique dans la page porte un identifiant unique. Pour une collection
associee a une cle metier stable, cette cle complete l'identifiant :

```text
search-market-option-euronext-paris
search-market-checkbox-euronext-paris
search-map-country-fr
login-username-input
login-password-input
search-beta-quota-state
results-feedback-form
results-feedback-response-state
```

Les noms de marches sont translitteres en ASCII puis normalises en kebab-case par
`_test_id_segment`. Les codes pays utilisent leur code ISO en minuscules.

Les lignes de resultats sont triees et paginees. Elles ne recoivent donc pas de
suffixe fonde sur leur position. Chaque ligne porte `results-document-row` et
les valeurs ou actions sont ciblees dans cette ligne :

```ts
const row = page
  .getByTestId("results-document-row")
  .filter({ hasText: "Annual report" });

await expect(row.getByTestId("results-document-issuer-name"))
  .toHaveText("Issuer A");
await row.getByTestId("results-document-open-link").click();
```

Le meme principe s'applique aux executions par marche : selectionner d'abord
`results-market-run` par son contenu, puis lire `results-market-run-status` ou
`results-market-run-count`. Les champs techniques de collecte restent internes
et ne font pas partie du contrat UI. L'adresse officielle du document reste
cependant visible sur l'action d'ouverture et dans les exports.

## Regles de couverture UI

Ajouter un `data-testid` aux elements suivants :

- controles interactifs : liens, boutons, champs, listes, tris et pays de la
  carte ;
- regions asynchrones ou remplacees par HTMX ;
- etats et compteurs utilises comme oracles de test ;
- etats conditionnels : chargement, liste vide, avertissements et erreurs ;
- conteneurs repetes et valeurs metier utiles pour les scoper.

Ne pas en ajouter aux icones, wrappers de mise en page ou textes purement
decoratifs qui ne servent ni d'action, ni de scope, ni d'assertion.

La carte est chargee dynamiquement. `app.js` pose `search-map-canvas` sur le SVG
injecte et `search-map-country-<code-iso>` sur chaque pays cliquable. Le
conteneur `search-map` permet d'attendre le chargement ou de calculer la zone de
glisser-deposer.

## Garde-fous automatiques

`tests/test_web_api.py` analyse les pages et partials rendus. Tout nouvel element
HTML interactif standard (`a`, `button`, champ de formulaire, `summary`,
`[onclick]`, etc.) sans `data-testid` fait echouer la suite. Les interactions
creees exclusivement en JavaScript doivent etre couvertes explicitement, comme
celles de la carte SVG.

Lorsqu'un nouveau point d'observation devient contractuel pour les tests UI,
l'ajouter aussi aux ensembles attendus dans `tests/test_web_api.py`.
