@echo off
rem Katapunta - arranque automatico (Programador de tareas, lun-vie 8:05 CDMX)
rem 1) recolector + tape, salvo que ya este corriendo  2) un minuto despues, el bot de papel (trae su propio candado)
cd /d C:\Users\xmoza\OneDrive\Documentos\Inversiones\Katapunta\API
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*flightdeck_recolector*' }) { exit 1 } else { exit 0 }"
if errorlevel 1 (
  echo El recolector ya esta corriendo: no abro otro.
) else (
  start "Katapunta Flight Deck - Recolector" cmd /c iniciar_recolector.bat
)
timeout /t 60 /nobreak >nul
start "Katapunta - Bot de papel" cmd /c iniciar_papel.bat
exit
