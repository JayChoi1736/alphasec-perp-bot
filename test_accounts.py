import json
import os
import unittest
from tempfile import TemporaryDirectory

from accounts import load_or_create


class AccountsTest(unittest.TestCase):
    def test_load_or_create_does_not_rewrite_when_group_has_enough_accounts(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "accounts.json")
            expected = {
                "address": "0x0000000000000000000000000000000000000001",
                "key": "0x" + "11" * 32,
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"maker": [expected]}, handle)
            os.chmod(path, 0o444)

            accounts = load_or_create(path, "maker", 1)

            self.assertEqual(accounts, [expected])


if __name__ == "__main__":
    unittest.main()
