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
from utils.message_format import ClipMessage, MessageType


class FileTransferResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_transfer_from_partial_file(self):
        with tempfile.TemporaryDirectory() as sender_dir, tempfile.TemporaryDirectory() as receiver_dir:
            sender_root = Path(sender_dir)
            receiver_root = Path(receiver_dir)
            source_path = sender_root / "sample.bin"
            source_bytes = b"resume-transfer-" * 6
            source_path.write_bytes(source_bytes)

            sender = FileHandler(sender_root)
            receiver = FileHandler(receiver_root)
            sender.chunk_size = 8
            receiver.chunk_size = 8

            first_pass_messages = []

            async def collect_first_pass(data: bytes):
                first_pass_messages.append(json.loads(data.decode("utf-8")))

            await sender.handle_file_transfer(str(source_path), collect_first_pass)

            self.assertEqual(first_pass_messages[0]["type"], MessageType.FILE_START)
            await receiver.handle_transfer_start(first_pass_messages[0])

            for message in first_pass_messages[1:3]:
                self.assertEqual(message["type"], MessageType.FILE_CHUNK)
                is_complete, _ = await receiver.handle_received_chunk(message)
                self.assertFalse(is_complete)

            file_hash = ClipMessage.calculate_file_hash(str(source_path))
            transfer_id = sender.build_transfer_id(source_path.name, len(source_bytes), file_hash)
            part_path = receiver._get_partial_path(source_path.name, transfer_id)
            self.assertTrue(part_path.exists())
            self.assertEqual(part_path.stat().st_size, 16)

            resume_requests = []

            async def collect_resume_request(data: bytes):
                resume_requests.append(json.loads(data.decode("utf-8")))

            await receiver.handle_received_files(
                {
                    "type": MessageType.FILE,
                    "files": [{
                        "filename": source_path.name,
                        "path": str(source_path),
                        "size": len(source_bytes),
                        "hash": file_hash,
                        "chunk_size": 8,
                        "transfer_id": transfer_id,
                    }]
                },
                collect_resume_request
            )

            self.assertEqual(len(resume_requests), 1)
            self.assertEqual(resume_requests[0]["type"], MessageType.FILE_REQUEST)
            self.assertEqual(resume_requests[0]["resume_from_chunk"], 2)

            resumed_messages = []

            async def collect_resumed_data(data: bytes):
                resumed_messages.append(json.loads(data.decode("utf-8")))

            await sender.handle_file_transfer(
                str(source_path),
                collect_resumed_data,
                start_chunk=resume_requests[0]["resume_from_chunk"],
                transfer_id=resume_requests[0]["transfer_id"]
            )

            self.assertEqual(resumed_messages[0]["type"], MessageType.FILE_START)
            await receiver.handle_transfer_start(resumed_messages[0])

            completed_path = None
            for message in resumed_messages[1:]:
                is_complete, completed_path = await receiver.handle_received_chunk(message)

            self.assertIsNotNone(completed_path)
            self.assertEqual(completed_path.read_bytes(), source_bytes)


if __name__ == "__main__":
    unittest.main()
