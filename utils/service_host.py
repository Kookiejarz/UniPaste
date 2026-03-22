import threading


class ServiceHost:
    """Runs a service object in a background thread and exposes UI-safe controls."""

    def __init__(self, service, runner, autostart_manager=None):
        self.service = service
        self.runner = runner
        self.autostart_manager = autostart_manager
        self._thread = None
        self._lock = threading.RLock()
        self._startup_error = None

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._startup_error = None
            self._thread = threading.Thread(
                target=self._thread_main,
                name=f"{self.service.platform_name}-service",
                daemon=True,
            )
            self._thread.start()

    def _thread_main(self):
        try:
            self.runner(self.service)
        except Exception as exc:
            self._startup_error = exc
            if hasattr(self.service, "report_ui_error"):
                self.service.report_ui_error(str(exc))

    def stop(self):
        self.service.stop()

    def join(self, timeout=None):
        thread = self._thread
        if thread:
            thread.join(timeout)

    def is_running(self):
        thread = self._thread
        return bool(thread and thread.is_alive() and self.service.running)

    def get_ui_snapshot(self):
        snapshot = self.service.get_ui_snapshot()
        snapshot["thread_alive"] = bool(self._thread and self._thread.is_alive())
        snapshot["startup_error"] = str(self._startup_error) if self._startup_error else None
        if self.autostart_manager:
            snapshot["autostart_enabled"] = self.autostart_manager.is_enabled()
        else:
            snapshot["autostart_enabled"] = None
        return snapshot

    def accept_pairing_request(self, device_id: str) -> bool:
        return self.service.accept_pairing_request(device_id)

    def reject_pairing_request(self, device_id: str) -> bool:
        return self.service.reject_pairing_request(device_id)

    def install_autostart(self):
        if not self.autostart_manager:
            return False, "当前平台不支持自动启动"
        return self.autostart_manager.install()

    def remove_autostart(self):
        if not self.autostart_manager:
            return False, "当前平台不支持自动启动"
        return self.autostart_manager.uninstall()
