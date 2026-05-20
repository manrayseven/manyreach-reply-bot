@echo off
REM Double-clique pour METTRE EN PAUSE le bot (il arrête de tourner/envoyer).
schtasks /Change /TN "ManyReach Reply Bot" /DISABLE
echo.
echo  ============================================================
echo   Bot MIS EN PAUSE. Il n'enverra plus rien.
echo   Pour le relancer : double-clique sur REACTIVER-LE-BOT.bat
echo  ============================================================
pause
