@echo off
rem Katapunta - arranque automatico (Programador de tareas, lun-vie 8:05 CDMX)
rem 1) recolector + tape  2) un minuto despues, el bot de papel
rem Ambos se apagan solos despues del cierre; a las 8:05 no deberia haber nada corriendo.
cd /d C:\Users\xmoza\OneDrive\Documentos\Inversiones\Katapunta\API
start "Katapunta Flight Deck - Recolector" cmd /c iniciar_recolector.bat
timeout /t 60 /nobreak >nul
start "Katapunta - Bot de papel" cmd /c iniciar_papel.bat
exit
