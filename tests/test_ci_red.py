import unittest


class DeliberatelyRed(unittest.TestCase):
    def test_ci_turns_red(self):
        # Proves the new workflow fails on a failing test (issue #20, done-when 2).
        self.assertEqual(1, 2)
