# 开发与目录说明

[返回项目首页](../README.md)

## 从源码运行

需要 Python 3.10 以上，推荐使用虚拟环境。以下命令均从仓库根目录执行。

```bash
python -m venv .venv
```

Windows PowerShell 激活环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux 激活环境：

```bash
source .venv/bin/activate
```

安装依赖并启动：

```bash
python -m pip install -r requirements.txt
python -m backend.server
```

浏览器访问 <http://127.0.0.1:5000>。也可以运行 `python backend/server.py`。
Windows 安装依赖后，可双击 `scripts/打开PuMail网页版.bat`。
启动器会检查端口并查找 Python，包括仓库根目录的 `.venv`。

修改 HTML、CSS、JavaScript 后刷新浏览器；修改 Python 后重新启动服务。

## 桌面外壳

需要 Node.js 18 以上。Electron 开发模式使用 `dist/PuMail/PuMail.exe`，
请先按[打包说明](building.md)生成后端。

```bash
cd electron
npm ci
npm start
```

开发模式默认使用端口 5000；安装后的桌面程序默认使用 5211。

## 目录约定

| 目录或文件 | 用途 |
| --- | --- |
| `backend/server.py` | Flask 接口、IMAP/SMTP、账号与本地数据管理 |
| `backend/sync_queue.py` | 按账号串行的后台任务队列 |
| `frontend/` | 网页界面和内置帮助、隐私、指南页面 |
| `frontend/assets/` | 界面图片、SVG 和新邮件提示音 |
| `frontend/guides/` | 应用内读取的邮箱绑定指南及配图 |
| `electron/` | 桌面外壳与依赖锁文件 |
| `tests/` | Python 和 JavaScript 回归测试 |
| `scripts/` | 网页启动器、打包脚本、安装器素材生成工具 |
| `packaging/` | PyInstaller、NSIS 配置和打包依赖 |
| `packaging/assets/` | 安装器原始图标和品牌图片 |
| `packaging/generated/` | 自动生成的安装器素材，不提交 |
| `docs/` | 仓库使用和维护文档 |

添加文件时按用途放入对应目录。根目录只保留项目入口说明、许可证、运行依赖和必要的仓库配置。
`frontend/guides/` 是应用资源，会随程序一起打包；`docs/` 是供仓库访问者阅读的文档。

## 本地数据

账号与邮件数据默认保存在用户目录的 `.PuMail/`，整理源码目录不会改变这个位置。
启动应用会读取本机已配置的账号。运行自动化检查时，请使用独立的测试用户目录和临时数据库。

不要提交本地数据库、账号密码库、日志、虚拟环境、`node_modules` 或打包产物。
依赖锁文件 `electron/package-lock.json` 应保留在版本控制中。

继续阅读：[测试](testing.md) · [Windows 打包](building.md)
