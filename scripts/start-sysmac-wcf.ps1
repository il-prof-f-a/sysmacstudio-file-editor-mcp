<# Preview by default. -Launch starts the installed IDE through normal Windows UAC. #>
[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9_-]{1,80}$')]
    [string]$HostId = ('sysmac-mcp-' + [Guid]::NewGuid().ToString('N')),
    [string]$VendorRoot = 'C:\Program Files\OMRON\Sysmac Studio',
    [switch]$Launch
)
$ErrorActionPreference = 'Stop'
$executable = Join-Path ([IO.Path]::GetFullPath($VendorRoot)) 'SysmacStudio.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw 'Installed SysmacStudio.exe was not found.'
}
$existing = @(Get-Process -Name SysmacStudio -ErrorAction SilentlyContinue)
$plan = [ordered]@{
    executable = $executable
    arguments = @('-wcfhost:' + $HostId)
    host_id = $HostId
    endpoint = ('net.pipe://localhost/' + $HostId + '/V1.0/SolutionService/')
    verb = 'RunAs'
    existing_sysmac_processes = @($existing | ForEach-Object { $_.Id })
    launched = $false
    scope = 'Local vendor WCF services; client limited to probe/open/export. No build proven.'
}
if ($Launch) {
    if ($existing.Count -gt 0) {
        throw 'Sysmac is already running. Save your work and close it before this launch.'
    }
    # This displays the normal UAC request; it does not alter elevation policies.
    $process = Start-Process -FilePath $executable -ArgumentList $plan.arguments -WorkingDirectory (Split-Path -Parent $executable) -Verb RunAs -WindowStyle Normal -PassThru
    $plan.launched = $true
    $plan['process_id'] = $process.Id
}
$plan | ConvertTo-Json -Depth 4
