import asyncio
import json
import time
import threading
from typing import Dict, Optional, Callable
from dataclasses import dataclass
from enum import Enum

class PairingStatus(Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"

@dataclass
class PairingRequest:
    device_id: str
    device_name: str
    platform: str
    ip_address: str
    timestamp: float
    status: PairingStatus = PairingStatus.PENDING

class PairingManager:
    def __init__(self, timeout_seconds: int = 60):
        self.pending_requests: Dict[str, PairingRequest] = {}
        self.timeout_seconds = timeout_seconds
        self.pairing_callback: Optional[Callable] = None
        self._lock = threading.RLock()
        
    def set_pairing_callback(self, callback: Callable[[PairingRequest], None]):
        """Set callback to notify UI about pairing requests"""
        self.pairing_callback = callback
        
    async def request_pairing(self, device_id: str, device_info: dict, ip: str) -> PairingRequest:
        """Create a new pairing request"""
        request = PairingRequest(
            device_id=device_id,
            device_name=device_info.get('device_name', 'Unknown Device'),
            platform=device_info.get('platform', 'Unknown'),
            ip_address=ip,
            timestamp=time.time()
        )
        
        with self._lock:
            self.pending_requests[device_id] = request
        
        # Notify UI if callback is set
        if self.pairing_callback:
            self.pairing_callback(request)
            
        print(f"🔗 新设备请求配对: {request.device_name} ({request.platform}) - {request.ip_address}")
        print("请在服务器端确认是否允许此设备连接...")
        
        return request
        
    async def wait_for_pairing_result(self, device_id: str) -> PairingStatus:
        """Wait for user to accept/reject pairing"""
        start_time = time.time()
        
        while True:
            with self._lock:
                request = self.pending_requests.get(device_id)
            if request is None:
                return PairingStatus.REJECTED
            
            # Check timeout
            if time.time() - start_time > self.timeout_seconds:
                request.status = PairingStatus.EXPIRED
                with self._lock:
                    self.pending_requests.pop(device_id, None)
                return PairingStatus.EXPIRED
                
            if request.status != PairingStatus.PENDING:
                result = request.status
                with self._lock:
                    self.pending_requests.pop(device_id, None)
                return result
                
            await asyncio.sleep(0.5)
        
    def accept_pairing(self, device_id: str) -> bool:
        """Accept a pairing request"""
        with self._lock:
            if device_id in self.pending_requests:
                self.pending_requests[device_id].status = PairingStatus.ACCEPTED
                print(f"✅ 已接受设备配对: {device_id}")
                return True
        return False
        
    def reject_pairing(self, device_id: str) -> bool:
        """Reject a pairing request"""
        with self._lock:
            if device_id in self.pending_requests:
                self.pending_requests[device_id].status = PairingStatus.REJECTED
                print(f"❌ 已拒绝设备配对: {device_id}")
                return True
        return False

    def list_pending_requests(self):
        with self._lock:
            return list(self.pending_requests.values())
        
    def cleanup_expired_requests(self):
        """Clean up expired pairing requests"""
        current_time = time.time()
        expired_devices = []
        
        with self._lock:
            for device_id, request in self.pending_requests.items():
                if current_time - request.timestamp > self.timeout_seconds:
                    expired_devices.append(device_id)
                
            for device_id in expired_devices:
                del self.pending_requests[device_id]
                print(f"⏰ 配对请求已过期: {device_id}")
