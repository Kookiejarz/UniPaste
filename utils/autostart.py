import os
import plistlib
import subprocess
import sys
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None


class WindowsRegistryAutostartManager:
    REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    APP_NAME = "UniPaste"

    def __init__(self, script_path=None):
        self.script_path = Path(script_path or sys.argv[0]).resolve()

    def _get_command(self):
        if getattr(sys, "frozen", False):
            # If compiled with PyInstaller
            exe = Path(sys.executable).resolve()
            return f'"{exe}" --headless'
        else:
            # If running as script
            python_exe = Path(sys.executable).resolve()
            return f'"{python_exe}" "{self.script_path}" --headless'

    def is_enabled(self) -> bool:
        if not winreg:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.REG_KEY, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, self.APP_NAME)
                return True
        except WindowsError:
            return False

    def install(self):
        if not winreg:
            return False, "当前平台不支持注册表操作"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.REG_KEY, 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, self.APP_NAME, 0, winreg.REG_SZ, self._get_command())
            return True, "已成功设置开机自动启动"
        except Exception as e:
            return False, f"设置开机自动启动失败: {e}"

    def uninstall(self):
        if not winreg:
            return False, "当前平台不支持注册表操作"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.REG_KEY, 0, winreg.KEY_WRITE) as key:
                winreg.DeleteValue(key, self.APP_NAME)
            return True, "已成功取消开机自动启动"
        except WindowsError as e:
            if e.winerror == 2:  # File not found (value doesn't exist)
                return True, "开机自动启动原本就未设置"
            return False, f"取消开机自动启动失败: {e}"
        except Exception as e:
            return False, f"取消开机自动启动失败: {e}"


class MacLaunchAgentManager:
    LABEL = "com.unipaste.agent"

    def __init__(self, script_path=None):
        self.script_path = Path(script_path or sys.argv[0]).resolve()
        self.plist_path = Path.home() / "Library" / "LaunchAgents" / f"{self.LABEL}.plist"
        self.log_dir = Path.home() / "Library" / "Logs"
        self.stdout_log = self.log_dir / "UniPaste.log"
        self.stderr_log = self.log_dir / "UniPaste.error.log"

    def is_enabled(self) -> bool:
        return self.plist_path.exists()

    def _program_arguments(self):
        if getattr(sys, "frozen", False):
            return [str(Path(sys.executable).resolve()), "--headless"]
        return [sys.executable, str(self.script_path), "--headless"]

    def build_plist_data(self) -> dict:
        return {
            "Label": self.LABEL,
            "ProgramArguments": self._program_arguments(),
            "RunAtLoad": True,
            "KeepAlive": True,
            "WorkingDirectory": str(self.script_path.parent),
            "StandardOutPath": str(self.stdout_log),
            "StandardErrorPath": str(self.stderr_log),
            "ProcessType": "Interactive",
        }

    def install(self):
        try:
            self.plist_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.plist_path, "wb") as file_obj:
                plistlib.dump(self.build_plist_data(), file_obj)

            self._run_launchctl("bootout")
            self._run_launchctl("bootstrap")
            return True, f"已安装 LaunchAgent: {self.plist_path}"
        except Exception as exc:
            return False, f"安装 LaunchAgent 失败: {exc}"

    def uninstall(self):
        try:
            self._run_launchctl("bootout")
            if self.plist_path.exists():
                self.plist_path.unlink()
            return True, "已移除 LaunchAgent"
        except Exception as exc:
            return False, f"移除 LaunchAgent 失败: {exc}"

    def _run_launchctl(self, action: str):
        target = f"gui/{os.getuid()}"
        if action == "bootstrap":
            cmd = ["launchctl", "bootstrap", target, str(self.plist_path)]
        elif action == "bootout":
            cmd = ["launchctl", "bootout", target, str(self.plist_path)]
        else:
            raise ValueError(f"Unsupported launchctl action: {action}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode == 0:
            return

        stderr = (result.stderr or "").strip().lower()
        benign_markers = (
            "could not find specified service",
            "no such process",
            "service is disabled",
            "boot-out failed: 5",
        )
        if action == "bootout" and (any(marker in stderr for marker in benign_markers) or result.returncode == 5):
            return
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "launchctl failed")
