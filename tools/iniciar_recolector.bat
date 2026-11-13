@echo off
title Katapunta Flight Deck - Recolector
cd /d C:\Users\xmoza\OneDrive\Documentos\Inversiones\Katapunta\API
echo Recolector Flight Deck - %date% %time%
rem Tape de opciones (streamer de Schwab) en su propia ventana; se apaga solo a las 15:01
start "Katapunta Flight Deck - Tape" /d C:\Users\xmoza\OneDrive\Documentos\Inversiones\Katapunta\API cmd /k python tape_schwab.py
:ciclo
python flightdeck_recolector.py
rem Auditoria 12-nov: si el recolector se cae antes de las 15:00, se reinicia solo en 15 s
set h=%time:~0,2%
set h=%h: =0%
if "%h%" LSS "15" (
  echo %time% El recolector se detuvo antes del cierre. Reiniciando en 15 s... Cierra esta ventana para cancelar.
  timeout /t 15
  goto ciclo
)
echo.
echo El recolector termino. Puedes cerrar esta ventana.
pause
