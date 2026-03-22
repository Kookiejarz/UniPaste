import unittest

from utils.autostart import MacLaunchAgentManager


class MacLaunchAgentManagerTests(unittest.TestCase):
    def test_build_plist_data_uses_headless_arguments(self):
        manager = MacLaunchAgentManager(script_path="/tmp/unipaste/mac_clip_check.py")
        plist_data = manager.build_plist_data()

        self.assertEqual(plist_data["Label"], "com.unipaste.agent")
        self.assertIn("--headless", plist_data["ProgramArguments"])
        self.assertTrue(plist_data["RunAtLoad"])
        self.assertTrue(plist_data["KeepAlive"])


if __name__ == "__main__":
    unittest.main()
