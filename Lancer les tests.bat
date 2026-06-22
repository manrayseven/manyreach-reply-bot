@echo off
REM Double-clique ce fichier pour lancer tous les tests du bot.
cd /d "%~dp0"
set PYTHONUTF8=1
python tests\run_all.py
echo.
pause
