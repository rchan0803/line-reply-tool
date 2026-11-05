$a = New-ScheduledTaskAction -Execute 'C:\Users\ranha\AppData\Local\Programs\Python\Python312\pythonw.exe' -Argument 'auto_local.py' -WorkingDirectory 'C:\Users\ranha\claude\line-reply-tool'
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(7).AddMinutes(7) -RepetitionInterval (New-TimeSpan -Hours 1)
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 50)
Register-ScheduledTask -TaskName 'MiraAutoReply' -Action $a -Trigger $t -Settings $s -Force | Out-Null
Get-ScheduledTask -TaskName 'MiraAutoReply' | Select-Object TaskName, State
