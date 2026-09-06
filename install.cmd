@echo off
py -3 "%~dp0scripts\phone_agent.py" install
if errorlevel 1 exit /b 1
