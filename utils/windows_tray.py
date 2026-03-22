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

    def _load_icon(self):
        icon_path = Path(__file__).resolve().parents[1] / "unipaste.png"
        if icon_path.exists():
            return Image.open(icon_path)

        image = Image.new("RGBA", (64, 64), "#0f766e")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=12, fill="#0f766e")
        draw.text((16, 18), "UP", fill="white")
        return image
