@echo off
setlocal EnableExtensions

pushd "%~dp0" >nul

set "DAYS="
set /p "DAYS=Nombre de jours d'historique [1] : "
if "%DAYS%"=="" set "DAYS=1"

powershell -NoProfile -Command "if ($env:DAYS -match '^[1-9][0-9]*$') { exit 0 } else { exit 1 }" >nul
if errorlevel 1 (
    echo Erreur: le nombre de jours doit etre un entier positif.
    echo.
    pause
    popd >nul
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

set "EXIT_CODE=0"

echo.
echo ============================================================
echo  Recuperation financiere OAM - univers complet
echo ============================================================
echo.
echo Etape 1: mise a jour des CSV de societes cotees par place.
echo Etape 2: recuperation des rapports financiers OAM.
echo.

echo [1/2] Synchronisation des listes d'emetteurs...
echo Commande: %PYTHON% main.py sync-issuer-lists --import
echo.
"%PYTHON%" main.py sync-issuer-lists --import
if errorlevel 1 set "EXIT_CODE=1"

echo.
echo [2/2] Recuperation sur toutes les places disponibles...
echo Historique: %DAYS% jour(s)
echo Commande: %PYTHON% main.py watch --all --lookback-days %DAYS% --confirm-large-run --max-documents-per-run 5000
echo.
"%PYTHON%" main.py watch --all --lookback-days %DAYS% --confirm-large-run --max-documents-per-run 5000
if errorlevel 1 set "EXIT_CODE=1"

echo.
if "%EXIT_CODE%"=="0" (
    echo Recuperation terminee.
) else (
    echo Recuperation terminee avec erreur, code %EXIT_CODE%.
)

echo.
pause
popd >nul
exit /b %EXIT_CODE%