@echo off
rem Katapunta - arranque automatico (Programador de tareas, lun-vie 8:05 CDMX)
rem 1) recolector + tape  2) un minuto despues, el bot de papel
cd /d C:\Users\xmoza\OneDrive\Documentos\Inversiones\Katapunta\API
tasklist /v /fi "imagename eq cmd.exe" | find /i "Katapunta Flight Deck - Recolector" >nul && (echo El recolector ya esta abierto: no lo duplico.) || start "Katapunta Flight Deck - Recolector" cmd /c iniciar_recolector.bat
timeout /t 60 /nobreak >nul
tasklist /v /fi "imagename eq cmd.exe" | find /i "Katapunta - Bot de papel" >nul && (echo El bot ya esta abierto: no lo duplico.) || start "Katapunta - Bot de papel" cmd /c iniciar_papel.bat
exit
