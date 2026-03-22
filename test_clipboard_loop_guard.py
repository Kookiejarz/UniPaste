import unittest

from utils.clipboard_loop_guard import ClipboardLoopGuard


class ClipboardLoopGuardTests(unittest.TestCase):
    def test_consumes_expected_echo_once(self):
        guard = ClipboardLoopGuard()
        guard.register(
            kind="text",
            fingerprint="abc123",
            expected_change_count=42,
            event_id="evt-1",
            origin_device_id="peer-a",
        )

        event = guard.consume_if_expected(
            kind="text",
            fingerprint="abc123",
            change_count=42,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.event_id, "evt-1")

        second = guard.consume_if_expected(
            kind="text",
            fingerprint="abc123",
            change_count=42,
        )
        self.assertIsNone(second)

    def test_does_not_consume_mismatched_change_count(self):
        guard = ClipboardLoopGuard()
        guard.register(
            kind="files",
            fingerprint="filehash",
            expected_change_count=12,
        )

        event = guard.consume_if_expected(
            kind="files",
            fingerprint="filehash",
            change_count=13,
        )
        self.assertIsNone(event)


if __name__ == "__main__":
    unittest.main()
