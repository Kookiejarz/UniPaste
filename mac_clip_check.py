import AppKit
import asyncio
import websockets
import json
import signal
import time
import os
import hmac

from utils.security.crypto import SecurityManager
from utils.security.auth import DeviceAuthManager
from utils.network.discovery import DeviceDiscovery
from utils.message_format import ClipMessage, MessageType
from pathlib import Path
import hashlib
from handlers.file_handler import FileHandler
from config import ClipboardConfig
from utils.security.pairing import PairingManager, PairingStatus
import threading

class ClipboardListener:
    """剪贴板监听和同步节点"""

    def __init__(self):
        """初始化剪贴板监听器"""
        self._init_basic_components()
        self._init_state_flags()
        self._init_file_handling()
        self.device_id = self._get_device_id()
        self.device_name = os.environ.get("HOSTNAME", "Mac设备")
        self.device_token = self._load_device_token()
        self.last_remote_content_hash = None
        self.last_remote_update_time = 0
        self.ignore_clipboard_until = 0 # Timestamp until which local clipboard changes are ignored
        self.pairing_mgr = PairingManager(timeout_seconds=60)
        self.pairing_mgr.set_pairing_callback(self._on_pairing_request)
        self.discovered_peers = {}
        self.peer_connections = {}
        self.websocket_peer_ids = {}
        self.connection_security = {}

    def _init_basic_components(self):
        """初始化基础组件"""
        try:
            self.pasteboard = AppKit.NSPasteboard.generalPasteboard()
            self.auth_mgr = DeviceAuthManager()
            self.discovery = DeviceDiscovery()
            self.connected_clients = set()
            print("✅ 基础组件初始化成功")
        except Exception as e:
            print(f"❌ 基础组件初始化失败: {e}")
            raise

    def _init_state_flags(self):
        """初始化状态标志"""
        self.last_change_count = self.pasteboard.changeCount()
        self.last_content_hash = None # Hash of the last content *sent* or *set* by this instance
        self.is_receiving = False # Flag to prevent processing while receiving
        self.last_update_time = 0 # Timestamp of the last clipboard update *initiated by this instance*
        self.running = True
        self.server = None

    def _init_file_handling(self):
        """初始化文件处理相关"""
        try:
            self.temp_dir = ClipboardConfig.get_temp_dir()
            self.file_handler = FileHandler(self.temp_dir)
            self.file_handler.load_file_cache()
        except Exception as e:
            print(f"❌ 文件处理初始化失败: {e}")
            raise

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
            return f"mac-{int(time.time())}"

    def _get_token_path(self):
        home_dir = Path.home()
        token_dir = home_dir / ".clipshare"
        token_dir.mkdir(parents=True, exist_ok=True)
        return token_dir / "device_token_mac.txt"

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

    def _should_initiate_connection(self, peer_id: str) -> bool:
        return bool(peer_id) and peer_id != self.device_id and self.device_id > peer_id

    def on_service_found(self, service_info):
        if isinstance(service_info, str):
            service_info = {"url": service_info, "properties": {}}

        url = service_info.get("url")
        properties = service_info.get("properties", {})
        peer_id = properties.get("device_id")
        platform = properties.get("platform", "unknown")
        if not url or not peer_id or peer_id == self.device_id:
            return

        self.discovered_peers[peer_id] = {
            "url": url,
            "platform": platform,
        }
        print(f"✅ 发现设备 {peer_id} ({platform}): {url}")

    def _register_peer(self, peer_id, websocket):
        existing = self.peer_connections.get(peer_id)
        if existing and existing != websocket:
            return False
        self.peer_connections[peer_id] = websocket
        self.websocket_peer_ids[websocket] = peer_id
        self.connected_clients.add(websocket)
        return True

    def _unregister_peer(self, websocket):
        peer_id = self.websocket_peer_ids.pop(websocket, None)
        if peer_id and self.peer_connections.get(peer_id) == websocket:
            del self.peer_connections[peer_id]
        self.connected_clients.discard(websocket)
        self.connection_security.pop(websocket, None)
        return peer_id

    def _get_connection_security(self, websocket, create=False):
        manager = self.connection_security.get(websocket)
        if manager is None and create:
            manager = SecurityManager()
            manager.generate_key_pair()
            self.connection_security[websocket] = manager
        return manager

    def _on_pairing_request(self, request):
        """Handle pairing request - show notification to user"""
        print(f"\n{'='*60}")
        print(f"🔗 新设备请求配对:")
        print(f"   设备名称: {request.device_name}")
        print(f"   平台: {request.platform}")
        print(f"   IP地址: {request.ip_address}")
        print(f"   设备ID: {request.device_id}")
        print(f"{'='*60}")
        print(f"是否允许此设备连接? (输入 'y' 接受, 'n' 拒绝)")
        
        # Start input thread to not block async operations
        def get_user_input():
            try:
                choice = input().strip().lower()
                if choice in ['y', 'yes', 'accept', '是', '接受']:
                    self.pairing_mgr.accept_pairing(request.device_id)
                else:
                    self.pairing_mgr.reject_pairing(request.device_id)
            except:
                self.pairing_mgr.reject_pairing(request.device_id)
                
        threading.Thread(target=get_user_input, daemon=True).start()

    async def handle_client(self, websocket):
        """处理入站 WebSocket 连接"""
        device_id = None
        client_ip = websocket.remote_address[0] if websocket.remote_address else "未知IP"
        try:
            auth_message = await websocket.recv()
            try:
                if isinstance(auth_message, str):
                    message_data = json.loads(auth_message)
                else:
                    message_data = json.loads(auth_message.decode('utf-8'))

                device_id = message_data.get('identity', f'unknown-{client_ip}')
                signature = message_data.get('signature', '')
                is_first_time = message_data.get('first_time', False)

                print(f"📱 设备 {device_id} ({client_ip}) 尝试连接")

                if is_first_time:
                    print(f"🆕 设备 {device_id} 首次连接，需要配对...")
                    pairing_request = await self.pairing_mgr.request_pairing(
                        device_id, message_data, client_ip
                    )
                    pairing_result = await self.pairing_mgr.wait_for_pairing_result(device_id)
                    
                    if pairing_result == PairingStatus.ACCEPTED:
                        token = self.auth_mgr.authorize_device(device_id, {
                            "name": message_data.get("device_name", "未命名设备"),
                            "platform": message_data.get("platform", "未知平台"),
                            "ip": client_ip
                        })
                        await websocket.send(json.dumps({
                            'status': 'pairing_accepted',
                            'peer_id': self.device_id,
                            'token': token
                        }))
                        print(f"✅ 设备 {device_id} 配对成功并已授权")
                    elif pairing_result == PairingStatus.REJECTED:
                        await websocket.send(json.dumps({
                            'status': 'pairing_rejected',
                            'reason': 'User rejected pairing request',
                            'peer_id': self.device_id
                        }))
                        print(f"❌ 设备 {device_id} 配对被拒绝")
                        return
                    else:  # EXPIRED
                        await websocket.send(json.dumps({
                            'status': 'pairing_expired',
                            'reason': 'Pairing request timed out',
                            'peer_id': self.device_id
                        }))
                        print(f"⏰ 设备 {device_id} 配对请求超时")
                        return
                else:
                    # Existing device authentication
                    print(f"🔐 验证设备 {device_id} 的签名")
                    is_valid = self.auth_mgr.validate_device(device_id, signature)
                    if not is_valid:
                        print(f"❌ 设备 {device_id} 验证失败")
                        await websocket.send(json.dumps({
                            'status': 'unauthorized',
                            'reason': 'Invalid signature or unknown device',
                            'peer_id': self.device_id
                        }))
                        return # Close connection
                    await websocket.send(json.dumps({
                        'status': 'authorized',
                        'peer_id': self.device_id
                    }))
                    print(f"✅ 设备 {device_id} 验证成功")

            except json.JSONDecodeError:
                print(f"❌ 来自 {client_ip} 的无效消息格式")
                await websocket.send(json.dumps({
                    'status': 'error',
                    'reason': 'Invalid message format'
                }))
                return
            except Exception as auth_err:
                print(f"❌ 处理消息错误 for {device_id or client_ip}: {auth_err}")
                await websocket.send(json.dumps({
                    'status': 'error',
                    'reason': f'Message processing failed: {auth_err}'
                }))
                return

            if not await self.perform_key_exchange(websocket):
                print(f"❌ 与 {device_id} 的密钥交换失败，断开连接")
                return

            if not self._register_peer(device_id, websocket):
                print(f"⚠️ 已存在与 {device_id} 的连接，关闭重复入站连接")
                await websocket.close()
                return
            print(f"✅ 设备 {device_id} 已连接并完成密钥交换")
            await self.send_current_clipboard_to_peer(websocket)

            while self.running:
                try:
                    encrypted_data = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                    await self.process_received_data(encrypted_data, sender_websocket=websocket)
                except asyncio.TimeoutError:
                    try:
                        pong_waiter = await websocket.ping()
                        await asyncio.wait_for(pong_waiter, timeout=5)
                    except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                        print(f"⌛ 与 {device_id} 的连接超时或关闭，断开")
                        break
                    continue
                except asyncio.CancelledError:
                    print(f"⏹️ {device_id} 的连接处理已取消")
                    break
                except websockets.exceptions.ConnectionClosedOK:
                     print(f"ℹ️ 设备 {device_id} 正常断开连接")
                     break
                except websockets.exceptions.ConnectionClosedError as e:
                     print(f"🔌 设备 {device_id} 异常断开连接: {e}")
                     break
                except Exception as e:
                    print(f"❌ 处理来自 {device_id} 的数据时出错: {e}")
                    import traceback
                    traceback.print_exc()
                    await asyncio.sleep(1)

        except websockets.exceptions.ConnectionClosed as e:
            # This might catch cases where connection closes before loop starts
            print(f"📴 设备 {device_id or client_ip} 连接已关闭: {e}")
        except Exception as e:
            print(f"❌ 处理入站连接 {device_id or client_ip} 时发生意外错误: {e}")
            import traceback
            traceback.print_exc()
        finally:
            removed_peer = self._unregister_peer(websocket)
            print(f"➖ 设备 {removed_peer or device_id or client_ip} 已断开")

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
                        event_id=ClipMessage.new_event_id()
                    )
                    print("📤 已向新连接设备发送当前文件剪贴板快照")
                    return

            if AppKit.NSPasteboardTypeString in types:
                text = self.pasteboard.stringForType_(AppKit.NSPasteboardTypeString)
                if text:
                    await self.file_handler.process_clipboard_content(
                        text,
                        time.time(),
                        None,
                        0,
                        send_direct,
                        origin_device_id=self.device_id,
                        event_id=ClipMessage.new_event_id()
                    )
                    print("📤 已向新连接设备发送当前文本剪贴板快照")

        except Exception as e:
            print(f"⚠️ 发送当前剪贴板快照失败: {e}")


    async def _send_encrypted(self, data: bytes, websocket):
        """Helper to encrypt and send data to a specific websocket."""
        try:
            security_mgr = self._get_connection_security(websocket)
            if not security_mgr or not security_mgr.has_shared_key():
                raise ValueError("Connection shared key not established")
            encrypted = security_mgr.encrypt_message(data)
            await websocket.send(encrypted)
        except Exception as e:
            print(f"❌ 发送加密数据失败: {e}")
            if websocket in self.connected_clients:
                self.connected_clients.remove(websocket)


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
            message_json = decrypted_data.decode('utf-8')
            message = ClipMessage.deserialize(message_json)

            if not message or "type" not in message:
                 print("⚠️ 收到的消息格式无效或无法解析")
                 return

            msg_type = message["type"]
            origin_device_id = message.get("origin_device_id")
            if origin_device_id and origin_device_id == self.device_id:
                return
            print(f"📬 收到消息类型: {msg_type}") # Log received type

            if msg_type == MessageType.TEXT:
                text = message.get("content", "")
                if not text:
                    print("⚠️ 收到空文本消息")
                    return

                if self.file_handler._looks_like_temp_file_path(text):
                    return

                content_hash = hashlib.md5(text.encode()).hexdigest()

                if content_hash == self.last_content_hash:
                     print("⏭️ 跳过重复内容 (与本地最后发送/设置一致)")
                     return

                self.pasteboard.clearContents()
                success = self.pasteboard.setString_forType_(text, AppKit.NSPasteboardTypeString)

                if success:
                    now = time.time()
                    self.last_change_count = self.pasteboard.changeCount()
                    self.last_content_hash = content_hash
                    self.last_update_time = now
                    self.ignore_clipboard_until = now + ClipboardConfig.UPDATE_DELAY
                    self.last_remote_content_hash = content_hash
                    self.last_remote_update_time = now

                    display_text = text[:ClipboardConfig.MAX_DISPLAY_LENGTH] + ("..." if len(text) > ClipboardConfig.MAX_DISPLAY_LENGTH else "")
                    print(f"📥 已复制文本: \"{display_text}\"")
                else:
                    print("❌ 更新Mac剪贴板失败")


            elif msg_type == MessageType.FILE:
                await self.file_handler.handle_received_files(
                    message,
                    lambda data: self._send_encrypted(data, sender_websocket),
                    sender_websocket=sender_websocket
                )

            elif msg_type == MessageType.FILE_START:
                await self.file_handler.handle_transfer_start(message)

            elif msg_type == MessageType.FILE_RESPONSE:
                # Handle incoming file chunk
                is_complete, completed_path = await self.file_handler.handle_received_chunk(
                    message,
                    lambda data: self._send_encrypted(data, sender_websocket)
                )
                if is_complete and completed_path:
                    print(f"✅ 文件接收完成: {completed_path}")

                    content_hash = self.file_handler.get_files_content_hash([str(completed_path)])

                    if content_hash and content_hash == self.last_content_hash:
                         print("⏭️ 跳过重复文件内容 (与本地最后发送/设置一致)")
                         return

                    file_to_set = completed_path
                    content_hash_to_use = content_hash
                    
                    await asyncio.sleep(0.1)
                    change_count = await self.file_handler.set_clipboard_file(file_to_set)
                    if change_count is not None:
                        self.last_change_count = change_count

                        self.last_content_hash = content_hash_to_use
                        self.last_update_time = time.time()

                        self.ignore_clipboard_until = time.time() + 10.0
                        self.last_remote_content_hash = content_hash_to_use
                        self.last_remote_update_time = time.time()

                        print("✅ 文件已设置到剪贴板并可用于粘贴")
                        print("🔄 文件已标记为已处理，防止重复广播")
                        print("⏳ 暂停监控10秒以确保文件可访问")
                        print("💡 在接下来10秒内，您可以自由粘贴文件而不受监控干扰")

                    else:
                         print(f"❌ 将文件 {completed_path.name} 设置到剪贴板失败")

            elif msg_type == MessageType.FILE_CHUNK:
                is_complete, completed_path = await self.file_handler.handle_received_chunk(
                    message,
                    lambda data: self._send_encrypted(data, sender_websocket)
                )
                if is_complete and completed_path:
                    print(f"✅ 文件接收完成: {completed_path}")

                    content_hash = self.file_handler.get_files_content_hash([str(completed_path)])

                    if content_hash and content_hash == self.last_content_hash:
                         print("⏭️ 跳过重复文件内容 (与本地最后发送/设置一致)")
                         return

                    file_to_set = completed_path
                    content_hash_to_use = content_hash
                    
                    await asyncio.sleep(0.1)
                    change_count = await self.file_handler.set_clipboard_file(file_to_set)
                    if change_count is not None:
                        self.last_change_count = change_count

                        self.last_content_hash = content_hash_to_use
                        self.last_update_time = time.time()

                        self.ignore_clipboard_until = time.time() + 10.0
                        self.last_remote_content_hash = content_hash_to_use
                        self.last_remote_update_time = time.time()

                        print("✅ 文件已设置到剪贴板并可用于粘贴")
                        print("🔄 文件已标记为已处理，防止重复广播")
                        print("⏳ 暂停监控10秒以确保文件可访问")
                        print("💡 在接下来10秒内，您可以自由粘贴文件而不受监控干扰")

                    else:
                         print(f"❌ 将文件 {completed_path.name} 设置到剪贴板失败")

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
                      await self.file_handler.handle_file_transfer(
                           normalized_path,
                           lambda data: self._send_encrypted(data, sender_websocket),
                           start_chunk=resume_from_chunk,
                           transfer_id=transfer_id,
                           origin_device_id=request_origin,
                           event_id=event_id
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
            import traceback
            traceback.print_exc()
        finally:
            self.is_receiving = False


    async def broadcast_encrypted_data(self, data_to_encrypt: bytes, exclude_websocket=None):
        """Encrypt and broadcast data to all active peers."""
        if not self.connected_clients:
            return

        active_clients = list(self.connected_clients)
        broadcast_count = len(active_clients) - (1 if exclude_websocket in active_clients else 0)

        if broadcast_count <= 0:
            return

        tasks = []
        for websocket in active_clients:
            if websocket == exclude_websocket:
                continue
            try:
                security_mgr = self._get_connection_security(websocket)
                if not security_mgr or not security_mgr.has_shared_key():
                    continue
                encrypted_data = security_mgr.encrypt_message(data_to_encrypt)
                tasks.append(asyncio.create_task(websocket.send(encrypted_data)))
            except Exception as e:
                print(f"❌ 创建广播任务失败: {e}")
                if websocket in self.connected_clients:
                    self.connected_clients.remove(websocket)

        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=10.0)

            if pending:
                print(f"⚠️ {len(pending)} 个广播任务超时")
                for task in pending:
                    task.cancel()
            for task in done:
                 if task.exception():
                      print(f"❌ 广播发送时出错: {task.exception()}")

    async def sync_clipboard(self):
        print("🔍 搜索剪贴板服务...")
        self.discovery.start_discovery(self.on_service_found)

        while self.running:
            try:
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
                        await self.connect_and_sync(candidate_url)
                    except Exception as e:
                        print(f"❌ 与设备 {candidate_peer_id} 建立连接失败: {e}")
                        await asyncio.sleep(2)
                else:
                    await asyncio.sleep(ClipboardConfig.CLIPBOARD_CHECK_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"❌ 主同步循环出错: {e}")
                await asyncio.sleep(2)

    async def connect_and_sync(self, ws_url):
        async with websockets.connect(
            ws_url,
            subprotocols=["binary"],
            max_size=10 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20
        ) as websocket:
            peer_id = await self.authenticate(websocket)
            if not peer_id:
                return

            if not await self.perform_key_exchange_as_client(websocket):
                return

            if not self._register_peer(peer_id, websocket):
                print(f"⚠️ 已存在与 {peer_id} 的连接，关闭重复出站连接")
                return

            print(f"✅ 已连接到设备 {peer_id}，开始同步剪贴板")
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
                "platform": "macos"
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
                    ping_interval=20,
                    ping_timeout=20
                )
                await self.discovery.start_advertising(
                    port,
                    device_id=self.device_id,
                    platform="macos"
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
                
                if self.connected_clients:
                    print(f"📤 正在关闭 {len(self.connected_clients)} 个连接...")
                    close_tasks = []
                    for websocket in list(self.connected_clients):
                        close_tasks.append(websocket.close())
                    if close_tasks:
                        await asyncio.gather(*close_tasks, return_exceptions=True)
                    self.connected_clients.clear()
                
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

                if self.is_receiving:
                    await asyncio.sleep(0.1)
                    continue

                if current_time < self.ignore_clipboard_until:
                    await asyncio.sleep(0.1)
                    continue

                time_since_process = current_time - last_processed_time
                if time_since_process < ClipboardConfig.MIN_PROCESS_INTERVAL:
                    await asyncio.sleep(0.1)
                    continue

                new_change_count = self.pasteboard.changeCount()
                if new_change_count != self.last_change_count:
                    print(f"📋 剪贴板变化 detected (Count: {self.last_change_count} -> {new_change_count})")
                    types = self.pasteboard.types()
                    print(f"🔍 剪贴板类型: {list(types)}")
                    
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
                import traceback
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

                if file_urls and self.connected_clients:
                    new_hash, update_sent = await self.file_handler.handle_clipboard_files(
                        file_urls,
                        self.last_content_hash,
                        self.broadcast_encrypted_data,
                        origin_device_id=self.device_id,
                        event_id=ClipMessage.new_event_id()
                    )
                    if update_sent:
                        self.last_content_hash = new_hash
                        self.last_update_time = time.time()
                        sent_update = True
                        print("📤 文件信息已发送，等待对端请求文件内容...")
                    return sent_update

            if AppKit.NSPasteboardTypeString in types:
                text = self.pasteboard.stringForType_(AppKit.NSPasteboardTypeString)
                if text and self.connected_clients:
                    content_hash = hashlib.md5(text.encode()).hexdigest()
                    if (self.last_remote_content_hash == content_hash and
                        time.time() - self.last_remote_update_time < ClipboardConfig.UPDATE_DELAY * 2):
                        return False

                    current_time = time.time()
                    new_hash, new_time, update_sent = await self.file_handler.process_clipboard_content(
                        text,
                        current_time,
                        self.last_content_hash,
                        self.last_update_time,
                        self.broadcast_encrypted_data,
                        origin_device_id=self.device_id,
                        event_id=ClipMessage.new_event_id()
                    )
                    if update_sent:
                        self.last_content_hash = new_hash
                        self.last_update_time = new_time
                        sent_update = True
                    return sent_update

            if AppKit.NSPasteboardTypePNG in types:
                print("⚠️ 图片同步暂不支持")

        except Exception as e:
            print(f"❌ 处理剪贴板内容时出错: {e}")
            import traceback
            traceback.print_exc()

        return sent_update # Return whether an update was sent


    async def perform_key_exchange(self, websocket):
        """Perform key exchange with an inbound peer"""
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

            security_mgr.generate_shared_key(peer_public_key)
            confirmation = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            confirm_data = json.loads(confirmation)
            return confirm_data.get("type") == "key_exchange_complete" and confirm_data.get("status") == "success"
        except Exception as e:
            print(f"❌ 密钥交换失败: {e}")
            return False

    def stop(self):
        """Signals the node and related tasks to stop."""
        if not self.running:
             return
        print("\n⏹️ 正在请求停止节点...")
        self.running = False

        if hasattr(self, '_stop_server_func'):
            self._stop_server_func()

        if hasattr(self, 'clipboard_task') and self.clipboard_task and not self.clipboard_task.done():
             self.clipboard_task.cancel()

        self.discovery.close()

        if hasattr(self, 'file_handler'):
             self.file_handler.save_file_cache()

        print("👋 感谢使用 UniPaste 节点!")


async def main():
    AppKit.NSApplication.sharedApplication()

    listener = ClipboardListener()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        print("\n⚠️ 接收到关闭信号...")
        if not stop_event.is_set():
             listener.stop()
             stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
             print(f"ℹ️ 信号 {sig} 处理在当前系统可能不受支持。请使用 Ctrl+C。")


    try:
        print("🚀 UniPaste Mac 节点已启动")
        print(f"📂 临时文件目录: {listener.temp_dir}")
        print("📋 按 Ctrl+C 退出程序")

        server_task = asyncio.create_task(listener.start_server())
        sync_task = asyncio.create_task(listener.sync_clipboard())
        listener.clipboard_task = asyncio.create_task(listener.check_clipboard())

        await asyncio.gather(server_task, sync_task, listener.clipboard_task)

    except asyncio.CancelledError:
        print("\n⏹️ 主任务已取消")
    except Exception as e:
        print(f"\n❌ 发生未处理的错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if listener.running:
             listener.stop()
        await asyncio.sleep(0.5)
        print("🚪 程序退出")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
         print("\n⌨️ 检测到 Ctrl+C，强制退出...")
