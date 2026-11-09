@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
  >&2 echo Ambiente Python mancante. Seguire l'installazione nel README.
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m sysmac_mcp --workspace "%~dp0workspace" %*
exit /b %errorlevel%
