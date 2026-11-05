@echo off
chcp 65001 >nul
title Automatizador Oficial RUNT Ciudadano - Control de Patios
cd /d "%~dp0"
echo ============================================================================
echo   INICIANDO AUTOMATIZADOR DE EXPEDIENTES RUNT CIUDADANO
echo   Estructura conectada a: G:\martha garcia\VEHICULOS RETENIDOS VALIDAR RUNT____
echo ============================================================================
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "app_automatizador_runt.py"
) else (
    python "app_automatizador_runt.py"
)
pause
