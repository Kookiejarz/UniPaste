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

from utils.clipboard_loop_guard import ClipboardLoopGuard
from utils.security.crypto import SecurityManager
from utils.security.auth import DeviceAuthManager
from utils.network.discovery import DeviceDiscovery
from utils.message_format import ClipMessage, MessageType
from handlers.file_handler import FileHandler
from utils.platform_config import verify_platform, IS_WINDOWS
from utils.clipboard_utils import ClipboardUtils
from config import ClipboardConfig

from utils.security.pairing import PairingManager, PairingStatus

# Verify platform at startup
verify_platform('windows')

if not IS_WINDOWS:
    raise RuntimeError("This script requires Windows")

class ConnectionStatus:
    """连接状态枚举"""
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2

class WindowsClipboardClient:
    def __init__(self):
        self.platform_name = "windows"
        self.discovery = DeviceDiscovery()
        self.ws_url = None
        self.is_receiving = False
        self.device_id = self._get_device_id()
        self.device_name = ClipboardConfig.get_device_name(self.platform_name)
        self.device_token = self._load_device_token()
        self.auth_mgr = DeviceAuthManager()
        self.pairing_mgr = PairingManager(timeout_seconds=ClipboardConfig.PAIRING_TIMEOUT_SECONDS)
        self.pairing_mgr.set_pairing_callback(self._on_pairing_request)
        self.running = True
        self.connection_status = ConnectionStatus.DISCONNECTED
        self.last_change_count = ClipboardUtils.get_clipboard_change_count()
        self.last_content_hash = None
        self.last_format_log = set()
        self.server = None
        self.peer_connections = {}
        self.websocket_peer_ids = {}
        self.connection_security = {}
        self.discovered_peers = {}
        self.service_name_to_id = {}
        self.peer_platforms = {}
        self.peer_retry_state = {}
        self.loop_guard = ClipboardLoopGuard()
        self.server_task = None
        self.clipboard_task = None
        self.sync_task = None
        self.status_task = None
        self.event_loop = None
        self.ui_attention_callback = None
        self.last_ui_error = None
        self.discovery_event = asyncio.Event()

        self.file_handler = FileHandler(ClipboardConfig.get_temp_dir())
        self.file_handler.load_file_cache()

    def _get_device_id(self):
        """获取唯一设备ID"""
        import socket
        try:
            hostname = socket.gethostname()
            import uuid
            mac_num = uuid.getnode()
            mac = ':'.join(('%012X' % mac_num)[i:i+2] for i in range(0, 12, 2))
            # Use a portion of the MAC to keep it shorter but still unique
            mac_part = mac.replace(':', '')[-6:]
            return f"{hostname}-{mac_part}"
        except Exception as e:
            print(f"⚠️ 无法获取MAC地址 ({e})，将生成随机ID。")
            import random
            return f"windows-{random.randint(10000, 99999)}"


    def _get_token_path(self):
        """获取令牌存储路径"""
        return ClipboardConfig.get_device_token_path(self.platform_name)

    def _load_device_token(self):
        """加载设备令牌"""
        token_path = self._get_token_path()
        if token_path.exists():
            try:
                with open(token_path, "r") as f:
                    return f.read().strip()
            except Exception as e:
                 print(f"❌ 加载设备令牌失败: {e}")
        return None

    def _save_device_token(self, token):
        """保存设备令牌"""
        token_path = self._get_token_path()
        try:
            with open(token_path, "w") as f:
                f.write(token)
            print(f"💾 设备令牌已保存到 {token_path}")
        except Exception as e:
             print(f"❌ 保存设备令牌失败: {e}")

    def _generate_signature(self):
        """生成签名"""
        if not self.device_token:
            return ""
        try:
            return hmac.new(
                self.device_token.encode(),
                self.device_id.encode(),
                hashlib.sha256
            ).hexdigest()
        except Exception as e:
             print(f"❌ 生成签名失败: {e}")
             return ""

    def _on_pairing_request(self, request):
        print(f"\n{'='*60}")
        print("🔗 新设备请求配对:")
        print(f"   设备名称: {request.device_name}")
        print(f"   平台: {request.platform}")
        print(f"   IP地址: {request.ip_address}")
        print(f"   设备ID: {request.device_id}")
        print(f"{'='*60}")
        print("请在托盘控制面板中确认是否允许此设备连接")

        if callable(self.ui_attention_callback):
            self.ui_attention_callback()
            return

        if sys.stdin and sys.stdin.isatty():
            print("是否允许此设备连接? (输入 'y' 接受, 'n' 拒绝)")

            def get_user_input():
                try:
                    choice = input().strip().lower()
                    if choice in ['y', 'yes', 'accept', '是', '接受']:
                        self.pairing_mgr.accept_pairing(request.device_id)
                    else:
                        self.pairing_mgr.reject_pairing(request.device_id)
                except Exception:
                    self.pairing_mgr.reject_pairing(request.device_id)

            threading.Thread(target=get_user_input, daemon=True).start()

    def report_ui_error(self, message: str):
        self.last_ui_error = message

    def accept_pairing_request(self, device_id: str) -> bool:
        return self.pairing_mgr.accept_pairing(device_id)

    def reject_pairing_request(self, device_id: str) -> bool:
        return self.pairing_mgr.reject_pairing(device_id)

    def get_ui_snapshot(self):
        now = time.time()
        connected_peers = []
        for peer_id in sorted(self.peer_connections):
            peer_info = self.discovered_peers.get(peer_id, {})
            connected_peers.append({
                "peer_id": peer_id,
                "platform": peer_info.get("platform", "unknown"),
                "url": peer_info.get("url"),
            })

        discovered_peers = []
        for peer_id, peer_info in sorted(self.discovered_peers.items()):
            retry_state = self.peer_retry_state.get(peer_id)
            retry_in = 0.0
            if retry_state and retry_state["next_retry_at"] > now:
                retry_in = retry_state["next_retry_at"] - now
            discovered_peers.append({
                "peer_id": peer_id,
                "platform": peer_info.get("platform", "unknown"),
                "url": peer_info.get("url"),
                "connected": peer_id in self.peer_connections,
                "retry_in": retry_in if retry_in > 0 else None,
            })

        pending_pairings = [
            {
                "device_id": request.device_id,
                "device_name": request.device_name,
                "platform": request.platform,
                "ip_address": request.ip_address,
            }
            for request in self.pairing_mgr.list_pending_requests()
        ]

        return {
            "platform": self.platform_name,
            "device_name": self.device_name,
            "device_id": self.device_id,
            "status_text": {
                ConnectionStatus.DISCONNECTED: "等待对端连接",
                ConnectionStatus.CONNECTING: "正在连接",
                ConnectionStatus.CONNECTED: f"已连接 {len(self.peer_connections)} 台设备",
            }.get(self.connection_status, "未知状态"),
            "connected_peers": connected_peers,
            "discovered_peers": discovered_peers,
            "pending_pairings": pending_pairings,
            "last_error": self.last_ui_error,
        }

    def _should_initiate_connection(self, peer_id: str) -> bool:
        """
        决定是否由本端发起连接。
        为了避免 Windows 总是成为被连接方，我们通过比较 ID 的哈希值来决定，
        这样发起方在所有设备之间是均匀分布的。
        """
        if not peer_id or peer_id == self.device_id:
            return False
            
        # 使用哈希值比较，确保即使主机名字母序小，也有一半几率成为发起方
        my_hash = hashlib.md5(self.device_id.encode()).hexdigest()
        peer_hash = hashlib.md5(peer_id.encode()).hexdigest()
        return my_hash > peer_hash

    def _update_connection_status(self):
        if self.peer_connections:
            self.connection_status = ConnectionStatus.CONNECTED
        elif self.ws_url:
            self.connection_status = ConnectionStatus.CONNECTING
        else:
            self.connection_status = ConnectionStatus.DISCONNECTED

    def on_service_found(self, service_info):
        """服务发现回调"""
        if isinstance(service_info, str):
            service_info = {"url": service_info, "properties": {}}

        url = service_info.get("url")
        properties = service_info.get("properties", {})
        peer_id = properties.get("device_id")
        platform = properties.get("platform", "unknown")
        service_name = service_info.get("name")

        if not url or not peer_id or peer_id == self.device_id:
            return

        self.discovered_peers[peer_id] = {
            "url": url,
            "platform": platform,
        }
        if service_name:
            self.service_name_to_id[service_name] = peer_id
            
        self.peer_platforms[peer_id] = platform
        if self._should_initiate_connection(peer_id):
            self.ws_url = url
        print(f"✅ 发现设备 {peer_id} ({platform}): {url}")
        
        # 唤醒重连循环
        if self.event_loop and self.event_loop.is_running():
            self.event_loop.call_soon_threadsafe(self.discovery_event.set)

    def on_service_lost(self, service_name):
        """服务丢失回调"""
        peer_id = self.service_name_to_id.pop(service_name, None)
        if peer_id:
            if peer_id in self.discovered_peers:
                del self.discovered_peers[peer_id]
                print(f"➖ 设备离线: {peer_id}")
            # If we were connecting to this peer, we might want to clear ws_url 
            # but usually it's handled by connection error.

    def stop(self):
        """停止节点运行"""
        if not self.running: return
        print("\n⏹️ 正在停止节点...")
        self.running = False
        # Close discovery
        if hasattr(self, 'discovery'):
            self.discovery.close()
        # Save file cache
        if hasattr(self, 'file_handler'):
            self.file_handler.save_file_cache()
            self.file_handler.cleanup()
            
        # Cancel tasks to wake up from sleep
        if self.event_loop and self.event_loop.is_running():
            for task_attr in ['server_task', 'clipboard_task', 'sync_task', 'status_task']:
                task = getattr(self, task_attr, None)
                if task and not task.done():
                    self.event_loop.call_soon_threadsafe(task.cancel)
            
            if hasattr(self, '_stop_server_func'):
                self.event_loop.call_soon_threadsafe(self._stop_server_func)
        else:
            if hasattr(self, '_stop_server_func'):
                self._stop_server_func()
                
        # Close all active connections
        if self.peer_connections:
            print(f"📤 正在关闭 {len(self.peer_connections)} 个连接...")
            # We don't necessarily need to wait for these here as the tasks handling them are cancelled
        
        print("👋 感谢使用 UniPaste!")

    def _register_peer(self, peer_id, websocket):
        existing = self.peer_connections.get(peer_id)
        if existing and existing != websocket:
            return False
        self.peer_connections[peer_id] = websocket
        self.websocket_peer_ids[websocket] = peer_id
        self._update_connection_status()
        return True

    def _unregister_peer(self, websocket):
        peer_id = self.websocket_peer_ids.pop(websocket, None)
        if peer_id and self.peer_connections.get(peer_id) == websocket:
            del self.peer_connections[peer_id]
        self.connection_security.pop(websocket, None)
        self._update_connection_status()
        return peer_id

    def _get_connection_security(self, websocket, create=False):
        manager = self.connection_security.get(websocket)
        if manager is None and create:
            manager = SecurityManager()
            manager.generate_key_pair()
            self.connection_security[websocket] = manager
        return manager

    def _build_clipboard_snapshot(self):
        file_paths = ClipboardUtils.get_clipboard_files()
        if file_paths:
            return {
                "kind": "files",
                "fingerprint": self.file_handler.get_files_content_hash(file_paths),
                "text": None,
            }

        text = ClipboardUtils.get_clipboard_text()
        if text:
            return {
                "kind": "text",
                "fingerprint": hashlib.md5(text.encode()).hexdigest(),
                "text": text,
            }
        return None

    def _consume_expected_clipboard_echo(self, change_count: int | None) -> bool:
        snapshot = self._build_clipboard_snapshot()
        if not snapshot:
            return False

        event = self.loop_guard.consume_if_expected(
            kind=snapshot["kind"],
            fingerprint=snapshot["fingerprint"],
            change_count=change_count,
        )
        if not event:
            return False

        self.last_change_count = change_count
        self.last_content_hash = snapshot["fingerprint"]
        print(f"⏭️ 已消费远端{event.kind}剪贴板回显，不再回传")
        return True

    def _register_applied_remote_event(self, message: dict, kind: str, fingerprint: str | None):
        self.loop_guard.register(
            kind=kind,
            fingerprint=fingerprint,
            expected_change_count=ClipboardUtils.get_clipboard_change_count(),
            event_id=message.get("event_id"),
            origin_device_id=message.get("origin_device_id"),
        )

    def _get_peer_retry_state(self, peer_id):
        return self.peer_retry_state.setdefault(
            peer_id,
            {"failures": 0, "next_retry_at": 0.0},
        )

    def _reset_peer_retry(self, peer_id):
        if peer_id:
            self.peer_retry_state.pop(peer_id, None)

    def _mark_peer_failure(self, peer_id, error=None):
        if not peer_id:
            return

        state = self._get_peer_retry_state(peer_id)
        state["failures"] += 1
        delay = min(
            ClipboardConfig.PEER_RETRY_BASE_DELAY * (2 ** (state["failures"] - 1)),
            ClipboardConfig.PEER_RETRY_MAX_DELAY,
        )
        state["next_retry_at"] = time.time() + delay

        if error:
            print(f"⏳ 设备 {peer_id} 进入冷却 {delay:.0f} 秒: {error}")
        else:
            print(f"⏳ 设备 {peer_id} 进入冷却 {delay:.0f} 秒")

    @staticmethod
    def _is_expected_peer_unavailable_error(error) -> bool:
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return True
        if isinstance(error, websockets.exceptions.ConnectionClosed):
            return getattr(error, "code", None) in {1000, 1001}

        message = str(error).lower()
        expected_markers = (
            "timed out during opening handshake",
            "received 1000",
            "received 1001",
            "connection refused",
            "connect call failed",
            "server rejected websocket connection",
        )
        return any(marker in message for marker in expected_markers)

    async def broadcast_encrypted_data(self, data_to_encrypt: bytes, exclude_websocket=None):
        if not self.peer_connections:
            return

        tasks = []
        for websocket in list(self.peer_connections.values()):
            if websocket == exclude_websocket:
                continue
            security_mgr = self._get_connection_security(websocket)
            if not security_mgr or not security_mgr.has_shared_key():
                continue
            try:
                encrypted_data = security_mgr.encrypt_message(data_to_encrypt)
            except Exception as e:
                print(f"❌ 加密广播数据失败: {e}")
                continue
            tasks.append(asyncio.create_task(websocket.send(encrypted_data)))

        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=10.0)
            for task in pending:
                task.cancel()
            for task in done:
                if task.exception():
                    print(f"❌ 广播发送时出错: {task.exception()}")

    async def send_current_clipboard_to_peer(self, websocket):
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
                    event_id=ClipMessage.new_event_id()
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

    async def start_server(self, port=ClipboardConfig.DEFAULT_PORT):
        stop_event = asyncio.Event()

        async def server_logic():
            try:
                self.server = await websockets.serve(
                    self.handle_client,
                    ClipboardConfig.HOST,
                    port,
                    subprotocols=["binary"],
                    ping_interval=ClipboardConfig.PING_INTERVAL,
                    ping_timeout=ClipboardConfig.PING_TIMEOUT
                )
                await self.discovery.start_advertising(
                    port,
                    device_id=self.device_id,
                    platform=self.platform_name
                )
                print(f"🌐 Windows 对等节点监听在 {ClipboardConfig.HOST}:{port}")
                await stop_event.wait()
            finally:
                if self.server:
                    self.server.close()
                    await self.server.wait_closed()
                    self.server = None

        self._stop_server_func = stop_event.set
        await server_logic()

    async def _authenticate_incoming(self, websocket):
        device_id = None
        client_ip = websocket.remote_address[0] if websocket.remote_address else "未知IP"
        auth_message = await websocket.recv()

        if isinstance(auth_message, str):
            message_data = json.loads(auth_message)
        else:
            message_data = json.loads(auth_message.decode("utf-8"))

        device_id = message_data.get("identity", f"unknown-{client_ip}")
        signature = message_data.get("signature", "")
        is_first_time = message_data.get("first_time", False)

        print(f"📱 设备 {device_id} ({client_ip}) 尝试连接")

        if is_first_time:
            print(f"🆕 设备 {device_id} 首次连接，需要配对...")
            await self.pairing_mgr.request_pairing(device_id, message_data, client_ip)
            pairing_result = await self.pairing_mgr.wait_for_pairing_result(device_id)
            if pairing_result == PairingStatus.ACCEPTED:
                token = self.auth_mgr.authorize_device(device_id, {
                    "name": message_data.get("device_name", "未命名设备"),
                    "platform": message_data.get("platform", "未知平台"),
                    "ip": client_ip,
                })
                await websocket.send(json.dumps({
                    "status": "pairing_accepted",
                    "peer_id": self.device_id,
                    "token": token,
                }))
                return device_id, message_data
            if pairing_result == PairingStatus.REJECTED:
                await websocket.send(json.dumps({
                    "status": "pairing_rejected",
                    "reason": "User rejected pairing request",
                    "peer_id": self.device_id,
                }))
                return None, None

            await websocket.send(json.dumps({
                "status": "pairing_expired",
                "reason": "Pairing request timed out",
                "peer_id": self.device_id,
            }))
            return None, None

        print(f"🔐 验证设备 {device_id} 的签名")
        if not self.auth_mgr.validate_device(device_id, signature):
            await websocket.send(json.dumps({
                "status": "unauthorized",
                "reason": "Invalid signature or unknown device",
                "peer_id": self.device_id,
            }))
            return None, None

        await websocket.send(json.dumps({
            "status": "authorized",
            "peer_id": self.device_id,
        }))
        return device_id, message_data

    async def handle_client(self, websocket):
        peer_id = None
        try:
            peer_id, _ = await self._authenticate_incoming(websocket)
            if not peer_id:
                return

            if not await self.perform_key_exchange_as_server(websocket):
                print(f"❌ 与 {peer_id} 的密钥交换失败，断开连接")
                return

            if not self._register_peer(peer_id, websocket):
                print(f"⚠️ 已存在与 {peer_id} 的连接，关闭重复入站连接")
                await websocket.close()
                return

            print(f"✅ 设备 {peer_id} 已连接并完成密钥交换")
            await self.send_current_clipboard_to_peer(websocket)
            await self.receive_clipboard_changes(websocket)

        except websockets.exceptions.ConnectionClosed as e:
            print(f"📴 设备 {peer_id or 'unknown'} 连接已关闭: {e}")
        except Exception as e:
            print(f"❌ 处理入站连接时出错: {e}")
            traceback.print_exc()
        finally:
            removed_peer = self._unregister_peer(websocket)
            if removed_peer:
                print(f"➖ 设备 {removed_peer} 已断开")

    async def sync_clipboard(self):
        """主同步循环，处理连接和重连"""
        print("🔍 搜索剪贴板服务...")
        self.discovery.start_discovery(self.on_service_found, self.on_service_lost)

        while self.running:
            try:
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
                    self.ws_url = candidate_url
                    self.connection_status = ConnectionStatus.CONNECTING
                    print(f"🔌 正在连接到设备 {candidate_peer_id}: {candidate_url}")
                    try:
                        await self.connect_and_sync(candidate_peer_id, candidate_url)
                    except Exception as e:
                        if self._is_expected_peer_unavailable_error(e):
                            print(f"ℹ️ 设备 {candidate_peer_id} 当前不可用，等待重连... ({e})")
                        else:
                            print(f"❌ 与设备 {candidate_peer_id} 建立连接失败: {e}")
                            traceback.print_exc()
                        # 失败后微休眠
                        await asyncio.sleep(2)
                else:
                    self._update_connection_status()
                    # 彻底挂起，直到被 mDNS 发现回调唤醒
                    await self.discovery_event.wait()

            except asyncio.CancelledError:
                print("🛑 同步任务被取消")
                break
            except Exception as e:
                print(f"❌ 主同步循环出错: {e}")
                traceback.print_exc()
                # Avoid tight loop on unexpected error
                await asyncio.sleep(5)

    async def connect_and_sync(self, peer_id, ws_url):
        """主动连接到对等设备并处理消息"""
        # Specify binary subprotocol and increase max message size
        async with websockets.connect(
            ws_url,
            subprotocols=["binary"],
            max_size=ClipboardConfig.WEBSOCKET_MAX_SIZE,
            ping_interval=ClipboardConfig.PING_INTERVAL,
            ping_timeout=ClipboardConfig.PING_TIMEOUT
        ) as websocket:
            # --- Authentication ---
            remote_peer_id = await self.authenticate(websocket)
            if not remote_peer_id:
                self._mark_peer_failure(peer_id, "身份验证失败")
                print("❌ 身份验证失败，断开连接")
                return # Close connection

            # --- Key Exchange ---
            if not await self.perform_key_exchange_as_client(websocket):
                self._mark_peer_failure(remote_peer_id, "密钥交换失败")
                print("❌ 密钥交换失败，断开连接")
                return # Close connection

            if not self._register_peer(remote_peer_id, websocket):
                self._reset_peer_retry(remote_peer_id)
                print(f"⚠️ 已存在与 {remote_peer_id} 的连接，关闭重复出站连接")
                return

            self._reset_peer_retry(peer_id)
            self._reset_peer_retry(remote_peer_id)
            self.connection_status = ConnectionStatus.CONNECTED
            self.last_content_hash = None
            print(f"✅ 已连接到设备 {remote_peer_id}，开始同步剪贴板")
            await self.send_current_clipboard_to_peer(websocket)
            try:
                await self.receive_clipboard_changes(websocket)
            finally:
                removed_peer = self._unregister_peer(websocket)
                if removed_peer:
                    print(f"➖ 设备 {removed_peer} 已断开")


    async def authenticate(self, websocket):
        """与对等设备进行身份验证"""
        try:
            is_first_time = self.device_token is None

            auth_info = {
                'identity': self.device_id,
                'signature': self._generate_signature(),
                'first_time': is_first_time,
                'device_name': self.device_name,
                'platform': self.platform_name
            }

            if is_first_time:
                print(f"🔗 首次连接设备 ID: {self.device_id}")
                print("正在请求与对端配对...")
            else:
                print(f"🔑 已注册设备 ID: {self.device_id}")
                
            await websocket.send(json.dumps(auth_info))

            # Wait for response with timeout
            auth_response_raw = await asyncio.wait_for(websocket.recv(), timeout=30.0)  # Longer timeout for pairing

            if isinstance(auth_response_raw, bytes):
                auth_response = auth_response_raw.decode('utf-8')
            else:
                 auth_response = auth_response_raw # Assume string

            response_data = json.loads(auth_response)
            status = response_data.get('status')

            if status == 'authorized':
                print(f"✅ 身份验证成功! 对端: {response_data.get('peer_id', '未知')}")
                return response_data.get("peer_id")
            elif status == 'pairing_accepted':
                token = response_data.get('token')
                if token:
                    self._save_device_token(token)
                    self.device_token = token
                    print(f"🎉 设备配对成功并获取授权令牌!")
                    return response_data.get("peer_id")
                else:
                    print(f"❌ 对端在配对成功时未提供令牌")
                    return None
            elif status == 'pairing_rejected':
                print(f"❌ 配对被对端拒绝: {response_data.get('reason', '未知原因')}")
                return None
            elif status == 'pairing_expired':
                print(f"⏰ 配对请求超时: {response_data.get('reason', '未知原因')}")
                print("请重新尝试连接并确保及时在对端确认配对")
                return None
            else:
                reason = response_data.get('reason', '未知原因')
                print(f"❌ 身份验证失败: {reason}")
                if status == 'unauthorized':
                    print("⚠️ 本地令牌可能已失效，正在清除并准备重新配对...")
                    token_path = self._get_token_path()
                    if token_path.exists():
                        token_path.unlink()
                    self.device_token = None
                return None
                
        except asyncio.TimeoutError:
            print("❌ 等待配对响应超时 (可能需要在对端手动确认)")
            return None
        except Exception as e:
            print(f"❌ 身份验证过程中出错: {e}")
            return None

    async def _send_encrypted(self, data: bytes, websocket):
        """Helper to encrypt and send data via the websocket."""
        try:
            security_mgr = self._get_connection_security(websocket)
            if not security_mgr or not security_mgr.has_shared_key():
                raise ValueError("Connection shared key not established")
            encrypted = security_mgr.encrypt_message(data)
            await websocket.send(encrypted)
        except websockets.exceptions.ConnectionClosed:
             print("❌ 发送数据失败：连接已关闭")
             self.connection_status = ConnectionStatus.DISCONNECTED # Update status
             raise # Re-raise to stop the sending loop
        except Exception as e:
            print(f"❌ 发送加密数据失败: {e}")
            traceback.print_exc()
            # Consider updating connection status on other errors too
            # self.connection_status = ConnectionStatus.DISCONNECTED
            raise # Re-raise


    async def send_clipboard_changes(self):
        """监控并发送剪贴板变化"""
        async def send_encrypted_wrapper(data_to_encrypt: bytes):
            await self.broadcast_encrypted_data(data_to_encrypt)

        while self.running:
            try:
                if not self.peer_connections:
                    await asyncio.sleep(ClipboardConfig.CLIPBOARD_CHECK_INTERVAL)
                    continue

                if self.is_receiving:
                    await asyncio.sleep(0.1)
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
                            event_id=ClipMessage.new_event_id()
                        )
                        if update_sent:
                            self.last_content_hash = new_hash
                            sent_update_this_cycle = True
                            print("📤 文件信息已发送，等待对端按需请求内容...")

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
                await asyncio.sleep(1) # Avoid tight loop on error


    async def receive_clipboard_changes(self, websocket):
        """接收来自对等设备的剪贴板变化"""
        async def send_encrypted_wrapper(data_to_encrypt: bytes):
            await self._send_encrypted(data_to_encrypt, websocket)

        while self.running:
            try:
                # Receive data with timeout
                received_data = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                self.is_receiving = True # Set flag

                # Decrypt and process
                security_mgr = self._get_connection_security(websocket)
                if not security_mgr or not security_mgr.has_shared_key():
                    raise ValueError("Connection shared key not established")
                decrypted_data = security_mgr.decrypt_message(received_data)
                message_json = decrypted_data.decode('utf-8')
                message = ClipMessage.deserialize(message_json)

                if not message or "type" not in message:
                     print("⚠️ 收到的消息格式无效或无法解析")
                     continue # Skip this message

                msg_type = message["type"]
                origin_device_id = message.get("origin_device_id")
                if origin_device_id and origin_device_id == self.device_id:
                    continue
                print(f"📬 收到消息类型: {msg_type}")

                if msg_type == MessageType.TEXT:
                    await self._handle_text_message(message)
                elif msg_type == MessageType.FILE:
                    # Handle file info - request missing files via wrapper
                    await self.file_handler.handle_received_files(
                         message, send_encrypted_wrapper, sender_websocket=websocket
                    )
                elif msg_type == MessageType.FILE_START:
                    await self.file_handler.handle_transfer_start(message)
                elif msg_type == MessageType.FILE_RESPONSE:
                    # Handle incoming file chunk
                    await self._handle_file_response(message, send_encrypted_wrapper)
                elif msg_type == MessageType.FILE_CHUNK:
                    await self._handle_file_response(message, send_encrypted_wrapper)
                elif msg_type == MessageType.FILE_REQUEST:
                     # Peer is requesting a file from us
                     file_path_requested = message.get("path")
                     resume_from_chunk = int(message.get("resume_from_chunk") or 0)
                     transfer_id = message.get("transfer_id")
                     event_id = message.get("event_id")
                     request_origin = message.get("origin_device_id")
                     if file_path_requested:
                          print(f"📤 收到文件请求: {Path(file_path_requested).name}")
                          await self.file_handler.handle_file_transfer(
                               file_path_requested,
                               send_encrypted_wrapper,
                               start_chunk=resume_from_chunk,
                               transfer_id=transfer_id,
                               origin_device_id=request_origin,
                               event_id=event_id
                          )
                     else:
                          print("⚠️ 收到的文件请求缺少路径")
                else:
                     print(f"⚠️ 未知消息类型: {msg_type}")


            except asyncio.TimeoutError:
                 # No message received, check connection with ping
                 try:
                      pong_waiter = await websocket.ping()
                      await asyncio.wait_for(pong_waiter, timeout=5)
                 except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                      print("⌛ 与对端的连接超时或关闭，断开")
                      break # Exit receive loop
                 continue # Continue loop after successful ping/pong
            except websockets.exceptions.ConnectionClosedOK:
                 print("ℹ️ 接收循环检测到连接正常关闭")
                 break
            except websockets.exceptions.ConnectionClosedError as e:
                 print(f"🔌 接收循环检测到连接异常关闭: {e}")
                 break
            except asyncio.CancelledError:
                print("⏹️ 接收任务被取消")
                break
            except json.JSONDecodeError:
                 print("❌ 收到的消息不是有效的JSON")
            except UnicodeDecodeError:
                 print("❌ 无法将收到的消息解码为UTF-8")
            except Exception as e:
                print(f"❌ 处理接收数据时出错: {e}")
                traceback.print_exc()
                await asyncio.sleep(1)
            finally:
                 self.is_receiving = False # Reset flag


    async def perform_key_exchange_as_client(self, websocket):
        """作为主动连接方执行密钥交换"""
        try:
            security_mgr = self._get_connection_security(websocket, create=True)

            peer_key_message = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            peer_data = json.loads(peer_key_message)

            if peer_data.get("type") != "key_exchange":
                print("❌ 对端未按预期发送公钥")
                return False

            peer_key_data = peer_data.get("public_key")
            peer_public_key = security_mgr.deserialize_public_key(peer_key_data)

            client_public_key = security_mgr.serialize_public_key()
            await websocket.send(json.dumps({
                "type": "key_exchange",
                "public_key": client_public_key
            }))
            print("📤 已发送本端公钥")

            security_mgr.generate_shared_key(peer_public_key)
            print("🔒 密钥交换完成，已建立共享密钥")

            confirmation = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            confirm_data = json.loads(confirmation)

            if confirm_data.get("type") == "key_exchange_complete" and confirm_data.get("status") == "success":
                print("✅ 对端确认密钥交换成功")
                return True
            else:
                print("⚠️ 未收到对端的密钥交换成功确认")
                return False

        except asyncio.TimeoutError:
             print("❌ 密钥交换步骤超时")
             return False
        except json.JSONDecodeError:
             print("❌ 密钥交换消息格式无效")
             return False
        except Exception as e:
            print(f"❌ 密钥交换失败: {e}")
            traceback.print_exc()
            return False

    async def perform_key_exchange_as_server(self, websocket):
        """作为接收连接方执行密钥交换"""
        security_mgr = self._get_connection_security(websocket, create=True)

        async def send_to_websocket(data):
            await websocket.send(data)

        async def receive_from_websocket():
            return await websocket.recv()

        return await security_mgr.perform_key_exchange(
            send_to_websocket,
            receive_from_websocket
        )

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
                    # Clear previous status line
                    if status_line:
                        sys.stdout.write("\r" + " " * len(status_line) + "\r")

                    # Display new status
                    status_line = status_messages.get(current_status, "⚪ 未知状态")
                    sys.stdout.write(f"\r{status_line}")
                    sys.stdout.flush()
                    last_status = current_status

                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                # Clear status line on exit
                if status_line:
                     sys.stdout.write("\r" + " " * len(status_line) + "\r")
                     sys.stdout.flush()
                break
            except Exception as e:
                 print(f"\n⚠️ 状态显示错误: {e}") # Avoid crashing status display
                 last_status = None # Force redraw on next iteration
                 await asyncio.sleep(2)


    async def _handle_text_message(self, message):
        """处理收到的文本消息"""
        try:
            text = message.get("content", "")
            if not text:
                print("⚠️ 收到空文本消息")
                return

            # Use FileHandler's check
            if self.file_handler._looks_like_temp_file_path(text):
                return

            # Calculate hash *before* setting clipboard
            content_hash = hashlib.md5(text.encode()).hexdigest()

            # Check if this content hash was the last one *we* sent or set
            if content_hash == self.last_content_hash:
                print("⏭️ 跳过重复内容 (与本地最后发送/设置一致)")
                return

            if not ClipboardUtils.set_clipboard_text(text):
                print("❌ 更新Windows文本剪贴板失败")
                return

            self.last_change_count = ClipboardUtils.get_clipboard_change_count()
            self.last_content_hash = content_hash
            self._register_applied_remote_event(message, "text", content_hash)

            display_text = text[:ClipboardConfig.MAX_DISPLAY_LENGTH] + ("..." if len(text) > ClipboardConfig.MAX_DISPLAY_LENGTH else "")
            print(f"📥 已复制文本: \"{display_text}\"")
        except Exception as e:
            print(f"❌ 处理文本消息时出错: {e}")
            traceback.print_exc()


    async def _handle_file_response(self, message, send_encrypted_wrapper):
        """处理接收到的文件响应 (块)"""
        try:
            # Use FileHandler to process the chunk
            is_complete, completed_path = await self.file_handler.handle_received_chunk(
                message,
                send_encrypted_wrapper
            )

            # If file transfer is complete
            if is_complete and completed_path:
                print(f"✅ 文件接收完成: {completed_path}")
                completed_path = self.file_handler.materialize_received_path(message, completed_path)

                # Calculate hash of the completed file
                content_hash = self.file_handler.get_files_content_hash([str(completed_path)])

                # Check if this file content hash was the last one *we* sent or set
                if content_hash and content_hash == self.last_content_hash:
                    print("⏭️ 跳过重复文件内容 (与本地最后发送/设置一致)")
                    return # Don't update clipboard

                if ClipboardUtils.set_clipboard_file(completed_path):
                     self.last_change_count = ClipboardUtils.get_clipboard_change_count()
                     self.last_content_hash = content_hash
                     self._register_applied_remote_event(message, "files", content_hash)
                     print("🔄 文件已登记为远端事件回显，下一次变化不会回传")
                else:
                     print(f"❌ 未能将文件 {completed_path.name} 设置到剪贴板")


        except Exception as e:
            print(f"❌ 处理文件响应时出错: {e}")
            traceback.print_exc()


async def run_client_tasks(client: WindowsClipboardClient, include_status: bool = True):
    client.event_loop = asyncio.get_running_loop()
    status_task = None
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
        # Expected on shutdown
        pass
    finally:
        client.stop()
        for task in task_group:
            if task and not task.done():
                task.cancel()
        if task_group:
            await asyncio.gather(*task_group, return_exceptions=True)


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
    client.ui_attention_callback = tray.notify_pairing_request
    host.start()
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
