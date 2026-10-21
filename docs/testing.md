# 测试

[返回项目首页](../README.md)

安装运行依赖后，在仓库根目录执行：

```bash
python -m tests.test_sync_queue
python -m tests.test_idle_handshake
python -m unittest discover -s tests -t . -v
node tests/test_port_probe.js
node tests/test_local_pages.js
```

前两条命令运行任务队列和 IMAP IDLE 的独立测试；`unittest discover`
运行收信流程、未读数、代理网络和页面资源回归测试。
JavaScript 测试检查 Electron 能否区分自己的后端、其他服务和空闲端口。
本地页面测试还会验证开发版、打包版和含空格、中文的路径下，指南配图与帮助页面都能加载。

测试使用模拟 IMAP、临时数据库、本机假 IMAP/代理/HTTP 服务，不连接真实邮箱。
本机套接字测试需要允许访问回环地址 `127.0.0.1`。

导入后端时会初始化用户目录及本地密钥，建议使用一个空的测试用户目录。
下面的 Windows PowerShell 示例设置临时位置，运行后恢复原来的环境变量：

```powershell
$testHome = Join-Path ([System.IO.Path]::GetTempPath()) ('pumail-tests-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $testHome | Out-Null
$previousProfile = $env:USERPROFILE
try {
    $env:USERPROFILE = $testHome
    python -m tests.test_sync_queue
    python -m tests.test_idle_handshake
    python -m unittest discover -s tests -t . -v
    node tests/test_port_probe.js
    node tests/test_local_pages.js
} finally {
    $env:USERPROFILE = $previousProfile
}
```

macOS / Linux 可为导入后端的测试指定临时用户目录：

```bash
HOME="$(mktemp -d)" python -m unittest discover -s tests -t . -v
```

语法检查：

```bash
python -m compileall -q backend tests scripts
node --check frontend/app.js
node --check electron/main.js
node --check electron/preload.js
```
