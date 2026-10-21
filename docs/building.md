# Windows 打包

[返回项目首页](../README.md)

## 准备环境

在 Windows 上安装 Python 3.10 以上、Node.js 18 以上。
建议先创建并激活[开发说明](development.md)中的虚拟环境，确保 `python` 指向该环境。
以下命令从仓库根目录执行：

```bash
python -m pip install -r packaging/requirements.txt
```

运行时只需要 Flask；Pillow 和 PyInstaller 是打包依赖，集中声明在 `packaging/requirements.txt`。

## 一键打包

PowerShell：

```powershell
.\scripts\pack.bat
```

脚本依次执行：

1. 从 `packaging/assets/` 生成应用图标和 NSIS 安装器图片。
2. 使用 `packaging/PuMail.spec` 生成 `dist/PuMail/` 后端。
3. 在 `electron/` 执行 `npm ci` 和 `npm run pack`，生成 Windows 安装包。

安装包输出：`PuMail-build/PuMail-Setup.exe`。
下载 Electron 和构建依赖需要网络连接。任一步失败，脚本会停止并返回非零退出码。

## 分步执行

```bash
python scripts/make-installer-assets.py
python -m PyInstaller packaging/PuMail.spec --noconfirm
cd electron
npm ci
npm run pack
```

只验证桌面目录构建时，可将最后一条命令替换为 `npm run pack:dir`。
生成后端后，也可以在 `electron/` 执行 `npm start` 启动开发外壳。

## 资源位置

PyInstaller 将整个 `frontend/` 目录打包到 `dist/PuMail/_internal/frontend/`，
包括页面、界面素材、邮箱指南和配图。Electron 将整个 `dist/PuMail/`
复制到安装目录的 `resources/pumail-server/`。

`packaging/assets/` 中的原始素材、`PuMail.spec` 和 `installer.nsh` 需要提交。
`packaging/generated/`、`build/`、`dist/` 和 `PuMail-build/` 都是生成结果，已被忽略。
对外分发安装包时使用 GitHub Releases，源码目录保持整洁。
