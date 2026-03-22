from pathlib import Path
import os
import tempfile

class ClipboardConfig:
    """剪贴板配置类"""
    
    # 文件传输相关
    MAX_FILE_SIZE_AUTO = 100 * 1024 * 1024  # 100MB自动传输限制
    CHUNK_SIZE = 700 * 1024  # 1MB分块大小
    
    # 时间间隔配置
    MIN_PROCESS_INTERVAL = 0.8  # 最小处理间隔
    UPDATE_DELAY = 1.0  # 更新延迟
    NETWORK_DELAY = 0.05  # 网络传输延迟
    CLIPBOARD_CHECK_INTERVAL = 0.5  # 剪贴板检查间隔
    
    # 显示相关
    MAX_DISPLAY_LENGTH = 100  # 最大显示长度
    
    # WebSocket配置
    DEFAULT_PORT = 8765
    HOST = "0.0.0.0"
    WEBSOCKET_MAX_SIZE = 10 * 1024 * 1024
    PING_INTERVAL = 20
    PING_TIMEOUT = 20
    PAIRING_TIMEOUT_SECONDS = 60

    PLATFORM_SETTINGS = {
        "windows": {
            "platform_name": "windows",
            "device_name_env": "COMPUTERNAME",
            "default_device_name": "Windows设备",
            "token_filename": "device_token.txt",
        },
        "macos": {
            "platform_name": "macos",
            "device_name_env": "HOSTNAME",
            "default_device_name": "Mac设备",
            "token_filename": "device_token_mac.txt",
        },
    }
    
    # 文件存储配置
    @classmethod
    def get_temp_dir(cls):
        """获取临时文件目录"""
        temp_dir = Path(tempfile.gettempdir()) / "unipaste_files"
        temp_dir.mkdir(exist_ok=True)
        return temp_dir

    @classmethod
    def get_platform_settings(cls, platform_name: str) -> dict:
        try:
            return cls.PLATFORM_SETTINGS[platform_name].copy()
        except KeyError as exc:
            raise ValueError(f"Unsupported platform config: {platform_name}") from exc

    @classmethod
    def get_device_name(cls, platform_name: str) -> str:
        settings = cls.get_platform_settings(platform_name)
        return os.environ.get(
            settings["device_name_env"],
            settings["default_device_name"],
        )

    @classmethod
    def get_device_token_path(cls, platform_name: str) -> Path:
        settings = cls.get_platform_settings(platform_name)
        token_dir = Path.home() / ".clipshare"
        token_dir.mkdir(parents=True, exist_ok=True)
        return token_dir / settings["token_filename"]
    
    # 临时文件路径标识
    TEMP_PATH_INDICATORS = [
        "\\AppData\\Local\\Temp\\clipshare_files\\",
        "/var/folders/",
        "/tmp/clipshare_files/",
        "C:\\Users\\\\AppData\\Local\\Temp\\clipshare_files\\"
    ]


class ReleaseConfig:
    """发布构建配置"""

    TARGETS = {
        "macos": {
            "runner": "macos-latest",
            "artifact_name": "UniPaste-macos",
            "release_dir": "macos",
            "executable_name": "UniPaste-Mac",
            "entry_script": "mac_clip_check.py",
        },
        "windows": {
            "runner": "windows-latest",
            "artifact_name": "UniPaste-windows",
            "release_dir": "windows",
            "executable_name": "UniPaste-Win",
            "entry_script": "windows_client.py",
        },
    }

    @classmethod
    def get_target(cls, platform_name: str) -> dict:
        try:
            return {"platform": platform_name, **cls.TARGETS[platform_name]}
        except KeyError as exc:
            raise ValueError(f"Unsupported release target: {platform_name}") from exc

    @classmethod
    def get_build_matrix(cls) -> list[dict]:
        return [cls.get_target(platform_name) for platform_name in cls.TARGETS]
