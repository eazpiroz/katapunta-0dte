@echo off
rem Katapunta - arranque del bot de papel (Programador de tareas "Katapunta Arranque", lun-vie 8:17 CDMX)
rem El recolector lo abre su propia tarea "Katapunta Recolector" a las 8:15. Aqui solo es respaldo:
rem si a las 8:17 no hay recolector corriendo, lo abre; luego abre el bot (que trae su propio candado).
cd /d C:\Users\xmoza\OneDrive\Documentos\Inversiones\Katapunta\API
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*flightdeck_recolector*' }) { exit 1 } else { exit 0 }"
if errorlevel 1 (
  echo El recolector ya esta corriendo: no abro otro.
) else (
  start "Katapunta Flight Deck - Recolector" cmd /c iniciar_recolector.bat
)
timeout /t 60 /nobreak >nul
start "Katapunta - Bot de papel" cmd /c iniciar_papel.bat
rem Bitacora automatica: recoge la liquidacion nocturna de ayer y actualiza la tarjeta de calificaciones
python bitacora_auto.py --ayer >> cierre_log.txt 2>&1
python calificaciones.py --publicar >> cierre_log.txt 2>&1
exit
