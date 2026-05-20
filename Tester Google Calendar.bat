@echo off
chcp 65001 >nul
cd /d "C:\Users\ManRa\Desktop\Rudy111\Claude\Manyreach Answers"
echo.
echo  ============================================================
echo   Test connexion Google Calendar
echo  ============================================================
echo.
py scripts\check_calendar.py
echo.
echo  ============================================================
echo   Test termine. Le resultat est aussi dans calendar_test.txt
echo   Appuie sur une touche pour fermer.
echo  ============================================================
pause >nul
