@echo off
rem FleetHub onboarding launcher (roadmap #29). Rendered by the hub for ONE onboarding code.
rem Elevates, saves the onboarding script for that code to a file, and runs that file. It
rem carries the code and the hub address only; the credential is fetched by the script.
rem
rem The script is saved to a file and run from it, under the RemoteSigned policy. The first
rem version of this launcher ran the download in memory with every policy check switched
rem off, which is how droppers look, and Kaspersky blocked it.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    set "FLEETHUB_ONBOARD_SELF=%~f0"
    powershell -NoProfile -Command "Start-Process -FilePath $env:FLEETHUB_ONBOARD_SELF -Verb RunAs"
    exit /b
)
set "FLEETHUB_ONBOARD_PS1=%TEMP%\FleetHub-Onboard.ps1"
echo Downloading the FleetHub onboarding script...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri '{{ script_url }}' -OutFile $env:FLEETHUB_ONBOARD_PS1; Unblock-File -LiteralPath $env:FLEETHUB_ONBOARD_PS1"
if errorlevel 1 (
    echo Could not download the onboarding script. Check the connection, or ask for a new code.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy RemoteSigned -File "%FLEETHUB_ONBOARD_PS1%"
del "%FLEETHUB_ONBOARD_PS1%" >nul 2>&1
echo.
pause
