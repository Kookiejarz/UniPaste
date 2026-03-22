import json
import os
import secrets
import hmac
import hashlib
import time
import uuid
import base64
from pathlib import Path

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

class DeviceAuthManager:
    def __init__(self, auth_file_path=None):
        # 默认存储在用户主目录下
        if auth_file_path is None:
            home_dir = Path.home()
            self.auth_dir = home_dir / ".unipaste"
            self.auth_file = self.auth_dir / "auth_devices.json"
        else:
            self.auth_file = Path(auth_file_path)
            self.auth_dir = self.auth_file.parent

        # 确保目录存在并设置严格权限 (仅当前用户)
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt': # Unix/Mac
            os.chmod(self.auth_dir, 0o700)
            if self.auth_file.exists():
                os.chmod(self.auth_file, 0o600)

        # 生成硬件绑定密钥
        self.encryption_key = self._get_hardware_key()
        self.fernet = Fernet(self.encryption_key) if Fernet else None

        # 加载授权设备列表
        self.authorized_devices = self._load_devices()

        # 生成服务器密钥（如果不存在）
        self.server_key = self._load_or_create_server_key()

    def _get_hardware_key(self):
        """生成基于硬件ID的32字节密钥 (Base64编码)"""
        try:
            # 组合 MAC 地址。uuid.getnode() 在某些系统上可能不稳，但它是目前最通用的。
            mac = str(uuid.getnode())
            # 加上用户主目录名作为辅助标识，比 os.getlogin() 更稳定
            user_part = Path.home().name
            seed = f"unipaste-v1-{mac}-{user_part}"
            key_hash = hashlib.sha256(seed.encode()).digest()
            return base64.urlsafe_b64encode(key_hash)
        except Exception:
            seed = f"unipaste-fallback-{uuid.getnode()}"
            key_hash = hashlib.sha256(seed.encode()).digest()
            return base64.urlsafe_b64encode(key_hash)

    def _load_or_create_server_key(self):
        key_file = self.auth_dir / "server_key.bin" # Changed to .bin for encrypted
        if key_file.exists():
            try:
                with open(key_file, "rb") as f:
                    encrypted_data = f.read()
                    if self.fernet:
                        return self.fernet.decrypt(encrypted_data).decode().strip()
                    return encrypted_data.decode().strip()
            except Exception:
                # If decryption fails (e.g. moved to another PC), generate new
                pass

        # 生成32字节随机密钥
        new_key = secrets.token_hex(32)
        try:
            with open(key_file, "wb") as f:
                if self.fernet:
                    f.write(self.fernet.encrypt(new_key.encode()))
                else:
                    f.write(new_key.encode())
            if os.name != 'nt': os.chmod(key_file, 0o600)
        except Exception as e:
            print(f"⚠️ 保存服务器密钥失败: {e}")
        return new_key

    def _load_devices(self):
        if not self.auth_file.exists():
            return {}

        try:
            with open(self.auth_file, "rb") as f:
                data = f.read()
                if not data: return {}

                if self.fernet:
                    try:
                        decrypted_data = self.fernet.decrypt(data)
                        return json.loads(decrypted_data)
                    except Exception:
                        # Possibly migration from unencrypted or different HW
                        try:
                            return json.loads(data)
                        except:
                            print("⚠️ 无法解析授权列表数据，可能已损坏")
                            return {}
                else:
                    return json.loads(data)
        except (json.JSONDecodeError, FileNotFoundError, Exception) as e:
            print(f"⚠️ 加载授权列表出错: {e}")
            return {}

    def _save_devices(self):
        try:
            json_data = json.dumps(self.authorized_devices, indent=2).encode()
            with open(self.auth_file, "wb") as f:
                if self.fernet:
                    f.write(self.fernet.encrypt(json_data))
                else:
                    f.write(json_data)
            if os.name != 'nt': os.chmod(self.auth_file, 0o600)
        except Exception as e:
            print(f"⚠️ 保存设备授权列表失败: {e}")
    def authorize_device(self, device_id, device_info=None):
        """授权新设备并生成令牌"""
        token = secrets.token_hex(16)
        timestamp = int(time.time())
        
        self.authorized_devices[device_id] = {
            "token": token,
            "created_at": timestamp,
            "last_seen": timestamp,
            "info": device_info or {}
        }
        
        self._save_devices()
        return token
        
    def validate_device(self, device_id, signature):
        """验证设备签名"""
        if device_id not in self.authorized_devices:
            print(f"❌ 设备 {device_id} 未授权")
            return False
            
        device_data = self.authorized_devices[device_id]
        device_token = device_data["token"]
        
        # 验证签名
        expected_signature = hmac.new(
            device_token.encode(), 
            device_id.encode(), 
            hashlib.sha256
        ).hexdigest()
        
        is_valid = hmac.compare_digest(signature, expected_signature)
        
        if is_valid:
            # 更新最后活动时间
            device_data["last_seen"] = int(time.time())
            self._save_devices()
            
        return is_valid
        
    def revoke_device(self, device_id):
        """撤销设备授权"""
        if device_id in self.authorized_devices:
            del self.authorized_devices[device_id]
            self._save_devices()
            return True
        return False
        
    def list_devices(self):
        """列出所有授权设备"""
        return self.authorized_devices