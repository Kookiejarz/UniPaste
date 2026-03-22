# UniPaste: 跨平台剪贴板同步工具

[简体中文](/README.md)|[English](/README_EN.md)

![UniPaste-favicon](https://github.com/Kookiejarz/UniPaste/blob/main/unipaste.png?raw=true)
![UniPaste](https://img.shields.io/badge/UniPaste-3.0.0-blue)
![Python](https://img.shields.io/badge/Python-3.9+-green)
![License](https://img.shields.io/badge/License-GPL%20v3-orange)

一个简单实用的 Mac 和 Windows 剪贴板同步工具，基于本地网络传输，无需云服务。

## 💾 快速下载使用

**开袋即食？直接下载可执行文件：**

📥 [Release 下载页面](https://github.com/Kookiejarz/UniPaste/releases)

下载后直接运行即可owo

## ✨ 主要特性

- **对等节点同步**: Mac 和 Windows 都可发现、监听和连接，不再固定主从角色
- **本地网络传输**: 数据在局域网内直接传输
- **自动设备发现**: 无需配置 IP，自动发现同网络设备
- **端到端加密**: AES-256 加密保护传输数据
- **支持多设备互联**: 可同时与 3 台及以上设备保持连接
- **文件分块续传**: 大文件按块传输，掉线后可从已完成块继续
- **后台常驻体验**: Windows 使用系统托盘常驻，macOS 可安装 LaunchAgent 开机自启
- **简易控制面板**: 可查看连接状态、发现的设备和待处理配对请求
- **支持多格式**: 文本、文件、图片都能同步

## 🚀 使用方法

### 方式一：直接运行（推荐）
1. 从 [Release](https://github.com/Kookiejarz/UniPaste/releases) 下载对应平台的可执行文件
2. Mac 上运行 UniPaste-Mac，Windows 上运行 UniPaste-Win.exe  
3. 首次连接时确认配对，之后自动连接
4. 开始跨设备复制粘贴

Windows 版会进入系统托盘，右键托盘图标即可打开控制面板或退出。  
macOS 版默认会打开控制面板；如需后台常驻开机自启，可执行：

```bash
python mac_clip_check.py --install-launch-agent
```

### 方式二：从源码运行
```bash
git clone https://github.com/Kookiejarz/UniPaste.git
cd UniPaste
pip install -r requirements.txt

# 任意顺序启动两端节点
python mac_clip_check.py

# Windows 节点
python windows_client.py
```

首次连接时在任一已授权设备上确认配对，之后会按设备 ID 自动决定连接方向，避免双向重复建链。

如果你只想让 macOS 在后台无窗口运行，可用：

```bash
python mac_clip_check.py --headless
```

## 🔧 常见问题

**设备无法连接？**
- 确保两台设备在同一 WiFi 网络
- 检查防火墙是否阻止了应用
- 尝试以管理员权限运行（Windows）

**剪贴板没有更新？**
- 关闭其他剪贴板管理工具
- 重启应用重新连接
- Windows 托盘菜单里打开控制面板，确认当前是否真的已经发现对端

**传输文件失败？**
- 检查文件是否被其他程序占用
- 确认网络连接稳定
- 大文件传输中断后，重新连上会自动续传未完成块

**如何关闭 macOS 开机自启？**

```bash
python mac_clip_check.py --remove-launch-agent
```

## 🛡️ 安全说明

- 所有数据仅在本地网络传输
- 使用 AES-256 端到端加密
- 建议仅在信任的网络环境中使用
- 首次连接需要手动确认配对

## 📋 系统要求

- **macOS**: 10.15 或更高版本
- **Windows**: Windows 10 或更高版本  
- **网络**: 同一局域网环境

## 🤝 贡献

欢迎提交 Issue 和 Pull Request。

## 🙏 致谢

感谢以下开源项目：
- [websockets](https://github.com/aaugustin/websockets)
- [zeroconf](https://github.com/python-zeroconf/python-zeroconf)  
- [cryptography](https://github.com/pyca/cryptography)
- [pyperclip](https://github.com/asweigart/pyperclip)

---

> 💡 简单好用的本地剪贴板同步工具，解决多设备协作痛点。初期开发可能还有点bug...帮我一起抓住它们!
