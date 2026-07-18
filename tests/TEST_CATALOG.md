# Catalogue et routage des tests

Ce fichier donne a un contributeur ou a une instance LLM la vue la plus courte
pour trouver les cas existants a modifier. Il ne remplace pas `TESTING.md`, qui
decrit la strategie, les commandes et le contrat `data-testid`.

## Demarrage rapide

Pour toute evolution :

1. partir du fichier applicatif modifie dans la table de routage ci-dessous ;
2. ouvrir les fichiers de tests indiques et rechercher les cas par nom ;
3. confirmer la liste reelle des cas avec les commandes d'inventaire ;
4. modifier ou ajouter le test au niveau le plus proche du comportement ;
5. si le comportement est visible dans le navigateur, mettre aussi a jour un
   parcours Playwright ;
6. mettre a jour ce catalogue si la responsabilite, le nom ou l'emplacement
   d'un cas change.

Commandes d'inventaire toujours a jour :

```powershell
# Tous les fichiers de tests
rg --files tests | Sort-Object

# Tous les cas pytest avec leur ligne
rg -n -g "test_*.py" "^def test_" tests

# Cas d'un module precis, tels que pytest les collecte
python -m pytest tests/test_web_api.py --collect-only -q

# Tous les parcours Playwright
npm run test:e2e:list
```

## Routage par zone de code

| Zone modifiee | Tests principaux | Quand ajouter Playwright |
| --- | --- | --- |
| `webapp/app.py`, routes et rendu Jinja | `test_web_api.py` | des que la reponse modifie une page, une navigation ou un etat visible |
| `webapp/templates/**` | `test_web_api.py`, `e2e/essential-flows.spec.ts` | toujours pour un controle, un oracle ou un parcours modifie |
| `webapp/static/app.js` | `test_web_api.py`, `e2e/essential-flows.spec.ts` | toujours ; couvrir l'interaction et son resultat observable |
| `webapp/services/document_search.py` | `test_web_document_search.py` | si les resultats ou etats affiches changent |
| `webapp/services/filters.py` | `test_web_filters.py`, `test_web_repository.py` | si un filtre UI ou son compteur change |
| `webapp/services/exports.py` | `test_web_api.py` et tests du service concernes | pour le telechargement, le nom ou le contenu du fichier |
| `webapp/jobs.py` | `test_web_jobs.py` | si les transitions, compteurs, alertes ou rafraichissements changent |
| `webapp/repositories.py` | `test_web_repository.py` | si tri, filtre, pagination ou donnees visibles changent |
| `webapp/firestore_repository.py`, `webapp/cloud_jobs.py`, `webapp/run_job.py` | `test_web_cloud.py` | pour le stockage Firestore, le lancement distant, le worker et la configuration Google Cloud |
| `webapp/schemas.py` | `test_web_api.py` | si le formulaire ou les erreurs de validation changent |
| `db.py` | `test_db.py` et test repository concerne | si la modification atteint un parcours utilisateur |
| `classification.py`, `classification_audit.py` | `test_classification.py`, `test_classification_audit.py` | si la classification ou son audit est expose dans la webapp |
| `download.py`, `http_client.py` | `test_download.py`, `test_ssl_verification.py` | seulement si le comportement visible de la webapp change |
| `main.py`, `operations.py` | `test_main.py`, `test_operations.py` | si le lancement ou une action utilisateur web change |
| `watcher.py` | `test_watcher.py`, `test_source_first_watch.py`, tests `*_watch.py` | si les donnees collectees sont exposees dans la webapp |
| `connectors/<pays>.py` | `test_<pays>_connector.py`, puis `test_<pays>_watch.py` | si le format ou les etats remontes a l'interface changent |
| migration d'un connecteur | `test_<pays>_migration.py` lorsqu'il existe | si la migration affecte des resultats visibles |
| integration officielle | tests `*_live.py` ou cas marques `live` | jamais dans la suite E2E standard sans fixture locale |
| `screener/**` | `test_screener.py` | uniquement si une interface web l'expose |
| fixtures HTML/JSON/XML/PDF/ZIP | test connecteur qui les consomme | mettre a jour `e2e/server.py` seulement pour un etat UI necessaire |

## Catalogue detaille de la webapp

### API, pages et contrat UI — `tests/test_web_api.py`

| Groupe | Cas existants | Modifier lorsque |
| --- | --- | --- |
| Referentiels | `test_get_markets`, `test_get_document_types`, `test_get_health` | une liste, une valeur ou la sante change |
| Authentification | `test_password_protects_every_web_route` | défi HTTP Basic global, identifiants ou couverture des routes change |
| Creation et statut | `test_post_search_returns_job_id`, `test_get_search_status`, `test_queued_search_is_publicly_reported_as_running`, `test_get_unknown_search_returns_404`, `test_private_search_controls_are_not_exposed` | payload public, identifiant, statut, erreur ou surface de documentation change |
| Resultats API | `test_get_search_results_paginates` | pagination, structure JSON ou adresse officielle du document change |
| Page de recherche | `test_get_home_contains_form`, `test_home_interactive_elements_have_test_ids` | formulaire, controle ou `data-testid` change |
| Page de resultats | `test_get_results_page_with_fake_job`, `test_results_interactive_elements_have_test_ids` | tableau, action, filtre ou export change |
| Etats conditionnels | `test_results_conditional_states_have_test_ids` | vide, chargement, warning, erreur ou execution par marche change |
| Carte dynamique | `test_dynamic_map_interactions_define_test_ids` | SVG, pays cliquables ou generation JS des identifiants change |
| Polling | `test_terminal_results_page_does_not_auto_poll_results`, `test_running_results_page_polls_with_current_filters` | terminalite, frequence ou filtres HTMX changent |
| Pagination HTMX | `test_results_pagination_uses_htmx_values_and_form_filters` | navigation, `hx-vals`, `hx-include` ou conservation des filtres change |
| Non-divulgation | `test_exports_hide_technical_provenance_but_keep_document_links` | page, statut, API ou export risque d'exposer des champs techniques autres que l'adresse officielle assumee |

La fixture `FakeJobManager` de ce fichier contient actuellement quatre etats de
reference : termine avec un resultat, en cours, partiel avec alertes et termine
avec 51 resultats pagines. Ajouter un etat ici lorsqu'un rendu serveur doit etre
teste sans navigateur.

### Recherche de documents — `tests/test_web_document_search.py`

| Cas | Responsabilite |
| --- | --- |
| `test_search_links_filters_dates_and_dedupes` | filtrage des dates et deduplication |
| `test_search_links_forwards_source_date_and_document_type_filters` | transmission des bornes et types aux connecteurs capables de filtrer a la source |
| `test_search_links_connector_error_does_not_block_other_markets` | isolation d'une erreur entre marches |
| `test_search_links_dedupe_url_aggregates_markets` | aggregation des marches par URL |
| `test_search_links_closes_session_on_connector_exception` | fermeture de session apres exception |
| `test_search_links_rejects_invalid_date_range` | rejet d'un intervalle invalide |
| `test_search_links_parallel_and_callback` | parallelisme et callback de progression |

### Filtres — `tests/test_web_filters.py`

| Cas | Responsabilite |
| --- | --- |
| `test_filter_by_annual_document_type` | type de document annuel |
| `test_filter_by_accent_insensitive_query` | recherche insensible aux accents |
| `test_normalize_search_text_strips_accents` | normalisation du texte |
| `test_filter_by_isin_in_comma_joined_list` | ISIN dans une liste agregee |
| `test_filter_by_source` | source |
| `test_text_query_does_not_match_private_provenance` | absence de recherche textuelle dans les champs de provenance internes |
| `test_filter_by_format` | format |
| `test_filter_by_date_confidence` | confiance de date |
| `test_filter_composes_multiple_filters` | composition de plusieurs filtres |

### Jobs — `tests/test_web_jobs.py`

| Cas | Responsabilite |
| --- | --- |
| `test_submit_creates_done_job` | job termine avec succes |
| `test_submit_partial_when_errors_and_results` | statut partiel avec erreurs et resultats |
| `test_submit_failed_when_errors_without_results` | statut echoue sans resultat |
| `test_cancel_on_finished_job_does_not_break` | annulation non destructive d'un job termine |
| `test_worker_failure_marks_job_failed` | exception inattendue convertie en état terminal persistant |

### Repository — `tests/test_web_repository.py`

| Cas | Responsabilite |
| --- | --- |
| `test_initialize_web_search_schema` | initialisation du schema |
| `test_create_and_get_job` | creation et lecture d'un job |
| `test_replace_results_and_list_paginated` | remplacement et pagination |
| `test_replace_results_overwrites_previous_rows` | ecrasement des anciennes lignes |
| `test_list_results_filters` | filtres repository |
| `test_list_results_sort_whitelist` | liste blanche de tri |
| `test_upsert_market_run_and_purge` | executions par marche et purge |

### Google Cloud — `tests/test_web_cloud.py`

| Cas | Responsabilite |
| --- | --- |
| `test_cloud_settings_are_loaded_from_environment` | sélection explicite de Firestore, Cloud Tasks, file, URL du service et identifiants HTTP par environnement |
| `test_firestore_repository_persists_filters_and_purges` | contrat de persistance, filtrage et suppression en cascade sans réseau |
| `test_cloud_run_launcher_overrides_the_search_job_id` | URL Cloud Run v2 et surcharge isolée de l'identifiant de recherche |
| `test_cloud_tasks_launcher_targets_the_warm_service` | création d'une tâche HTTP authentifiée vers le worker du service maintenu chaud |
| `test_cloud_job_manager_persists_before_dispatch` | persistance du job avant son lancement distant |
| `test_cloud_job_manager_marks_dispatch_failure` | état terminal explicite lorsque l'API Cloud Run est indisponible |
| `test_cloud_worker_executes_the_persisted_request` | reprise de la requête Firestore et écriture du résultat par le worker |
| `test_cloud_tasks_worker_endpoint_executes_once` | exécution idempotente d'une tâche sur le worker HTTP interne |
| `test_cloud_app_uses_shared_repository_without_sqlite` | intégration FastAPI, Firestore et dispatcher sans création de fichier SQLite |
| `test_google_cloud_deployment_assets_keep_free_tier_guards` | conteneur, worker Cloud Tasks chaud, file séquentielle sans relance, secret mobile et garde-fous de coût |

## Catalogue Playwright

Les cas sont dans `tests/e2e/essential-flows.spec.ts` :

| Cas | Responsabilites protegees | Fixtures principales |
| --- | --- | --- |
| `la recherche permet de sélectionner les critères et affiche les résultats` | refus sans mot de passe, accès HTTP Basic, sante SQLite/local, carte chargee, criteres periodiques annuel/semestriel/trimestriel, payload POST, navigation, statut et premiere page | 51 rapports periodiques |
| `la sélection rapide, la carte et la validation restent synchronisées` | Tous/Aucun, synchronisation France, soumission sans marche | marches du formulaire et dialogue de validation |
| `l'état technique queued est affiché comme une recherche en cours` | état persistant initial masqué derrière `running`, puis transition vers `done` via le worker interne chaud | recherche maintenue en file puis exécutée par l'endpoint Cloud Tasks |
| `une recherche par type exclut les autres périodicités` | filtre annuel strict et absence de rapports semestriels ou trimestriels dans les resultats | 51 rapports annuels |
| `les filtres HTMX couvrent le type, le texte et l'état vide` | absence du filtre ISIN redondant, filtre de type, ligne Beta, recherche sans resultat, compteur et vide | un rapport semestriel unique parmi 51 documents |
| `le tri, la pagination et l'export CSV sont opérationnels` | pages 1/2, retour, tri societe, nom et contenu CSV | 51 documents sur Paris et Oslo |
| `le tableau mobile conserve son défilement horizontal après la fin de la recherche` | arrêt du polling terminal et conservation de la position horizontale jusqu'au bouton d'ouverture | viewport smartphone et 51 documents terminés |
| `les métadonnées techniques restent masquées et les documents s'ouvrent à leur adresse officielle` | absence de source et identifiant techniques ; lien officiel direct, ouverture HTTP 200 et absence du bouton de copie | provenance interne sentinelle et URL officielle interceptee des 51 documents |

Fichiers de support :

- `tests/e2e/server.py` : application FastAPI isolee, SQLite temporaire,
  `DeterministicJobManager` et documents metier ;
- `tests/e2e/fixtures.ts` : HTMX local, neutralisation des CDN et detection des
  erreurs JavaScript non gerees ;
- `playwright.config.ts` : Chromium, serveur, traces, captures, videos et
  politique CI.

Lorsqu'un nouveau parcours visible est ajoute, completer le tableau ci-dessus
et le tableau de couverture essentielle de `TESTING.md` dans la meme
modification.

## Garde-fou de synchronisation du catalogue

`tests/test_test_catalog.py` compare automatiquement ce document avec les cas
reellement declares :

- `test_catalog_lists_every_web_pytest_case` exige que chaque fonction
  `test_*` des fichiers `tests/test_web*.py` apparaisse ici ;
- `test_catalog_lists_every_playwright_case` exige que chaque titre Playwright
  de `tests/e2e/*.spec.ts` apparaisse ici.

Ce garde-fou fait echouer pytest lorsqu'un cas web ou Playwright est ajoute ou
renomme sans mise a jour du catalogue.

## Autres familles de tests

Le nommage permet de router rapidement les changements hors webapp :

- `test_<pays>_connector.py` : parsing et comportement du connecteur avec les
  fixtures de `tests/fixtures/` ;
- `test_poland_connector.py` : classification KNF, matérialisation et plafond
  de pagination couvrant une journée volumineuse de 18 pages ;
- `test_<pays>_watch.py` : integration du connecteur dans la collecte ;
- `test_<pays>_migration.py` : compatibilite ou migration des donnees ;
- `test_<pays>_live.py` et cas marques `live` : verification reseau opt-in ;
- `test_bulgaria_connector.py` : archive BSE, listing X3News courant,
  classification des periodicites, pieces jointes et filtrage annuel par dates ;
- `test_db.py`, `test_download.py`, `test_classification.py`,
  `test_classification_audit.py`, `test_load_watchlist.py`,
  `test_issuer_list_sync.py` : briques centrales et audit independant des
  categories produites ;
- `test_market_document_links.py`, `test_operations.py`, `test_main.py` :
  orchestration et interfaces de commande ;
- `test_ssl_verification.py` : politiques SSL des integrations ;
- `test_screener.py` : sous-systeme screener.

Pour un connecteur, verifier les quatre familles `connector`, `watch`,
`migration` et `live` avec `rg --files tests | rg "<pays>"` avant de choisir les
cas a modifier.
