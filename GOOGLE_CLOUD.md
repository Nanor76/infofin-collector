# Déploiement Google Cloud à coût maîtrisé

Cette cible déploie la webapp sur Cloud Run en conservant le mode local
SQLite. En production, les recherches sont persistées dans Firestore. Le mode
standard les exécute dans un Cloud Run Job ; le mode `-WarmWorker` utilise
Cloud Tasks pour les exécuter dans le service web déjà maintenu chaud. Aucun
fichier SQLite n'est monté dans le conteneur.

```text
Navigateur -> Cloud Run service -> Firestore
                         |
                         +-> Cloud Tasks -> worker HTTP chaud -> sources officielles

Cloud Scheduler -> Job de purge quotidienne -> Firestore
```

Le déploiement est conçu pour un usage personnel ou une démonstration. Le mode
par défaut conserve zéro instance minimale afin de viser les quotas gratuits.
Le mode explicite `-Performance` garde une instance web chaude et active le
boost CPU au démarrage. Associé à `-WarmWorker`, il supprime le démarrage à
froid de 20 à 35 secondes observé avec Cloud Run Jobs : Cloud Tasks appelle un
endpoint interne du service chaud. La file limite l'exécution à une recherche
à la fois et désactive les relances automatiques. Tous les modes conservent une
instance maximale, 1 000 candidats maximum par source et une purge après 30
jours.

Le mode performance entraîne un coût d'inactivité prévisible, proche de 10 USD
par mois pour l'instance web chaude aux tarifs de référence. Il convient donc
notamment à un compte bénéficiant d'un crédit Cloud mensuel. Les garde-fous et
les crédits réduisent le risque de dépassement, mais ne constituent pas un
plafond de facturation. Un compte de facturation Google Cloud actif reste
requis.

Références tarifaires officielles :

- [Cloud Run](https://cloud.google.com/run/pricing) ;
- [quotas gratuits Firestore](https://cloud.google.com/firestore/quotas) ;
- [tarifs Cloud Tasks](https://cloud.google.com/tasks/pricing) ;
- [Cloud Scheduler](https://cloud.google.com/scheduler/pricing).

## Prérequis

1. Installer la [Google Cloud CLI](https://cloud.google.com/sdk/docs/install).
2. Créer un projet Google Cloud et y activer la facturation.
3. S'authentifier :

```powershell
gcloud auth login
gcloud auth application-default login
```

Le compte qui exécute le script doit pouvoir activer des API, créer des comptes
de service, modifier IAM, construire une image et déployer Cloud Run.

## Déploiement

Depuis la racine du dépôt :

```powershell
.\deploy\google-cloud.ps1 -ProjectId "mon-projet-google"
```

Pour privilégier la réactivité de l'interface tout en conservant le Cloud Run
Job :

```powershell
.\deploy\google-cloud.ps1 `
  -ProjectId "mon-projet-google" `
  -Performance
```

Pour supprimer le démarrage à froid du worker, utiliser le mode chaud après
avoir créé le secret HTTP décrit plus bas :

```powershell
.\deploy\google-cloud.ps1 `
  -ProjectId "mon-projet-google" `
  -Performance `
  -WarmWorker `
  -Public `
  -AccessUsername "infofin" `
  -AccessPasswordSecret "infofin-web-password"
```

La région par défaut est `europe-west1` (Belgique). Pour en choisir une autre :

```powershell
.\deploy\google-cloud.ps1 `
  -ProjectId "mon-projet-google" `
  -Region "europe-west9"
```

Le script est rejouable. Il réalise les opérations suivantes :

- activation des API Cloud Run, Cloud Tasks, Cloud Build, Artifact Registry,
  Firestore, Cloud Scheduler et Secret Manager ;
- création du dépôt Docker et de la base Firestore Native `(default)` ;
- création de comptes de service séparés pour la webapp, les jobs et le
  planificateur ;
- construction d'une image depuis `Dockerfile` ;
- création de la file `infofin-search-queue` en mode worker chaud, déploiement
  du service `infofin-web` et du job `infofin-purge` ;
- création d'une tâche Scheduler quotidienne à 03:00, heure de Paris.

Le script autorise le compte `gcloud` actif à invoquer le service privé. Pour
l'ouvrir :

```powershell
gcloud run services proxy infofin-web --region=europe-west1 --port=8080
```

Puis consulter `http://127.0.0.1:8080`. Le calcul et les données restent dans
Google Cloud ; seul l'accès HTTP passe par le proxy authentifié local.

Pour publier volontairement une URL anonyme :

```powershell
.\deploy\google-cloud.ps1 `
  -ProjectId "mon-projet-google" `
  -Public
```

Ce mode est déconseillé pour la cible gratuite : toute personne possédant
l'URL peut créer des recherches et consommer les quotas Cloud Run et
Firestore.

Pour utiliser directement l'URL depuis un smartphone, publier le service tout
en protégeant toutes ses routes par un mot de passe HTTP. Créer d'abord le
secret sans enregistrer sa valeur dans le dépôt :

```powershell
gcloud services enable secretmanager.googleapis.com
gcloud secrets create infofin-web-password --replication-policy=automatic

$PasswordBytes = New-Object byte[] 24
[Security.Cryptography.RandomNumberGenerator]::Fill($PasswordBytes)
$Password = [Convert]::ToBase64String($PasswordBytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
$SecretFile = New-TemporaryFile
[IO.File]::WriteAllText($SecretFile.FullName, $Password)
gcloud secrets versions add infofin-web-password --data-file=$SecretFile.FullName
Remove-Item -LiteralPath $SecretFile.FullName
Write-Host "Utilisateur : infofin"
Write-Host "Mot de passe : $Password"
```

Déployer ensuite la protection :

```powershell
.\deploy\google-cloud.ps1 `
  -ProjectId "mon-projet-google" `
  -Public `
  -AccessUsername "infofin" `
  -AccessPasswordSecret "infofin-web-password"
```

Ajouter `-Performance -WarmWorker` à cette commande pour éliminer le démarrage
à froid du worker. Ce mode nécessite volontairement `-Public` et un secret :
Cloud Tasks réutilise l'authentification HTTP pour appeler l'endpoint interne.

Le navigateur affiche sa boîte de dialogue d'authentification native. La
valeur du secret est injectée dans Cloud Run sans apparaître dans l'image ni
dans les variables ordinaires. Pour retrouver ultérieurement le mot de passe :

```powershell
gcloud secrets versions access latest --secret=infofin-web-password
```

Pour ne pas créer la tâche planifiée :

```powershell
.\deploy\google-cloud.ps1 `
  -ProjectId "mon-projet-google" `
  -SkipScheduler
```

## Configuration de production

Le script configure ces variables dans Cloud Run :

```dotenv
INFOFIN_WEB_STORAGE_BACKEND=firestore
INFOFIN_WEB_JOB_BACKEND=cloud-tasks
GOOGLE_CLOUD_PROJECT=mon-projet-google
GOOGLE_CLOUD_REGION=europe-west1
INFOFIN_CLOUD_RUN_JOB=infofin-search
INFOFIN_CLOUD_TASKS_QUEUE=infofin-search-queue
INFOFIN_WEB_SERVICE_URL=https://infofin-web-123456789.europe-west1.run.app
INFOFIN_FIRESTORE_PREFIX=infofin_web
INFOFIN_WEB_MAX_CANDIDATES=1000
INFOFIN_WEB_ACCESS_USERNAME=infofin
# INFOFIN_WEB_ACCESS_PASSWORD provient de Secret Manager.
POLAND_KNF_OAM_MAX_PAGES_PER_DATE=25
```

La limite KNF autorise les journées polonaises volumineuses (notamment celles
qui nécessitent 18 pages), tout en conservant un plafond explicite à 25 pages
par date.

En local, les valeurs par défaut restent `sqlite` et `local`. Les tests
standards n'utilisent donc ni identifiants Google, ni réseau, ni données de
production.

Firestore ne nécessite aucun index composite pour cette implémentation : les
résultats sont rangés dans une sous-collection propre à chaque recherche, puis
filtrés en mémoire. Ce choix convient aux petits volumes visés par le quota
gratuit, pas à une application publique à fort trafic.

## Contrôle des coûts

Après le premier déploiement :

1. créer un budget et des alertes dans **Facturation > Budgets et alertes** ;
2. vérifier que le service indique `Minimum instances: 0` en mode économique
   ou `1` avec `-Performance`, et toujours `Maximum instances: 1` ;
3. surveiller les lectures/écritures Firestore, les exécutions Cloud Tasks et
   le temps CPU du service ;
4. conserver une seule image récente dans Artifact Registry si les anciennes
   révisions deviennent volumineuses.

Les alertes de budget préviennent mais n'arrêtent pas automatiquement les
ressources. Une recherche couvrant beaucoup de marchés ou une forte exposition
publique peut dépasser les quotas gratuits.

## Vérification

Avec le proxy privé lancé, vérifier ensuite :

```powershell
Invoke-RestMethod "http://127.0.0.1:8080/api/health"
```

La réponse attendue est :

```json
{
  "status": "ok",
  "storage_backend": "firestore",
  "job_backend": "cloud-tasks"
}
```

Les journaux sont disponibles avec :

```powershell
gcloud run services logs read infofin-web --region=europe-west1
gcloud tasks queues describe infofin-search-queue --location=europe-west1
```

## Suppression

Pour arrêter toute utilisation, supprimer le service, les jobs et la tâche
planifiée. La base Firestore est volontairement conservée afin d'éviter une
perte de données accidentelle :

```powershell
gcloud scheduler jobs delete infofin-purge-daily --location=europe-west1
gcloud tasks queues delete infofin-search-queue --location=europe-west1
gcloud run services delete infofin-web --region=europe-west1
gcloud run jobs delete infofin-search --region=europe-west1
gcloud run jobs delete infofin-purge --region=europe-west1
```
