@echo off
chcp 65001 >nul
cd /d "C:\Users\ManRa\Desktop\Rudy111\Claude\Manyreach Answers"

:menu
cls
echo.
echo  ============================================================
echo   MANYREACH ANSWERS - Bot reply automatique
echo  ============================================================
echo.
echo   Que veux-tu faire ?
echo.
echo   [1] Voir tes VRAIS prospects + les drafts du bot
echo       (Interested, rdv, etc. - ignore les bounces)
echo.
echo   [2] Traiter TOUS les replies recents (inclut bounces/MailInBlack)
echo.
echo   [3] Revoir les derniers drafts deja generes
echo.
echo   [4] Ouvrir le dossier projet
echo.
echo   [5] Quitter
echo.
set /p choice=" Tape 1, 2, 3, 4 ou 5 puis Entree : "

if "%choice%"=="1" goto important
if "%choice%"=="2" goto allreplies
if "%choice%"=="3" goto drafts
if "%choice%"=="4" goto folder
if "%choice%"=="5" exit
goto menu

:important
cls
echo.
echo  ============================================================
echo   Tes VRAIS prospects + drafts (mode TEST, aucun mail envoye)
echo   Patience, 30 a 90 secondes...
echo  ============================================================
echo.
py scripts\run_bot.py --important-only --since-days 90 --limit 8 --reprocess
echo.
echo  ============================================================
echo   Ouverture de la page des drafts dans ton navigateur...
echo  ============================================================
py scripts\view_drafts.py
echo.
echo   Appuie sur une touche pour revenir au menu.
pause >nul
goto menu

:allreplies
cls
echo.
echo  ============================================================
echo   TOUS les replies recents (mode TEST, aucun mail envoye)
echo   Patience, 30 a 60 secondes...
echo  ============================================================
echo.
py scripts\run_bot.py --limit 10
echo.
echo  ============================================================
echo   Termine. Appuie sur une touche pour revenir au menu.
echo  ============================================================
pause >nul
goto menu

:drafts
cls
echo.
echo  ============================================================
echo   Generation de la page des drafts (ouverture navigateur)
echo  ============================================================
echo.
py scripts\view_drafts.py
echo.
echo  ============================================================
echo   La page s'est ouverte dans ton navigateur.
echo   Appuie sur une touche pour revenir au menu.
echo  ============================================================
pause >nul
goto menu

:folder
explorer "%~dp0"
goto menu
