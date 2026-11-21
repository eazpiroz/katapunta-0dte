@echo off
rem Katapunta - alarma de salud (Programador de tareas "Katapunta Salud", lun-vie cada 10 min de 8:10 a 15:10 CDMX)
cd /d C:\Users\xmoza\OneDrive\Documentos\Inversiones\Katapunta\API
python salud.py >> salud_log.txt 2>&1
exit
