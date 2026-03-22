import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

if "AppKit" not in sys.modules:
    appkit_stub = types.ModuleType("AppKit")
    appkit_stub.NSObject = object
    sys.modules["AppKit"] = appkit_stub

if "objc" not in sys.modules:
    sys.modules["objc"] = types.ModuleType("objc")

from handlers.file_handler import FileHandler
from utils.message_format import MessageType


class DirectoryClipboardTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_directory_clipboard_transfer_materializes_directory(self):
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as sender_temp_dir, tempfile.TemporaryDirectory() as receiver_temp_dir:
            workspace = Path(workspace_dir)
            source_dir = workspace / "ExHyperV"
            (source_dir / "nested").mkdir(parents=True)
            (source_dir / "empty").mkdir()
            (source_dir / "nested" / "hello.txt").write_text("folder-transfer", encoding="utf-8")
            (source_dir / "root.txt").write_text("top-level", encoding="utf-8")

            sender = FileHandler(Path(sender_temp_dir))
            receiver = FileHandler(Path(receiver_temp_dir))

            announcements = []

            async def collect_announcement(data: bytes):
                announcements.append(json.loads(data.decode("utf-8")))

            content_hash, sent_update = await sender.handle_clipboard_files(
                [str(source_dir)],
                "",
                collect_announcement,
                origin_device_id="sender-device",
                event_id="evt-directory",
            )

            self.assertTrue(sent_update)
            self.assertEqual(len(announcements), 1)

            file_message = announcements[0]
            self.assertEqual(file_message["type"], MessageType.FILE)
            self.assertEqual(len(file_message["files"]), 1)
            file_info = file_message["files"][0]
            self.assertEqual(file_info["item_type"], "directory")
            self.assertEqual(file_info["clipboard_name"], "ExHyperV")
            self.assertEqual(file_info["archive_format"], "zip")
            self.assertTrue(Path(file_info["path"]).is_file())

            requests = []

            async def collect_request(data: bytes):
                requests.append(json.loads(data.decode("utf-8")))

            await receiver.handle_received_files(file_message, collect_request)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["type"], MessageType.FILE_REQUEST)

            transferred_messages = []

            async def collect_transfer(data: bytes):
                transferred_messages.append(json.loads(data.decode("utf-8")))

            await sender.handle_file_transfer(
                requests[0]["path"],
                collect_transfer,
                start_chunk=requests[0]["resume_from_chunk"],
                transfer_id=requests[0]["transfer_id"],
                origin_device_id=requests[0].get("origin_device_id"),
                event_id=requests[0].get("event_id"),
            )

            self.assertGreaterEqual(len(transferred_messages), 2)
            self.assertEqual(transferred_messages[0]["type"], MessageType.FILE_START)
            self.assertEqual(transferred_messages[0]["item_type"], "directory")

            await receiver.handle_transfer_start(transferred_messages[0])

            completed_path = None
            for message in transferred_messages[1:]:
                is_complete, completed_path = await receiver.handle_received_chunk(message)

            self.assertIsNotNone(completed_path)
            materialized_path = receiver.materialize_received_path(transferred_messages[-1], completed_path)

            self.assertTrue(materialized_path.is_dir())
            self.assertEqual(materialized_path.name, "ExHyperV")
            self.assertEqual((materialized_path / "nested" / "hello.txt").read_text(encoding="utf-8"), "folder-transfer")
            self.assertEqual((materialized_path / "root.txt").read_text(encoding="utf-8"), "top-level")
            self.assertTrue((materialized_path / "empty").is_dir())
            self.assertEqual(content_hash, receiver.get_files_content_hash([str(materialized_path)]))


if __name__ == "__main__":
    unittest.main()
