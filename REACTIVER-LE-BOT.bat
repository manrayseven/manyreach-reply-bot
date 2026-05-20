@echo off
REM Double-clique pour RÉACTIVER le bot (reprend toutes les 15 min).
schtasks /Change /TN "ManyReach Reply Bot" /ENABLE
echo.
echo  ============================================================
echo   Bot RÉACTIVÉ. Il traite les nouvelles réponses toutes les 15 min.
echo  ============================================================
pause
