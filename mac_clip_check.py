import AppKit
import asyncio
import websockets
import json
import queue
import signal
import time
import os
import hmac
import sys
import argparse
import rumps
import threading
import traceback
import multiprocessing
from multiprocessing import Process, Manager, Queue
from pathlib import Path
import hashlib

from utils.base_node import ClipboardNode
from utils.message_format import ClipMessage, MessageType
from utils.clipboard_utils import ClipboardUtils
from utils.service_host import ServiceHost
from utils.autostart import MacLaunchAgentManager
from config import ClipboardConfig


def _resource_path(*parts: str) -> str:
    if getattr(sys, "frozen", False):
        base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    else:
        base_dir = Path(__file__).resolve().parent
    return str(base_dir.joinpath(*parts))


class ClipboardListener(ClipboardNode):
    """macOS 剪贴板监听和同步节点"""

    def __init__(self):
        super().__init__("macos")
        self.pasteboard = AppKit.NSPasteboard.generalPasteboard()
        self.last_change_count = self.pasteboard.changeCount()

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def _read_clipboard_snapshot(self) -> dict | None:
        """Read current clipboard state from NSPasteboard."""
        types = self.pasteboard.types()

        if AppKit.NSPasteboardTypeFileURL in types:
            file_urls = []
            for item in self.pasteboard.pasteboardItems():
                url_str = item.stringForType_(AppKit.NSPasteboardTypeFileURL)
                if not url_str:
                    continue
                url = AppKit.NSURL.URLWithString_(url_str)
                if url and url.isFileURL():
                    file_path = url.path()
                    if file_path and Path(file_path).exists():
                        file_urls.append(file_path)
            if file_urls:
                return {
                    "kind": "files",
                    "fingerprint": self.file_handler.get_files_content_hash(file_urls),
                }

        text = self.pasteboard.stringForType_(AppKit.NSPasteboardTypeString)
        if text:
            return {
                "kind": "text",
                "fingerprint": hashlib.md5(text.encode()).hexdigest(),
            }
        return None

    async def _apply_text_to_clipboard(self, text: str) -> int | None:
        """Write text to NSPasteboard. Returns new changeCount or None on failure."""
        self.pasteboard.clearContents()
        success = self.pasteboard.setString_forType_(text, AppKit.NSPasteboardTypeString)
        if success:
            return self.pasteboard.changeCount()
        print("❌ 更新Mac剪贴板失败")
        return None

    async def _apply_received_file_to_clipboard(self, message: dict, completed_path: Path):
        """Set a received file onto the NSPasteboard."""
        print(f"✅ 文件接收完成: {completed_path}")

        completed_path = self.file_handler.materialize_received_path(message, completed_path)

        content_hash = self.file_handler.get_files_content_hash([str(completed_path)])
        if content_hash and content_hash == self.last_content_hash:
            print("⏭️ 跳过重复文件内容 (与本地最后发送/设置一致)")
            return

        await asyncio.sleep(0.1)
        change_count = await self.file_handler.set_clipboard_file(completed_path)
        if change_count is None:
            print(f"❌ 将文件 {completed_path.name} 设置到剪贴板失败")
            return

        self.last_change_count = change_count
        self.last_content_hash = content_hash
        self._register_applied_remote_event(message, "files", content_hash, change_count)

        print("✅ 文件已设置到剪贴板并可用于粘贴")
        print("🔄 文件已登记为远端事件回显，下一次变化不会回传")

    async def _watch_clipboard(self):
        """Alias for check_clipboard — satisfies the abstract method requirement."""
        await self.check_clipboard()

    async def send_current_clipboard_to_peer(self, websocket):
        """向新连接设备发送当前剪贴板快照，便于恢复未完成传输。"""
        async def send_direct(data):
            await self._send_encrypted(data, websocket)

        try:
            types = self.pasteboard.types()

            if AppKit.NSPasteboardTypeFileURL in types:
                file_urls = []
                for item in self.pasteboard.pasteboardItems():
                    url_str = item.stringForType_(AppKit.NSPasteboardTypeFileURL)
                    if not url_str:
                        continue
                    url = AppKit.NSURL.URLWithString_(url_str)
                    if url and url.isFileURL():
                        file_path = url.path()
                        if file_path and Path(file_path).exists():
                            file_urls.append(file_path)

                if file_urls:
                    await self.file_handler.handle_clipboard_files(
                        file_urls,
                        None,
                        send_direct,
                        origin_device_id=self.device_id,
                        event_id=ClipMessage.new_event_id(),
                        delivery_mode="request"
                    )
                    print("📤 已向新连接设备发送当前文件剪贴板快照")
                    return

            if AppKit.NSPasteboardTypeString in types:
                text = self.pasteboard.stringForType_(AppKit.NSPasteboardTypeString)
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


    async def sync_clipboard(self):
        print("🔍 搜索剪贴板服务...")
        self.discovery.start_discovery(self.on_service_found, self.on_service_lost)

        while self.running:
            try:
                # 重置发现事件
                self.discovery_event.clear()

                now = time.time()
                connected_peer_ids = set(self.peer_connections.keys())
                candidate_peer_id = None
                candidate_url = None

                for peer_id, peer_info in sorted(self.discovered_peers.items()):
                    if not self._should_initiate_connection(peer_id):
                        continue
                    if peer_id in connected_peer_ids:
                        continue

                    candidate_peer_id = peer_id
                    candidate_url = peer_info.get("url")
                    break

                if candidate_peer_id and candidate_url:
                    print(f"🔌 正在连接到设备 {candidate_peer_id}: {candidate_url}")
                    try:
                        await self.connect_and_sync(candidate_peer_id, candidate_url)
                    except Exception as e:
                        if self._is_expected_peer_unavailable_error(e):
                            print(f"ℹ️ 设备 {candidate_peer_id} 当前不可用，等待重连... ({e})")
                        else:
                            print(f"❌ 与设备 {candidate_peer_id} 建立连接失败: {e}")
                        # 失败后，短暂休眠，防止 CPU 尖峰
                        await asyncio.sleep(2)
                else:
                    # 【核心】如果没有可连接的设备，彻底挂起，直到 mDNS 发现新设备
                    await self.discovery_event.wait()

            except asyncio.CancelledError:

                break
            except Exception as e:
                print(f"❌ 主同步循环出错: {e}")
                await asyncio.sleep(2)

    async def connect_and_sync(self, peer_id, ws_url):
        async with websockets.connect(
            ws_url,
            subprotocols=["binary"],
            max_size=ClipboardConfig.WEBSOCKET_MAX_SIZE,
            ping_interval=ClipboardConfig.PING_INTERVAL,
            ping_timeout=ClipboardConfig.PING_TIMEOUT
        ) as websocket:
            remote_peer_id = await self.authenticate(websocket)
            if not remote_peer_id:
                self._mark_peer_failure(peer_id, "身份验证失败")
                return

            if not await self.perform_key_exchange_as_client(websocket):
                self._mark_peer_failure(remote_peer_id, "密钥交换失败")
                return

            if not self._register_peer(remote_peer_id, websocket):
                self._reset_peer_retry(remote_peer_id)
                print(f"⚠️ 已存在与 {remote_peer_id} 的连接，关闭重复出站连接")
                return

            self._reset_peer_retry(peer_id)
            self._reset_peer_retry(remote_peer_id)
            print(f"✅ 已连接到设备 {remote_peer_id}，开始同步剪贴板")
            await self.send_current_clipboard_to_peer(websocket)
            try:
                while self.running:
                    encrypted_data = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                    await self.process_received_data(encrypted_data, sender_websocket=websocket)
            finally:
                removed_peer = self._unregister_peer(websocket)
                if removed_peer:
                    print(f"➖ 设备 {removed_peer} 已断开")

    async def authenticate(self, websocket):
        try:
            auth_info = {
                "identity": self.device_id,
                "signature": self._generate_signature(),
                "first_time": self.device_token is None,
                "device_name": self.device_name,
                "platform": self.platform_name
            }

            await websocket.send(json.dumps(auth_info))
            response_raw = await asyncio.wait_for(websocket.recv(), timeout=30.0)
            response_data = json.loads(response_raw if isinstance(response_raw, str) else response_raw.decode("utf-8"))
            status = response_data.get("status")

            if status == "authorized":
                return response_data.get("peer_id")
            if status == "pairing_accepted":
                token = response_data.get("token")
                if token:
                    self._save_device_token(token)
                    self.device_token = token
                    return response_data.get("peer_id")
            
            # If rejected or unauthorized, handle failure
            reason = response_data.get("reason", "未知原因")
            print(f"❌ 身份验证失败: {reason}")
            if status == "unauthorized":
                print("⚠️ 本地令牌可能已失效，正在清除并准备重新配对...")
                token_path = self._get_token_path()
                if token_path.exists():
                    token_path.unlink()
                self.device_token = None
            return None
        except Exception as e:
            print(f"❌ 身份验证过程中出错: {e}")
            return None


    async def start_server(self, port=ClipboardConfig.DEFAULT_PORT): # Use config
        """启动 WebSocket 对等节点监听"""
        stop_event = asyncio.Event()

        async def server_logic():
            try:
                self.server = await websockets.serve(
                    self.handle_client,
                    ClipboardConfig.HOST,
                    port,
                    subprotocols=["binary"],
                    max_size=ClipboardConfig.WEBSOCKET_MAX_SIZE,
                    ping_interval=ClipboardConfig.PING_INTERVAL,
                    ping_timeout=ClipboardConfig.PING_TIMEOUT
                )
                await self.discovery.start_advertising(
                    port,
                    device_id=self.device_id,
                    platform=self.platform_name
                )
                print(f"🌐 WebSocket 对等节点监听在 {ClipboardConfig.HOST}:{port}")

                # Wait until stop_event is set
                await stop_event.wait()

            except OSError as e:
                 if "Address already in use" in str(e):
                      print(f"❌ 错误: 端口 {port} 已被占用。请关闭使用该端口的其他程序或选择不同端口。")
                 else:
                      print(f"❌ 节点启动错误: {e}")
            except Exception as e:
                print(f"❌ 节点错误: {e}")
            finally:
                self.discovery.close()
                
                if self.peer_connections:
                    print(f"📤 正在关闭 {len(self.peer_connections)} 个连接...")
                    close_tasks = [ws.close() for ws in list(self.peer_connections.values())]
                    await asyncio.gather(*close_tasks, return_exceptions=True)
                
                if self.server:
                    self.server.close()
                    try:
                         await asyncio.wait_for(self.server.wait_closed(), timeout=2.0)
                         print("✅ WebSocket 对等节点已关闭")
                    except asyncio.TimeoutError:
                         print("⚠️ WebSocket 对等节点关闭超时")
                self.server = None

        self._stop_server_func = stop_event.set
        await server_logic()


    async def check_clipboard(self):
        """轮询检查剪贴板内容变化"""
        print("📋 剪贴板监听已启动...")
        last_processed_time = 0

        while self.running:
            try:
                current_time = time.time()

                time_since_process = current_time - last_processed_time
                if time_since_process < ClipboardConfig.MIN_PROCESS_INTERVAL:
                    await asyncio.sleep(0.1)
                    continue

                new_change_count = self.pasteboard.changeCount()
                if new_change_count != self.last_change_count:
                    print(f"📋 剪贴板变化 detected (Count: {self.last_change_count} -> {new_change_count})")
                    types = self.pasteboard.types()
                    print(f"🔍 剪贴板类型: {list(types)}")

                    if self._consume_expected_clipboard_echo(new_change_count):
                        last_processed_time = time.time()
                        await asyncio.sleep(ClipboardConfig.CLIPBOARD_CHECK_INTERVAL)
                        continue

                    self.last_change_count = new_change_count
                    processed = await self.process_clipboard()
                    if processed:
                        last_processed_time = time.time()

                await asyncio.sleep(ClipboardConfig.CLIPBOARD_CHECK_INTERVAL)

            except asyncio.CancelledError:
                print("⏹️ 剪贴板监听已停止")
                break
            except Exception as e:
                print(f"❌ 剪贴板监听错误: {e}")
                traceback.print_exc()
                await asyncio.sleep(1)

    async def process_clipboard(self) -> bool:
        """
        处理本地剪贴板内容变化, 发送给对等设备.
        Returns True if an update was sent, False otherwise.
        """
        types = self.pasteboard.types()
        sent_update = False
        try:
            if AppKit.NSPasteboardTypeFileURL in types:
                file_urls = []
                for item in self.pasteboard.pasteboardItems():
                    url_str = item.stringForType_(AppKit.NSPasteboardTypeFileURL)
                    if url_str:
                        url = AppKit.NSURL.URLWithString_(url_str)
                        if url and url.isFileURL():
                            file_path = url.path()
                            if file_path and Path(file_path).exists():
                                file_urls.append(file_path)
                            else:
                                print(f"⚠️ 剪贴板中的文件路径无效或不存在: {file_path}")

                if file_urls and self.peer_connections:
                    new_hash, update_sent = await self.file_handler.handle_clipboard_files(
                        file_urls,
                        self.last_content_hash,
                        self.broadcast_encrypted_data,
                        origin_device_id=self.device_id,
                        event_id=ClipMessage.new_event_id(),
                        delivery_mode="oneshot",
                        schedule_transfer=self._schedule_background_transfer
                    )
                    if update_sent:
                        self.last_content_hash = new_hash
                        sent_update = True
                        print("📤 文件已进入 oneshot 直传队列")
                    return sent_update

            if AppKit.NSPasteboardTypeString in types:
                text = self.pasteboard.stringForType_(AppKit.NSPasteboardTypeString)
                if text and self.peer_connections:
                    new_hash, update_sent = await self.file_handler.process_clipboard_content(
                        text,
                        self.last_content_hash,
                        self.broadcast_encrypted_data,
                        origin_device_id=self.device_id,
                        event_id=ClipMessage.new_event_id()
                    )
                    if update_sent:
                        self.last_content_hash = new_hash
                        sent_update = True
                    return sent_update

            if AppKit.NSPasteboardTypePNG in types:
                print("⚠️ 图片同步暂不支持")

        except Exception as e:
            print(f"❌ 处理剪贴板内容时出错: {e}")
            traceback.print_exc()

        return sent_update


# ------------------------------------------------------------------
# Runner functions
# ------------------------------------------------------------------

async def run_listener(listener: ClipboardListener):
    listener.event_loop = asyncio.get_running_loop()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        print("\n⚠️ 接收到关闭信号...")
        if not stop_event.is_set():
            listener.stop()
            stop_event.set()

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                print(f"ℹ️ 信号 {sig} 处理在当前系统可能不受支持。请使用 Ctrl+C。")

    try:
        print("🚀 UniPaste Mac 节点已启动")
        print(f"📂 临时文件目录: {listener.file_handler.temp_dir}")
        print("📋 按 Ctrl+C 退出程序")

        listener.server_task = asyncio.create_task(listener.start_server())
        listener.sync_task = asyncio.create_task(listener.sync_clipboard())
        listener.clipboard_task = asyncio.create_task(listener.check_clipboard())

        await asyncio.gather(listener.server_task, listener.sync_task, listener.clipboard_task)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"\n❌ 发生未处理的错误: {e}")
        traceback.print_exc()
    finally:
        if listener.running:
            listener.stop()
        await listener.finalize_shutdown()
        print("🚪 程序退出")


async def main():
    listener = ClipboardListener()
    await run_listener(listener)


def run_headless():
    asyncio.run(main())


# ------------------------------------------------------------------
# Tray app
# ------------------------------------------------------------------



class MacTrayApp(rumps.App):
    def __init__(self, host, open_panel_callback):
        icon_path = _resource_path("assets", "unipaste_mac_template.png")
        if not os.path.exists(icon_path):
            icon_path = _resource_path("assets", "unipaste.png")
        if not os.path.exists(icon_path):
            icon_path = None
        super(MacTrayApp, self).__init__("UniPaste", icon=icon_path, quit_button=None)
        self.host = host
        self.open_panel_callback = open_panel_callback
        self._status_item = rumps.MenuItem("○ 等待连接")
        self.menu = [
            self._status_item,
            None,
            rumps.MenuItem("打开控制面板", callback=self.on_open_panel),
            None,
            rumps.MenuItem("退出 UniPaste", callback=self.on_quit)
        ]
        self._status_timer = rumps.Timer(self._update_status, 3)
        self._status_timer.start()
        self._notification_queue = queue.SimpleQueue()
        self._notif_timer = rumps.Timer(self._flush_notifications, 0.5)
        self._notif_timer.start()

    def _update_status(self, _):
        try:
            snapshot = self.host.get_ui_snapshot() or {}
            n = len(snapshot.get("connected_peers", []))
            self._status_item.title = f"● 已连接 {n} 台" if n > 0 else "○ 等待连接"
        except Exception:
            pass

    def on_open_panel(self, _):
        if self.open_panel_callback:
            self.open_panel_callback()

    def on_quit(self, _):
        print("👋 正在通过菜单栏退出...")
        self.host.stop()
        rumps.quit_application()

    def notify_pairing_request(self, device_name, platform):
        self._notification_queue.put((
            "UniPaste 配对请求",
            f"{device_name} ({platform}) 请求配对",
            "请打开控制面板确认或拒绝请求",
        ))
        if self.open_panel_callback:
            self.open_panel_callback()

    def notify_transfer_complete(self, filename: str, peer_id: str | None, direction: str):
        if direction == "receive":
            subtitle = f"已接收: {filename}"
            message = f"来自 {peer_id}" if peer_id else ""
        else:
            subtitle = f"已发送: {filename}"
            message = ""
        self._notification_queue.put(("UniPaste 文件传输完成", subtitle, message))

    def _flush_notifications(self, _):
        """Called on the main thread every 0.5 s by rumps.Timer."""
        while not self._notification_queue.empty():
            try:
                title, subtitle, message = self._notification_queue.get()
                rumps.notification(title=title, subtitle=subtitle, message=message)
            except Exception:
                break


# ------------------------------------------------------------------
# Control panel and multiprocess support
# ------------------------------------------------------------------

class SharedHostProxy:
    """A proxy that looks like ServiceHost but works across processes."""
    def __init__(self, shared_dict, cmd_queue):
        self.shared_dict = shared_dict
        self.cmd_queue = cmd_queue

    def get_ui_snapshot(self):
        return dict(self.shared_dict)

    def accept_pairing_request(self, device_id):
        self.cmd_queue.put(("accept", device_id))
        return True

    def reject_pairing_request(self, device_id):
        self.cmd_queue.put(("reject", device_id))
        return True

    def install_autostart(self):
        self.cmd_queue.put(("install_autostart", None))
        return True, "LaunchAgent request sent"

    def remove_autostart(self):
        self.cmd_queue.put(("remove_autostart", None))
        return True, "LaunchAgent request sent"

    def full_quit(self):
        self.cmd_queue.put(("full_quit", None))

    def stop(self):
        pass  # Panel process shouldn't stop the service


def run_panel_process(shared_dict, cmd_queue):
    """Entry point for the separate UI process."""
    try:
        from utils.control_panel import ControlPanel
        proxy = SharedHostProxy(shared_dict, cmd_queue)
        panel = ControlPanel(proxy, title="UniPaste 控制面板", on_quit_callback=proxy.full_quit)
        panel.run()
    except Exception as e:
        print(f"❌ 控制面板进程崩溃: {e}")


def run_control_panel():
    with Manager() as manager:
        shared_dict = manager.dict()
        cmd_queue = Queue()
        shared_dict["panel_focus_token"] = time.time()

        listener = ClipboardListener()
        autostart_manager = MacLaunchAgentManager(script_path=Path(__file__).resolve())
        host = ServiceHost(
            listener,
            lambda service: asyncio.run(run_listener(service)),
            autostart_manager=autostart_manager,
        )

        panel_process_info = {"process": None}

        def open_panel():
            p = panel_process_info["process"]
            if p and p.is_alive():
                shared_dict["panel_focus_token"] = time.time()
                return

            shared_dict["panel_focus_token"] = time.time()
            p = Process(target=run_panel_process, args=(shared_dict, cmd_queue), daemon=True)
            p.start()
            panel_process_info["process"] = p

        def sync_worker():
            while listener.running:
                try:
                    snapshot = host.get_ui_snapshot()
                    shared_dict.update(snapshot)

                    while not cmd_queue.empty():
                        try:
                            cmd_info = cmd_queue.get_nowait()
                            cmd, arg = cmd_info
                            if cmd == "accept":
                                host.accept_pairing_request(arg)
                            elif cmd == "reject":
                                host.reject_pairing_request(arg)
                            elif cmd == "install_autostart":
                                host.install_autostart()
                            elif cmd == "remove_autostart":
                                host.remove_autostart()
                            elif cmd == "full_quit":
                                rumps.quit_application()
                        except Exception as qe:
                            print(f"⚠️ 处理指令失败: {qe}")

                    time.sleep(0.5)
                except Exception as e:
                    if listener.running:
                        print(f"⚠️ 状态同步出错: {e}")
                    time.sleep(1)

        sync_thread = threading.Thread(target=sync_worker, daemon=True)
        sync_thread.start()

        tray = MacTrayApp(host, open_panel)
        listener.ui_attention_callback = tray.notify_pairing_request
        listener.ui_transfer_notify_callback = tray.notify_transfer_complete

        host.start()
        open_panel()
        try:
            tray.run()
        finally:
            host.stop()
            p = panel_process_info["process"]
            if p and p.is_alive():
                p.terminate()
            host.join(5)


def parse_args():
    parser = argparse.ArgumentParser(description="UniPaste macOS node")
    parser.add_argument("--headless", action="store_true", help="仅在后台运行同步服务")
    parser.add_argument("--install-launch-agent", action="store_true", help="安装开机自启动 LaunchAgent")
    parser.add_argument("--remove-launch-agent", action="store_true", help="移除开机自启动 LaunchAgent")
    parser.add_argument("--launch-agent-status", action="store_true", help="查看 LaunchAgent 状态")
    args, unknown = parser.parse_known_args()
    return args


if __name__ == '__main__':
    if getattr(sys, 'frozen', False):
        multiprocessing.set_executable(sys.executable)
    if getattr(sys, 'frozen', False) and sys.platform == 'darwin':
        if len(sys.argv) > 1:
            if sys.argv[1] == '-c' and len(sys.argv) > 2:
                exec(sys.argv[2])
                sys.exit()
            for i, arg in enumerate(sys.argv):
                if arg == '-c' and i + 1 < len(sys.argv):
                    exec(sys.argv[i+1])
                    sys.exit()

    multiprocessing.freeze_support()
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        pass
    args = parse_args()
    launch_agent_manager = MacLaunchAgentManager(script_path=Path(__file__).resolve())

    if args.install_launch_agent:
        ok, message = launch_agent_manager.install()
        print(message)
        raise SystemExit(0 if ok else 1)

    if args.remove_launch_agent:
        ok, message = launch_agent_manager.uninstall()
        print(message)
        raise SystemExit(0 if ok else 1)

    if args.launch_agent_status:
        print("已启用" if launch_agent_manager.is_enabled() else "未启用")
        raise SystemExit(0)

    try:
        if args.headless:
            run_headless()
        else:
            run_control_panel()
    except KeyboardInterrupt:
        print("\n⌨️ 检测到 Ctrl+C，强制退出...")
