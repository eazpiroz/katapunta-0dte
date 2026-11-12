@echo off
title Katapunta Flight Deck - Recolector
cd /d C:\Users\xmoza\OneDrive\Documentos\Inversiones\Katapunta\API
echo Recolector Flight Deck - %date% %time%
rem Tape de opciones (streamer de Schwab) en su propia ventana; se apaga solo a las 15:01
start "Katapunta Flight Deck - Tape" /d C:\Users\xmoza\OneDrive\Documentos\Inversiones\Katapunta\API cmd /k python tape_schwab.py
python flightdeck_recolector.py
echo.
echo El recolector termino. Puedes cerrar esta ventana.
pause
