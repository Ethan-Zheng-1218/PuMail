# PuMail

一款在你自己电脑上运行的桌面邮件客户端，支持 QQ 邮箱、163、Gmail、Outlook、iCloud 及自定义 IMAP/SMTP 邮箱。

[English](docs/README.en.md) · [下载桌面版](https://github.com/Ethan-Zheng-1218/PuMail/releases) · [邮箱绑定指南](frontend/guides/邮箱绑定指南.md)

## 主要功能

- 多账号管理与统一收件箱
- IMAP IDLE 新邮件推送，不支持时自动切换为轮询
- 读信、写信、回复、附件、草稿与划词翻译
- 未读数统计、托盘闪烁与新邮件提示音
- Windows 桌面程序与本地网页预览

## 快速开始

普通用户可以前往 [Releases](https://github.com/Ethan-Zheng-1218/PuMail/releases) 查找 Windows 安装包。

从源码体验需要 Python 3.10 以上：

```bash
git clone https://github.com/Ethan-Zheng-1218/PuMail.git
cd PuMail
python -m pip install -r requirements.txt
python -m backend.server
```

浏览器访问 <http://127.0.0.1:5000>。
Windows 安装依赖后，也可以双击 `scripts/打开PuMail网页版.bat`。

## 文档

- [邮箱绑定指南](frontend/guides/邮箱绑定指南.md)：获取各邮箱的授权码或应用专用密码
- [开发与目录说明](docs/development.md)：环境准备、桌面开发、文件如何归类
- [Windows 打包](docs/building.md)：生成桌面安装包
- [测试说明](docs/testing.md)：运行自动化检查

## 隐私

PuMail 的邮件后端运行在本机，直接通过 IMAP/SMTP 连接邮箱服务商。
账号凭据和邮件数据默认保存在用户目录的 `.PuMail/` 中。
更多说明见应用内的[隐私页面](frontend/privacy.html)。

## 关于

我是一个不会写代码的产品爱好者，产品名称来自我的猫噗噗。
项目代码由 Cursor 根据我的产品需求和设计构思生成。
感谢 AI 让创意不再被技术门槛束缚。

作者：Ethan · [MIT License](LICENSE)
