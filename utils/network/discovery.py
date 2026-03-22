import socket
from zeroconf import ServiceBrowser, Zeroconf, ServiceInfo, ServiceListener
import netifaces
import asyncio
from concurrent.futures import ThreadPoolExecutor

class ClipboardServiceListener(ServiceListener):
    def __init__(self, add_callback, remove_callback=None):
        self.add_callback = add_callback
        self.remove_callback = remove_callback
        
    def add_service(self, zc, type_, name):
        info = zc.get_service_info(type_, name)
        if info:
            address = str(info.parsed_addresses()[0])
            port = info.port
            properties = {}
            if info.properties:
                for key, value in info.properties.items():
                    decoded_key = key.decode("utf-8") if isinstance(key, bytes) else str(key)
                    if isinstance(value, bytes):
                        properties[decoded_key] = value.decode("utf-8")
                    else:
                        properties[decoded_key] = str(value)

            self.add_callback({
                "url": f"ws://{address}:{port}",
                "address": address,
                "port": port,
                "name": name,
                "properties": properties,
            })
            
    def remove_service(self, zeroconf, type_, name):
        if self.remove_callback:
            self.remove_callback(name)

    def update_service(self, zeroconf, type_, name):
        pass

class DeviceDiscovery:
    def __init__(self, service_name="_clipshare._tcp.local."):
        self.zeroconf = Zeroconf()
        self.service_name = service_name
        self.discovered_devices = {}
        self._executor = ThreadPoolExecutor(max_workers=1)
        self.browser = None # Initialize browser attribute

    async def start_advertising(self, port, device_id=None, platform=None):
        """Advertise this device on the network."""
        ip_addr = self._get_local_ip()
        print(f"🌐 使用IP地址 {ip_addr} 和端口 {port} 注册服务")
        properties = {}
        if device_id:
            properties["device_id"] = device_id
        if platform:
            properties["platform"] = platform
        
        info = ServiceInfo(
            self.service_name,
            f"Device_{device_id or socket.gethostname()}.{self.service_name}",
            addresses=[socket.inet_aton(ip_addr)],
            port=port,
            properties=properties,
        )
        
        print(f"📢 广播服务: {self.service_name}")
        print(f"📛 服务名称: Device_{device_id or socket.gethostname()}.{self.service_name}")
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self.zeroconf.register_service, info)
        print("✅ 服务注册成功")

    def start_discovery(self, add_callback, remove_callback=None):
        """Discover clipboard services on the network."""
        # Stop any existing browser first
        self.stop_browser()
        # Create and start a new browser
        self.browser = ServiceBrowser(
            self.zeroconf, 
            self.service_name,
            ClipboardServiceListener(add_callback, remove_callback)
        )
        print("🔍 开始搜索剪贴板服务...")

    def stop_browser(self):
        """Stop the current service browser if it's running."""
        if self.browser:
            print("DEBUG: Stopping existing service browser.")
            try:
                self.browser.cancel() # Preferred way to stop ServiceBrowser
                # self.browser.close() # close() might be needed depending on zeroconf version/impl details
            except Exception as e:
                 print(f"⚠️ Error stopping service browser: {e}")
            finally:
                 self.browser = None

    def _get_local_ip(self):
        """Get the local IP address."""
        for interface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(interface)
            if netifaces.AF_INET in addrs:
                for addr in addrs[netifaces.AF_INET]:
                    if addr['addr'] != '127.0.0.1':
                        return addr['addr']
        return '127.0.0.1'

    def close(self):
        """Clean up resources, including Zeroconf instance."""
        print("DEBUG: Closing DeviceDiscovery (Zeroconf and Executor).")
        self.stop_browser() # Ensure browser is stopped
        if hasattr(self, 'zeroconf') and self.zeroconf:
            try:
                print("DEBUG: Closing Zeroconf...")
                self.zeroconf.close()
                print("DEBUG: Zeroconf closed.")
            except Exception as e:
                 print(f"⚠️ Error closing zeroconf: {e}")
            finally:
                 self.zeroconf = None
        if hasattr(self, '_executor'):
            self._executor.shutdown(wait=False)
