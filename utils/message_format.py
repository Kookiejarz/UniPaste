import json
import base64
from pathlib import Path
import hashlib
import time
import uuid

class MessageType:
    TEXT = "text"
    FILE = "file"
    FILE_START = "file_start"
    FILE_CHUNK = "file_chunk"
    FILE_RESPONSE = "file_response"
    FILE_REQUEST = "file_request"

class ClipMessage:
    """剪贴板消息格式化工具"""
    
    @staticmethod
    def text_message(text):
        """创建文本消息"""
        return {
            "type": MessageType.TEXT,
            "content": text
        }

    @staticmethod
    def add_event_metadata(message, origin_device_id=None, event_id=None, timestamp=None):
        if origin_device_id:
            message["origin_device_id"] = origin_device_id
        if event_id:
            message["event_id"] = event_id
        message["timestamp"] = timestamp if timestamp is not None else time.time()
        return message

    @staticmethod
    def new_event_id():
        return uuid.uuid4().hex
    
    @staticmethod
    def file_message(file_paths, origin_device_id=None, event_id=None):
        """创建文件路径消息
        
        file_paths 可以是单个路径或路径列表
        """
        if not isinstance(file_paths, list):
            file_paths = [file_paths]
            
        file_infos = []
        for path in file_paths:
            path_obj = Path(path)
            if path_obj.exists():
                size = path_obj.stat().st_size
                file_hash = ClipMessage.calculate_file_hash(str(path_obj))
                chunk_size = ClipMessage.default_chunk_size()
                total_chunks = (size + chunk_size - 1) // chunk_size if size else 0
                
                file_infos.append({
                    "filename": path_obj.name,
                    "path": str(path_obj),
                    "size": size,
                    "mtime": path_obj.stat().st_mtime,
                    "hash": file_hash,
                    "chunk_size": chunk_size,
                    "total_chunks": total_chunks,
                    "transfer_id": ClipMessage.calculate_transfer_id(
                        path_obj.name,
                        size,
                        file_hash
                    )
                })
        
        return ClipMessage.add_event_metadata({
            "type": MessageType.FILE,
            "files": file_infos
        }, origin_device_id=origin_device_id, event_id=event_id)
    
    @staticmethod
    def file_request_message(
        file_path,
        transfer_id=None,
        resume_from_chunk=0,
        filename=None,
        origin_device_id=None,
        event_id=None
    ):
        """请求特定文件内容"""
        path_obj = Path(file_path)
        return ClipMessage.add_event_metadata({
            "type": MessageType.FILE_REQUEST,
            "filename": filename or path_obj.name,
            "path": str(path_obj),
            "transfer_id": transfer_id,
            "resume_from_chunk": max(0, int(resume_from_chunk or 0))
        }, origin_device_id=origin_device_id, event_id=event_id)
    
    @staticmethod
    def file_response_message(file_path, chunk_index=0, total_chunks=1):
        """文件内容响应消息"""
        path_obj = Path(file_path)
        
        if not path_obj.exists():
            return {
                "type": MessageType.FILE_RESPONSE,
                "filename": path_obj.name,
                "exists": False
            }
        
        # 计算文件分块
        file_size = path_obj.stat().st_size
        chunk_size = ClipMessage.default_chunk_size()
        total_chunks = (file_size + chunk_size - 1) // chunk_size
        file_hash = ClipMessage.calculate_file_hash(file_path)
        transfer_id = ClipMessage.calculate_transfer_id(path_obj.name, file_size, file_hash)
        
        # 读取对应块的内容
        with open(file_path, "rb") as f:
            f.seek(chunk_size * chunk_index)
            chunk_data = f.read(chunk_size)
            encoded_data = base64.b64encode(chunk_data).decode('utf-8')
        
        # 计算块哈希
        chunk_hash = hashlib.md5(chunk_data).hexdigest()
        
        return {
            "type": MessageType.FILE_CHUNK,
            "filename": path_obj.name,
            "exists": True,
            "path": str(path_obj),
            "size": file_size,
            "chunk_size": chunk_size,
            "transfer_id": transfer_id,
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
            "chunk_data": encoded_data,
            "file_hash": file_hash,
            "chunk_hash": chunk_hash
        }
    
    @staticmethod
    def calculate_file_hash(file_path):
        """计算文件的MD5哈希值"""
        hasher = hashlib.md5()
        with open(file_path, "rb") as f:
            # 读取文件块并更新哈希
            chunk = f.read(65536)  # 64KB 块
            while chunk:
                hasher.update(chunk)
                chunk = f.read(65536)
        return hasher.hexdigest()

    @staticmethod
    def calculate_transfer_id(filename, size, file_hash):
        raw = f"{filename}|{size}|{file_hash or ''}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def default_chunk_size():
        from config import ClipboardConfig
        return ClipboardConfig.CHUNK_SIZE
    
    @staticmethod
    def serialize(message):
        """序列化消息为JSON字符串"""
        return json.dumps(message)
    
    @staticmethod
    def deserialize(json_str):
        """反序列化JSON字符串为消息"""
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None
