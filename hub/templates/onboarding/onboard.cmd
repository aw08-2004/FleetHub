@echo off
rem FleetHub onboarding launcher (roadmap #29). Rendered by the hub for ONE onboarding code.
rem Elevates, saves the onboarding script for that code to a file, and runs that file. It
rem carries the code and the hub address only; the credential is fetched by the script.
rem
rem The script is saved to a file and run from it, under the RemoteSigned policy. The first
rem version of this launcher ran the download in memory with every policy check switched
rem off, which is how droppers look, and Kaspersky blocked it. The file goes into a fresh
rem folder only administrators can write to, never the user's TEMP, where any of the user's
rem own processes could swap it before it runs elevated. The command doing all of that is
rem built by the hub (onboarding_web.run_command), the same one the onboarding page shows.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    set "FLEETHUB_ONBOARD_SELF=%~f0"
    powershell -NoProfile -Command "Start-Process -FilePath $env:FLEETHUB_ONBOARD_SELF -Verb RunAs"
    exit /b
)
echo Downloading and running the FleetHub onboarding script...
powershell -NoProfile -Command "{{ command }}"
echo.
pause
