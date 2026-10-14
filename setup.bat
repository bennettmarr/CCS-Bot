@echo off
cd /d "%~dp0"
echo ============================================
echo  CCS Bot setup
echo ============================================
echo.
echo This copies your private files (token, Google key, saved state)
echo from your OLD bot folder into this one, then installs the libraries.
echo.
set "OLD=C:\path\to\old\ccsbot"
set /p "OLD=Old bot folder [%OLD%]: "
echo.
for %%F in (config.json service_account.json) do (
  if exist "%OLD%\%%F" (copy /y "%OLD%\%%F" "%%F" >nul & echo   copied %%F) else (echo   MISSING %%F in old folder)
)
if not exist data mkdir data
for %%F in (state.json identities.json) do (
  if exist "%OLD%\data\%%F" (copy /y "%OLD%\data\%%F" "data\%%F" >nul & echo   copied data\%%F) else (echo   no data\%%F in old folder - fine if this is a fresh start)
)
echo.
echo Installing libraries...
python -m pip install -r requirements.txt
echo.
echo Done. Start the bot with:  run.bat
pause
