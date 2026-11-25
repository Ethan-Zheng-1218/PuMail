param([string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$electronRoot = Join-Path $repoRoot 'electron'
$package = Get-Content -LiteralPath (Join-Path $electronRoot 'package.json') -Raw | ConvertFrom-Json
if ($ConfigPath) {
    $config = Get-Content -LiteralPath (Resolve-Path -LiteralPath $ConfigPath).Path -Raw | ConvertFrom-Json
} else {
    $config = $package.build
}
$outputRoot = if ([IO.Path]::IsPathRooted($config.directories.output)) {
    [IO.Path]::GetFullPath($config.directories.output)
} else {
    [IO.Path]::GetFullPath((Join-Path $electronRoot $config.directories.output))
}
$artifactName = $config.nsis.artifactName.Replace('${version}', $package.version)
$config.directories.output = Join-Path $outputRoot '_payload'
$config.nsis.artifactName = 'PuMail-install-payload.exe'
$diagnostics = Join-Path $repoRoot '.diagnostics'
$compilerTemp = Join-Path $diagnostics ('nsis-temp-' + $package.version)
New-Item -ItemType Directory -Path $compilerTemp -Force | Out-Null
$generatedConfig = Join-Path $diagnostics ('electron-bootstrap-' + $package.version + '.json')
$config | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $generatedConfig -Encoding utf8
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
$previousCache = $env:ELECTRON_BUILDER_CACHE
Push-Location $electronRoot
try {
    $env:TEMP = $compilerTemp
    $env:TMP = $compilerTemp
    $cache = Join-Path $diagnostics 'builder-cache'
    if (Test-Path -LiteralPath $cache) { $env:ELECTRON_BUILDER_CACHE = $cache }
    & node node_modules/electron-builder/cli.js --win nsis --config $generatedConfig
    if ($LASTEXITCODE -ne 0) { throw "NSIS payload build failed: $LASTEXITCODE" }
    & (Join-Path $PSScriptRoot 'bootstrap-installer.ps1') `
        -PayloadPath (Join-Path $config.directories.output 'PuMail-install-payload.exe') `
        -OutputPath (Join-Path $outputRoot $artifactName)
} finally {
    Pop-Location
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
    $env:ELECTRON_BUILDER_CACHE = $previousCache
}
