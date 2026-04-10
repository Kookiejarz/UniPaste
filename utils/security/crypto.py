from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import os
import base64
import json

class SecurityManager:
    def __init__(self):
        self.private_key = None
        self.public_key = None
        self.shared_key = None
        self._cipher: AESGCM | None = None  # cached — key schedule computed once

    def generate_key_pair(self):
        """Generate new ECDH key pair"""
        try:
            self.private_key = ec.generate_private_key(ec.SECP256R1())
            self.public_key = self.private_key.public_key()
            return self.public_key
        except Exception as e:
            print(f"密钥对生成失败: {e}")
            raise

    def has_shared_key(self):
        """Check if shared key exists"""
        return self.shared_key is not None

    def serialize_public_key(self):
        """Serialize public key for transmission"""
        if not self.public_key:
            raise ValueError("No public key available")
        
        serialized = self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return base64.b64encode(serialized).decode('utf-8')

    def deserialize_public_key(self, key_data):
        """Deserialize a received public key"""
        try:
            key_bytes = base64.b64decode(key_data)
            peer_public_key = serialization.load_pem_public_key(key_bytes)
            return peer_public_key
        except Exception as e:
            print(f"公钥反序列化失败: {e}")
            raise


    def generate_shared_key(self, peer_public_key):
        """Generate shared key using ECDH"""
        if not self.private_key:
            raise ValueError("No private key available")
            
        shared_key = self.private_key.exchange(ec.ECDH(), peer_public_key)
        self.shared_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b'handshake data',
        ).derive(shared_key)
        self._cipher = AESGCM(self.shared_key)
        print(f"🔑 ECDH密钥交换成功，前8字节: {self.shared_key[:8].hex()}")
        return self.shared_key

    def set_shared_key_from_password(self, password: str):
        """Set shared key from a password (for testing)"""
        import hashlib
        self.shared_key = hashlib.sha256(password.encode()).digest()
        self._cipher = AESGCM(self.shared_key)
        print(f"🔑 从密码设置密钥，前8字节: {self.shared_key[:8].hex()}")
        return self.shared_key

    def encrypt_message(self, message: bytes) -> bytes:
        """Encrypt a message using AES-256-GCM."""
        if not self._cipher:
            raise ValueError("Shared key not established")
        try:
            nonce = os.urandom(12)
            return nonce + self._cipher.encrypt(nonce, message, None)
        except Exception as e:
            print(f"❌ 加密失败: {e}")
            raise

    def decrypt_message(self, encrypted_data: bytes) -> bytes:
        """Decrypt a message using AES-256-GCM."""
        if not self._cipher:
            raise ValueError("Shared key not established")
        if not isinstance(encrypted_data, bytes):
            raise TypeError(f"无法处理的数据类型: {type(encrypted_data)}")
        try:
            if len(encrypted_data) <= 12:
                raise ValueError(f"数据太短: {len(encrypted_data)} 字节")
            return self._cipher.decrypt(encrypted_data[:12], encrypted_data[12:], None)
        except Exception as e:
            print(f"❌ 解密失败: {e} (数据长度={len(encrypted_data)}字节)")
            raise

    async def perform_key_exchange(self, send_data_func, receive_data_func):
        """
        Perform key exchange using provided send and receive functions
        
        Args:
            send_data_func: async function to send data
            receive_data_func: async function to receive data
            
        Returns:
            bool: True if key exchange was successful
        """
        try:
            # Generate our key pair if needed
            if not self.public_key:
                self.generate_key_pair()
            
            # Serialize and send our public key
            server_public_key = self.serialize_public_key()
            key_message = json.dumps({
                "type": "key_exchange",
                "public_key": server_public_key
            })
            await send_data_func(key_message)
            print("📤 已发送公钥")
            
            # Receive peer's public key
            response = await receive_data_func()
            peer_data = json.loads(response)
            
            if peer_data.get("type") == "key_exchange":
                peer_key_data = peer_data.get("public_key")
                peer_public_key = self.deserialize_public_key(peer_key_data)
                
                # Generate shared key
                self.generate_shared_key(peer_public_key)
                print("🔒 密钥交换完成，已建立共享密钥")
                
                # Send confirmation
                await send_data_func(json.dumps({
                    "type": "key_exchange_complete",
                    "status": "success"
                }))
                return True
            else:
                print("❌ 对方未发送公钥")
                return False
                
        except Exception as e:
            print(f"❌ 密钥交换失败: {e}")
            return False
