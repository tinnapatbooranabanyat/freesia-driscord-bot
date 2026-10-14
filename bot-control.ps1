# Start/stop the bot and turn autostart on/off. Usually used through the .bat files.
#   powershell -ExecutionPolicy Bypass -File bot-control.ps1 start | stop | autostart-on | autostart-off | status
param([Parameter(Mandatory)][ValidateSet('start', 'stop', 'autostart-on', 'autostart-off', 'status')][string]$Action)

$root = $PSScriptRoot
$taskName = 'DiscordLLMBot'
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$runner = Join-Path $root 'run_bot.pyw'

function Get-BotProcesses {
    $all = Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'python.exe', 'pythonw.exe' }
    $found = @($all | Where-Object { $_.CommandLine -like '*run_bot.pyw*' -or $_.ExecutablePath -like "$root\.venv\*" })
    # venv launchers spawn the real interpreter as a child process; include descendants
    do {
        $ids = $found.ProcessId
        $more = @($all | Where-Object { $ids -contains $_.ParentProcessId -and $ids -notcontains $_.ProcessId })
        $found += $more
    } while ($more.Count -gt 0)
    return $found
}

function Stop-Bot {
    $procs = Get-BotProcesses
    # Kill the runner first so it can't restart the bot
    $procs | Sort-Object { $_.CommandLine -notlike '*run_bot.pyw*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    return $procs.Count
}

switch ($Action) {
    'start' {
        Start-Process -FilePath $pythonw -ArgumentList "`"$runner`"" -WorkingDirectory $root
        Write-Host 'Bot started in the background. Log: logs\bot.log'
    }
    'stop' {
        if ((Stop-Bot) -gt 0) { Write-Host 'Bot stopped.' } else { Write-Host 'Bot was not running.' }
    }
    'autostart-on' {
        $taskAction = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$runner`"" -WorkingDirectory $root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Settings $settings `
            -Description 'Discord AI + music bot' -Force | Out-Null
        Write-Host 'Autostart ON: the bot will start every time you log in to Windows.'
    }
    'autostart-off' {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host 'Autostart OFF: the bot will no longer start at login (use start-bot.bat to run it).'
    }
    'status' {
        $running = (Get-BotProcesses).Count -gt 0
        $auto = $null -ne (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
        Write-Host "Bot running: $running"
        Write-Host "Autostart:   $auto"
    }
}
