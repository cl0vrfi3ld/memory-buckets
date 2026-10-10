import logging
import unittest

from memory_buckets import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        # A leftover value in one load must not warn again in the next.
        config._confined_warned = False
        self.addCleanup(setattr, config, "_confined_warned", False)

    def test_leftover_confined_warns_once_and_is_ignored(self):
        with self.assertLogs("memory_buckets", level="WARNING") as logs:
            cfg = config.Config.from_dict({"write_policy": "confined"})
        self.assertFalse(hasattr(cfg, "write_policy"))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("write_policy was removed", logs.output[0])
        # Once per process: a second load is silent.
        with self.assertNoLogs("memory_buckets", level="WARNING"):
            config.Config.from_dict({"write_policy": "confined"})

    def test_other_leftover_values_are_silently_ignored(self):
        for value in ("shared", "strict", "", None, True):
            with self.assertNoLogs("memory_buckets", level="WARNING"):
                config.Config.from_dict({"write_policy": value})

    def test_confined_case_and_space_insensitive(self):
        with self.assertLogs("memory_buckets", level="WARNING"):
            config.Config.from_dict({"write_policy": " Confined "})


if __name__ == "__main__":
    unittest.main()
