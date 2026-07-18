param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,
    [string]$Region = "europe-west1",
    [string]$ServiceName = "infofin-web",
    [string]$SearchJobName = "infofin-search",
    [string]$SearchQueueName = "infofin-search-queue",
    [string]$PurgeJobName = "infofin-purge",
    [string]$ArtifactRepository = "infofin",
    [switch]$Public,
    [switch]$Performance,
    [switch]$WarmWorker,
    [string]$AccessUsername = "infofin",
    [string]$AccessPasswordSecret = "",
    [string]$BetaUsersSecret = "",
    [string]$BetaSessionSecret = "",
    [string]$WorkerTokenSecret = "",
    [int]$BetaDailySearchLimit = 3,
    [string]$ContactEmail = "",
    [string]$LegalPublisher = "InfoFin",
    [switch]$SkipScheduler
)

$ErrorActionPreference = "Stop"

function Invoke-Gcloud {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & gcloud @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Échec de gcloud $($Arguments -join ' ')"
    }
}

function Test-GcloudResource {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & gcloud @Arguments *> $null
    return $LASTEXITCODE -eq 0
}

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "Google Cloud CLI (gcloud) est requis."
}
if ($AccessPasswordSecret -and -not $Public) {
    throw "AccessPasswordSecret nécessite le mode Public."
}
if (($BetaUsersSecret -or $BetaSessionSecret) -and -not ($BetaUsersSecret -and $BetaSessionSecret)) {
    throw "BetaUsersSecret et BetaSessionSecret doivent être fournis ensemble."
}
if ($BetaUsersSecret -and -not $Public) {
    throw "La bêta avec comptes individuels nécessite le mode Public."
}
if ($BetaUsersSecret -and -not $WorkerTokenSecret) {
    throw "La bêta Cloud nécessite WorkerTokenSecret."
}
if ($BetaUsersSecret -and $AccessPasswordSecret) {
    throw "Choisissez les comptes bêta ou l'ancien mot de passe partagé, pas les deux."
}
if ($BetaDailySearchLimit -lt 1) {
    throw "BetaDailySearchLimit doit être supérieur ou égal à 1."
}
foreach ($Value in @($ContactEmail, $LegalPublisher)) {
    if ($Value -match "[,=]") {
        throw "ContactEmail et LegalPublisher ne doivent contenir ni virgule ni signe égal."
    }
}
if ($AccessUsername -match "[,=]") {
    throw "AccessUsername ne doit contenir ni virgule ni signe égal."
}
if ($WarmWorker -and -not $Performance) {
    throw "WarmWorker nécessite Performance pour maintenir le service chaud."
}
if ($WarmWorker -and -not $Public) {
    throw "WarmWorker nécessite Public."
}
if ($WarmWorker -and $BetaUsersSecret -and -not $WorkerTokenSecret) {
    throw "La bêta WarmWorker nécessite WorkerTokenSecret."
}
if ($WarmWorker -and -not $BetaUsersSecret -and -not $AccessPasswordSecret) {
    throw "WarmWorker nécessite AccessPasswordSecret ou les secrets de bêta."
}

Invoke-Gcloud config set project $ProjectId
Invoke-Gcloud services enable `
    run.googleapis.com `
    artifactregistry.googleapis.com `
    cloudbuild.googleapis.com `
    firestore.googleapis.com `
    cloudtasks.googleapis.com `
    cloudscheduler.googleapis.com `
    secretmanager.googleapis.com

if (-not (Test-GcloudResource artifacts repositories describe $ArtifactRepository --location=$Region)) {
    Invoke-Gcloud artifacts repositories create $ArtifactRepository `
        --repository-format=docker `
        --location=$Region `
        --description="Images InfoFin"
}

if (-not (Test-GcloudResource firestore databases describe '--database=(default)')) {
    Invoke-Gcloud firestore databases create `
        '--database=(default)' `
        --location=$Region `
        --type=firestore-native
}

$WebServiceAccountId = "infofin-web"
$JobServiceAccountId = "infofin-job"
$SchedulerServiceAccountId = "infofin-scheduler"
$WebServiceAccount = "$WebServiceAccountId@$ProjectId.iam.gserviceaccount.com"
$JobServiceAccount = "$JobServiceAccountId@$ProjectId.iam.gserviceaccount.com"
$SchedulerServiceAccount = "$SchedulerServiceAccountId@$ProjectId.iam.gserviceaccount.com"

foreach ($ServiceAccountId in @(
    $WebServiceAccountId,
    $JobServiceAccountId,
    $SchedulerServiceAccountId
)) {
    if (-not (Test-GcloudResource iam service-accounts describe "$ServiceAccountId@$ProjectId.iam.gserviceaccount.com")) {
        Invoke-Gcloud iam service-accounts create $ServiceAccountId `
            --display-name="InfoFin $ServiceAccountId"
    }
}

foreach ($ServiceAccount in @($WebServiceAccount, $JobServiceAccount)) {
    Invoke-Gcloud projects add-iam-policy-binding $ProjectId `
        --member="serviceAccount:$ServiceAccount" `
        --role=roles/datastore.user `
        --condition=None `
        --quiet
}

if ($WarmWorker) {
    Invoke-Gcloud projects add-iam-policy-binding $ProjectId `
        --member="serviceAccount:$WebServiceAccount" `
        --role=roles/cloudtasks.enqueuer `
        --condition=None `
        --quiet

    $QueueArguments = @(
        $SearchQueueName,
        "--location=$Region",
        "--max-concurrent-dispatches=1",
        "--max-dispatches-per-second=1",
        "--max-attempts=1",
        "--max-retry-duration=0s",
        "--quiet"
    )
    if (Test-GcloudResource tasks queues describe $SearchQueueName --location=$Region) {
        Invoke-Gcloud tasks queues update @QueueArguments
    }
    else {
        Invoke-Gcloud tasks queues create @QueueArguments
    }
}

foreach ($SecretName in @(
    $AccessPasswordSecret,
    $BetaUsersSecret,
    $BetaSessionSecret,
    $WorkerTokenSecret
)) {
    if (-not $SecretName) {
        continue
    }
    if (-not (Test-GcloudResource secrets describe $SecretName --project=$ProjectId)) {
        throw "Le secret Secret Manager '$SecretName' est introuvable."
    }
    Invoke-Gcloud secrets add-iam-policy-binding $SecretName `
        --project=$ProjectId `
        --member="serviceAccount:$WebServiceAccount" `
        --role=roles/secretmanager.secretAccessor `
        --quiet
}

$Image = "$Region-docker.pkg.dev/$ProjectId/$ArtifactRepository/infofin:latest"
Invoke-Gcloud builds submit --ignore-file=.dockerignore --tag=$Image .

$CommonEnvironment = "INFOFIN_WEB_STORAGE_BACKEND=firestore,GOOGLE_CLOUD_PROJECT=$ProjectId,GOOGLE_CLOUD_REGION=$Region,INFOFIN_FIRESTORE_PREFIX=infofin_web,POLAND_KNF_OAM_MAX_PAGES_PER_DATE=25"
$SearchCpu = if ($Performance) { 2 } else { 1 }
$SearchMemory = if ($Performance) { "1Gi" } else { "512Mi" }
$WebMinInstances = if ($Performance) { 1 } else { 0 }

if (-not $WarmWorker) {
    Invoke-Gcloud run jobs deploy $SearchJobName `
        --image=$Image `
        --region=$Region `
        --service-account=$JobServiceAccount `
        --command=python `
        '--args=-m,webapp.run_job' `
        --set-env-vars=$CommonEnvironment `
        --cpu=$SearchCpu `
        --memory=$SearchMemory `
        --tasks=1 `
        --max-retries=0 `
        --task-timeout=3600s `
        --quiet

    Invoke-Gcloud run jobs add-iam-policy-binding $SearchJobName `
        --region=$Region `
        --member="serviceAccount:$WebServiceAccount" `
        --role=roles/run.developer `
        --quiet
}

$AuthenticationFlag = if ($Public) {
    "--allow-unauthenticated"
}
else {
    "--no-allow-unauthenticated"
}

$ProjectNumber = (& gcloud projects describe $ProjectId '--format=value(projectNumber)').Trim()
if ($LASTEXITCODE -ne 0 -or -not $ProjectNumber) {
    throw "Impossible d'identifier le numéro du projet."
}
$WebServiceUrl = "https://${ServiceName}-${ProjectNumber}.${Region}.run.app"
$WebJobEnvironment = if ($WarmWorker) {
    "INFOFIN_WEB_JOB_BACKEND=cloud-tasks,INFOFIN_CLOUD_TASKS_QUEUE=$SearchQueueName,INFOFIN_WEB_SERVICE_URL=$WebServiceUrl"
}
else {
    "INFOFIN_WEB_JOB_BACKEND=cloud-run,INFOFIN_CLOUD_RUN_JOB=$SearchJobName"
}
$WebTimeout = if ($WarmWorker) { "1800s" } else { "300s" }
$WebEnvironment = "$CommonEnvironment,$WebJobEnvironment,INFOFIN_WEB_MAX_CANDIDATES=1000"
$WebDeployArguments = @(
    "deploy", $ServiceName,
    "--image=$Image",
    "--region=$Region",
    "--service-account=$WebServiceAccount",
    $AuthenticationFlag,
    "--cpu=1",
    "--memory=512Mi",
    "--concurrency=20",
    "--min-instances=$WebMinInstances",
    "--max-instances=1",
    "--timeout=$WebTimeout",
    "--quiet"
)
if ($Performance) {
    $WebDeployArguments += "--cpu-boost"
}
if ($AccessPasswordSecret) {
    $WebEnvironment += ",INFOFIN_WEB_ACCESS_USERNAME=$AccessUsername"
    $WebDeployArguments += "--set-secrets=INFOFIN_WEB_ACCESS_PASSWORD=${AccessPasswordSecret}:latest"
}
if ($BetaUsersSecret) {
    $WebEnvironment += ",INFOFIN_BETA_DAILY_SEARCH_LIMIT=$BetaDailySearchLimit,INFOFIN_CONTACT_EMAIL=$ContactEmail,INFOFIN_LEGAL_PUBLISHER=$LegalPublisher"
    $WebDeployArguments += (
        "--update-secrets=" +
        "INFOFIN_BETA_USERS_JSON=${BetaUsersSecret}:latest," +
        "INFOFIN_BETA_SESSION_SECRET=${BetaSessionSecret}:latest," +
        "INFOFIN_WORKER_TOKEN=${WorkerTokenSecret}:latest"
    )
    $WebDeployArguments += "--remove-secrets=INFOFIN_WEB_ACCESS_PASSWORD"
}
$WebDeployArguments += "--set-env-vars=$WebEnvironment"
Invoke-Gcloud run @WebDeployArguments

Invoke-Gcloud run jobs deploy $PurgeJobName `
    --image=$Image `
    --region=$Region `
    --service-account=$JobServiceAccount `
    --command=python `
    '--args=-m,webapp.purge_firestore' `
    --set-env-vars="$CommonEnvironment,INFOFIN_WEB_RETENTION_DAYS=30" `
    --cpu=1 `
    --memory=512Mi `
    --tasks=1 `
    --max-retries=0 `
    --task-timeout=900s `
    --quiet

if (-not $SkipScheduler) {
    Invoke-Gcloud run jobs add-iam-policy-binding $PurgeJobName `
        --region=$Region `
        --member="serviceAccount:$SchedulerServiceAccount" `
        --role=roles/run.invoker `
        --quiet

    $SchedulerName = "infofin-purge-daily"
    $SchedulerUri = "https://run.googleapis.com/v2/projects/$ProjectId/locations/$Region/jobs/${PurgeJobName}:run"
    $SchedulerArguments = @(
        $SchedulerName,
        "--location=$Region",
        '--schedule=0 3 * * *',
        '--time-zone=Europe/Paris',
        "--uri=$SchedulerUri",
        '--http-method=POST',
        '--message-body={}',
        "--oauth-service-account-email=$SchedulerServiceAccount",
        '--oauth-token-scope=https://www.googleapis.com/auth/cloud-platform',
        '--max-retry-attempts=1',
        '--quiet'
    )
    if (Test-GcloudResource scheduler jobs describe $SchedulerName --location=$Region) {
        Invoke-Gcloud scheduler jobs update http @SchedulerArguments
    }
    else {
        Invoke-Gcloud scheduler jobs create http @SchedulerArguments
    }
}

$ServiceUrl = & gcloud run services describe $ServiceName `
    --region=$Region `
    '--format=value(status.url)'
if ($LASTEXITCODE -ne 0) {
    throw "Le service est déployé mais son URL n'a pas pu être lue."
}

Write-Host "InfoFin est déployé : $ServiceUrl"
if ($Public) {
    if ($BetaUsersSecret) {
        Write-Host "URL publique protégée par les comptes individuels de la bêta."
    }
    elseif ($AccessPasswordSecret) {
        Write-Host "URL publique protégée par l'utilisateur '$AccessUsername'."
    }
    else {
        Write-Warning "Le service est public : toute personne ayant l'URL peut lancer des jobs."
    }
}
else {
    $ActiveAccount = (& gcloud config get-value account).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $ActiveAccount) {
        throw "Impossible d'identifier le compte gcloud actif."
    }
    $MemberType = if ($ActiveAccount.EndsWith("gserviceaccount.com")) {
        "serviceAccount"
    }
    else {
        "user"
    }
    Invoke-Gcloud run services add-iam-policy-binding $ServiceName `
        --region=$Region `
        --member="${MemberType}:$ActiveAccount" `
        --role=roles/run.invoker `
        --quiet
    Write-Host "Service privé. Ouvrez un proxy authentifié avec :"
    Write-Host "  gcloud run services proxy $ServiceName --region=$Region --port=8080"
    Write-Host "Puis consultez http://127.0.0.1:8080"
}
Write-Host "Surveillez les quotas et la facturation dans Google Cloud Console."
