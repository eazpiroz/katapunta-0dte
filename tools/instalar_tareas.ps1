# Katapunta - instala las dos tareas nuevas del Programador de tareas (correr una sola vez en PowerShell).
#   Katapunta Cierre : lun-vie 15:20 CDMX (16:20 ET) -> bitacora automatica + tarjeta de calificaciones
#   Katapunta Salud  : lun-vie cada 10 min de 8:10 a 15:10 CDMX -> alarma de salud al celular
$dir  = "C:\Users\xmoza\OneDrive\Documentos\Inversiones\Katapunta\API"
$dias = "Monday","Tuesday","Wednesday","Thursday","Friday"
$yo   = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
$conf = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

$a1 = New-ScheduledTaskAction -Execute "$dir\cierre_katapunta.bat" -WorkingDirectory $dir
$t1 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $dias -At 15:20
Register-ScheduledTask -TaskName "Katapunta Cierre" -Action $a1 -Trigger $t1 -Principal $yo -Settings $conf -Force | Out-Null

$a2 = New-ScheduledTaskAction -Execute "$dir\salud_katapunta.bat" -WorkingDirectory $dir
$t2 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $dias -At 08:10
$t2.Repetition = (New-ScheduledTaskTrigger -Once -At 08:10 -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Hours 7)).Repetition
Register-ScheduledTask -TaskName "Katapunta Salud" -Action $a2 -Trigger $t2 -Principal $yo -Settings $conf -Force | Out-Null

Get-ScheduledTask -TaskName "Katapunta*" | Select-Object TaskName, State | Format-Table -AutoSize
