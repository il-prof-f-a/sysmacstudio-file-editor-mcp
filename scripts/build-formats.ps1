[CmdletBinding()]
param(
    [string]$DotnetPath,
    [switch]$Package
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $DotnetPath) {
    $localSdk = Join-Path $repoRoot '.tools\dotnet\dotnet.exe'
    if (Test-Path -LiteralPath $localSdk) { $DotnetPath = $localSdk }
    else { $DotnetPath = (Get-Command dotnet -ErrorAction Stop).Source }
}
$DotnetPath = (Resolve-Path -LiteralPath $DotnetPath).Path
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
$projectRoot = Join-Path $repoRoot 'bridge\SysmacFormats'
Push-Location $projectRoot
try {
    & $DotnetPath publish SysmacFormats.csproj --configuration Release --nologo -p:RestoreLockedMode=true
    if ($LASTEXITCODE -ne 0) { throw 'SysmacFormats publish failed.' }
}
finally { Pop-Location }
$executable = Join-Path $projectRoot 'bin\Release\net10.0\win-x64\publish\SysmacFormats.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw 'Published executable missing.' }
Write-Output $executable

if ($Package) {
    $stage = Join-Path $repoRoot 'build\formats-win-x64'
    $distribution = Join-Path $repoRoot 'dist'
    New-Item -ItemType Directory -Path $stage, $distribution -Force | Out-Null
    Copy-Item -LiteralPath $executable -Destination (Join-Path $stage 'SysmacFormats.exe') -Force
    $sdkRoot = Split-Path -Parent $DotnetPath
    foreach ($name in @('LICENSE.txt', 'ThirdPartyNotices.txt')) {
        $notice = Join-Path $sdkRoot $name
        if (-not (Test-Path -LiteralPath $notice)) { throw "Required .NET notice missing: $notice" }
        Copy-Item -LiteralPath $notice -Destination (Join-Path $stage $name) -Force
    }
    $packageCache = if ($env:NUGET_PACKAGES) { $env:NUGET_PACKAGES } else { Join-Path $env:USERPROFILE '.nuget\packages' }
    $nrbfNotice = Join-Path $packageCache 'system.formats.nrbf\10.0.14\THIRD-PARTY-NOTICES.TXT'
    if (-not (Test-Path -LiteralPath $nrbfNotice)) { throw "Required NRBF notice missing: $nrbfNotice" }
    Copy-Item -LiteralPath $nrbfNotice -Destination (Join-Path $stage 'NRBF-THIRD-PARTY-NOTICES.txt') -Force
    $metadata = @{
        protocol = 1
        runtime = 'win-x64 self-contained .NET 10'
        package = 'System.Formats.Nrbf 10.0.14'
        executable_sha256 = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $metadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stage 'manifest.json') -Encoding UTF8
    $files = @('SysmacFormats.exe', 'LICENSE.txt', 'ThirdPartyNotices.txt', 'NRBF-THIRD-PARTY-NOTICES.txt', 'manifest.json') | ForEach-Object { Join-Path $stage $_ }
    $archive = Join-Path $distribution 'sysmac-formats-win-x64.zip'
    Compress-Archive -LiteralPath $files -DestinationPath $archive -Force
    Write-Output $archive
}
