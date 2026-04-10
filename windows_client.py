import asyncio
import json
import os
import hmac
import hashlib
import sys
import time
from pathlib import Path
import threading
import traceback
import argparse

import websockets

from utils.platform_config import verify_platform, IS_WINDOWS
from utils.clipboard_utils import ClipboardUtils
from utils.base_node import ClipboardNode
from utils.message_format import ClipMessage, MessageType
from config import ClipboardConfig

# Verify platform at startup
verify_platform('windows')

if not IS_WINDOWS:
    raise RuntimeError("This script requires Windows")


class ConnectionStatus:
    """连接状态枚举"""
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2


class WindowsClipboardClient(ClipboardNode):

    def __init__(self):
        super().__init__("windows")
        self.connection_status = ConnectionStatus.DISCONNECTED
        self.last_change_count = ClipboardUtils.get_clipboard_change_count()
        self.last_format_log = set()
        self.ws_url = None

    # ------------------------------------------------------------------
    # Override connection status tracking
    # ------------------------------------------------------------------

    def _update_connection_status(self):
        if self.peer_connections:
            self.connection_status = ConnectionStatus.CONNECTED
        elif self.ws_url:
            self.connection_status = ConnectionStatus.CONNECTING
        else:
            self.connection_status = ConnectionStatus.DISCONNECTED

    # ------------------------------------------------------------------
    # Override on_service_found to also track ws_url
    # ------------------------------------------------------------------

    def on_service_found(self, service_info):
        super().on_service_found(service_info)
        # Update ws_url for connection status tracking
        if isinstance(service_info, dict):
            peer_id = service_info.get("properties", {}).get("device_id")
            if peer_id and self._should_initiate_connection(peer_id):
                self.ws_url = service_info.get("url")

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def _read_clipboard_snapshot(self) -> dict | None:
        """Read current clipboard state via ClipboardUtils."""
        file_paths = ClipboardUtils.get_clipboard_files()
        if file_paths:
            return {
                "kind": "files",
                "fingerprint": self.file_handler.get_files_content_hash(file_paths),
            }

        text = ClipboardUtils.get_clipboard_text()
        if text:
            return {
                "kind": "text",
                "fingerprint": hashlib.md5(text.encode()).hexdigest(),
            }
        return None

    async def _apply_text_to_clipboard(self, text: str) -> int | None:
        """Write text to Windows clipboard. Returns new change_count or None on failure."""
        if not ClipboardUtils.set_clipboard_text(text):
            print("❌ 更新Windows文本剪贴板失败")
            return None
        return ClipboardUtils.get_clipboard_change_count()

    async def _apply_received_file_to_clipboard(self, message: dict, completed_path: Path):
        """Set a received file onto the Windows clipboard."""
        print(f"✅ 文件接收完成: {completed_path}")

        completed_path = self.file_handler.materialize_received_path(message, completed_path)

        content_hash = self.file_handler.get_files_content_hash([str(completed_path)])
        if content_hash and content_hash == self.last_content_hash:
            print("⏭️ 跳过重复文件内容 (与本地最后发送/设置一致)")
            return

        if ClipboardUtils.set_clipboard_file(completed_path):
            self.last_change_count = ClipboardUtils.get_clipboard_change_count()
            self.last_content_hash = content_hash
            self._register_applied_remote_event(message, "files", content_hash, self.last_change_count)
            print("🔄 文件已登记为远端事件回显，下一次变化不会回传")
        else:
            print(f"❌ 未能将文件 {completed_path.name} 设置到剪贴板")

    async def _watch_clipboard(self):
        """Alias for send_clipboard_changes — satisfies the abstract method requirement."""
        await self.send_clipboard_changes()

    async def send_current_clipboard_to_peer(self, websocket):
        """向新连接设备发送当前剪贴板快照，便于恢复未完成传输。"""
        async def send_direct(data):
            await self._send_encrypted(data, websocket)

        try:
            file_paths = ClipboardUtils.get_clipboard_files()
            if file_paths:
                await self.file_handler.handle_clipboard_files(
                    file_paths,
                    None,
                    send_direct,
                    origin_device_id=self.device_id,
                    event_id=ClipMessage.new_event_id(),
                    delivery_mode="request"
                )
                print("📤 已向新连接设备发送当前文件剪贴板快照")
                return

            text = ClipboardUtils.get_clipboard_text()
            if text:
                await self.file_handler.process_clipboard_content(
                    text,
                    None,
                    send_direct,
                    origin_device_id=self.device_id,
                    event_id=ClipMessage.new_event_id()
                )
                print("📤 已向新连接设备发送当前文本剪贴板快照")
        except Exception as e:
            print(f"⚠️ 发送当前剪贴板快照失败: {e}")

    # ------------------------------------------------------------------
    # Windows-specific methods
    # ------------------------------------------------------------------

    async def send_clipboard_changes(self):
        """监控并发送剪贴板变化"""
        async def send_encrypted_wrapper(data_to_encrypt: bytes):
            await self.broadcast_encrypted_data(data_to_encrypt)

        while self.running:
            try:
                if not self.peer_connections:
                    await asyncio.sleep(ClipboardConfig.CLIPBOARD_CHECK_INTERVAL)
                    continue

                current_change_count = ClipboardUtils.get_clipboard_change_count()
                if current_change_count is None or current_change_count == self.last_change_count:
                    await asyncio.sleep(ClipboardConfig.CLIPBOARD_CHECK_INTERVAL)
                    continue

                if self._consume_expected_clipboard_echo(current_change_count):
                    await asyncio.sleep(ClipboardConfig.CLIPBOARD_CHECK_INTERVAL)
                    continue

                self.last_change_count = current_change_count
                sent_update_this_cycle = False

                file_paths = ClipboardUtils.get_clipboard_files()
                if file_paths:
                    content_hash = self.file_handler.get_files_content_hash(file_paths)

                    if content_hash and content_hash != self.last_content_hash:
                        new_hash, update_sent = await self.file_handler.handle_clipboard_files(
                            file_paths,
                            self.last_content_hash,
                            send_encrypted_wrapper,
                            origin_device_id=self.device_id,
                            event_id=ClipMessage.new_event_id(),
                            delivery_mode="oneshot",
                            schedule_transfer=self._schedule_background_transfer
                        )
                        if update_sent:
                            self.last_content_hash = new_hash
                            sent_update_this_cycle = True
                            print("📤 文件已进入 oneshot 直传队列")

                    if sent_update_this_cycle:
                        await asyncio.sleep(ClipboardConfig.CLIPBOARD_CHECK_INTERVAL)
                        continue

                current_content = ClipboardUtils.get_clipboard_text()
                if current_content:
                    content_hash = hashlib.md5(current_content.encode()).hexdigest()
                    if content_hash != self.last_content_hash:
                        print(f"📋 检测到剪贴板文本变化 (Hash: {content_hash[:8]}...)")
                        new_hash, update_sent = await self.file_handler.process_clipboard_content(
                            current_content,
                            self.last_content_hash,
                            send_encrypted_wrapper,
                            origin_device_id=self.device_id,
                            event_id=ClipMessage.new_event_id()
                        )
                        if update_sent:
                            self.last_content_hash = new_hash
                            sent_update_this_cycle = True

                if not sent_update_this_cycle:
                    await asyncio.sleep(ClipboardConfig.CLIPBOARD_CHECK_INTERVAL)

            except asyncio.CancelledError:
                print("⏹️ 发送任务被取消")
                break
            except Exception as e:
                print(f"❌ 发送剪贴板变化时出错: {e}")
                traceback.print_exc()
                await asyncio.sleep(1)

    async def show_connection_status(self):
        """显示连接状态"""
        last_status = None
        status_messages = {
            ConnectionStatus.DISCONNECTED: "🔴 已断开连接 - 等待对等设备",
            ConnectionStatus.CONNECTING: "🟡 正在连接...",
            ConnectionStatus.CONNECTED: "🟢 已连接 - 剪贴板同步已激活"
        }

        status_line = ""
        while self.running:
            try:
                current_status = self.connection_status
                if current_status != last_status:
                    if status_line:
                        sys.stdout.write("\r" + " " * len(status_line) + "\r")

                    status_line = status_messages.get(current_status, "⚪ 未知状态")
                    sys.stdout.write(f"\r{status_line}")
                    sys.stdout.flush()
                    last_status = current_status

                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                if status_line:
                    sys.stdout.write("\r" + " " * len(status_line) + "\r")
                    sys.stdout.flush()
                break
            except Exception as e:
                print(f"\n⚠️ 状态显示错误: {e}")
                last_status = None
                await asyncio.sleep(2)

    def get_ui_snapshot(self):
        """Override to include connection_status field."""
        snapshot = super().get_ui_snapshot()
        snapshot["status_text"] = {
            ConnectionStatus.DISCONNECTED: "等待对端连接",
            ConnectionStatus.CONNECTING: "正在连接",
            ConnectionStatus.CONNECTED: f"已连接 {len(self.peer_connections)} 台设备",
        }.get(self.connection_status, "未知状态")
        return snapshot


# ------------------------------------------------------------------
# Runner functions
# ------------------------------------------------------------------

async def run_client_tasks(client: WindowsClipboardClient, include_status: bool = True):
    client.event_loop = asyncio.get_running_loop()
    task_group = []

    if include_status:
        client.status_task = asyncio.create_task(client.show_connection_status())
        task_group.append(client.status_task)

    try:
        print("🚀 UniPaste Windows 节点已启动")
        print(f"📂 临时文件目录: {client.file_handler.temp_dir}")
        client.server_task = asyncio.create_task(client.start_server())
        client.clipboard_task = asyncio.create_task(client.send_clipboard_changes())
        client.sync_task = asyncio.create_task(client.sync_clipboard())
        task_group.extend([client.server_task, client.clipboard_task, client.sync_task])
        await asyncio.gather(client.server_task, client.clipboard_task, client.sync_task)
    except asyncio.CancelledError:
        pass
    finally:
        client.stop()
        for task in task_group:
            if task and not task.done():
                task.cancel()
        if task_group:
            await asyncio.gather(*task_group, return_exceptions=True)
        await client.finalize_shutdown()


async def main():
    client = WindowsClipboardClient()
    await run_client_tasks(client)


def _run_service_thread(service: WindowsClipboardClient):
    asyncio.run(run_client_tasks(service, include_status=False))


def run_tray_app():
    from utils.service_host import ServiceHost
    from utils.control_panel import ControlPanel
    from utils.windows_tray import WindowsTrayApp
    from utils.autostart import WindowsRegistryAutostartManager

    client = WindowsClipboardClient()
    autostart_manager = WindowsRegistryAutostartManager(script_path=Path(__file__).resolve())
    host = ServiceHost(client, _run_service_thread, autostart_manager=autostart_manager)

    panel_state = {"thread": None, "panel": None}

    def open_panel():
        panel = panel_state.get("panel")
        if panel and panel.root:
            panel.focus()
            return

        def panel_main():
            panel = ControlPanel(host, title="UniPaste 控制面板", on_quit_callback=tray.stop)
            panel_state["panel"] = panel
            try:
                panel.run()
            finally:
                panel_state["panel"] = None

        panel_thread = threading.Thread(target=panel_main, name="unipaste-control-panel", daemon=True)
        panel_state["thread"] = panel_thread
        panel_thread.start()

    tray = WindowsTrayApp(host, open_panel)
    client.ui_attention_callback = lambda name, platform: tray.notify_pairing_request(name, platform)
    host.start()
    open_panel()
    try:
        tray.run()
    finally:
        host.stop()
        host.join(5)


def parse_args():
    parser = argparse.ArgumentParser(description="UniPaste Windows node")
    parser.add_argument("--headless", action="store_true", help="仅在控制台前台运行，不启动托盘")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        if args.headless:
            asyncio.run(main())
        else:
            run_tray_app()
    except RuntimeError as e:
        if "Event loop is closed" in str(e):
            print("ℹ️ Event loop closed.")
        else:
            raise
