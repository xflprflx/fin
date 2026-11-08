@echo off
chcp 65001 >nul
title Controle Financeiro Pessoal - Servidor Local (SQLite + FastAPI)
cd /d "%~dp0"

echo ===============================================================================
echo   INICIALIZANDO SISTEMA DE CONTROLE FINANCEIRO PESSOAL (2026)
echo ===============================================================================

:: 1. Verifica e cria ambiente virtual (.venv) caso nao exista
if not exist ".venv\Scripts\activate.bat" (
    echo [1/3] Criando ambiente virtual Python ^(.venv^)...
    python -m venv .venv
)

:: 2. Ativa o ambiente virtual e garante dependencias instaladas
call ".venv\Scripts\activate.bat"
echo [2/3] Verificando dependencias ^(FastAPI, Uvicorn, Pydantic^)...
pip install --quiet --disable-pip-version-check fastapi uvicorn pydantic

:: 3. Abre o navegador padrao e inicia o servidor Uvicorn na porta 8000
echo [3/3] Iniciando servidor local em http://127.0.0.1:8000 ...
start "" "http://127.0.0.1:8000"
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload

pause
