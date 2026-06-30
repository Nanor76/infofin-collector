# Plan de développement - Webapp de recherche de documents financiers

Ce document est le plan d'implémentation de la webapp décrite dans
`ARCHITECTURE_WEBAPP.md`. Il est volontairement prescriptif: suivre les lots
dans l'ordre, ne pas sauter les tests, et conserver la compatibilité de la CLI
existante.

## Règles de travail

- Ne pas télécharger les documents dans le flux web. La webapp affiche des
  liens officiels.
- Ne pas modifier les connecteurs tant qu'un lot ne le demande pas.
- Réutiliser `connector_for_market(...)`, `normalize_market(...)` et
  `DocumentCandidate`.
- Garder `python main.py discover-market-documents ...` fonctionnel à chaque
  étape.
- Ajouter les dépendances seulement quand le lot qui en a besoin est abordé.
- Après chaque lot: lancer au minimum les tests ciblés indiqués, puis
  `python -m pytest -q` si le changement touche un module partagé.

## Etat de départ à connaître

Fichiers existants importants:

- `connectors/base.py`
  - `DocumentCandidate`: modèle retourné par les connecteurs.
  - `Connector.search_recent_documents(...)`: mode source-first à réutiliser.
- `connectors/__init__.py`
  - `SUPPORTED_WATCH_MARKETS`: liste de marchés déjà supportés.
  - `connector_for_market(...)`: factory officielle.
- `main.py`
  - `discover_market_document_links(...)`: logique actuelle d'export de liens.
  - commande CLI `discover-market-documents`.
- `classification.py`
  - `classify_document(...)` et `supported_extension(...)`.
- `load_watchlist.py`
  - `normalize_market(...)`.
- `db.py`
  - `Database.initialize(...)` et connexion SQLite WAL.
- `tests/test_market_document_links.py`
  - tests existants à préserver.

## Cible technique MVP

Créer une webapp locale avec:

- backend FastAPI;
- templates Jinja2 + HTMX pour l'interface;
- SQLite pour stocker jobs et résultats;
- worker local en thread pour les recherches;
- exports CSV/JSON;
- recherche source-first multi-marchés;
- filtres par type de document, période, marché, source, texte libre, ISIN,
  format et confiance de date.

## Lot 0 - Préparation et garde-fous

### Objectif

Préparer l'espace de travail sans changer le comportement.

### A faire

1. Créer l'arborescence vide:

```text
webapp/
  __init__.py
  app.py
  schemas.py
  jobs.py
  repositories.py
  services/
    __init__.py
    document_search.py
    exports.py
    filters.py
  templates/
    layout.html
    search.html
    results.html
    partials/
      job_status.html
      results_table.html
  static/
    app.css
    app.js
tests/
  test_web_document_search.py
  test_web_filters.py
  test_web_repository.py
  test_web_api.py
```

2. Mettre seulement des fichiers Python minimaux importables.
3. Ne pas encore ajouter FastAPI aux imports des tests unitaires hors API.

### Tests

```powershell
python -m pytest tests/test_market_document_links.py -q
```

### Critère de fin

Le dépôt importe toujours les modules existants et aucun test historique ne
change de résultat.

## Lot 1 - Extraire le service de recherche pur

### Objectif

Sortir la logique de recherche de `main.py` dans un service réutilisable par la
CLI et par la future API.

### Fichier à créer

`webapp/services/document_search.py`

### Modèles dataclass attendus

Implémenter ces dataclasses dans `document_search.py`:

```python
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

@dataclass(frozen=True, slots=True)
class LinkSearchRequest:
    markets: tuple[str, ...]
    date_from: date
    date_to: date
    document_types: tuple[str, ...] = ()
    query: str | None = None
    issuer_isin: str | None = None
    sources: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    date_confidences: tuple[str, ...] = ()
    max_candidates: int = 100000
    dedupe_url: bool = False

@dataclass(frozen=True, slots=True)
class LinkSearchDocument:
    market: str
    source: str
    source_document_id: str
    published_at: str
    period_end_date: str
    reporting_year: int | str
    document_type: str
    classification: str
    title: str
    url: str
    issuer_name: str
    issuer_isin: str
    issuer_lei: str
    category: str
    file_format: str
    date_confidence: str
    source_publication_date_raw: str
    metadata: dict[str, object] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class MarketSearchSummary:
    market: str
    source: str
    status: str
    candidates_returned: int = 0
    documents_count: int = 0
    warning: str = ""
    error: str = ""

@dataclass(frozen=True, slots=True)
class LinkSearchResultSet:
    request: LinkSearchRequest
    documents: tuple[LinkSearchDocument, ...]
    market_summaries: tuple[MarketSearchSummary, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
```

### Service attendu

Créer une classe:

```python
class DocumentSearchService:
    def __init__(
        self,
        settings,
        *,
        session_factory=build_http_session,
        connector_factory=connector_for_market,
    ) -> None:
        ...

    def search_links(self, request: LinkSearchRequest) -> LinkSearchResultSet:
        ...
```

### Comportement exact

`search_links(...)` doit reprendre la logique de
`discover_market_document_links(...)`:

1. Refuser `date_from > date_to` avec `ValueError`.
2. Refuser `max_candidates < 1` avec `ValueError`.
3. Créer une session HTTP via `build_http_session(...)`.
4. Normaliser chaque marché avec `normalize_market(...)`.
5. Construire le connecteur via `connector_factory(normalized_market, settings=settings, session=session)`.
6. Si aucun connecteur: ajouter une erreur `<market>: aucun connecteur`.
7. Si `supports_source_first` est faux: ajouter une erreur `<market>: source-first non supporté`.
8. Appeler `connector.search_recent_documents(normalized_market, since=date_from, limit=max_candidates)`.
9. En cas d'exception: ajouter une erreur `<market>: <exception>`.
10. Filtrer les candidats sans date publiée.
11. Filtrer les candidats hors `[date_from, date_to]`.
12. Dédupliquer par `(candidate.source, candidate.source_document_id or candidate.url)`.
13. Convertir chaque `DocumentCandidate` en `LinkSearchDocument`.
14. Appliquer les filtres métier de `LinkSearchRequest`.
15. Si `dedupe_url=True`, dédupliquer globalement par URL et agréger les marchés dans `market`.
16. Fermer la session dans un `finally`.

### Mapping candidat -> document

Reprendre les champs de `_document_link_row(...)` dans `main.py`, puis ajouter
`file_format`:

- `market`: marché normalisé.
- `source`: `candidate.source`.
- `source_document_id`: `candidate.source_document_id or ""`.
- `published_at`: `candidate.published_at or candidate.published_date`, format ISO ou chaîne vide.
- `period_end_date`: ISO ou chaîne vide.
- `reporting_year`: entier ou chaîne vide.
- `document_type`: `candidate.document_type`.
- `classification`: `candidate.classification or ""`.
- `title`: `candidate.title`.
- `url`: `candidate.url`.
- `issuer_name`: première valeur non vide parmi `metadata["issuer_name"]`, `metadata["issuer"]`, `metadata["company_name"]`.
- `issuer_isin`: première valeur non vide parmi `metadata["issuer_isin"]`, `metadata["issuer_isins"]`, `metadata["isin"]`.
- `issuer_lei`: première valeur non vide parmi `metadata["issuer_lei"]`, `metadata["lei"]`.
- `category`: `metadata["category"]` si présent.
- `file_format`: `metadata["file_format"]` si présent, sinon extension d'URL sans point, sinon chaîne vide.
- `date_confidence`: `candidate.date_confidence or ""`.
- `source_publication_date_raw`: `candidate.source_publication_date_raw or ""`.
- `metadata`: copie du dictionnaire `candidate.metadata`.

Pour les valeurs `list`, `tuple` ou `set`, joindre par `", "`.

### Tests à écrire

`tests/test_web_document_search.py`:

- reprend les scénarios de `tests/test_market_document_links.py`;
- vérifie que le service filtre les dates et déduplique par source/id;
- vérifie qu'une erreur connecteur n'empêche pas les autres marchés;
- vérifie que `dedupe_url=True` agrège les marchés;
- vérifie que la session est fermée même en cas d'erreur.

### Tests à lancer

```powershell
python -m pytest tests/test_web_document_search.py tests/test_market_document_links.py -q
```

## Lot 2 - Filtres métier

### Objectif

Isoler les filtres de résultats pour les réutiliser en service, API et exports.

### Fichier à créer

`webapp/services/filters.py`

### Fonctions attendues

```python
def normalize_search_text(value: object) -> str:
    ...

def document_matches_query(document: LinkSearchDocument, query: str | None) -> bool:
    ...

def filter_documents(
    documents: tuple[LinkSearchDocument, ...],
    *,
    document_types: tuple[str, ...] = (),
    query: str | None = None,
    issuer_isin: str | None = None,
    sources: tuple[str, ...] = (),
    formats: tuple[str, ...] = (),
    date_confidences: tuple[str, ...] = (),
) -> tuple[LinkSearchDocument, ...]:
    ...
```

### Règles de filtre

- `document_types`: comparaison exacte sur `document.document_type`.
- `query`: recherche insensible à la casse et aux accents dans `title`,
  `issuer_name`, `issuer_isin`, `issuer_lei`, `category`, `source`,
  `source_document_id`.
- `issuer_isin`: comparaison insensible à la casse; accepter si l'ISIN est
  contenu dans une liste jointe par virgules.
- `sources`: comparaison exacte sur `source`.
- `formats`: comparaison exacte sur `file_format`.
- `date_confidences`: comparaison exacte sur `date_confidence`.
- Un filtre vide ne filtre rien.

### Tests à écrire

`tests/test_web_filters.py`:

- filtre par type annuel;
- filtre par texte accentué/non accentué;
- filtre par ISIN dans une liste jointe;
- filtre par source;
- filtre par format;
- filtre par confiance de date;
- composition de plusieurs filtres.

### Tests à lancer

```powershell
python -m pytest tests/test_web_filters.py tests/test_web_document_search.py -q
```

## Lot 3 - Adapter la CLI existante au service

### Objectif

La CLI continue à produire les mêmes CSV/JSON, mais elle délègue la recherche
au nouveau service.

### Fichiers à modifier

- `main.py`
- `webapp/services/exports.py`

### Exports à implémenter

Dans `webapp/services/exports.py`:

```python
CSV_FIELDNAMES = (
    "market",
    "source",
    "source_document_id",
    "published_at",
    "period_end_date",
    "reporting_year",
    "document_type",
    "classification",
    "title",
    "url",
    "issuer_name",
    "issuer_isin",
    "issuer_lei",
    "category",
    "date_confidence",
    "source_publication_date_raw",
)

def documents_to_rows(documents: tuple[LinkSearchDocument, ...]) -> list[dict[str, object]]:
    ...

def write_search_export(result_set: LinkSearchResultSet, *, output_format: str, output_dir: str | Path) -> Path:
    ...
```

Important: pour compatibilité avec les tests existants,
`discover-market-documents` ne doit pas ajouter `file_format`, `job_id` ou
`created_at` dans le CSV historique. Ces champs pourront être ajoutés aux
exports web plus tard via une fonction séparée.

### Modification attendue dans `main.py`

Conserver la fonction publique:

```python
def discover_market_document_links(...) -> MarketDocumentLinksExport:
    ...
```

Mais remplacer son coeur par:

1. construire `LinkSearchRequest`;
2. appeler `DocumentSearchService(settings, ...).search_links(request)`;
3. appeler `write_search_export(...)`;
4. retourner `MarketDocumentLinksExport(output_path=..., documents_count=..., errors=..., warnings=...)`.

### Tests à lancer

```powershell
python -m pytest tests/test_market_document_links.py tests/test_web_document_search.py tests/test_web_filters.py -q
```

### Critère de fin

Les fichiers CSV/JSON produits par la CLI ont les mêmes champs et le même
comportement qu'avant.

## Lot 4 - Persistance SQLite des recherches web

### Objectif

Stocker les jobs et les résultats de recherche sans polluer les tables
`documents` et `download_runs`.

### Fichiers à modifier ou créer

- `db.py`
- `webapp/repositories.py`
- `tests/test_web_repository.py`

### Modification minimale dans `db.py`

Ajouter une méthode à `Database`:

```python
def initialize_web_search_schema(self) -> None:
    ...
```

Cette méthode crée:

- `web_search_jobs`;
- `web_search_market_runs`;
- `web_search_results`;
- les index décrits dans `ARCHITECTURE_WEBAPP.md`.

Ne pas appeler automatiquement cette méthode dans `Database.initialize()` au
premier commit si cela risque de perturber les tests historiques. L'app web
l'appellera explicitement au démarrage.

### Repository attendu

Dans `webapp/repositories.py`, créer:

```python
class WebSearchRepository:
    def __init__(self, database: Database) -> None:
        ...

    def create_job(self, job_id: str, request: LinkSearchRequest) -> None:
        ...

    def mark_job_running(self, job_id: str) -> None:
        ...

    def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        results_count: int,
        warnings: tuple[str, ...],
        errors: tuple[str, ...],
    ) -> None:
        ...

    def upsert_market_run(self, job_id: str, summary: MarketSearchSummary) -> None:
        ...

    def replace_results(self, job_id: str, documents: tuple[LinkSearchDocument, ...]) -> None:
        ...

    def get_job(self, job_id: str) -> dict[str, object] | None:
        ...

    def list_market_runs(self, job_id: str) -> list[dict[str, object]]:
        ...

    def list_results(
        self,
        job_id: str,
        *,
        document_type: str | None = None,
        market: str | None = None,
        source: str | None = None,
        q: str | None = None,
        issuer_isin: str | None = None,
        sort: str = "-published_at",
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, object]], int]:
        ...
```

### SQL attendu

- Toujours utiliser des paramètres SQL, jamais de concaténation utilisateur.
- `sort` doit être mappé via une whitelist:
  - `published_at` -> `published_at ASC`
  - `-published_at` -> `published_at DESC`
  - `title` -> `title ASC`
  - `market` -> `market ASC`
  - `document_type` -> `document_type ASC`
- Pagination:
  - `page = max(page, 1)`;
  - `page_size` entre 1 et 200;
  - `offset = (page - 1) * page_size`.

### Tests à écrire

`tests/test_web_repository.py`:

- création du schéma;
- création d'un job;
- insertion et remplacement de résultats;
- récupération paginée;
- filtre type, marché, source, texte libre;
- whitelist de tri;
- `get_job(...)` retourne les erreurs/warnings JSON décodés.

### Tests à lancer

```powershell
python -m pytest tests/test_web_repository.py tests/test_db.py -q
```

## Lot 5 - Gestionnaire de jobs local

### Objectif

Exécuter les recherches longues en arrière-plan et exposer leur progression.

### Fichier à créer

`webapp/jobs.py`

### Interface attendue

```python
class JobManager:
    def __init__(
        self,
        *,
        repository: WebSearchRepository,
        search_service: DocumentSearchService,
        max_workers: int = 2,
    ) -> None:
        ...

    def submit(self, request: LinkSearchRequest) -> str:
        ...

    def get_status(self, job_id: str) -> dict[str, object] | None:
        ...

    def cancel(self, job_id: str) -> bool:
        ...

    def shutdown(self) -> None:
        ...
```

### Comportement exact

- `submit(...)` génère un UUID4 hexadécimal.
- `submit(...)` persiste le job en `queued`.
- Le worker passe le job en `running`.
- Le worker appelle `search_service.search_links(request)`.
- Le worker persiste les `market_summaries` et les résultats.
- Statut final:
  - `done` si `errors == ()`;
  - `partial` si `errors` non vide et au moins un résultat;
  - `failed` si `errors` non vide et aucun résultat;
  - `cancelled` seulement si le job n'a pas encore commencé ou si une annulation coopérative est ajoutée plus tard.
- `cancel(...)` doit au minimum annuler un futur non démarré.
- `shutdown(...)` ferme le `ThreadPoolExecutor`.

### Tests à écrire

Ajouter dans `tests/test_web_repository.py` ou créer
`tests/test_web_jobs.py`:

- `submit(...)` crée un job;
- un service fake retourne un résultat et le job finit `done`;
- un service fake avec erreur et résultat finit `partial`;
- un service fake avec erreur sans résultat finit `failed`;
- `cancel(...)` ne casse pas si le job est déjà fini.

### Tests à lancer

```powershell
python -m pytest tests/test_web_repository.py tests/test_web_document_search.py -q
```

## Lot 6 - API FastAPI

### Objectif

Exposer les recherches, statuts, résultats et exports en JSON.

### Dépendances à ajouter

Dans `requirements.txt`:

```text
fastapi>=0.115,<1
uvicorn[standard]>=0.30,<1
jinja2>=3.1,<4
python-multipart>=0.0.9,<1
```

Ne pas retirer les dépendances existantes.

### Fichiers à créer ou modifier

- `webapp/schemas.py`
- `webapp/app.py`
- `tests/test_web_api.py`

### Schémas Pydantic attendus

Dans `webapp/schemas.py`:

```python
from datetime import date
from pydantic import BaseModel, Field

class SearchCreateRequest(BaseModel):
    markets: list[str] = Field(min_length=1)
    date_from: date
    date_to: date
    document_types: list[str] = []
    query: str | None = None
    issuer_isin: str | None = None
    sources: list[str] = []
    formats: list[str] = []
    date_confidences: list[str] = []
    max_candidates: int = Field(default=100000, ge=1, le=500000)
    dedupe_url: bool = False

class SearchCreateResponse(BaseModel):
    job_id: str
    status_url: str
    results_url: str

class SearchStatusResponse(BaseModel):
    job_id: str
    status: str
    results_count: int
    warnings: list[str]
    errors: list[str]
    markets: list[dict[str, object]]

class SearchResultsResponse(BaseModel):
    job_id: str
    total: int
    page: int
    page_size: int
    results: list[dict[str, object]]
```

### App factory attendue

Dans `webapp/app.py`:

```python
def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    job_manager: JobManager | None = None,
) -> FastAPI:
    ...
```

La factory doit:

1. Charger `Settings.from_env()` si `settings is None`.
2. Créer `Database(settings.db_path)` si `database is None`.
3. Appeler `database.initialize_web_search_schema()`.
4. Construire `WebSearchRepository`.
5. Construire `DocumentSearchService`.
6. Construire `JobManager` si non fourni.
7. Enregistrer les routes API et HTML.

### Routes API attendues

- `GET /api/markets`
  - retourne `{"markets": list(SUPPORTED_WATCH_MARKETS)}`.
- `GET /api/document-types`
  - retourne les types et libellés.
- `POST /api/searches`
  - crée un job et retourne `SearchCreateResponse`.
- `GET /api/searches/{job_id}`
  - retourne `404` si inconnu.
- `GET /api/searches/{job_id}/results`
  - accepte `document_type`, `market`, `source`, `q`, `issuer_isin`,
    `sort`, `page`, `page_size`.
- `GET /api/searches/{job_id}/export`
  - `format=csv|json`; retourne `404` si job inconnu.
- `POST /api/searches/{job_id}/cancel`
  - retourne `{ "cancelled": true|false }`.
- `GET /api/health`
  - retourne `{ "status": "ok" }`.

### Validation

- Si `date_from > date_to`, répondre HTTP 422 ou 400 avec message clair.
- Si un marché est inconnu, ne pas bloquer la création du job: le service
  enregistrera une erreur de marché.
- Ne jamais renvoyer de trace Python dans une réponse API.

### Tests API

`tests/test_web_api.py` avec `fastapi.testclient.TestClient`:

- `GET /api/markets` retourne au moins `Euronext Paris`;
- `POST /api/searches` retourne un `job_id`;
- `GET /api/searches/{job_id}` retourne le statut;
- `GET /api/searches/unknown` retourne 404;
- `GET /api/searches/{job_id}/results` respecte pagination;
- `GET /api/document-types` retourne `annual_financial_report`;
- `GET /api/health` retourne `ok`.

Utiliser un `JobManager` fake injecté dans `create_app(...)` pour éviter les
appels réseau dans les tests API.

### Tests à lancer

```powershell
python -m pytest tests/test_web_api.py tests/test_web_repository.py -q
```

## Lot 7 - Interface HTML

### Objectif

Fournir une interface utilisable sans SPA.

### Fichiers à compléter

- `webapp/templates/layout.html`
- `webapp/templates/search.html`
- `webapp/templates/results.html`
- `webapp/templates/partials/job_status.html`
- `webapp/templates/partials/results_table.html`
- `webapp/static/app.css`
- `webapp/static/app.js`
- `webapp/app.py`

### Routes HTML attendues

- `GET /`
  - rend `search.html`.
- `GET /searches/{job_id}`
  - rend `results.html`.
- `GET /partials/searches/{job_id}/status`
  - rend `partials/job_status.html`.
- `GET /partials/searches/{job_id}/results`
  - rend `partials/results_table.html`.

### Comportement UI

Page `/`:

- formulaire avec:
  - multi-select marchés;
  - `date_from`;
  - `date_to`;
  - checkboxes types de documents;
  - champ texte libre;
  - champ ISIN;
  - checkbox déduplication URL;
  - bouton lancer.
- au submit, créer le job via `POST /api/searches`, puis rediriger vers
  `/searches/{job_id}`.

Page `/searches/{job_id}`:

- statut global;
- progression par marché;
- filtres secondaires;
- tableau paginé;
- bouton export CSV;
- bouton export JSON.

Tableau:

- date;
- marché;
- émetteur;
- ISIN;
- type;
- titre;
- source;
- catégorie;
- format;
- confiance date;
- action ouvrir.

Actions:

- "Ouvrir" pointe vers `url`, `target="_blank"`, `rel="noopener noreferrer"`.
- "Copier" utilise `navigator.clipboard.writeText(url)` si disponible.

### Contraintes UI

- Pas de gros texte marketing.
- Priorité à une interface dense et lisible.
- Les erreurs par marché doivent rester visibles.
- Les valeurs utilisateur doivent être échappées par Jinja.
- Les liens externes doivent afficher leur domaine ou source.

### Tests

Tests minimaux dans `tests/test_web_api.py`:

- `GET /` retourne 200 et contient le formulaire;
- `GET /searches/{job_id}` retourne 200 avec un job fake;
- les liens de résultat contiennent `rel="noopener noreferrer"`.

### Vérification manuelle

```powershell
python -m uvicorn webapp.app:create_app --factory --reload --host 127.0.0.1 --port 8000
```

Ouvrir `http://127.0.0.1:8000`.

## Lot 8 - Commande `serve` et configuration web

### Objectif

Permettre de lancer la webapp depuis le projet sans connaître Uvicorn.

### Fichiers à modifier

- `main.py`
- `config.py`
- `.env.example`
- `README.md`

### Variables d'environnement à ajouter

Dans `config.py`, ajouter des champs `Settings` avec valeurs par défaut:

- `web_host: str = "127.0.0.1"`
- `web_port: int = 8000`
- `web_workers: int = 2`
- `web_max_period_days: int = 370`
- `web_max_candidates: int = 100000`

Dans `.env.example`:

```dotenv
INFOFIN_WEB_HOST=127.0.0.1
INFOFIN_WEB_PORT=8000
INFOFIN_WEB_WORKERS=2
INFOFIN_WEB_MAX_PERIOD_DAYS=370
INFOFIN_WEB_MAX_CANDIDATES=100000
```

### Commande CLI attendue

Dans `main.py`, ajouter:

```powershell
python main.py serve
python main.py serve --host 127.0.0.1 --port 8000
```

Implémentation:

- importer `uvicorn` seulement dans le bloc de commande `serve`;
- appeler `uvicorn.run("webapp.app:create_app", factory=True, host=..., port=...)`;
- ne pas initialiser le watcher ou la base documents hors web schema.

### Tests

- Test parser: vérifier que `python main.py serve --help` ne plante pas.
- Ne pas lancer de serveur dans les tests unitaires.

## Lot 9 - Exports web enrichis et purge

### Objectif

Ajouter les finitions utiles pour un usage réel.

### Exports web enrichis

Créer une fonction séparée dans `webapp/services/exports.py`:

```python
def write_web_results_export(
    *,
    rows: list[dict[str, object]],
    output_format: str,
    target: Path,
) -> Path:
    ...
```

Champs web:

- tous les champs historiques;
- `file_format`;
- `job_id`;
- `created_at`.

### Purge

Dans `WebSearchRepository`:

```python
def purge_jobs_older_than(self, cutoff_iso: str) -> int:
    ...
```

Ajouter une commande:

```powershell
python main.py purge-web-searches --older-than-days 30
```

La purge supprime les jobs et, via `ON DELETE CASCADE`, leurs résultats et
market runs.

## Lot 10 - Documentation finale

### README

Mettre à jour `README.md` avec:

- section "Recherche de liens sans téléchargement";
- section "Webapp cible";
- commande `discover-market-documents`;
- commande `serve` quand elle existe;
- liens vers `ARCHITECTURE_WEBAPP.md` et ce plan.

### Vérifications finales

```powershell
python -m pytest -q
python main.py discover-market-documents --market "Euronext Paris" --date-from 2026-01-01 --date-to 2026-01-02 --format json
python main.py serve --help
```

La commande `discover-market-documents` peut faire du réseau. Si les tests
doivent rester offline, ne pas l'ajouter à la CI.

## Définition de terminé MVP

Le MVP est terminé quand:

- `/` affiche un formulaire de recherche.
- Une recherche crée un job persistant.
- Le job interroge les connecteurs en source-first.
- Les résultats s'affichent sans téléchargement serveur.
- Les filtres type, date, marché, source, texte et ISIN fonctionnent.
- Les exports CSV/JSON fonctionnent.
- Les erreurs par marché sont visibles.
- Les tests unitaires et API passent.
- La CLI historique `discover-market-documents` passe ses tests existants.

## Ordre de commits recommandé

1. `webapp: add search service and filters`
2. `cli: reuse web link search service`
3. `webapp: add search persistence repository`
4. `webapp: add local job manager`
5. `webapp: add FastAPI search API`
6. `webapp: add HTML search interface`
7. `cli: add serve command and web config`
8. `docs: document webapp architecture and development plan`
