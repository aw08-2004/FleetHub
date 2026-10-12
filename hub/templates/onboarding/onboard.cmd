@echo off
rem FleetHub onboarding launcher (roadmap #29). Rendered by the hub for ONE onboarding code.
rem Elevates, then downloads and runs the onboarding script for that code. It carries the
rem code and the hub address only; the credential is fetched by the script, on this PC.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    set "FLEETHUB_ONBOARD_SELF=%~f0"
    powershell -NoProfile -Command "Start-Process -FilePath $env:FLEETHUB_ONBOARD_SELF -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; irm '{{ script_url }}' | iex"
echo.
pause
