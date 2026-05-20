@echo off
REM Exécution LIVE du bot (envoie réellement les réponses safes en heures ouvrées).
REM Lancé automatiquement par la tâche planifiée "ManyReach Reply Bot" toutes les 15 min.
cd /d "C:\Users\ManRa\Desktop\Rudy111\Claude\Manyreach Answers"
"C:\Users\ManRa\AppData\Local\Python\pythoncore-3.14-64\python.exe" scripts\run_bot.py --no-dry-run >> "logs\live_runs.log" 2>&1
