import types
import unittest

from .helpers import fake_hermes
from memory_buckets import projects


class ProjectsTest(unittest.TestCase):
    def test_innermost_project_owns_the_path(self):
        with fake_hermes({"outer": "/src", "inner": "/src/inner"}):
            self.assertEqual(projects.project_for_path("/src/inner/x").bucket, "inner")
            self.assertEqual(projects.project_for_path("/src/other").bucket, "outer")
            self.assertIsNone(projects.project_for_path("/home"))
            self.assertIsNone(projects.project_for_path(""))

    def test_cwd_order_pinned_then_init_then_agent(self):
        with fake_hermes(pinned="/pinned", agent_cwd="/agent"):
            self.assertEqual(projects.session_cwd("/init"), "/pinned")
        with fake_hermes(agent_cwd="/agent"):
            self.assertEqual(projects.session_cwd("/init"), "/init")
            self.assertEqual(projects.session_cwd(""), "/agent")

    def test_slug_to_bucket(self):
        self.assertEqual(projects.bucket_for_slug("home_server"), "home-server")
        self.assertEqual(projects.bucket_for_slug("codojo"), "codojo")
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
