import asyncio
import unittest

from utils.security.pairing import PairingManager, PairingStatus


class PairingManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_pairing_accept_flow(self):
        manager = PairingManager(timeout_seconds=1)
        callback_requests = []
        manager.set_pairing_callback(callback_requests.append)

        request = await manager.request_pairing(
            "device-a",
            {"device_name": "Peer A", "platform": "windows"},
            "127.0.0.1",
        )
        self.assertEqual(request.status, PairingStatus.PENDING)
        self.assertEqual(len(callback_requests), 1)

        async def accept_later():
            await asyncio.sleep(0.1)
            manager.accept_pairing("device-a")

        accept_task = asyncio.create_task(accept_later())
        result = await manager.wait_for_pairing_result("device-a")
        await accept_task

        self.assertEqual(result, PairingStatus.ACCEPTED)
        self.assertNotIn("device-a", manager.pending_requests)

    async def test_pairing_timeout_flow(self):
        manager = PairingManager(timeout_seconds=0.1)
        await manager.request_pairing(
            "device-b",
            {"device_name": "Peer B", "platform": "macos"},
            "127.0.0.2",
        )

        result = await manager.wait_for_pairing_result("device-b")

        self.assertEqual(result, PairingStatus.EXPIRED)
        self.assertNotIn("device-b", manager.pending_requests)


if __name__ == "__main__":
    unittest.main()
