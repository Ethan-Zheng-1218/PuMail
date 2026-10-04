$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    $listener = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        Write-Host '端口 5000 已被占用。请先关闭之前的网页启动窗口，或退出托盘中的 PuMail，再双击启动。'
        Write-Host '如果已经启动的是当前源码，可直接访问 http://127.0.0.1:5000'
        Read-Host '按回车关闭'
        exit 1
    }

    $candidates = @(
        (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
        (Join-Path $PSScriptRoot 'venv\Scripts\python.exe')
    )
    foreach ($name in @('python', 'py')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
    }
    $candidates += Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    $pythonPath = $null
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            try { & $candidate -c 'import flask' 2>$null } catch { continue }
            if ($LASTEXITCODE -eq 0) {
                $pythonPath = $candidate
                break
            }
        }
    }
    if (-not $pythonPath) {
        throw '未找到带 Flask 的 Python。请安装 Python 并运行 python -m pip install flask 后重试。'
    }

    Write-Host 'PuMail 网页预览：http://127.0.0.1:5000'
    Write-Host '请保留此窗口。修改 HTML/CSS/JS 后，在浏览器按 Ctrl+F5 刷新。'
    Write-Host '修改 server.py 后，关闭此窗口再双击启动。关闭窗口即可停止服务。'
    Remove-Item Env:PUMAIL_NO_BROWSER -ErrorAction SilentlyContinue
    & $pythonPath (Join-Path $PSScriptRoot 'server.py')
    if ($LASTEXITCODE -ne 0) { throw "服务退出，退出码：$LASTEXITCODE" }
} catch {
    Write-Host "启动失败：$_" -ForegroundColor Red
    Read-Host '按回车关闭'
    exit 1
}
