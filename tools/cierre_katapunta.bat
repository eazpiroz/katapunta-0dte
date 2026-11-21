@echo off
rem Katapunta - cierre del dia (Programador de tareas "Katapunta Cierre", lun-vie 15:20 CDMX = 16:20 ET)
rem 1) lee de Schwab tus operaciones de hoy (solo lectura)  2) califica el dia y lo publica
cd /d C:\Users\xmoza\OneDrive\Documentos\Inversiones\Katapunta\API
python bitacora_auto.py >> cierre_log.txt 2>&1
python calificaciones.py --publicar >> cierre_log.txt 2>&1
exit
