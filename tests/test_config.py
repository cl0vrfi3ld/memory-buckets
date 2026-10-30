import os
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

    # --- issue #47: boolean spellings at load ---------------------------------
    # These load-side booleans must accept the same spellings provider._coerce
    # accepts on the save side, and junk must warn instead of silently flipping.

    def test_env_shorthand_y_enables_auto_download(self):
        os.environ["MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD"] = "y"
        self.addCleanup(os.environ.pop, "MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD", None)
        with self.assertNoLogs("memory_buckets", level="WARNING"):
            cfg = config.load({})
        self.assertIs(cfg.embeddings.auto_download, True)

    def test_yes_loads_true_on_a_real_boolean_field(self):
        cfg = config.Config.from_dict({"readonly": "yes"})
        self.assertIs(cfg.readonly, True)

    def test_short_true_spellings_load_true(self):
        cfg = config.Config.from_dict({"cron_writes": "y", "embeddings": {"auto_download": "t"}})
        self.assertIs(cfg.cron_writes, True)
        self.assertIs(cfg.embeddings.auto_download, True)

    def test_false_spellings_load_false(self):
        for value in ("1", "y", "t", "yes", "on", "true"):
            cfg = config.Config.from_dict({"cron_writes": value})
            self.assertIs(cfg.cron_writes, True)
        for value in ("0", "false", "no", "off", "n", "f"):
            cfg = config.Config.from_dict({"embeddings": {"auto_download": value}})
            self.assertIs(cfg.embeddings.auto_download, False)

    def test_junk_boolean_warns_and_keeps_the_default(self):
        # auto_download's default is True: a junk value must leave it True, not False.
        with self.assertLogs("memory_buckets", level="WARNING") as logs:
            cfg = config.Config.from_dict(
                {"cron_writes": "banana", "embeddings": {"auto_download": "maybe"}}
            )
        self.assertIs(cfg.cron_writes, False)
        self.assertIs(cfg.embeddings.auto_download, True)
        self.assertEqual(len(logs.output), 2)
        self.assertIn("banana", logs.output[0])
        self.assertIn("maybe", logs.output[1])

    # --- prefetch_min_query_tokens: the int gate from issue #109's review -------
    # The token floor is configurable; junk text and negatives must warn and keep
    # the default 4 rather than opening or disabling the gate silently.

    def test_token_floor_number_loads(self):
        cfg = config.Config.from_dict({"prefetch_min_query_tokens": "6"})
        self.assertEqual(cfg.prefetch_min_query_tokens, 6)

    def test_token_floor_junk_warns_and_keeps_default(self):
        with self.assertLogs("memory_buckets", level="WARNING") as logs:
            cfg = config.Config.from_dict({"prefetch_min_query_tokens": "banana"})
        self.assertEqual(cfg.prefetch_min_query_tokens, 4)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("banana", logs.output[0])

    def test_token_floor_negative_warns_and_keeps_default(self):
        with self.assertLogs("memory_buckets", level="WARNING") as logs:
            cfg = config.Config.from_dict({"prefetch_min_query_tokens": -3})
        self.assertEqual(cfg.prefetch_min_query_tokens, 4)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("negative", logs.output[0])

    def test_token_floor_zero_opens_the_gate(self):
        cfg = config.Config.from_dict({"prefetch_min_query_tokens": 0})
        self.assertEqual(cfg.prefetch_min_query_tokens, 0)


if __name__ == "__main__":
    unittest.main()
