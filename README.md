# UniPaste: 跨平台剪贴板同步工具

[简体中文](/README.md)|[English](/README_EN.md)

![UniPaste](https://img.shields.io/badge/UniPaste-3.0.0-blue)
![Python](https://img.shields.io/badge/Python-3.9+-green)
![License](https://img.shields.io/badge/License-GPL%20v3-orange)

一个简单实用的 Mac 和 Windows 剪贴板同步工具，基于本地网络传输，无需云服务。

## 💾 快速下载使用

**开袋即食？直接下载可执行文件：**

📥 [Release 下载页面](https://github.com/Kookiejarz/UniPaste/releases)

下载后直接运行即可owo

## ✨ 主要特性

- **本地网络传输**: 数据在局域网内直接传输
- **自动设备发现**: 无需配置 IP，自动发现同网络设备
- **端到端加密**: AES-256 加密保护传输数据
- **支持多格式**: 文本、文件、图片都能同步

## 🚀 使用方法

### 方式一：直接运行（推荐）
1. 从 [Release](https://github.com/Kookiejarz/UniPaste/releases) 下载对应平台的可执行文件
2. Mac 上运行 UniPast可执行文件，Windows 上运行 UniPaste.exe  
3. 首次连接时确认配对，之后自动连接
4. 开始跨设备复制粘贴

### 方式二：从源码运行
```bash
git clone https://github.com/Kookiejarz/UniPaste.git
cd UniPaste
pip install -r requirements.txt

# Mac 启动服务端
python mac_clip_check.py

# Windows 启动客户端  
python windows_client.py
```

## 🔧 常见问题

**设备无法连接？**
- 确保两台设备在同一 WiFi 网络
- 检查防火墙是否阻止了应用
- 尝试以管理员权限运行（Windows）

**剪贴板没有更新？**
- 关闭其他剪贴板管理工具
- 重启应用重新连接

**传输文件失败？**
- 检查文件是否被其他程序占用
- 确认网络连接稳定

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
