"""Credential refusal: memory writes that hold a secret are refused, ordinary mentions are not.

Fixtures are assembled from fragments at run time so this file holds nothing a secret
scanner (GitHub push protection on the mirror, for one) would take for a real token.
"""

import json
import unittest
from unittest import mock

from .helpers import StoreCase, doc
from memory_buckets import _compat, config, credentials, index, provider, scopes, tools

# Right shape, invented values.
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
AWS_TEMP = "ASIA" + "IOSFODNN7EXAMPLE"
OPENAI = "sk-" + "proj-" + "abc123ABCxyz987654321"
ANTHROPIC = "sk-" + "ant-api03-" + "Zx81Qw92Lk3Mn47Pq05Rs"
GITHUB = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
GITHUB_FINE = "github_" + "pat_" + "11ABCDEFG0aBcDeFgHiJkL_mNoPqRsTuVwXyZ0123456789"
GITLAB = "gl" + "pat-" + "aB3dE5gH7jK9mN1pQ3sT"
SLACK = "xo" + "xb-" + "123456789012-1234567890123-aBcDeFgHiJkLmNoPqRsTuVwX"
PEM = ("-----BEGIN " + "RSA PRIVATE KEY-----\n"
       "MIIEowIBAAKCAQEA1x2y3z4a5b6c7d8e9f0g1h2i3j4k5l6m7n8o9p0q1r2s3t4u5v6w\n"
       "-----END " + "RSA PRIVATE KEY-----")

# (text, family) for every family the issue names.
POSITIVES = [
    (f"the aws id is {AWS}", "aws_access_key"),
    (f"temporary credentials use {AWS_TEMP}", "aws_access_key"),
    (f"the key is {OPENAI}", "api_key"),
    (f"anthropic key: {ANTHROPIC}", "api_key"),
    (f"gh auth token {GITHUB}", "github_token"),
    (f"fine-grained: {GITHUB_FINE}", "github_token"),
    (f"gitlab: {GITLAB}", "gitlab_token"),
    (f"slack bot {SLACK}", "slack_token"),
    (f"the deploy key:\n{PEM}\n", "private_key"),
    ("password: hunter2", "credential_assignment"),
    ("- **Password**: hunter2", "credential_assignment"),
    ("db_password = 'P@ssw0rd!'", "credential_assignment"),
    ('{"password": "hunter2"}', "credential_assignment"),
    ("secret=s3cr3tValue", "credential_assignment"),
    ("GITHUB_TOKEN=abc123def456ghi", "credential_assignment"),
    ("Token: 8f3a9c1d2e", "credential_assignment"),
    ("api_key: k9X2mQ7wLp", "credential_assignment"),
]

# Memories a real user keeps. Each one is a control for a pattern above.
NEGATIVES = [
    "Prefers a password manager over reusing passwords",
    "Never paste a password into chat; ask the user to type it",
    "The password: ask Ivy, she keeps it in the manager",
    "Passwords: kept in 1Password",
    "token: kept in the password manager",
    "GitHub token: stored in the system keyring",
    "secret: see /run/secrets/db_password",
    "API key: in $OPENAI_API_KEY",
    "api_key: <your key here>",
    "token: GITHUB_TOKEN_2",
    "Token endpoint: https://auth.example.com/oauth2/token",
    "token: https://example.com/v2/callback",
    "password: hunter",  # a letters-only value is not told from a word
    "refresh the token: 30min",
    "Works on task-management-system for class",
    "Uses the desk-lamp-model-2000-black-matte sizing guide",
    "risk-assessment-2024-final-version-three is the doc name",
    "Supports Arsenal-fc-supporters-club events",
    "Phone is +18025550123, SSH key is at ~/.ssh/id_ed25519",
    "The PEM header -----BEGIN PRIVATE KEY----- marks the start of a key",
    "Uses AKIA-style ids in examples",
    "ghp_ prefixes mark GitHub personal access tokens",
    "sk-learn is the scikit alias",
    "A token bucket limits the request rate",
    "token = model.to_str_tokens(tokens[0, pos:pos+1])[0]",
    "secret = load_secret(path2)",
]


class ScanTest(unittest.TestCase):
    def test_every_family_is_found_and_named(self):
        for text, family in POSITIVES:
            with self.subTest(text=text[:30]):
                self.assertEqual(credentials.scan(text), [family])

    def test_ordinary_memories_are_not_flagged(self):
        for text in NEGATIVES:
            with self.subTest(text=text):
                self.assertEqual(credentials.scan(text), [])

    def test_families_are_listed_once_each_in_a_stable_order(self):
        text = f"{GITHUB}\n{AWS}\npassword: hunter2\n{GITHUB}\n"
        self.assertEqual(credentials.scan(text), ["aws_access_key", "github_token", "credential_assignment"])

    def test_a_key_assigned_to_a_named_variable_is_reported_for_both_shapes(self):
        self.assertEqual(credentials.scan(f"OPENAI_API_KEY={OPENAI}"), ["api_key", "credential_assignment"])

    def test_a_truncated_pem_block_still_counts(self):
        self.assertEqual(credentials.scan("-----BEGIN " + "OPENSSH PRIVATE KEY-----\n" + "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAA"),
                         ["private_key"])

    def test_empty_and_huge_inputs_are_cheap(self):
        self.assertEqual(credentials.scan(""), [])
        # Long runs of the characters the patterns repeat over: quadratic backtracking
        # would show up here as a test that never finishes.
        for run in ("a" * 60_000, "a-" * 30_000, "token" * 12_000, "sk-" * 20_000, "password:" * 7_000,
                    "-----BEGIN PRIVATE KEY-----\n" * 2_000):
            with self.subTest(run=run[:12]):
                credentials.scan(run)


class ThreatlessCase(StoreCase):
    """The tool layer over a real store, with no Hermes scanner, so only this module speaks."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(_compat, "_scan_for_threats", None, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.put("global/profile.md", doc("profile", "Who the user is", "- Sam, they/them\n"))
        self.index = index.Index(self.store)
        self.addCleanup(self.index.close)
        self.written = []

    def call(self, name, **args):
        c = config.Config()
        ctx = tools.Context(self.store, self.index, scopes.resolve(c, None, "primary"), c,
                            source="telegram", on_write=self.written.append)
        result = tools.handle(name, args, ctx)
        json.dumps(result)
        return result

    def content(self, path):
        return self.store.read(path)["content"]


class WriteTest(ThreatlessCase):
    def assert_refused(self, result, families, secret):
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "secret_detected")
        self.assertEqual(result["patterns"], families)
        self.assertIn(families[0], result["error"]["message"])
        self.assertNotIn(secret, json.dumps(result), "the error goes back into the conversation; it must not repeat the secret")
        self.assertEqual(self.written, [])

    def test_write_refuses_each_family(self):
        for text, family in POSITIVES:
            with self.subTest(text=text[:30]):
                got = self.call("memory_write", path="global/topics/creds.md", description="Accounts",
                                body=f"- {text}\n", if_version="new")
                self.assertFalse(got["ok"], got)
                self.assertEqual(got["error"]["code"], "secret_detected")
                self.assertEqual(got["patterns"], [family])
                self.assertFalse((self.store.memories / "global/topics/creds.md").exists())

    def test_the_error_tells_the_agent_what_to_do_and_does_not_echo(self):
        got = self.call("memory_write", path="global/topics/creds.md", description="Accounts",
                        body=f"- openai: {OPENAI}\n", if_version="new")
        self.assert_refused(got, ["api_key"], OPENAI)
        message = got["error"]["message"]
        self.assertIn("global/topics/creds.md was not saved", message)
        self.assertIn("where the user keeps it", message)

    def test_append_refuses_and_leaves_the_file_alone(self):
        before = self.content("global/profile.md")
        got = self.call("memory_append", path="global/profile.md", lines=[f"aws {AWS}"])
        self.assert_refused(got, ["aws_access_key"], AWS)
        self.assertEqual(self.content("global/profile.md"), before)

    def test_str_replace_refuses_and_leaves_the_file_alone(self):
        before = self.content("global/profile.md")
        got = self.call("memory_str_replace", path="global/profile.md", old="Sam, they/them", new="password: hunter2")
        self.assert_refused(got, ["credential_assignment"], "hunter2")
        self.assertEqual(self.content("global/profile.md"), before)

    def test_description_is_scanned_too(self):
        got = self.call("memory_write", path="global/topics/creds.md", description=f"Uses {GITHUB}",
                        body="- harmless\n", if_version="new")
        self.assert_refused(got, ["github_token"], GITHUB)

    def test_every_family_in_one_write_is_named(self):
        got = self.call("memory_append", path="global/profile.md", lines=[AWS, GITHUB, "password: hunter2"])
        self.assert_refused(got, ["aws_access_key", "github_token", "credential_assignment"], AWS)

    def test_markdown_bold_around_the_keyword_does_not_hide_it(self):
        got = self.call("memory_append", path="global/profile.md", lines=["- **Password**: hunter2"])
        self.assertEqual(got["error"]["code"], "secret_detected")

    def test_mentioning_passwords_is_fine(self):
        for text in NEGATIVES:
            with self.subTest(text=text):
                got = self.call("memory_append", path="global/profile.md", lines=[text])
                self.assertTrue(got["ok"], got)

    def test_the_advice_in_the_error_is_itself_a_writable_note(self):
        for note in ("GitHub token: kept in the password manager", "AWS keys: in 1Password under Work",
                     "Server password: ask Ivy"):
            with self.subTest(note=note):
                self.assertTrue(self.call("memory_append", path="global/profile.md", lines=[note])["ok"])

    def test_a_secret_comes_back_as_secret_detected_not_threat_detected(self):
        # Hermes's own `hardcoded_secret` pattern fires on a quoted 20+ character value, and its
        # message talks about injection. The credential check goes first so the agent is told the truth.
        scanner = mock.Mock(return_value=["hardcoded_secret"])
        with mock.patch.object(_compat, "_scan_for_threats", scanner):
            got = self.call("memory_append", path="global/profile.md", lines=[f"token = '{OPENAI}'"])
        self.assertEqual(got["error"]["code"], "secret_detected")

    def test_an_edit_that_leaves_an_existing_secret_in_place_is_refused(self):
        self.put("global/topics/old.md", doc("old", "Old notes", f"- {AWS}\n- keep me\n"))
        got = self.call("memory_append", path="global/topics/old.md", lines=["another line"])
        self.assertEqual(got["error"]["code"], "secret_detected")

    def test_an_edit_that_removes_the_secret_is_allowed(self):
        self.put("global/topics/old.md", doc("old", "Old notes", f"- {AWS}\n- keep me\n"))
        got = self.call("memory_str_replace", path="global/topics/old.md", old=AWS, new="in the password manager")
        self.assertTrue(got["ok"], got)

    def test_a_file_holding_a_secret_can_still_be_deleted(self):
        self.put("global/topics/old.md", doc("old", "Old notes", f"- {AWS}\n"))
        v = self.call("memory_read", paths=["global/topics/old.md"])["files"][0]["version"]
        self.assertTrue(self.call("memory_delete", path="global/topics/old.md", if_version=v)["ok"])

    def test_the_check_needs_no_hermes(self):
        # Local patterns: they must work with no scanner at all (this case patches it out).
        got = self.call("memory_append", path="global/profile.md", lines=[AWS])
        self.assertEqual(got["error"]["code"], "secret_detected")


class InboxMirrorTest(ThreatlessCase):
    """Built-in memory writes are copied into global/inbox.md without going through the tools."""

    def make_provider(self):
        import os
        with mock.patch.dict(os.environ, {"MEMORY_BUCKETS_EMBEDDINGS_BACKEND": "off"}):
            p = provider.MemoryBucketsProvider()
            p.initialize("s1", hermes_home=str(self.home), platform="telegram")
        self.addCleanup(p.shutdown)
        return p

    def test_a_builtin_entry_holding_a_secret_is_mirrored_as_a_placeholder(self):
        p = self.make_provider()
        with self.assertLogs("memory_buckets", "WARNING"):
            p.on_memory_write("add", "memory", f"deploy key {AWS}")
        content = self.store.read("global/inbox.md")["content"]
        self.assertIn("[blocked: a built-in memory add on memory", content)
        self.assertIn("tell the user to review it by hand", content)
        self.assertNotIn(AWS, content)

    def test_the_old_value_of_a_replace_is_checked_too(self):
        p = self.make_provider()
        with self.assertLogs("memory_buckets", "WARNING"):
            p.on_memory_write("replace", "memory", "keys moved to the manager", {"previous_content": "password: hunter2"})
        content = self.store.read("global/inbox.md")["content"]
        self.assertNotIn("hunter2", content)
        self.assertIn("[blocked:", content)

    def test_a_clean_builtin_entry_is_mirrored_as_before(self):
        p = self.make_provider()
        p.on_memory_write("add", "memory", "keeps passwords in the manager")
        self.assertIn("[memory add] keeps passwords in the manager", self.store.read("global/inbox.md")["content"])


if __name__ == "__main__":
    unittest.main()
