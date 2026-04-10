import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import threading
import traceback
from abc import ABC, abstractmethod
from pathlib import Path

import websockets

from config import ClipboardConfig
from handlers.file_handler import FileHandler
from utils.clipboard_loop_guard import ClipboardLoopGuard
from utils.message_format import ClipMessage, MessageType
from utils.network.discovery import DeviceDiscovery
from utils.security.auth import DeviceAuthManager
from utils.security.crypto import SecurityManager
from utils.security.pairing import PairingManager, PairingStatus


class ClipboardNode(ABC):
    """Abstract base class for clipboard sync nodes (macOS and Windows)."""

    # ------------------------------------------------------------------
    # Abstract interface — subclasses MUST implement
    # ------------------------------------------------------------------

    @abstractmethod
    def _read_clipboard_snapshot(self) -> dict | None:
        """Return {kind, fingerprint} or None if clipboard is empty."""

    @abstractmethod
    async def _apply_text_to_clipboard(self, text: str) -> int | None:
        """Write text to clipboard. Return new change_count or None on failure."""

    @abstractmethod
    async def _apply_received_file_to_clipboard(self, message: dict, completed_path: Path):
        """Write a received file to the platform clipboard."""

    @abstractmethod
    async def _watch_clipboard(self):
        """Platform polling loop — detect local clipboard changes and broadcast."""

    @abstractmethod
    async def send_current_clipboard_to_peer(self, websocket):
        """Send current clipboard snapshot to a newly connected peer."""

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, platform_name: str):
        self.platform_name = platform_name
        self.auth_mgr = DeviceAuthManager()
        self.discovery = DeviceDiscovery()
        self.file_handler = FileHandler(ClipboardConfig.get_temp_dir())
        self.file_handler.load_file_cache()
        self.loop_guard = ClipboardLoopGuard()
        self.device_id = self._get_device_id()
        self.device_name = ClipboardConfig.get_device_name(platform_name)
        self.device_token = self._load_device_token()
        self.pairing_mgr = PairingManager(timeout_seconds=ClipboardConfig.PAIRING_TIMEOUT_SECONDS)
        self.pairing_mgr.set_pairing_callback(self._on_pairing_request)
        self.discovered_peers: dict = {}
        self.service_name_to_id: dict = {}
        self.peer_connections: dict = {}
        self.websocket_peer_ids: dict = {}
        self.connection_security: dict = {}
        self.connection_send_locks: dict = {}
        self.peer_retry_state: dict = {}
        self.background_tasks: set = set()
        self.server_task = None
        self.clipboard_task = None
        self.sync_task = None
        self.status_task = None
        self.event_loop = None
        self.last_ui_error = None
        self.server = None
        self.last_content_hash = None
        self.last_change_count = 0
        self.is_receiving = False
        self.running = True
        self.discovery_event = asyncio.Event()
        self.ui_attention_callback = None
        self.ui_transfer_notify_callback = None
        self.file_handler.on_transfer_complete = self._on_file_transfer_complete

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def _get_device_id(self):
        import socket
        import uuid
        try:
            hostname = socket.gethostname()
            mac_num = uuid.getnode()
            mac = ':'.join(('%012X' % mac_num)[i:i+2] for i in range(0, 12, 2))
            mac_part = mac.replace(':', '')[-6:]
            return f"{hostname}-{mac_part}"
        except Exception:
            return f"{self.platform_name}-{int(time.time())}"

    def _get_token_path(self):
        return ClipboardConfig.get_device_token_path(self.platform_name)

    def _load_device_token(self):
        token_path = self._get_token_path()
        if token_path.exists():
            try:
                with open(token_path, "r") as f:
                    return f.read().strip()
            except Exception as e:
                print(f"❌ 加载设备令牌失败: {e}")
        return None

    def _save_device_token(self, token):
        token_path = self._get_token_path()
        try:
            with open(token_path, "w") as f:
                f.write(token)
            print(f"💾 设备令牌已保存到 {token_path}")
        except Exception as e:
            print(f"❌ 保存设备令牌失败: {e}")

    def _generate_signature(self):
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

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _register_peer(self, peer_id, websocket):
        existing = self.peer_connections.get(peer_id)
        if existing and existing != websocket:
            return False
        self.peer_connections[peer_id] = websocket
        self.websocket_peer_ids[websocket] = peer_id
        self.connection_send_locks.setdefault(websocket, asyncio.Lock())
        self._update_connection_status()
        return True

    def _unregister_peer(self, websocket):
        peer_id = self.websocket_peer_ids.pop(websocket, None)
        if peer_id and self.peer_connections.get(peer_id) == websocket:
            del self.peer_connections[peer_id]
        self.connection_security.pop(websocket, None)
        self.connection_send_locks.pop(websocket, None)
        self._update_connection_status()
        return peer_id

    def _get_connection_security(self, websocket, create=False):
        manager = self.connection_security.get(websocket)
        if manager is None and create:
            manager = SecurityManager()
            manager.generate_key_pair()
            self.connection_security[websocket] = manager
        return manager

    def _get_send_lock(self, websocket):
        return self.connection_send_locks.setdefault(websocket, asyncio.Lock())

    def _update_connection_status(self):
        """No-op in base. Subclasses that track connection_status (Windows) override this."""
        pass

    # ------------------------------------------------------------------
    # Retry state
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def on_service_found(self, service_info):
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

        print(f"✅ 发现设备 {peer_id} ({platform}): {url}")

        if self.event_loop and self.event_loop.is_running():
            self.event_loop.call_soon_threadsafe(self.discovery_event.set)

    def on_service_lost(self, service_name):
        peer_id = self.service_name_to_id.pop(service_name, None)
        if peer_id:
            if peer_id in self.discovered_peers:
                del self.discovered_peers[peer_id]
                print(f"➖ 设备离线: {peer_id}")

    # ------------------------------------------------------------------
    # Should initiate
    # ------------------------------------------------------------------

    def _should_initiate_connection(self, peer_id: str) -> bool:
        if not peer_id or peer_id == self.device_id:
            return False
        my_hash = hashlib.md5(self.device_id.encode()).hexdigest()
        peer_hash = hashlib.md5(peer_id.encode()).hexdigest()
        return my_hash > peer_hash

    # ------------------------------------------------------------------
    # Pairing / UI
    # ------------------------------------------------------------------

    def _on_pairing_request(self, request):
        print(f"\n{'='*60}")
        print(f"🔗 新设备请求配对:")
        print(f"   设备名称: {request.device_name}")
        print(f"   平台: {request.platform}")
        print(f"   IP地址: {request.ip_address}")
        print(f"   设备ID: {request.device_id}")
        print(f"{'='*60}")
        print("请在控制面板中确认是否允许此设备连接")

        if callable(self.ui_attention_callback):
            self.ui_attention_callback(request.device_name, request.platform)
            return

    def _on_file_transfer_complete(self, filename: str, peer_id: str | None, direction: str):
        if callable(self.ui_transfer_notify_callback):
            self.ui_transfer_notify_callback(filename, peer_id, direction)

    def report_ui_error(self, message: str):
        self.last_ui_error = message

    def accept_pairing_request(self, device_id: str) -> bool:
        return self.pairing_mgr.accept_pairing(device_id)

    def reject_pairing_request(self, device_id: str) -> bool:
        return self.pairing_mgr.reject_pairing(device_id)

    def _status_text(self) -> str:
        if not self.running:
            return "已停止"
        if self.peer_connections:
            return f"已连接 {len(self.peer_connections)} 台设备"
        if self.discovered_peers:
            return "已发现设备，等待连接"
        return "正在后台监听"

    def get_ui_snapshot(self):
        now = time.time()
        connected_peers = []
        for peer_id in sorted(self.peer_connections):
            peer_info = self.discovered_peers.get(peer_id, {})
            connected_peers.append({
                "peer_id": peer_id,
                "platform": peer_info.get("platform", "unknown"),
                "url": peer_info.get("url"),
                "will_initiate": self._should_initiate_connection(peer_id),
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
                "will_initiate": self._should_initiate_connection(peer_id),
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
            "status_text": self._status_text(),
            "connected_peers": connected_peers,
            "discovered_peers": discovered_peers,
            "pending_pairings": pending_pairings,
            "active_transfers": self.file_handler.get_active_transfers(),
            "last_error": self.last_ui_error,
            "thread_alive": True,  # ServiceHost will override this
        }

    # ------------------------------------------------------------------
    # Clipboard echo guard
    # ------------------------------------------------------------------

    def _build_clipboard_snapshot(self):
        return self._read_clipboard_snapshot()

    def _consume_expected_clipboard_echo(self, change_count) -> bool:
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

    def _register_applied_remote_event(self, message: dict, kind: str, fingerprint: str | None, change_count=None):
        self.loop_guard.register(
            kind=kind,
            fingerprint=fingerprint,
            expected_change_count=change_count,
            event_id=message.get("event_id"),
            origin_device_id=message.get("origin_device_id"),
        )

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    def _track_background_task(self, task: asyncio.Task):
        self.background_tasks.add(task)

        def _cleanup(done_task: asyncio.Task):
            self.background_tasks.discard(done_task)
            if done_task.cancelled():
                return
            try:
                exc = done_task.exception()
            except Exception as error:
                print(f"❌ 后台任务状态读取失败: {error}")
                return
            if exc:
                print(f"❌ 后台任务执行失败: {exc}")

        task.add_done_callback(_cleanup)
        return task

    def _schedule_background_transfer(self, transfer_coro, label: str):
        async def _runner():
            try:
                await transfer_coro
            except asyncio.CancelledError:
                print(f"⏹️ 后台文件传输已取消: {label}")
                raise
            except Exception as e:
                print(f"❌ 后台文件传输失败 ({label}): {e}")
                traceback.print_exc()

        return self._track_background_task(asyncio.create_task(_runner()))

    # ------------------------------------------------------------------
    # Networking
    # ------------------------------------------------------------------

    async def _send_encrypted(self, data: bytes, websocket):
        try:
            security_mgr = self._get_connection_security(websocket)
            if not security_mgr or not security_mgr.has_shared_key():
                raise ValueError("Connection shared key not established")
            encrypted = security_mgr.encrypt_message(data)
            async with self._get_send_lock(websocket):
                await websocket.send(encrypted)
        except websockets.exceptions.ConnectionClosed:
            print("❌ 发送数据失败：连接已关闭")
            raise
        except Exception as e:
            print(f"❌ 发送加密数据失败: {e}")
            traceback.print_exc()
            raise

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
            tasks.append(asyncio.create_task(self._send_encrypted(data_to_encrypt, websocket)))

        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=10.0)
            for task in pending:
                task.cancel()
            for task in done:
                if task.exception():
                    print(f"❌ 广播发送时出错: {task.exception()}")

    async def perform_key_exchange_as_server(self, websocket):
        security_mgr = self._get_connection_security(websocket, create=True)

        async def send_to_websocket(data):
            await websocket.send(data)

        async def receive_from_websocket():
            return await websocket.recv()

        return await security_mgr.perform_key_exchange(
            send_to_websocket,
            receive_from_websocket
        )

    async def perform_key_exchange_as_client(self, websocket):
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

    async def authenticate(self, websocket):
        """作为客户端向服务端进行身份验证"""
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

            auth_response_raw = await asyncio.wait_for(websocket.recv(), timeout=30.0)

            if isinstance(auth_response_raw, bytes):
                auth_response = auth_response_raw.decode('utf-8')
            else:
                auth_response = auth_response_raw

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

    async def _authenticate_incoming(self, websocket):
        """验证入站连接，返回 (device_id, message_data) 或 (None, None)"""
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

    # ------------------------------------------------------------------
    # Server and connection loop
    # ------------------------------------------------------------------

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
                print(f"🌐 {self.platform_name} 对等节点监听在 {ClipboardConfig.HOST}:{port}")
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

    async def sync_clipboard(self):
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
                    print(f"🔌 正在连接到设备 {candidate_peer_id}: {candidate_url}")
                    try:
                        await self.connect_and_sync(candidate_peer_id, candidate_url)
                    except Exception as e:
                        if self._is_expected_peer_unavailable_error(e):
                            print(f"ℹ️ 设备 {candidate_peer_id} 当前不可用，等待重连... ({e})")
                        else:
                            print(f"❌ 与设备 {candidate_peer_id} 建立连接失败: {e}")
                            traceback.print_exc()
                        await asyncio.sleep(2)
                else:
                    self._update_connection_status()
                    await self.discovery_event.wait()

            except asyncio.CancelledError:
                print("🛑 同步任务被取消")
                break
            except Exception as e:
                print(f"❌ 主同步循环出错: {e}")
                traceback.print_exc()
                await asyncio.sleep(5)

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
                print("❌ 身份验证失败，断开连接")
                return

            if not await self.perform_key_exchange_as_client(websocket):
                self._mark_peer_failure(remote_peer_id, "密钥交换失败")
                print("❌ 密钥交换失败，断开连接")
                return

            if not self._register_peer(remote_peer_id, websocket):
                self._reset_peer_retry(remote_peer_id)
                print(f"⚠️ 已存在与 {remote_peer_id} 的连接，关闭重复出站连接")
                return

            self._reset_peer_retry(peer_id)
            self._reset_peer_retry(remote_peer_id)
            self.last_content_hash = None
            print(f"✅ 已连接到设备 {remote_peer_id}，开始同步剪贴板")
            await self.send_current_clipboard_to_peer(websocket)
            try:
                await self._receive_loop(websocket)
            finally:
                removed_peer = self._unregister_peer(websocket)
                if removed_peer:
                    print(f"➖ 设备 {removed_peer} 已断开")

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
            await self._receive_loop(websocket)

        except websockets.exceptions.ConnectionClosed as e:
            print(f"📴 设备 {peer_id or 'unknown'} 连接已关闭: {e}")
        except Exception as e:
            print(f"❌ 处理入站连接时出错: {e}")
            traceback.print_exc()
        finally:
            removed_peer = self._unregister_peer(websocket)
            if removed_peer:
                print(f"➖ 设备 {removed_peer} 已断开")

    async def _receive_loop(self, websocket):
        """Receive and dispatch messages from a connected peer."""
        while self.running:
            try:
                received_data = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                await self.process_received_data(received_data, sender_websocket=websocket)
            except asyncio.TimeoutError:
                try:
                    pong = await websocket.ping()
                    await asyncio.wait_for(pong, timeout=5)
                except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                    print("⌛ 与对端的连接超时或关闭，断开")
                    break
                continue
            except websockets.exceptions.ConnectionClosedOK:
                break
            except websockets.exceptions.ConnectionClosedError as e:
                print(f"🔌 接收循环检测到连接异常关闭: {e}")
                break
            except asyncio.CancelledError:
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
                self.is_receiving = False

    async def process_received_data(self, encrypted_data, sender_websocket=None):
        """处理从对等设备接收到的加密数据"""
        if not sender_websocket:
            print("⚠️ process_received_data called without sender_websocket")
            return

        try:
            self.is_receiving = True
            security_mgr = self._get_connection_security(sender_websocket)
            if not security_mgr or not security_mgr.has_shared_key():
                raise ValueError("Connection shared key not established")
            decrypted_data = security_mgr.decrypt_message(encrypted_data)

            # Binary-framed chunk: b'BCHK' + 4-byte header-len + JSON header + raw bytes
            if decrypted_data[:4] == b'BCHK':
                header_len = int.from_bytes(decrypted_data[4:8], "big")
                header_bytes = decrypted_data[8:8 + header_len]
                binary_chunk = decrypted_data[8 + header_len:]
                message = json.loads(header_bytes.decode("utf-8"))
                message["_binary_data"] = binary_chunk
            else:
                message = ClipMessage.deserialize(decrypted_data.decode("utf-8"))

            if not message or "type" not in message:
                print("⚠️ 收到的消息格式无效或无法解析")
                return

            msg_type = message["type"]
            origin_device_id = message.get("origin_device_id")
            if origin_device_id and origin_device_id == self.device_id:
                return
            print(f"📬 收到消息类型: {msg_type}")

            if msg_type == MessageType.TEXT:
                text = message.get("content", "")
                if not text:
                    return
                if self.file_handler._looks_like_temp_file_path(text):
                    return
                content_hash = hashlib.md5(text.encode()).hexdigest()
                if content_hash == self.last_content_hash:
                    print("⏭️ 跳过重复内容")
                    return
                change_count = await self._apply_text_to_clipboard(text)
                if change_count is not None:
                    self.last_change_count = change_count
                    self.last_content_hash = content_hash
                    self._register_applied_remote_event(message, "text", content_hash, change_count)
                    display_text = text[:ClipboardConfig.MAX_DISPLAY_LENGTH] + ("..." if len(text) > ClipboardConfig.MAX_DISPLAY_LENGTH else "")
                    print(f"📥 已复制文本: \"{display_text}\"")

            elif msg_type == MessageType.FILE:
                await self.file_handler.handle_received_files(
                    message,
                    lambda data: self._send_encrypted(data, sender_websocket),
                    sender_websocket=sender_websocket,
                    current_content_hash=self.last_content_hash
                )

            elif msg_type == MessageType.FILE_START:
                await self.file_handler.handle_transfer_start(message)

            elif msg_type == MessageType.FILE_RESPONSE:
                is_complete, completed_path = await self.file_handler.handle_received_chunk(
                    message,
                    lambda data: self._send_encrypted(data, sender_websocket)
                )
                if is_complete and completed_path:
                    await self._apply_received_file_to_clipboard(message, completed_path)

            elif msg_type == MessageType.FILE_CHUNK:
                is_complete, completed_path = await self.file_handler.handle_received_chunk(
                    message,
                    lambda data: self._send_encrypted(data, sender_websocket)
                )
                if is_complete and completed_path:
                    await self._apply_received_file_to_clipboard(message, completed_path)

            elif msg_type == MessageType.FILE_REQUEST:
                file_path_requested = message.get("path")
                resume_from_chunk = int(message.get("resume_from_chunk") or 0)
                transfer_id = message.get("transfer_id")
                event_id = message.get("event_id")
                request_origin = message.get("origin_device_id")
                if file_path_requested:
                    normalized_path = file_path_requested.replace('\\', '/')
                    print(f"📤 收到文件请求: {Path(normalized_path).name}")
                    print(f"🔍 原始路径: {file_path_requested}")
                    print(f"🔍 标准化路径: {normalized_path}")
                    self._schedule_background_transfer(
                        self.file_handler.handle_file_transfer(
                            normalized_path,
                            lambda data: self._send_encrypted(data, sender_websocket),
                            start_chunk=resume_from_chunk,
                            transfer_id=transfer_id,
                            origin_device_id=request_origin,
                            event_id=event_id
                        ),
                        Path(normalized_path).name
                    )
                else:
                    print("⚠️ 收到的文件请求缺少路径")

            else:
                print(f"⚠️ 未知消息类型: {msg_type}")

        except json.JSONDecodeError:
            print("❌ 收到的消息不是有效的JSON")
        except UnicodeDecodeError:
            print("❌ 无法将收到的消息解码为UTF-8")
        except Exception as e:
            print(f"❌ 处理接收数据时出错: {e}")
            traceback.print_exc()
        finally:
            self.is_receiving = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self):
        if not self.running:
            return
        print("\n⏹️ 正在停止节点...")
        self.running = False
        # Run discovery.close() in a daemon thread — zeroconf.close() can block
        # waiting for its own threads, and we don't want to stall the caller
        threading.Thread(target=self.discovery.close, daemon=True, name="discovery-close").start()

        if self.event_loop and self.event_loop.is_running():
            for task_attr in ['server_task', 'clipboard_task', 'sync_task', 'status_task']:
                task = getattr(self, task_attr, None)
                if task and not task.done():
                    self.event_loop.call_soon_threadsafe(task.cancel)
            for task in list(self.background_tasks):
                if not task.done():
                    self.event_loop.call_soon_threadsafe(task.cancel)

            if hasattr(self, '_stop_server_func'):
                self.event_loop.call_soon_threadsafe(self._stop_server_func)
        else:
            if hasattr(self, '_stop_server_func'):
                self._stop_server_func()

        print("👋 感谢使用 UniPaste!")

    async def finalize_shutdown(self):
        if self.background_tasks:
            await asyncio.gather(*list(self.background_tasks), return_exceptions=True)

        if hasattr(self, 'file_handler'):
            self.file_handler.save_file_cache()
            self.file_handler.cleanup()
