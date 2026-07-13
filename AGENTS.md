# Bonnes pratiques de developpement

Ces regles s'appliquent a toute modification du depot, y compris celles
realisees par une instance LLM.

## Obligation de maintenir la base de tests

Avant toute modification de l'application, lire integralement ce fichier,
`TESTING.md` et `tests/TEST_CATALOG.md`. Utiliser le catalogue pour identifier
les tests qui couvrent deja la zone concernee, puis confirmer l'inventaire avec
les commandes de collecte qui y sont indiquees.

Toute evolution de l'application doit etre accompagnee, dans la meme
modification, d'une creation ou d'une mise a jour des cas de tests automatises
pertinents. Une evolution sans test associe est incomplete. Cette regle couvre
notamment : fonctionnalite, correction de bug, route ou schema API, logique
metier, acces aux donnees, template, JavaScript, HTMX, export, etat conditionnel,
configuration ayant un effet a l'execution et changement de dependance.

Appliquer les regles suivantes selon la nature du changement :

- correction de bug : ajouter d'abord un test qui reproduit la regression, puis
  verifier qu'il passe avec le correctif ;
- logique Python, API, service ou repository : ajouter ou adapter les tests
  pytest au niveau le plus proche de la logique modifiee ;
- parcours utilisateur, template, JavaScript, carte ou interaction HTMX :
  adapter les garde-fous de `tests/test_web_api.py` et au moins un scenario dans
  `tests/e2e/` ;
- nouvel etat metier, vide, chargement, avertissement ou erreur : couvrir l'etat
  et son oracle visible ;
- modification de schema ou de donnees : couvrir la migration, la lecture et
  l'ecriture, puis mettre a jour les fixtures E2E si l'etat est visible ;
- suppression ou renommage fonctionnel : retirer les cas devenus obsoletes et
  mettre a jour le registre de couverture dans `TESTING.md` sans reduire la
  protection des autres comportements.

Un test ajoute doit echouer si le comportement qu'il protege regresse. Il ne
suffit pas d'ouvrir une page ou de verifier seulement un code HTTP lorsque le
risque porte sur une valeur metier ou une interaction.

## Qualite et isolation des tests

Les cas de tests doivent etre deterministes, independants et reproductibles :

- ne jamais utiliser la base de production, les donnees personnelles ou les
  fichiers de `data/` dans les tests ;
- ne pas appeler les sources officielles ou un CDN dans la suite standard ; les
  tests live restent explicitement opt-in ;
- utiliser une base temporaire, des fixtures metier stables et le serveur
  `tests/e2e/server.py` pour Playwright ;
- ne pas utiliser de temporisation arbitraire (`sleep`, `waitForTimeout`) ;
  attendre un etat observable avec les assertions auto-attendues de Playwright ;
- cibler les interactions avec `getByTestId`. Le texte metier peut servir a
  scoper un item repete, jamais un libelle traduit comme selecteur principal ;
- ne pas masquer une regression en relachant une assertion, en augmentant un
  delai, en ajoutant `test.skip`, `test.fixme`, `test.only` ou une nouvelle
  tentative sans expliquer et corriger la cause ;
- garder les donnees de test minimales tout en couvrant le cas nominal, les
  limites et les erreurs pertinentes.

## Testabilite de l'interface web

Toute creation ou modification d'interface web doit maintenir un contrat de
test UI stable avec des attributs `data-testid`.

Il est obligatoire d'attribuer un `data-testid` a chaque element utile aux
tests UI automatises, notamment :

- les controles interactifs : liens, boutons, champs, listes, cases a cocher,
  tris et zones graphiques cliquables ;
- les regions chargees ou remplacees de facon asynchrone, notamment par HTMX ;
- les etats, compteurs et valeurs metier servant d'oracles d'assertion ;
- les etats conditionnels tels que chargement, contenu vide, avertissement et
  erreur ;
- les conteneurs repetes servant a scoper une ligne, une carte ou un item avant
  de cibler ses valeurs et ses actions.

Ne pas ajouter de `data-testid` aux icones, wrappers de presentation ou textes
purement decoratifs qui ne servent ni d'action, ni de scope, ni d'assertion.

Les identifiants doivent :

- respecter la convention `<page>-<zone>-<cible>-<type>` en kebab-case decrite
  dans `TESTING.md` ;
- exprimer un role fonctionnel stable, jamais une classe CSS, un libelle traduit
  ou une position visuelle ;
- utiliser une cle metier stable pour une collection lorsque celle-ci existe ;
- rester communs aux items repetes lorsque le test doit d'abord scoper l'item
  par son contenu metier ;
- etre mis a jour avec prudence, car ils constituent une API pour les tests UI.

## Definition de termine pour une evolution de l'application

Avant de terminer une evolution :

1. ajouter ou mettre a jour les tests unitaires et d'integration concernes ;
2. pour une evolution web, verifier les templates rendus et les elements crees
   dynamiquement en JavaScript ;
3. mettre a jour les attentes et garde-fous de `tests/test_web_api.py` ;
4. ajouter ou adapter les parcours Playwright dans `tests/e2e/` ;
5. mettre a jour le registre de couverture, les conventions ou les exceptions
   dans `TESTING.md`, ainsi que le routage et l'inventaire dans
   `tests/TEST_CATALOG.md` ;
6. executer au minimum la suite web pytest et la suite Playwright indiquees dans
   `TESTING.md` ;
7. consigner dans le compte rendu final les tests crees ou modifies, les
   commandes executees et leur resultat exact.

Si une commande ne peut pas etre executee, ne jamais presenter l'evolution
comme entierement validee : indiquer la commande manquante, la cause et le
risque residuel.
