from pathlib import Path
import hashlib
import json
import base64
import asyncio
import os
import time
import shutil
import zipfile

from utils.platform_config import IS_MACOS, IS_WINDOWS
from utils.message_format import ClipMessage, MessageType
from config import ClipboardConfig

# Only import AppKit and objc on macOS
if IS_MACOS:
    import AppKit
    import objc

    class PasteboardSetter(AppKit.NSObject):
        @classmethod
        def setFileURL_(cls, path_str):
            try:
                pasteboard = AppKit.NSPasteboard.generalPasteboard()
                pasteboard.clearContents()
                url = AppKit.NSURL.fileURLWithPath_(path_str)
                if not url:
                    print(f"❌ [MainThread] 无法创建文件URL: {path_str}")
                    return "0|-1"
                urls = AppKit.NSArray.arrayWithObject_(url)
                success = pasteboard.writeObjects_(urls)
                if success:
                    change_count = pasteboard.changeCount()
                    print(f"📎 [MainThread] 已将文件添加到Mac剪贴板: {Path(path_str).name}")
                    return f"1|{change_count}"

                print(f"❌ [MainThread] 添加文件到Mac剪贴板失败: {Path(path_str).name}")
                return "0|-1"
            except Exception as e:
                print(f"❌ [MainThread] 设置剪贴板文件时出错: {e}")
                import traceback
                traceback.print_exc()
                return "0|-1"


class FileHandler:
    """文件处理管理器"""

    def __init__(self, temp_dir: Path):
        self.temp_dir = temp_dir
        self.file_cache = {}
        self.shared_item_metadata = {}
        self._init_temp_dir()
        self.load_file_cache()
        self.chunk_size = ClipboardConfig.CHUNK_SIZE
        self.pending_transfers = {}
        self.active_outbound = {}  # transfer_id -> {filename, file_size, sent_chunks, total_chunks}
        self.on_transfer_complete = None  # callback(filename, peer_id, direction)

    def _init_temp_dir(self):
        """初始化临时目录"""
        self.temp_dir.mkdir(exist_ok=True)
        print(f"✅ 文件处理初始化成功，临时目录: {self.temp_dir}")

    def _looks_like_temp_file_path(self, text: str) -> bool:
        """检查文本是否看起来像临时文件路径"""
        for indicator in ClipboardConfig.TEMP_PATH_INDICATORS:
            if indicator in text:
                print(f"⏭️ 跳过临时文件路径: \"{text[:40]}...\"")
                return True
        return False

    @staticmethod
    def build_transfer_id(filename: str, file_size: int, file_hash: str | None) -> str:
        return ClipMessage.calculate_transfer_id(filename, file_size, file_hash)

    def _sanitize_name(self, filename: str) -> str:
        safe = []
        for char in Path(filename).name or "unknown":
            if char.isalnum() or char in "._-":
                safe.append(char)
            else:
                safe.append("_")
        return "".join(safe) or "unknown"

    def _get_partial_path(self, filename: str, transfer_id: str) -> Path:
        safe_name = self._sanitize_name(filename)
        safe_id = "".join(ch for ch in (transfer_id or "") if ch.isalnum()) or "transfer"
        return self.temp_dir / f".{safe_name}.{safe_id}.part"

    def _normalize_partial_file(self, part_path: Path, file_size: int, chunk_size: int) -> tuple[int, int]:
        """根据块边界修正部分下载文件，返回 (resume_chunk, aligned_bytes)。"""
        if not part_path.exists():
            return 0, 0

        existing_size = part_path.stat().st_size
        bounded_size = min(existing_size, file_size)
        if file_size and bounded_size == file_size:
            total_chunks = (file_size + chunk_size - 1) // chunk_size if chunk_size else 0
            if existing_size != bounded_size:
                with open(part_path, "r+b") as f:
                    f.truncate(bounded_size)
            return total_chunks, bounded_size

        aligned_size = (bounded_size // chunk_size) * chunk_size if chunk_size else bounded_size

        if aligned_size != existing_size:
            with open(part_path, "r+b") as f:
                f.truncate(aligned_size)

        resume_chunk = aligned_size // chunk_size if chunk_size else 0
        return resume_chunk, aligned_size

    def _build_transfer_state(
        self,
        filename: str,
        remote_path: str,
        transfer_id: str,
        file_size: int,
        total_chunks: int,
        chunk_size: int,
        file_hash: str | None,
        content_fingerprint: str | None = None,
        origin_device_id: str | None = None,
        event_id: str | None = None
    ) -> dict:
        final_path = self.temp_dir / Path(filename).name
        part_path = self._get_partial_path(filename, transfer_id)
        part_path.parent.mkdir(exist_ok=True)

        resume_chunk, aligned_bytes = self._normalize_partial_file(
            part_path,
            file_size,
            chunk_size
        )
        next_chunk = min(total_chunks, resume_chunk)

        return {
            "filename": Path(filename).name,
            "remote_path": remote_path,
            "transfer_id": transfer_id,
            "file_size": file_size,
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
            "file_hash": file_hash,
            "content_fingerprint": content_fingerprint,
            "origin_device_id": origin_device_id,
            "event_id": event_id,
            "part_path": part_path,
            "final_path": final_path,
            "next_chunk": next_chunk,
            "received_bytes": aligned_bytes,
        }

    def _discard_transfer_state(self, transfer_id: str, remove_partial: bool = False):
        transfer = self.pending_transfers.pop(transfer_id, None)
        if remove_partial and transfer:
            try:
                transfer["part_path"].unlink(missing_ok=True)
            except OSError as e:
                print(f"⚠️ 删除临时分块文件失败: {e}")

    def _try_finalize_existing_partial(
        self,
        filename: str,
        transfer_id: str,
        file_size: int,
        file_hash: str | None
    ) -> str | None:
        part_path = self._get_partial_path(filename, transfer_id)
        final_path = self.temp_dir / Path(filename).name

        if not part_path.exists():
            return None

        if part_path.stat().st_size != file_size:
            return None

        if file_hash:
            actual_hash = ClipMessage.calculate_file_hash(str(part_path))
            if actual_hash != file_hash:
                print(f"⚠️ 检测到损坏的续传临时文件，已重置: {filename}")
                part_path.unlink(missing_ok=True)
                return None

        os.replace(part_path, final_path)
        self.add_to_file_cache(file_hash or ClipMessage.calculate_file_hash(str(final_path)), str(final_path))
        return str(final_path)

    async def _request_transfer_resume(self, transfer: dict, send_encrypted_fn, resume_from_chunk: int):
        remote_path = transfer.get("remote_path")
        if not remote_path:
            return

        request_message = ClipMessage.file_request_message(
            remote_path,
            transfer_id=transfer["transfer_id"],
            resume_from_chunk=resume_from_chunk,
            filename=transfer["filename"],
            origin_device_id=transfer.get("origin_device_id"),
            event_id=transfer.get("event_id")
        )
        print(f"🔁 请求续传 {transfer['filename']}，从第 {resume_from_chunk + 1} 块开始")
        await send_encrypted_fn(json.dumps(request_message).encode("utf-8"))

    async def handle_transfer_start(self, message: dict) -> bool:
        """处理文件传输开始消息，准备流式写盘状态。"""
        if message.get("exists") is False:
            print(f"⚠️ 对端文件不可用: {message.get('filename', 'unknown')}")
            return False

        filename = message.get("filename")
        remote_path = message.get("path")
        file_size = int(message.get("size") or 0)
        chunk_size = int(message.get("chunk_size") or self.chunk_size)
        total_chunks = int(message.get("total_chunks") or 0)
        file_hash = message.get("file_hash")
        content_fingerprint = message.get("content_fingerprint")
        origin_device_id = message.get("origin_device_id")
        event_id = message.get("event_id")
        transfer_id = message.get("transfer_id") or self.build_transfer_id(
            filename or "unknown",
            file_size,
            file_hash
        )

        if not filename or not remote_path or file_size < 0 or total_chunks < 0:
            print("⚠️ 收到的 FILE_START 元数据不完整")
            return False

        transfer = self._build_transfer_state(
            filename,
            remote_path,
            transfer_id,
            file_size,
            total_chunks,
            chunk_size,
            file_hash,
            content_fingerprint=content_fingerprint,
            origin_device_id=origin_device_id,
            event_id=event_id
        )
        self.pending_transfers[transfer_id] = transfer

        start_chunk = int(message.get("start_chunk") or 0)
        if transfer["next_chunk"] > 0:
            print(
                f"📦 准备续传文件: {filename} "
                f"({transfer['received_bytes']}/{file_size} 字节, 下一个块 {transfer['next_chunk'] + 1}/{max(total_chunks, 1)})"
            )
        else:
            print(f"📦 准备接收文件: {filename} ({file_size/1024/1024:.1f}MB, {total_chunks}块)")

        if start_chunk and start_chunk != transfer["next_chunk"]:
            print(
                f"ℹ️ 发送端计划从块 {start_chunk + 1} 开始，"
                f"本地期望从块 {transfer['next_chunk'] + 1} 开始"
            )

        return True

    async def handle_file_transfer(
        self,
        file_path: str,
        send_encrypted_fn,
        start_chunk: int = 0,
        transfer_id: str | None = None,
        origin_device_id: str | None = None,
        event_id: str | None = None
    ):
        """处理文件传输，基于请求块号进行流式发送。"""
        path_obj = Path(file_path)

        if not path_obj.exists() or not path_obj.is_file():
            print(f"⚠️ 文件不存在或无效: {file_path}")
            start_message = {
                "type": MessageType.FILE_START,
                "filename": path_obj.name,
                "path": str(path_obj),
                "exists": False,
                "transfer_id": transfer_id,
                "origin_device_id": origin_device_id,
                "event_id": event_id,
            }
            await send_encrypted_fn(json.dumps(start_message).encode("utf-8"))
            return False

        try:
            file_size = path_obj.stat().st_size
            total_chunks = (file_size + self.chunk_size - 1) // self.chunk_size if file_size else 0
            file_hash = ClipMessage.calculate_file_hash(str(path_obj))
            transfer_id = transfer_id or self.build_transfer_id(path_obj.name, file_size, file_hash)
            item_metadata = self.shared_item_metadata.get(str(path_obj), {})
            content_fingerprint = item_metadata.get("content_fingerprint")
            start_chunk = max(0, min(int(start_chunk or 0), total_chunks))
            start_offset = start_chunk * self.chunk_size

            start_message = {
                "type": MessageType.FILE_START,
                "filename": path_obj.name,
                "path": str(path_obj),
                "exists": True,
                "size": file_size,
                "chunk_size": self.chunk_size,
                "total_chunks": total_chunks,
                "file_hash": file_hash,
                "content_fingerprint": content_fingerprint,
                "transfer_id": transfer_id,
                "start_chunk": start_chunk,
                "origin_device_id": origin_device_id,
                "event_id": event_id,
                **item_metadata,
            }
            await send_encrypted_fn(json.dumps(start_message).encode("utf-8"))

            if total_chunks == 0:
                print(f"✅ 空文件传输完成: {path_obj.name}")
                return True

            print(
                f"📤 开始传输文件: {path_obj.name} "
                f"({file_size/1024/1024:.1f}MB, 从第 {start_chunk + 1} 块开始，共 {total_chunks} 块)"
            )

            self.active_outbound[transfer_id] = {
                "filename": path_obj.name,
                "file_size": file_size,
                "sent_chunks": start_chunk,
                "total_chunks": total_chunks,
            }
            try:
                with open(path_obj, "rb") as f:
                    f.seek(start_offset)
                    for chunk_index in range(start_chunk, total_chunks):
                        chunk_data = f.read(self.chunk_size)
                        if not chunk_data:
                            break

                        # Binary-framed chunk: BCHK + 4-byte header-len + JSON header + raw bytes
                        # Avoids base64 encoding (saves ~33% bandwidth + CPU)
                        chunk_header = {
                            "type": MessageType.FILE_CHUNK,
                            "filename": path_obj.name,
                            "transfer_id": transfer_id,
                            "chunk_index": chunk_index,
                            "total_chunks": total_chunks,
                            "chunk_size": self.chunk_size,
                            "size": file_size,
                            "file_hash": file_hash,
                            "content_fingerprint": content_fingerprint,
                            "chunk_hash": hashlib.md5(chunk_data).hexdigest(),
                            "origin_device_id": origin_device_id,
                            "event_id": event_id,
                            **item_metadata,
                        }
                        header_bytes = json.dumps(chunk_header).encode("utf-8")
                        payload = b'BCHK' + len(header_bytes).to_bytes(4, "big") + header_bytes + chunk_data

                        self.active_outbound[transfer_id]["sent_chunks"] = chunk_index + 1
                        progress = self._format_progress(chunk_index + 1, total_chunks)
                        print(f"\r📤 传输文件 {path_obj.name}: {progress}", end="", flush=True)

                        await send_encrypted_fn(payload)
                        # Yield to event loop periodically to avoid starving other coroutines
                        if (chunk_index - start_chunk + 1) % ClipboardConfig.CHUNK_YIELD_INTERVAL == 0:
                            await asyncio.sleep(0)
            finally:
                self.active_outbound.pop(transfer_id, None)

            print(f"\n✅ 文件 {path_obj.name} 传输完成")
            if callable(self.on_transfer_complete):
                self.on_transfer_complete(path_obj.name, origin_device_id, "send")
            return True

        except Exception as e:
            print(f"\n❌ 文件传输失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _format_progress(self, current: int, total: int) -> str:
        """格式化进度显示"""
        if total <= 0:
            return "[░░░░░░░░░░░░░░░░░░░░] 0% (0/0)"
        percentage = (current * 100) // total
        bar_length = 20
        filled = min(bar_length, (percentage * bar_length) // 100)
        bar = "█" * filled + "░" * (bar_length - filled)
        return f"[{bar}] {percentage}% ({current}/{total})"

    def get_active_transfers(self) -> list:
        """返回当前所有活跃传输的状态，供 UI snapshot 使用。"""
        result = []
        for tid, t in list(self.pending_transfers.items()):
            total = t.get("total_chunks", 0)
            done = t.get("next_chunk", 0)
            pct = int(done * 100 / total) if total > 0 else 0
            file_size = t.get("file_size", 0)
            result.append({
                "transfer_id": tid,
                "filename": t.get("filename", ""),
                "file_size": file_size,
                "received_bytes": t.get("received_bytes", 0),
                "percent": pct,
                "direction": "receive",
                "peer_id": t.get("origin_device_id"),
            })
        for tid, t in list(self.active_outbound.items()):
            total = t.get("total_chunks", 0)
            done = t.get("sent_chunks", 0)
            pct = int(done * 100 / total) if total > 0 else 0
            file_size = t.get("file_size", 0)
            sent_bytes = int(done * file_size / total) if total > 0 else 0
            result.append({
                "transfer_id": tid,
                "filename": t.get("filename", ""),
                "file_size": file_size,
                "received_bytes": sent_bytes,
                "percent": pct,
                "direction": "send",
                "peer_id": None,
            })
        return result

    async def handle_received_chunk(self, message: dict, send_encrypted_fn=None) -> tuple[bool, Path | None]:
        """
        处理接收到的文件块.
        Returns: (is_complete, file_path_if_complete)
        """
        try:
            filename = message.get("filename", "unknown")
            chunk_index = int(message.get("chunk_index") or 0)
            total_chunks = int(message.get("total_chunks") or 0)
            chunk_size = int(message.get("chunk_size") or self.chunk_size)
            file_size = int(message.get("size") or 0)
            file_hash = message.get("file_hash")
            content_fingerprint = message.get("content_fingerprint")
            origin_device_id = message.get("origin_device_id")
            event_id = message.get("event_id")
            transfer_id = message.get("transfer_id") or self.build_transfer_id(
                filename,
                file_size,
                file_hash
            )

            transfer = self.pending_transfers.get(transfer_id)
            if not transfer:
                transfer = self._build_transfer_state(
                    filename,
                    message.get("path", ""),
                    transfer_id,
                    file_size or total_chunks * chunk_size,
                    total_chunks,
                    chunk_size,
                    file_hash,
                    content_fingerprint=content_fingerprint,
                    origin_device_id=origin_device_id,
                    event_id=event_id
                )
                self.pending_transfers[transfer_id] = transfer

            # Support both binary-framed (preferred) and legacy base64 formats
            if "_binary_data" in message:
                chunk_data = message["_binary_data"]
            else:
                try:
                    chunk_data = base64.b64decode(message.get("chunk_data", ""))
                except Exception as decode_error:
                    print(f"❌ 文件块 Base64 解码失败: {decode_error}")
                    return False, None

            if not chunk_data and transfer["total_chunks"] > 0:
                print("⚠️ 收到的文件块数据为空")
                return False, None

            chunk_hash = message.get("chunk_hash")
            if chunk_hash and hashlib.md5(chunk_data).hexdigest() != chunk_hash:
                print(f"⚠️ 块 {chunk_index + 1}/{transfer['total_chunks']} 校验失败 for {filename}")
                if send_encrypted_fn:
                    await self._request_transfer_resume(transfer, send_encrypted_fn, transfer["next_chunk"])
                return False, None

            expected_chunk = transfer["next_chunk"]
            if chunk_index < expected_chunk:
                print(f"ℹ️ 忽略重复块 {chunk_index + 1}/{transfer['total_chunks']} for {filename}")
                return False, None

            if chunk_index > expected_chunk:
                print(
                    f"⚠️ 收到乱序块 {chunk_index + 1}/{transfer['total_chunks']} "
                    f"(期望 {expected_chunk + 1}) for {filename}"
                )
                if send_encrypted_fn:
                    await self._request_transfer_resume(transfer, send_encrypted_fn, expected_chunk)
                return False, None

            part_path = transfer["part_path"]
            with open(part_path, "r+b" if part_path.exists() else "wb") as f:
                f.seek(chunk_index * transfer["chunk_size"])
                f.write(chunk_data)

            transfer["next_chunk"] = chunk_index + 1
            transfer["received_bytes"] = min(
                transfer["file_size"] or ((chunk_index + 1) * transfer["chunk_size"]),
                (chunk_index * transfer["chunk_size"]) + len(chunk_data)
            )

            progress = self._format_progress(transfer["next_chunk"], transfer["total_chunks"])
            print(f"\r📥 接收文件 {filename}: {progress}", end="", flush=True)

            is_complete = transfer["next_chunk"] >= transfer["total_chunks"]
            if not is_complete:
                return False, None

            print(f"\n✅ 文件 {filename} 所有块接收完成，开始校验...")
            actual_hash = ClipMessage.calculate_file_hash(str(part_path))
            expected_hash = transfer["file_hash"]
            if expected_hash and actual_hash != expected_hash:
                print(
                    f"❌ 文件 {filename} 哈希校验失败! "
                    f"Expected: {expected_hash}, Got: {actual_hash}"
                )
                self._discard_transfer_state(transfer_id, remove_partial=True)
                if send_encrypted_fn:
                    await self._request_transfer_resume(
                        self._build_transfer_state(
                            filename,
                            transfer["remote_path"],
                            transfer_id,
                            transfer["file_size"],
                            transfer["total_chunks"],
                            transfer["chunk_size"],
                            transfer["file_hash"],
                            content_fingerprint=transfer.get("content_fingerprint"),
                            origin_device_id=transfer.get("origin_device_id"),
                            event_id=transfer.get("event_id")
                        ),
                        send_encrypted_fn,
                        0
                    )
                return False, None

            final_path = transfer["final_path"]
            os.replace(part_path, final_path)
            final_hash = expected_hash or actual_hash
            self.add_to_file_cache(final_hash, str(final_path))
            content_fingerprint = transfer.get("content_fingerprint")
            if content_fingerprint and content_fingerprint != final_hash:
                self.add_to_file_cache(content_fingerprint, str(final_path))
            self._discard_transfer_state(transfer_id, remove_partial=False)
            print(f"✅ 文件 {filename} 哈希校验成功")
            if callable(self.on_transfer_complete):
                self.on_transfer_complete(filename, transfer.get("origin_device_id"), "receive")
            return True, final_path

        except Exception as e:
            print(f"❌ 处理文件块失败: {e}")
            import traceback
            traceback.print_exc()
            return False, None

    # --- File Cache Methods ---
    def load_file_cache(self):
        """加载文件缓存"""
        cache_path = self.temp_dir / "filecache.json"
        try:
            if cache_path.exists():
                with open(cache_path, "r") as f:
                    self.file_cache = json.load(f)
                print(f"📚 已加载 {len(self.file_cache)} 个文件缓存条目")
            else:
                self.file_cache = {}
                print("📝 创建新的文件缓存")
        except Exception as e:
            print(f"⚠️ 加载文件缓存失败: {e}")
            self.file_cache = {}

    def save_file_cache(self):
        """保存文件缓存信息"""
        cache_path = self.temp_dir / "filecache.json"
        try:
            with open(cache_path, "w") as f:
                json.dump(self.file_cache, f)
        except Exception as e:
            print(f"❌ 保存文件缓存失败: {e}")

    def cleanup(self):
        """清理临时目录下的所有文件"""
        print(f"🧹 正在清理临时文件目录: {self.temp_dir}")
        had_failures = False
        try:
            for item in self.temp_dir.iterdir():
                try:
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        import shutil
                        shutil.rmtree(item)
                except Exception as e:
                    had_failures = True
                    print(f"⚠️ 无法删除 {item.name}: {e}")
            if had_failures:
                print("⚠️ 临时文件清理未完全完成，将保留缓存索引以便下次继续处理")
                return False

            self.file_cache = {}
            self.shared_item_metadata = {}
            print("✅ 临时文件清理完成")
            return True
        except Exception as e:
            print(f"❌ 清理临时目录失败: {e}")
            return False

    def add_to_file_cache(self, file_hash, file_path):
        """添加文件到缓存"""
        if file_hash and Path(file_path).exists():
            self.file_cache[file_hash] = str(file_path)
            self.save_file_cache()

    def get_from_file_cache(self, file_hash):
        """从缓存获取文件路径"""
        path = self.file_cache.get(file_hash)
        if path:
            path_obj = Path(path)
            if path_obj.exists():
                return str(path_obj)

            print(f"🧹 清理无效缓存条目: {file_hash} -> {path}")
            del self.file_cache[file_hash]
            self.save_file_cache()
        return None

    def get_files_content_hash(self, file_paths):
        """计算多个文件/目录内容的稳定MD5哈希值，跳过不存在的路径"""
        md5 = hashlib.md5()
        valid_paths_found = False
        for path_str in file_paths:
            path = Path(path_str)
            try:
                if not path.exists():
                    print(f"⚠️ 跳过不存在的路径: {path}")
                    continue

                valid_paths_found = True
                self._update_path_fingerprint(md5, path, path.name)
            except FileNotFoundError:
                print(f"⚠️ 文件不存在，跳过哈希: {path}")
            except PermissionError:
                print(f"⚠️ 权限不足，无法读取文件: {path}")
            except Exception as e:
                print(f"❌ 计算文件哈希失败: {path} - {e}")

        return md5.hexdigest() if valid_paths_found else None

    def _update_path_fingerprint(self, hasher, path: Path, label: str):
        if path.is_file():
            hasher.update(f"F:{label}\n".encode("utf-8"))
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
            return

        if path.is_dir():
            hasher.update(f"D:{label}\n".encode("utf-8"))
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                self._update_path_fingerprint(hasher, child, f"{label}/{child.name}")
            return

        raise ValueError(f"Unsupported path type: {path}")

    def _build_directory_archive_path(self, dir_path: Path) -> Path:
        suffix = hashlib.md5(str(dir_path.resolve()).encode("utf-8")).hexdigest()[:10]
        safe_name = self._sanitize_name(dir_path.name)
        return self.temp_dir / f".clipboard_dir_{safe_name}_{suffix}.zip"

    def _archive_directory(self, dir_path: Path) -> Path:
        archive_path = self._build_directory_archive_path(dir_path)
        archive_path.parent.mkdir(exist_ok=True)
        archive_path.unlink(missing_ok=True)

        root_name = dir_path.name
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{root_name}/", b"")
            for current_root, dir_names, file_names in os.walk(dir_path):
                dir_names.sort()
                file_names.sort()
                current_root_path = Path(current_root)
                rel_root = current_root_path.relative_to(dir_path)
                base_arc = Path(root_name) / rel_root if rel_root != Path(".") else Path(root_name)

                for dir_name in dir_names:
                    zf.writestr(f"{(base_arc / dir_name).as_posix()}/", b"")

                for file_name in file_names:
                    file_path = current_root_path / file_name
                    zf.write(file_path, arcname=(base_arc / file_name).as_posix())

        return archive_path

    def prepare_clipboard_entries(self, file_urls):
        prepared_entries = []
        for raw_path in file_urls:
            path_obj = Path(raw_path)
            if not path_obj.exists():
                print(f"⚠️ 跳过不存在的路径: {path_obj}")
                continue

            if path_obj.is_dir():
                content_fingerprint = self.get_files_content_hash([str(path_obj)])
                archive_path = self._archive_directory(path_obj)
                metadata = {
                    "item_type": "directory",
                    "clipboard_name": path_obj.name,
                    "archive_format": "zip",
                    "content_fingerprint": content_fingerprint,
                }
                self.shared_item_metadata[str(archive_path)] = metadata
                prepared_entries.append({
                    "path": str(archive_path),
                    **metadata,
                })
                print(f"📦 已打包目录用于传输: {path_obj.name} -> {archive_path.name}")
                continue

            if path_obj.is_file():
                content_fingerprint = ClipMessage.calculate_file_hash(str(path_obj))
                metadata = {
                    "item_type": "file",
                    "clipboard_name": path_obj.name,
                    "content_fingerprint": content_fingerprint,
                }
                self.shared_item_metadata[str(path_obj)] = metadata
                prepared_entries.append({
                    "path": str(path_obj),
                    **metadata,
                })
                continue

            print(f"⚠️ 跳过非文件或非目录路径: {path_obj}")

        return prepared_entries

    def materialize_received_path(self, message: dict, completed_path: Path) -> Path:
        item_type = message.get("item_type") or "file"
        if item_type != "directory":
            return completed_path

        if message.get("archive_format") != "zip":
            print(f"⚠️ 不支持的目录归档格式: {message.get('archive_format')}")
            return completed_path

        clipboard_name = self._sanitize_name(message.get("clipboard_name") or completed_path.stem)
        staging_dir = self.temp_dir / f".extract_{message.get('transfer_id') or completed_path.stem}"
        final_dir = self.temp_dir / clipboard_name

        shutil.rmtree(staging_dir, ignore_errors=True)
        staging_dir.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(completed_path, "r") as zf:
                for member in zf.namelist():
                    member_path = Path(member)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise ValueError(f"Illegal archive entry: {member}")
                zf.extractall(staging_dir)

            extracted_dir = staging_dir / clipboard_name
            if not extracted_dir.exists():
                top_level_dirs = [item for item in staging_dir.iterdir() if item.is_dir()]
                if len(top_level_dirs) != 1:
                    raise ValueError("Directory archive layout is invalid")
                extracted_dir = top_level_dirs[0]

            if final_dir.exists():
                if final_dir.is_dir():
                    shutil.rmtree(final_dir)
                else:
                    final_dir.unlink()

            shutil.move(str(extracted_dir), str(final_dir))
            shutil.rmtree(staging_dir, ignore_errors=True)

            file_hash = message.get("file_hash")
            if file_hash:
                self.add_to_file_cache(file_hash, str(final_dir))
            content_fingerprint = message.get("content_fingerprint")
            if content_fingerprint and content_fingerprint != file_hash:
                self.add_to_file_cache(content_fingerprint, str(final_dir))

            try:
                completed_path.unlink(missing_ok=True)
            except OSError as e:
                print(f"⚠️ 清理目录归档失败: {e}")

            print(f"📂 已还原目录到临时目录: {final_dir}")
            return final_dir
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    async def handle_received_files(
        self,
        file_info_message,
        send_encrypted_func,
        sender_websocket=None,
        current_content_hash: str | None = None
    ):
        """
        Handles a received FILE message containing file metadata.
        Checks the cache and requests missing files from the sender.
        """
        files = file_info_message.get("files", [])
        delivery_mode = file_info_message.get("delivery_mode") or "request"
        clipboard_fingerprint = file_info_message.get("clipboard_fingerprint")
        if not files:
            print("❌ 收到空的文件列表")
            return False

        if (
            delivery_mode != "oneshot"
            and clipboard_fingerprint
            and current_content_hash
            and clipboard_fingerprint == current_content_hash
        ):
            print("⏭️ 跳过重复文件快照 (与当前剪贴板内容一致)")
            return True

        files_to_request = []
        file_names = []
        cached_files = []

        for file_info in files:
            file_hash = file_info.get("hash")
            content_fingerprint = file_info.get("content_fingerprint")
            filename = file_info.get("filename")
            file_path = file_info.get("path")
            file_size = int(file_info.get("size") or 0)
            chunk_size = int(file_info.get("chunk_size") or self.chunk_size)
            origin_device_id = file_info.get("origin_device_id")
            event_id = file_info.get("event_id")
            transfer_id = file_info.get("transfer_id") or self.build_transfer_id(
                filename or "unknown",
                file_size,
                file_hash
            )

            if not filename or not file_path:
                print("⚠️ 收到的文件信息缺少名称或路径")
                continue

            file_names.append(filename)

            cached_file_path = self.get_from_file_cache(file_hash) if file_hash else None
            if not cached_file_path and content_fingerprint:
                cached_file_path = self.get_from_file_cache(content_fingerprint)
            if cached_file_path:
                cache_key = content_fingerprint or file_hash or ""
                print(f"✅ 文件 '{filename}' 在缓存中找到 (Hash: {cache_key[:8]}...)")
                cached_files.append(cached_file_path)
                continue

            restored_file = self._try_finalize_existing_partial(
                filename,
                transfer_id,
                file_size,
                file_hash
            )
            if restored_file:
                print(f"✅ 文件 '{filename}' 已从续传临时文件恢复")
                cached_files.append(restored_file)
                continue

            part_path = self._get_partial_path(filename, transfer_id)
            resume_from_chunk, aligned_bytes = self._normalize_partial_file(
                part_path,
                file_size,
                chunk_size
            )
            if aligned_bytes > 0:
                print(
                    f"ℹ️ 文件 '{filename}' 存在未完成传输，"
                    f"将从第 {resume_from_chunk + 1} 块继续"
                )
            else:
                print(f"ℹ️ 文件 '{filename}' 不在缓存中，请求传输。")

            files_to_request.append(
                ClipMessage.file_request_message(
                    file_path,
                    transfer_id=transfer_id,
                    resume_from_chunk=resume_from_chunk,
                    filename=filename,
                    origin_device_id=origin_device_id,
                    event_id=event_id
                )
            )

        if not files_to_request:
            print("✅ 所有收到的文件都在缓存中，无需请求。")
            if cached_files:
                if len(cached_files) == 1:
                    await self.set_clipboard_file(Path(cached_files[0]))
                    print("📎 已将缓存文件设置到剪贴板")
                else:
                    from utils.clipboard_utils import ClipboardUtils
                    if hasattr(ClipboardUtils, "set_clipboard_files"):
                        ClipboardUtils.set_clipboard_files([Path(f) for f in cached_files])
                    else:
                        await self.set_clipboard_file(Path(cached_files[0]))
                    print(f"📎 已将 {len(cached_files)} 个缓存文件设置到剪贴板")
            return True

        if delivery_mode == "oneshot":
            print(f"📥 收到文件信息: {', '.join(file_names[:3])}{' 等' if len(file_names) > 3 else ''}")
            print("🚀 对端使用 oneshot 文件直传，等待后续文件流...")
            return True

        print(f"📥 收到文件信息: {', '.join(file_names[:3])}{' 等' if len(file_names) > 3 else ''}")
        print(f"📤 请求 {len(files_to_request)} 个文件内容...")

        for file_request in files_to_request:
            print(
                f"📤 请求文件: {file_request['filename']} "
                f"(resume_from_chunk={file_request['resume_from_chunk']})"
            )
            try:
                await send_encrypted_func(json.dumps(file_request).encode("utf-8"))
            except Exception as e:
                print(f"❌ 发送文件请求失败 ({file_request['filename']}): {e}")
            await asyncio.sleep(ClipboardConfig.NETWORK_DELAY)

        return True

    async def set_clipboard_file(self, file_path: Path):
        """将文件路径设置到剪贴板 (Uses main thread for macOS)"""
        try:
            path_str = str(file_path)
            print(f"🔍 检查文件是否存在: {path_str} -> {file_path.exists()}")
            if not file_path.exists():
                print(f"❌ 文件不存在，无法设置到剪贴板: {path_str}")
                return None

            if file_path.is_file():
                file_path.chmod(0o644)
                print("🔓 设置文件权限: 644")
            elif file_path.is_dir():
                file_path.chmod(0o755)
                print("🔓 设置目录权限: 755")
            if IS_MACOS:
                print(f"🔄 正在设置文件到剪贴板: {Path(path_str).name}")
                try:
                    pasteboard = AppKit.NSPasteboard.generalPasteboard()
                    pasteboard.clearContents()

                    import Foundation

                    url = AppKit.NSURL.fileURLWithPath_(path_str)
                    if not url:
                        print(f"❌ 无法创建文件URL (方法1): {path_str}")
                        return None

                    print(f"🔗 创建文件URL: {url}")
                    print(f"🔍 URL路径: {url.path()}")
                    print(f"🔍 文件是否可读: {url.checkResourceIsReachableAndReturnError_(None)[0]}")

                    pasteboard.clearContents()
                    urls = AppKit.NSArray.arrayWithObject_(url)
                    success = pasteboard.writeObjects_(urls)
                    print(f"📋 writeObjects (无所有权声明) 结果: {success}")

                    if not success:
                        pasteboard.declareTypes_owner_([AppKit.NSFilenamesPboardType], None)
                        filenames = [path_str]
                        success = pasteboard.setPropertyList_forType_(filenames, AppKit.NSFilenamesPboardType)
                        print(f"📋 setPropertyList (NSFilenamesPboardType) 结果: {success}")

                        if success:
                            pasteboard.declareTypes_owner_([], None)
                            print("📋 已释放剪贴板所有权")

                    if not success:
                        pasteboard.declareTypes_owner_([AppKit.NSPasteboardTypeFileURL], None)
                        success = pasteboard.setString_forType_(url.absoluteString(), AppKit.NSPasteboardTypeFileURL)
                        print(f"📋 setString (NSPasteboardTypeFileURL) 结果: {success}")

                        if success:
                            pasteboard.declareTypes_owner_([], None)
                            print("📋 已释放剪贴板所有权")

                    if success:
                        change_count = pasteboard.changeCount()
                        print(f"✅ 文件已直接添加到Mac剪贴板: {Path(path_str).name}")
                        print(f"📋 剪贴板变化计数: {change_count}")

                        types_after = pasteboard.types()
                        print(f"🔍 设置后剪贴板类型: {list(types_after)}")

                        if AppKit.NSPasteboardTypeString in types_after:
                            text_content = pasteboard.stringForType_(AppKit.NSPasteboardTypeString)
                            print(f"🔍 剪贴板文本内容: {text_content[:50] if text_content else 'None'}...")

                        if AppKit.NSFilenamesPboardType in types_after:
                            file_list = pasteboard.propertyListForType_(AppKit.NSFilenamesPboardType)
                            print(f"🔍 剪贴板文件列表: {file_list}")

                        return change_count

                    print(f"❌ 直接添加文件到Mac剪贴板失败: {Path(path_str).name}")
                    return None

                except Exception as e:
                    print(f"❌ 直接设置剪贴板文件时出错: {e}")
                    import traceback
                    traceback.print_exc()
                    return None

            if IS_WINDOWS:
                from utils.clipboard_utils import ClipboardUtils
                try:
                    success = ClipboardUtils.set_clipboard_file(file_path)
                    if success:
                        print(f"📎 已将文件添加到剪贴板: {file_path.name}")
                        return True

                    print(f"❌ 设置Windows剪贴板文件失败: {file_path.name}")
                    return False
                except Exception as e:
                    print(f"❌ Windows剪贴板设置出错: {e}")
                    return False

            print("⚠️ 未知的操作系统，无法设置剪贴板文件")
            return None

        except Exception as e:
            print(f"❌ 设置剪贴板文件时出错 (Outer): {e}")
            import traceback
            traceback.print_exc()
            return None

    async def handle_clipboard_files(
        self,
        file_urls,
        last_content_hash,
        send_encrypted_fn,
        origin_device_id=None,
        event_id=None,
        delivery_mode: str = "request",
        schedule_transfer=None
    ):
        """处理剪贴板中的文件, 发送文件信息"""
        content_hash = self.get_files_content_hash(file_urls)
        if not content_hash:
            file_paths_str = str(sorted(file_urls))
            content_hash = hashlib.md5(file_paths_str.encode()).hexdigest()

        if content_hash == last_content_hash:
            return content_hash, False

        file_names = [os.path.basename(p) for p in file_urls]
        print(f"📤 发送文件信息: {', '.join(file_names[:3])}{' 等' if len(file_names) > 3 else ''}")

        prepared_entries = self.prepare_clipboard_entries(file_urls)
        if not prepared_entries:
            print("⚠️ 没有可发送的文件或目录")
            return last_content_hash, False

        file_msg = ClipMessage.file_message(
            prepared_entries,
            origin_device_id=origin_device_id,
            event_id=event_id,
            delivery_mode=delivery_mode,
            clipboard_fingerprint=content_hash
        )
        message_json = ClipMessage.serialize(file_msg)

        await send_encrypted_fn(message_json.encode("utf-8"))
        print("🔐 已发送加密的文件信息")

        if delivery_mode == "oneshot":
            print("🚀 已启用 oneshot 文件直传，不再等待对端请求")
            for entry in prepared_entries:
                transfer_coro = self.handle_file_transfer(
                    entry["path"],
                    send_encrypted_fn,
                    origin_device_id=origin_device_id,
                    event_id=event_id
                )
                if schedule_transfer:
                    schedule_transfer(transfer_coro, Path(entry["path"]).name)
                else:
                    await transfer_coro

        return content_hash, True

    async def process_clipboard_content(
        self,
        text: str,
        last_content_hash: str,
        send_encrypted_fn,
        origin_device_id=None,
        event_id=None
    ) -> tuple[str, bool]:
        """
        处理剪贴板文本内容, 发送文本消息.
        Returns: (new_hash, sent_update)
        """
        if not text or text.strip() == "" or self._looks_like_temp_file_path(text):
            return last_content_hash, False

        content_hash = hashlib.md5(text.encode()).hexdigest()
        if content_hash == last_content_hash:
            return last_content_hash, False

        display_content = text[:ClipboardConfig.MAX_DISPLAY_LENGTH] + (
            "..." if len(text) > ClipboardConfig.MAX_DISPLAY_LENGTH else ""
        )
        print(f"📤 发送文本: \"{display_content}\"")

        text_msg = ClipMessage.add_event_metadata(
            ClipMessage.text_message(text),
            origin_device_id=origin_device_id,
            event_id=event_id
        )
        message_json = ClipMessage.serialize(text_msg)

        await send_encrypted_fn(message_json.encode("utf-8"))
        print("🔐 已发送加密的文本")

        return content_hash, True
