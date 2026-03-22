import unittest

from utils.security.crypto import SecurityManager


class MultiPeerSecurityTests(unittest.TestCase):
    def _pair(self):
        left = SecurityManager()
        right = SecurityManager()
        left.generate_key_pair()
        right.generate_key_pair()
        left.generate_shared_key(right.public_key)
        right.generate_shared_key(left.public_key)
        return left, right

    def test_each_peer_pair_uses_independent_shared_key(self):
        a_to_b, b = self._pair()
        a_to_c, c = self._pair()

        plaintext = b"hello-multi-peer"
        encrypted_for_b = a_to_b.encrypt_message(plaintext)
        encrypted_for_c = a_to_c.encrypt_message(plaintext)

        self.assertEqual(b.decrypt_message(encrypted_for_b), plaintext)
        self.assertEqual(c.decrypt_message(encrypted_for_c), plaintext)

        with self.assertRaises(Exception):
            c.decrypt_message(encrypted_for_b)

        with self.assertRaises(Exception):
            b.decrypt_message(encrypted_for_c)


if __name__ == "__main__":
    unittest.main()
