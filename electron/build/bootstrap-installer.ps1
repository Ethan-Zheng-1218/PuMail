param(
    [Parameter(Mandatory = $true)][string]$PayloadPath,
    [Parameter(Mandatory = $true)][string]$OutputPath
)
$ErrorActionPreference = 'Stop'
$payloadFile = (Resolve-Path -LiteralPath $PayloadPath).Path
$outputFile = [IO.Path]::GetFullPath($OutputPath)
if ($payloadFile -eq $outputFile) { throw 'Payload and output must have different paths' }
$repoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$version = (Get-Content -LiteralPath (Join-Path $repoRoot 'electron\package.json') -Raw | ConvertFrom-Json).version
$compiler = Join-Path ([Environment]::GetFolderPath('Windows')) 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw '.NET Framework C# compiler is unavailable' }
$outputDirectory = Split-Path $outputFile -Parent
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$generatedFile = Join-Path $outputDirectory 'installer-payload.g.cs'
$payloadHash = (Get-FileHash -LiteralPath $payloadFile -Algorithm SHA256).Hash
$generatedSource = @"
using System.Reflection;
[assembly: AssemblyTitle("PuMail 安装程序")]
[assembly: AssemblyProduct("PuMail")]
[assembly: AssemblyCompany("PuMail")]
[assembly: AssemblyVersion("$version.0")]
[assembly: AssemblyFileVersion("$version.0")]
internal static class InstallerPayload { internal const string Sha256 = "$payloadHash"; }
"@
[IO.File]::WriteAllText($generatedFile, $generatedSource, [Text.UTF8Encoding]::new($false))
& $compiler /nologo /target:winexe /platform:anycpu /optimize+ "/out:$outputFile" `
    "/win32icon:$(Join-Path $repoRoot 'cat.ico')" "/win32manifest:$(Join-Path $PSScriptRoot 'installer-bootstrap.manifest')" `
    /reference:System.Windows.Forms.dll "/resource:$payloadFile,PuMail.Installer" `
    (Join-Path $PSScriptRoot 'installer-bootstrap.cs') $generatedFile
if ($LASTEXITCODE -ne 0) { throw "Installer bootstrap compilation failed: $LASTEXITCODE" }
$hash = (Get-FileHash -LiteralPath $outputFile -Algorithm SHA256).Hash
[IO.File]::WriteAllText((Join-Path $outputDirectory 'SHA256SUMS.txt'), "$hash  $([IO.Path]::GetFileName($outputFile))`r`n", [Text.Encoding]::ASCII)
[pscustomobject]@{ Output = $outputFile; Version = $version; Bytes = (Get-Item -LiteralPath $outputFile).Length; SHA256 = $hash }
