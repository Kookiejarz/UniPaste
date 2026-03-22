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


class OneShotFileTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_oneshot_transfer_streams_without_file_request(self):
        with tempfile.TemporaryDirectory() as sender_dir, tempfile.TemporaryDirectory() as receiver_dir:
            sender_root = Path(sender_dir)
            receiver_root = Path(receiver_dir)
            source_path = sender_root / "oneshot.txt"
            source_bytes = b"oneshot-transfer-" * 4
            source_path.write_bytes(source_bytes)

            sender = FileHandler(sender_root)
            receiver = FileHandler(receiver_root)
            sender.chunk_size = 8
            receiver.chunk_size = 8

            sent_messages = []
            scheduled_transfers = []

            async def collect_sent(data: bytes):
                sent_messages.append(json.loads(data.decode("utf-8")))

            def schedule_transfer(transfer_coro, label: str):
                scheduled_transfers.append((label, transfer_coro))

            _, sent_update = await sender.handle_clipboard_files(
                [str(source_path)],
                "",
                collect_sent,
                origin_device_id="sender-device",
                event_id="evt-oneshot",
                delivery_mode="oneshot",
                schedule_transfer=schedule_transfer,
            )

            self.assertTrue(sent_update)
            self.assertEqual(len(sent_messages), 1)
            self.assertEqual(sent_messages[0]["type"], MessageType.FILE)
            self.assertEqual(sent_messages[0]["delivery_mode"], "oneshot")
            self.assertEqual(len(scheduled_transfers), 1)

            requests = []

            async def collect_request(data: bytes):
                requests.append(json.loads(data.decode("utf-8")))

            await receiver.handle_received_files(sent_messages[0], collect_request)
            self.assertEqual(requests, [])

            for _, transfer_coro in scheduled_transfers:
                await transfer_coro

            self.assertGreaterEqual(len(sent_messages), 2)
            self.assertEqual(sent_messages[1]["type"], MessageType.FILE_START)

            await receiver.handle_transfer_start(sent_messages[1])

            completed_path = None
            for message in sent_messages[2:]:
                self.assertEqual(message["type"], MessageType.FILE_CHUNK)
                is_complete, completed_path = await receiver.handle_received_chunk(message)
                if is_complete:
                    break

            self.assertIsNotNone(completed_path)
            self.assertEqual(completed_path.read_bytes(), source_bytes)


if __name__ == "__main__":
    unittest.main()
