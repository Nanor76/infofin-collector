@echo off
cd /d "%~dp0"
echo ==========================================
echo Démarrage de l'application Web InfoFin...
echo ==========================================
python main.py serve
if %errorlevel% neq 0 (
    echo Erreur lors du lancement de l'application.
    pause
)
