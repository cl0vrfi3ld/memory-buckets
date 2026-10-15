import sys
import types
import unittest
from unittest import mock

from .helpers import fake_hermes
from memory_buckets import projects


class ProjectsTest(unittest.TestCase):
    def test_innermost_project_owns_the_path(self):
        with fake_hermes({"outer": "/src", "inner": "/src/inner"}):
            self.assertEqual(projects.project_for_path("/src/inner/x").bucket, "inner")
            self.assertEqual(projects.project_for_path("/src/other").bucket, "outer")
            self.assertIsNone(projects.project_for_path("/home"))
            self.assertIsNone(projects.project_for_path(""))

    def test_cwd_order_workspace_override_then_pinned_then_init_then_agent_and_ignores_terminal_cd(self):
        seen_keys = []
        overrides_by_key = {
            "skey-1": {"cwd": "  /workspace/skey  ", "cwd_source": "session"},
            "s1": {"cwd": "  /workspace/s1  ", "cwd_source": "session"},
            "non-session": {"cwd": "/workspace/ignored", "cwd_source": "default"},
        }

        def resolve_task_overrides(task_id):
            seen_keys.append(task_id)
            return overrides_by_key.get(task_id, {})

        # A terminal `cd` updates get_session_cwd, never resolve_task_overrides; session_cwd must ignore it.
        term = types.SimpleNamespace(
            get_session_cwd=lambda session_key=None: (_ for _ in ()).throw(
                AssertionError("session_cwd must not read terminal cd state from get_session_cwd")
            ),
            resolve_task_overrides=resolve_task_overrides,
        )
        tools_pkg = types.SimpleNamespace(__path__=[], terminal_tool=term)
        sctx = types.SimpleNamespace(
            get_session_env=lambda name, default="": "skey-1" if name == "HERMES_SESSION_KEY" else default
        )
        gw_pkg = types.SimpleNamespace(__path__=[], session_context=sctx)
        with fake_hermes({"proj-1": "/workspace/s1"}, pinned="/pinned", agent_cwd="/agent"):
            with mock.patch.dict(sys.modules, {"tools": tools_pkg, "tools.terminal_tool": term,
                                               "gateway": gw_pkg, "gateway.session_context": sctx}):
                self.assertEqual(projects.session_cwd("/init"), "/workspace/skey")
                self.assertEqual(seen_keys, ["skey-1"])
            with mock.patch.dict(sys.modules, {"tools": tools_pkg, "tools.terminal_tool": term}):
                seen_keys.clear()
                self.assertEqual(projects.session_cwd("/init", "s1"), "/workspace/s1")
                self.assertEqual(seen_keys, ["s1"])
                proj = projects.session_project("/init", "s1")
                assert proj is not None
                self.assertEqual(proj.bucket, "proj-1")
                seen_keys.clear()
                self.assertEqual(projects.session_cwd("/init", "non-session"), "/pinned")
                self.assertEqual(seen_keys, ["non-session"])
                seen_keys.clear()
                self.assertEqual(projects.session_cwd("/init"), "/pinned")
                self.assertEqual(seen_keys, [])
                term.resolve_task_overrides = lambda task_id: {"cwd": "   ", "cwd_source": "session"}
                self.assertEqual(projects.session_cwd("/init", "s1"), "/pinned")
                term.resolve_task_overrides = lambda task_id: (_ for _ in ()).throw(RuntimeError("boom"))
                self.assertEqual(projects.session_cwd("/init", "s1"), "/pinned")
            self.assertEqual(projects.session_cwd("/init"), "/pinned")
        with fake_hermes(agent_cwd="/agent"):
            self.assertEqual(projects.session_cwd("/init"), "/init")
            self.assertEqual(projects.session_cwd(""), "/agent")

    def test_slug_to_bucket(self):
        self.assertEqual(projects.bucket_for_slug("home_server"), "home-server")
        self.assertEqual(projects.bucket_for_slug("webapp"), "webapp")
        self.assertIsNone(projects.bucket_for_slug("global"))
        self.assertIsNone(projects.bucket_for_slug("-x"))
        with fake_hermes({"global": "/g", "ok": "/ok"}):
            self.assertIsNone(projects.project_for_path("/g/x"))
            self.assertEqual([p.bucket for p in projects.list_projects()], ["ok"])

    def test_carries_folders_and_description(self):
        row = types.SimpleNamespace(slug="proj_1", name="Proj", primary_path="/a", description="  A web app  ",
                                    folders=[types.SimpleNamespace(path="/a"), types.SimpleNamespace(path="/b")])
        p = projects._to_project(row)
        assert p is not None
        self.assertEqual((p.bucket, p.folders, p.description), ("proj-1", ("/a", "/b"), "A web app"))
        bare = projects._to_project(types.SimpleNamespace(slug="x", name="x", primary_path="/x"))
        assert bare is not None
        self.assertEqual((bare.folders, bare.description), (("/x",), None))

    def test_outside_hermes(self):
        self.assertIsNone(projects.list_projects())
        self.assertIsNone(projects.session_project("/anywhere"))
        self.assertEqual(projects.unlinked_note("proj-1"), "")

    def test_unlinked_note(self):
        with fake_hermes({"proj-1": "/src/proj-1"}):
            self.assertEqual(projects.unlinked_note("proj-1"), "")
            self.assertIn("--slug proj-2", projects.unlinked_note("proj-2"))


if __name__ == "__main__":
    unittest.main()
