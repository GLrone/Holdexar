param(
    [ValidateSet('enable', 'disable', 'status', 'run')][string]$Mode = 'status',
    [Parameter(Mandatory)][string]$Entry,
    [Parameter(Mandatory)][string]$DataDir,
    [string]$Python,
    [switch]$Preview
)

$ErrorActionPreference = 'Stop'
$resolvedEntry = if ($Mode -eq 'run') { [IO.Path]::GetFullPath($Entry) } else { (Resolve-Path -LiteralPath $Entry).Path }
$resolvedData = [IO.Path]::GetFullPath($DataDir)
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$bytes = [Text.Encoding]::UTF8.GetBytes($identity + '|' + $resolvedData.ToLowerInvariant())
$hash = [Security.Cryptography.SHA256]::Create()
try { $suffix = [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-', '').Substring(0, 16) }
finally { $hash.Dispose() }
$taskName = 'HoldexarBackground-' + $suffix

if ($Mode -eq 'disable') {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output 'Background startup disabled. The running application is unchanged.'
    return
}
if ($Mode -eq 'status' -and -not $Preview) {
    Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue |
        Select-Object TaskName, State
    return
}

function Quote-Argument([string]$Value) {
    '"' + ($Value -replace '(\\*)$', '$1$1') + '"'
}

$executable = $resolvedEntry
$arguments = '--server --data-dir ' + (Quote-Argument $resolvedData)
if ([IO.Path]::GetExtension($resolvedEntry) -eq '.py') {
    if (-not $Python) { $Python = (Get-Command pythonw -ErrorAction SilentlyContinue).Source }
    if (-not $Python) { $Python = (Get-Command python).Source }
    $executable = if ($Mode -eq 'run') { [IO.Path]::GetFullPath($Python) } else { (Resolve-Path -LiteralPath $Python).Path }
    $arguments = (Quote-Argument $resolvedEntry) + ' ' + $arguments
}
if ($Mode -eq 'run') {
    while ($true) {
        try {
            $process = Start-Process -FilePath $executable -ArgumentList $arguments `
                -WorkingDirectory (Split-Path -Parent $resolvedEntry) -WindowStyle Hidden -PassThru
            $process.WaitForExit()
            if ($process.ExitCode -eq 0) { return }
        } catch {
            Write-Warning 'Background startup failed; retrying in one minute.'
        } finally {
            if ($process) { $process.Dispose(); $process = $null }
        }
        Start-Sleep -Seconds 60
    }
}
$supervisorArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File ' + (Quote-Argument $PSCommandPath) +
    ' -Mode run -Entry ' + (Quote-Argument $resolvedEntry) + ' -DataDir ' + (Quote-Argument $resolvedData)
if ($Python) { $supervisorArguments += ' -Python ' + (Quote-Argument $executable) }
$action = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
    -Argument $supervisorArguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings
if ($Preview) {
    $task
    return
}
Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output 'Background startup enabled. Closing the window keeps updates running; the tray exit stops this run.'
