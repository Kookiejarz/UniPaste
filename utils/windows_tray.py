from pathlib import Path

import pystray
from PIL import Image, ImageDraw


class WindowsTrayApp:
    def __init__(self, controller, open_panel_callback):
        self.controller = controller
        self.open_panel_callback = open_panel_callback
        self.icon = pystray.Icon("UniPaste")
        self.icon.icon = self._load_icon()
        self.icon.title = "UniPaste"
        self.icon.menu = pystray.Menu(
            pystray.MenuItem(self._status_title, None, enabled=False),
            pystray.MenuItem("打开控制面板", self._open_panel, default=True),
            pystray.MenuItem(self._pairing_title, self._open_panel, enabled=self._has_pending_pairings),
            pystray.MenuItem("退出", self._quit),
        )

    def run(self):
        self.icon.run()

    def stop(self):
        self.icon.stop()

    def notify_pairing_request(self):
        self.icon.update_menu()
        self._open_panel()

    def _open_panel(self, *_args):
        self.open_panel_callback()

    def _quit(self, *_args):
        self.controller.stop()
        self.stop()

    def _status_title(self, *_args):
        snapshot = self.controller.get_ui_snapshot()
        return f"状态: {snapshot.get('status_text', '未知')}"

    def _pairing_title(self, *_args):
        count = len(self.controller.get_ui_snapshot().get("pending_pairings", []))
        return f"待处理配对: {count}"

    def _has_pending_pairings(self, *_args):
        return bool(self.controller.get_ui_snapshot().get("pending_pairings"))

    def _is_dark_theme(self):
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return value == 0  # 0 means dark mode, 1 means light mode
        except Exception:
            return False

    def _load_icon(self):
        root_dir = Path(__file__).resolve().parents[1]
        assets_dir = root_dir / "assets"
        is_dark = self._is_dark_theme()
        
        ico_name = "unipaste_dark.ico" if is_dark else "unipaste.ico"
        ico_path = assets_dir / ico_name
        png_path = assets_dir / "unipaste.png"

        if ico_path.exists():
            return Image.open(ico_path)
        if (assets_dir / "unipaste.ico").exists():
            return Image.open(assets_dir / "unipaste.ico")
        if png_path.exists():
            return Image.open(png_path)

        image = Image.new("RGBA", (64, 64), "#0f766e")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=12, fill="#0f766e")
        draw.text((16, 18), "UP", fill="white")
        return image
