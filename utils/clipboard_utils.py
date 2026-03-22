"""剪贴板通用工具函数"""
import hashlib
import time
from pathlib import Path

from utils.platform_config import IS_WINDOWS, IS_MACOS
from config import ClipboardConfig

if IS_WINDOWS:
    import win32clipboard
    import win32con
    import ctypes
    from ctypes import Structure, c_uint, sizeof
    import pyperclip

    class _DropFiles(Structure):
        _fields_ = [
            ("pFiles", c_uint),
            ("pt", c_uint * 2),
            ("fNC", c_uint),
            ("fWide", c_uint),
        ]
elif IS_MACOS:
    import AppKit

class ClipboardUtils:
    """剪贴板工具类，提供跨平台的剪贴板操作"""
    
    @staticmethod
    def calculate_content_hash(content: str) -> str:
        """计算内容的哈希值"""
        return hashlib.md5(content.encode()).hexdigest()
    
    @staticmethod
    def should_ignore_content(content_hash: str, last_remote_hash: str, 
                            last_remote_time: float, delay_multiplier: float = 2) -> bool:
        """检查是否应该忽略内容（防止回环）"""
        current_time = time.time()
        return (last_remote_hash == content_hash and 
                current_time - last_remote_time < ClipboardConfig.UPDATE_DELAY * delay_multiplier)
    
    @staticmethod
    def format_display_content(content: str, max_length: int = None) -> str:
        """格式化显示内容"""
        if max_length is None:
            max_length = ClipboardConfig.MAX_DISPLAY_LENGTH
        return content[:max_length] + ("..." if len(content) > max_length else "")

    # Windows specific methods
    if IS_WINDOWS:
        @staticmethod
        def get_clipboard_change_count():
            """获取Windows剪贴板序列号。"""
            try:
                return ctypes.windll.user32.GetClipboardSequenceNumber()
            except Exception as e:
                print(f"⚠️ 获取Windows剪贴板序列号失败: {e}")
                return None

        @staticmethod
        def get_clipboard_text():
            """获取Windows剪贴板中的Unicode文本"""
            try:
                win32clipboard.OpenClipboard()
                try:
                    if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                        data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                        return data if isinstance(data, str) and data else None
                finally:
                    win32clipboard.CloseClipboard()
            except Exception as e:
                if "OpenClipboard" in str(e):
                    print(f"⚠️ 无法访问剪贴板文本: {e} (可能被其他应用占用)")
                    time.sleep(0.2)
                else:
                    print(f"❌ 读取剪贴板文本失败: {e}")
            return None

        @staticmethod
        def set_clipboard_text(text: str) -> bool:
            """使用原生 Win32 API 设置Unicode文本剪贴板"""
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                    return True
                finally:
                    win32clipboard.CloseClipboard()
            except Exception as e:
                print(f"❌ 设置Windows文本剪贴板失败: {e}")
                return False

        @staticmethod
        def get_clipboard_files():
            """获取Windows剪贴板中的文件列表"""
            try:
                win32clipboard.OpenClipboard()
                try:
                    if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                        data = win32clipboard.GetClipboardData(win32con.CF_HDROP)
                        if data:
                            paths = [str(p) for p in data if Path(p).exists()]
                            return paths if paths else None
                finally:
                    win32clipboard.CloseClipboard()
            except Exception as e:
                if "OpenClipboard" in str(e):
                    print(f"⚠️ 无法访问剪贴板: {e} (可能被其他应用占用)")
                    time.sleep(0.5)
                else:
                    print(f"❌ 读取剪贴板文件失败: {e}")
            return None

        @staticmethod 
        def set_clipboard_file(file_path: Path) -> bool:
            """设置Windows剪贴板文件"""
            try:
                path_str = str(file_path.resolve())
                file_bytes = (path_str + "\0").encode("utf-16le") + b"\0\0"

                df = _DropFiles()
                df.pFiles = sizeof(df)
                df.pt[0] = df.pt[1] = 0
                df.fNC = 0
                df.fWide = 1

                data = bytes(df) + file_bytes

                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32con.CF_HDROP, data)
                    print(f"📎 已将文件添加到剪贴板: {file_path.name}")
                    return True
                finally:
                    win32clipboard.CloseClipboard()

            except Exception as e:
                print(f"❌ 使用 CF_HDROP 设置剪贴板文件失败: {e}")
                try:
                    pyperclip.copy(str(file_path))
                    print(f"📎 已将文件路径作为文本复制到剪贴板: {file_path.name}")
                    return True
                except Exception as text_err:
                    print(f"❌ 将文件路径作为文本复制也失败了: {text_err}")
                    return False
            return False

    # macOS specific methods  
    elif IS_MACOS:
        @staticmethod
        def get_clipboard_change_count():
            """获取macOS剪贴板变化计数。"""
            try:
                return AppKit.NSPasteboard.generalPasteboard().changeCount()
            except Exception as e:
                print(f"⚠️ 获取Mac剪贴板变化计数失败: {e}")
                return None

        @staticmethod
        def get_clipboard_files():
            """获取macOS剪贴板中的文件列表"""
            pasteboard = AppKit.NSPasteboard.generalPasteboard()
            types = pasteboard.types()
            
            if AppKit.NSPasteboardTypeFileURL in types:
                file_urls = []
                for item in pasteboard.pasteboardItems():
                    url_str = item.stringForType_(AppKit.NSPasteboardTypeFileURL)
                    if url_str:
                        url = AppKit.NSURL.URLWithString_(url_str)
                        if url and url.isFileURL():
                            file_path = url.path()
                            if file_path and Path(file_path).exists():
                                file_urls.append(file_path)
                return file_urls if file_urls else None
            return None
