@echo off
cd /d "%~dp0"
title WATCHDOG Cloud Connector

echo.
echo  ==========================================
echo   WATCH-DOG Cloud Connector
echo   Connecting to watchdogbot.cloud ...
echo  ==========================================
echo.

python sdk/wd_cloud.py

echo.
echo  [Cloud Connector stopped]
pause
