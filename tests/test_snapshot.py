import os
import re
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

from .helpers import StoreCase, doc
from memory_buckets import config, index, scopes, snapshot
from memory_buckets.projects import Project
from memory_buckets.store import Store, StoreError


def header_of(text, bucket):
    """The generated lines under ``### Project: <bucket>``."""
    return text.split(f"### Project: {bucket}\n", 1)[1].split("\n\n", 1)[0]


class SnapshotTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.put("global/profile.md", doc("profile", "Who the user is", "- Sam, they/them\n"))
        self.put("global/preferences.md", doc("preferences", "How to behave", "- British spelling\n"))
        self.put("global/topics/nix.md", doc("nix", "Read for Nix"))
        self.put("proj-1/profile.md", doc("profile", "What proj-1 is", "- a web app\n"))
        self.put("proj-1/preferences.md", doc("preferences", "How to work on proj-1", "- run the linter first\n"))
        self.put("proj-1/topics/deploy.md", doc("deploy", "How proj-1 deploys"))
        self.index = index.Index(self.store)
        self.addCleanup(self.index.close)

    def build(self, project=None, hermes=None, context="primary", hermes_buckets=None, **cfg):
        c = config.Config.from_dict(cfg)
        return snapshot.build(self.store, self.index, scopes.resolve(c, project, context), c, hermes,
                              hermes_buckets=hermes_buckets)

    @staticmethod
    def section(text, heading):
        """The text under ``### <heading>``, up to the next heading."""
        return text.split(f"### {heading}\n", 1)[1].split("\n###", 1)[0]

    def test_unscoped(self):
        snap = self.build()
        self.assertIn("- Sam, they/them", snap.text)
        self.assertIn("- British spelling", snap.text)
        self.assertIn("- global/topics/nix.md: Read for Nix", snap.text)
        self.assertNotIn("proj-1/topics/deploy.md", snap.text)
        self.assertNotIn("### Project:", snap.text)
        self.assertIn("Projects with memory: proj-1.\n- Read their files only when the user asks", snap.text)
        self.assertIn("- You may only add to their files. Do not edit or delete them.", snap.text)
        self.assertEqual(snap.paths, {"global/profile.md", "global/preferences.md"})

    def test_unscoped_routing(self):
        where = self.section(self.build().text, "Where facts go")
        self.assertIn("This session is not in a project. Save general facts under global/.", where)
        for path in ("global/profile.md:", "global/preferences.md:", "global/people/<name>.md:",
                     "global/areas/<name>.md:", "global/topics/<subject>.md:"):
            self.assertIn(path, where)
        self.assertIn("- When you are not sure where a fact goes, save it under global/.", where)  # D3
        self.assertNotIn("proj-1/", where)
        self.assertNotIn("Their role in a project", where)

    def test_no_rule_sends_facts_to_the_inbox(self):
        # The inbox only holds imported memory. The Inbox note may name it; no rule may.
        self.put("global/inbox.md", doc("inbox", "Imported memory waiting to be sorted"))
        self.index.reconcile()
        for kw in ({}, {"project": "proj-1"}):
            with self.subTest(**kw):
                text = self.build(**kw).text
                rules = text.split("### global/profile.md", 1)[0]
                self.assertNotIn("inbox", rules.lower())
                self.assertNotIn("propos", text.lower(), "proposals are gone")

    def test_facts_for_another_project(self):
        # D1: add to another bucket, never edit it. D2: no bucket and no Hermes project, a global topic.
        steps = self.section(self.build().text, "Facts for another project")
        self.assertEqual(
            steps.rstrip("\n"),
            "- Save a fact about another project under <project>/ (one of the projects below).\n"
            "- To add to an existing file there, use memory_append.\n"
            '- To create a file there, use memory_write with if_version "new".\n'
            "- Do not edit or delete another project's files.\n"
            "- When the project is not in the list, use global/topics/<name>.md (name it after the project).\n"
            "Projects: proj-1.")
        # Hermes projects with no memory yet can be added to; the session's own project isn't listed.
        steps = self.section(self.build(project="proj-1", hermes_buckets=["proj-1", "nixos-config"]).text,
                             "Facts for another project")
        self.assertIn("Projects: nixos-config.", steps)
        self.assertIn("Projects: none yet.", self.build(project="proj-1").text)

    def test_project_routing_matches_write_scope(self):
        shared = self.build(project="proj-1").text
        where = self.section(shared, "Where facts go")
        for path in ("proj-1/profile.md:", "proj-1/preferences.md:", "proj-1/topics/<subject>.md:",
                     "global/profile.md:", "global/preferences.md:", "global/people/<name>.md:"):
            self.assertIn(path, where)
        self.assertNotIn("index.md", shared)
        self.assertNotIn("{p}", shared)
        self.assertIn("Save each fact once, in one place:", where)
        self.assertIn("When a fact only matters to proj-1, save it under proj-1/.", where)
        self.assertIn("When a fact is also true outside proj-1, save it under global/ only.", where)
        self.assertIn("in proj-1/preferences.md.", where)  # the split example names the project
        self.assertIn("- When you are not sure where a fact goes, save it under global/.", where)  # D3

    def test_other_projects_line(self):
        self.put("proj-2/topics/x.md", doc("x", "Something about proj-2"))
        self.index.reconcile()
        text = self.build(project="proj-1").text
        self.assertIn("Other projects with memory: proj-2.\n"
                      "- Read their files only when the user asks about that project.\n"
                      "- You may only add to their files. Do not edit or delete them.", text)
        # Read-only sessions get no save instruction, here or anywhere.
        text = self.build(project="proj-1", context="cron").text
        self.assertIn("Other projects with memory: proj-2.\n- Read their files only", text)
        self.assertNotIn("add to their files", text)

    def test_read_only_sessions_get_no_save_rules(self):
        text = self.build(project="proj-1", context="cron").text
        self.assertIn("Memory is read-only in this session (cron context). Read it; do not try to save.", text)
        for gone in ("### Saving", "### Where facts go", "### Facts for another project", "### Inbox"):
            self.assertNotIn(gone, text)

    def test_prompt_style(self):
        # Short lines for small models: no instruction line over 40 words, in any session type.
        for kw in ({}, {"project": "proj-1"}):
            rules = self.build(**kw).text.split("### global/profile.md", 1)[0]
            longest = max(rules.splitlines(), key=lambda line: len(line.split()))
            self.assertLessEqual(len(longest.split()), 40, longest)

    def test_new_wording_keeps_sentences_short(self):
        # The 0.2.0 blocks: one instruction per line, sentences under 15 words.
        self.put("global/inbox.md", doc("inbox", "Imported memory waiting to be sorted"))
        self.put("proj-2/topics/x.md", doc("x", "Something about proj-2"))
        self.index.reconcile()
        text = self.build(project="proj-1").text
        lines = [*self.section(text, "Facts for another project").splitlines(),
                 *self.section(text, "Inbox").splitlines(),
                 snapshot.OTHER_PROJECTS_ADD, snapshot.UNSURE]
        with mock.patch.object(snapshot, "SORT_SKILL", None):
            lines += self.section(self.build().text, "Inbox").splitlines()
        for line in lines:
            if line.startswith("Projects:"):
                continue
            for sentence in line.removeprefix("- ").replace("(", ". ").split(". "):
                self.assertLess(len(sentence.split()), 15, line)

    def test_saving_rules_are_split_and_drop_the_dead_clause(self):
        # QA 2026-10-11: the 18-word "To create a file" line, and a "unless a
        # tool result tells you to" clause no tool result can trigger.
        saving = self.section(self.build().text, "Saving")
        self.assertIn(
            "- To add a fact, use memory_append. To change a fact, use memory_str_replace.\n"
            "- When a fact changes or is no longer true, update or remove it.",
            saving,
        )
        self.assertIn('- To create a file, use memory_write with if_version "new".', saving)
        self.assertIn("- Give the file a description that says when to read it.", saving)
        self.assertIn("- Do not mention reading or saving memory to the user.", saving)
        self.assertNotIn("unless a tool result", self.build().text)
        for sentence in "When a fact changes or is no longer true, update or remove it.".split(". "):
            self.assertLess(len(sentence.split()), 15, sentence)

    def test_project_index_inlines_the_projects_files(self):
        snap = self.build(project="proj-1")
        text = snap.text
        self.assertIn("### proj-1/profile.md\n- a web app", text)
        self.assertIn("### proj-1/preferences.md\n- run the linter first", text)
        self.assertEqual(snap.paths, {"global/profile.md", "global/preferences.md",
                                      "proj-1/profile.md", "proj-1/preferences.md"})
        # Inlined files aren't listed again; the rest of the bucket is, before global files.
        listing = text.split("### Other files", 1)[1]
        self.assertNotIn("proj-1/profile.md", listing)
        self.assertLess(listing.index("proj-1/topics/deploy.md"), listing.index("global/topics/nix.md"))
        # Global files, then the project header, then the project's files.
        self.assertLess(text.index("### global/preferences.md"), text.index("### Project: proj-1"))
        self.assertLess(text.index("### Project: proj-1"), text.index("### proj-1/profile.md"))
        self.assertIn("This session is in the project proj-1.", text)

    def test_project_header_from_hermes(self):
        home = os.path.expanduser("~")
        hermes = Project(bucket="proj-1", slug="proj_1", name="Project One", description="A web app for Sam",
                         primary_path=f"{home}/src/proj-1", folders=(f"{home}/src/proj-1", "/srv/proj-1"))
        self.assertEqual(header_of(self.build(project="proj-1", hermes=hermes).text, "proj-1"),
                         "Hermes project name: Project One\n"
                         "Hermes project description: A web app for Sam\n"
                         "Folders: ~/src/proj-1 (primary), /srv/proj-1")

    def test_project_header_skips_what_adds_nothing(self):
        # Name equal to the bucket, no description, one folder: just the folder.
        hermes = Project(bucket="proj-1", slug="proj-1", name="proj-1", folders=("/src/proj-1",))
        self.assertEqual(header_of(self.build(project="proj-1", hermes=hermes).text, "proj-1"),
                         "Folders: /src/proj-1")

    def test_empty_bucket_gets_a_cold_start_line(self):
        self.assertEqual(header_of(self.build(project="fresh").text, "fresh"),
                         "This project has no memory yet. When you learn what the project is, create "
                         "fresh/profile.md with memory_write. Include its purpose, technology, status, and where things are.")
        self.assertEqual(header_of(self.build(project="fresh", context="cron").text, "fresh"),
                         "This project has no memory yet.")

    def test_bucket_without_a_profile(self):
        self.put("proj-2/topics/x.md", doc("x", "Something about proj-2"))
        self.index.reconcile()
        text = self.build(project="proj-2").text
        self.assertTrue(header_of(text, "proj-2").startswith("proj-2/profile.md does not exist yet. When you learn"))
        self.assertIn("- proj-2/topics/x.md: Something about proj-2", text)

    def test_an_empty_profile_is_not_a_missing_one(self):
        self.put("proj-1/profile.md", doc("profile", "What proj-1 is", ""))
        text = self.build(project="proj-1").text
        self.assertEqual(header_of(text, "proj-1"),
                         "proj-1/profile.md is empty. When you learn what the project is, add to it with "
                         "memory_append. Include its purpose, technology, status, and where things are.")
        self.assertNotIn("does not exist yet", text)
        # The nudge is a save instruction: read-only sessions don't get it.
        self.assertNotIn("is empty", self.build(project="proj-1", context="cron").text)

    def test_an_unparseable_profile_tells_the_user(self):
        self.put("proj-1/profile.md", "- a web app\n")  # no frontmatter: no tool can edit it
        text = self.build(project="proj-1").text
        self.assertEqual(
            header_of(text, "proj-1"),
            "proj-1/profile.md exists, but its frontmatter is missing or can't be parsed. No memory tool "
            "can edit it. Tell the user in one line to fix it (hermes memory-buckets lint names the problem).")
        self.assertNotIn("does not exist yet", text)
        self.assertIn("### proj-1/profile.md\n- a web app", text, "the content is still readable")
        # The broken line is a fact, not a save instruction: it shows read-only too.
        self.assertIn("can't be parsed", self.build(project="proj-1", context="cron").text)

    def test_cap_trims_listing_then_files_never_profile(self):
        for i in range(200):
            self.put(f"global/topics/t{i}.md", doc(f"t{i}", "A rather long description of this particular file"))
        snap = self.build(snapshot_max_chars=5500)
        self.assertLessEqual(len(snap.text), 5500)
        self.assertIn("more files. Call memory_list", snap.text)
        self.assertIn("- British spelling", snap.text)
        self.put("global/preferences.md", doc("preferences", "How to behave", "- x\n" * 1500))
        self.index.reconcile()
        snap = self.build(snapshot_max_chars=5500)
        self.assertIn("- Sam, they/them", snap.text)
        self.assertIn("memory_read when they are relevant: global/preferences.md", snap.text)
        self.assertNotIn("global/preferences.md", snap.paths)

    @staticmethod
    def stamped(path, epoch, description="A rather long description of this particular file"):
        """A memory file whose ``edited_at`` is ``epoch`` seconds after the Unix epoch."""
        when = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return doc(path.rsplit("/", 1)[-1][:-3], description, extra=f"edited_at: {when}\n")

    def age(self, *paths):
        """Make fixture files from ``setUp`` the oldest in the store, so they never win a recency test."""
        for path in paths:
            os.utime(self.store.memories / path, (0, 0))

    def listed(self, text):
        """The paths under ``### Other files``, in the order shown."""
        lines = self.section(text, "Other files").split("\n\n", 1)[0].splitlines()
        return [line[2:].split(":", 1)[0] for line in lines if line.startswith("- ") and "more files" not in line]

    @staticmethod
    def hidden(text):
        """N from the "… and N more files" line, 0 when the listing was not cut."""
        found = re.search(r"- … and (\d+) more files", text)
        return int(found.group(1)) if found else 0

    def test_issue_3_store_lists_the_newest_files_under_the_default_cap(self):
        # The issue's store: a 1.2k profile, 0.6k of preferences, 200 topics, the default 12,000 cap.
        # Topic i was edited i minutes after topic 0, so the newest sort last by name, which is
        # what the old path-order cut hid first.
        self.put("global/profile.md", doc("profile", "Who the user is", "".join(f"- fact {i:02} {'x' * 28}\n" for i in range(30))))
        self.put("global/preferences.md", doc("preferences", "How to behave", "".join(f"- rule {i:02} {'y' * 28}\n" for i in range(15))))
        for i in range(200):
            path = f"global/topics/topic-{i:03}.md"
            self.put(path, self.stamped(path, 1_700_000_000 + i * 60, f"Topic {i:03}"))
        self.age("global/topics/nix.md")
        self.index.reconcile()
        text = self.build().text
        listed, hidden = self.listed(text), self.hidden(text)
        self.assertLessEqual(len(text), 12_000)
        self.assertTrue(0 < hidden < 200, hidden)
        self.assertEqual(len(listed) + hidden, 201)
        self.assertEqual(listed, [f"global/topics/topic-{i:03}.md" for i in range(200 - len(listed), 200)])
        self.assertIn("global/topics/topic-199.md", listed)
        self.assertNotIn("global/topics/topic-000.md", listed)

    def test_edited_at_beats_path_order_whatever_the_write_order(self):
        # Mtimes rise with the name, so path order picks t-0 and mtime order picks t-2: only
        # the edited_at stamps can pick t-1.
        for i, epoch in enumerate((1_700_000_100, 1_700_000_900, 1_700_000_500)):
            target = self.put(f"global/topics/t-{i}.md", self.stamped(f"global/topics/t-{i}.md", epoch))
            os.utime(target, (1_800_000_000 + i, 1_800_000_000 + i))
        self.age("global/topics/nix.md")
        self.index.reconcile()
        cap = len(self.build(snapshot_max_chars=100_000).text) - 1
        while len(self.listed(self.build(snapshot_max_chars=cap).text)) > 1:
            cap -= 1
        self.assertEqual(self.listed(self.build(snapshot_max_chars=cap).text), ["global/topics/t-1.md"])

    def test_a_stamp_outranks_mtime(self):
        # A hand edit to a stamped file does not move it: the stamp wins, mtime is only the fallback.
        for i in range(60):
            self.put(f"global/topics/t-{i:03}.md", self.stamped(f"global/topics/t-{i:03}.md", 1_700_000_000 + i))
        old = self.put("global/topics/zz-old-stamp.md", self.stamped("global/topics/zz-old-stamp.md", 1_600_000_000))
        os.utime(old, (1_900_000_000, 1_900_000_000))  # touched last, but stamped long ago
        self.age("global/topics/nix.md")
        self.index.reconcile()
        listed = self.listed(self.build(snapshot_max_chars=4600).text)
        self.assertNotIn("global/topics/t-000.md", listed)  # the cap really did cut
        self.assertNotIn("global/topics/zz-old-stamp.md", listed)

    def test_files_without_a_stamp_fall_back_to_mtime(self):
        # Hand-written files carry no edited_at. They rank by mtime against stamped files.
        for i in range(60):
            self.put(f"global/topics/t-{i:03}.md", self.stamped(f"global/topics/t-{i:03}.md", 1_700_000_000 + i))
        fresh = self.put("global/topics/zz-hand-edited.md", doc("zz-hand-edited", "A rather long description of this file"))
        stale = self.put("global/topics/zy-old-hand-edit.md", doc("zy-old-hand-edit", "A rather long description of this file"))
        os.utime(fresh, (1_800_000_000, 1_800_000_000))
        os.utime(stale, (1_600_000_000, 1_600_000_000))
        self.age("global/topics/nix.md")
        self.index.reconcile()
        listed = self.listed(self.build(snapshot_max_chars=4600).text)
        self.assertIn("global/topics/zz-hand-edited.md", listed)
        self.assertNotIn("global/topics/zy-old-hand-edit.md", listed)
        self.assertNotIn("global/topics/t-000.md", listed)
        self.assertIn("global/topics/t-059.md", listed)

    def test_an_unreadable_stamp_falls_back_to_mtime(self):
        # A hand-written date is not a stamp, and neither is frontmatter that will not parse.
        # Neither may break the snapshot, and both rank by mtime.
        for i in range(60):
            self.put(f"global/topics/t-{i:03}.md", self.stamped(f"global/topics/t-{i:03}.md", 1_700_000_000 + i))
        dated = self.put("global/topics/zz-dated.md",
                         doc("zz-dated", "A rather long description of this file", extra="edited_at: last Tuesday\n"))
        broken = self.put("global/topics/zy-broken.md", "- no frontmatter at all\n")
        for target in (dated, broken):
            os.utime(target, (1_800_000_000, 1_800_000_000))
        self.age("global/topics/nix.md")
        self.index.reconcile()
        listed = self.listed(self.build(snapshot_max_chars=4600).text)
        self.assertIn("global/topics/zz-dated.md", listed)
        self.assertIn("global/topics/zy-broken.md", listed)

    def test_a_file_that_became_unreadable_still_lists_by_mtime(self):
        # A listed file can turn unreadable after it was indexed (its permissions
        # changed): reconcile has no reason to drop it, so its stamp is unreachable,
        # not absent. Falling back to the mtime keeps the snapshot buildable; a raise
        # here would cost the session its whole memory block, because the provider
        # catches every exception. Mocked rather than chmod so it holds as root and
        # on Windows.
        for i in range(60):
            path = f"global/topics/t-{i:03}.md"
            self.put(path, self.stamped(path, 1_700_000_000 + i))
        sealed = self.put("global/topics/zz-sealed.md",
                          self.stamped("global/topics/zz-sealed.md", 1_600_000_000))
        os.utime(sealed, (1_800_000_000, 1_800_000_000))  # fresh mtime the store can no longer read past
        self.age("global/topics/nix.md")
        self.index.reconcile()
        original_read_bytes = Store.read_bytes

        def read_bytes(store, path):
            if path == "global/topics/zz-sealed.md":
                raise PermissionError(13, "Permission denied", path)
            return original_read_bytes(store, path)

        with mock.patch.object(Store, "read_bytes", read_bytes):
            listed = self.listed(self.build(snapshot_max_chars=4600).text)
        self.assertIn("global/topics/zz-sealed.md", listed)
        self.assertIn("global/topics/t-059.md", listed)
        self.assertNotIn("global/topics/t-000.md", listed)

    def test_project_files_outrank_newer_global_files_in_the_cut(self):
        for i in range(15):
            self.put(f"proj-1/topics/p-{i:03}.md", self.stamped(f"proj-1/topics/p-{i:03}.md", 1_700_000_000 + i))
        for i in range(60):
            self.put(f"global/topics/g-{i:03}.md", self.stamped(f"global/topics/g-{i:03}.md", 1_800_000_000 + i))
        self.age("proj-1/topics/deploy.md", "global/topics/nix.md")
        self.index.reconcile()
        listed = self.listed(self.build(project="proj-1", snapshot_max_chars=5500).text)
        project = [p for p in listed if p.startswith("proj-1/")]
        other = listed[len(project):]
        # Every project file is older than every global file and still survives, shown first.
        self.assertEqual(project, ["proj-1/topics/deploy.md", *(f"proj-1/topics/p-{i:03}.md" for i in range(15))])
        self.assertTrue(0 < len(other) < 60, len(other))
        # The room left over goes to the newest global files, in path order.
        self.assertEqual(other, [f"global/topics/g-{i:03}.md" for i in range(60 - len(other), 60)])

    def test_a_listing_that_fits_stays_in_path_order(self):
        # Nothing is cut, so recency changes nothing: plain path order, newest or not.
        self.put("global/topics/a.md", self.stamped("global/topics/a.md", 1_700_000_000))
        self.put("global/topics/b.md", self.stamped("global/topics/b.md", 1_800_000_000))
        self.index.reconcile()
        self.assertEqual(self.listed(self.build().text),
                         ["global/topics/a.md", "global/topics/b.md", "global/topics/nix.md"])
    def test_a_profile_that_cannot_fit_is_truncated(self):
        # Issue 17: the 48 KiB write cap allows a 40 KB profile, and the block runs
        # on every turn. An always-kept file keeps its head and ends with a line
        # saying how much was cut; the project header stays whole.
        body = "".join(f"- profile line {i}: a durable fact about Sam\n" for i in range(1000))
        self.put("global/profile.md", doc("profile", "Who the user is", body))
        self.index.reconcile()
        snap = self.build(snapshot_max_chars=12000)
        self.assertLessEqual(len(snap.text), 12000)
        section = self.section(snap.text, "global/profile.md")
        self.assertIn("- profile line 0:", section, "the head is kept")
        self.assertNotIn("- profile line 999:", section, "the tail is not")
        self.assertIn("characters are not shown", section)
        self.assertIn("the rest of global/profile.md.", section)
        self.assertIn("global/profile.md", snap.paths)
        # The header is never cut, even in a project session with an oversized profile.
        hermes = Project(bucket="proj-1", slug="proj-1", name="proj-1", folders=("/src/proj-1",))
        text = self.build(project="proj-1", hermes=hermes, snapshot_max_chars=12000).text
        self.assertIn("Folders: /src/proj-1", text)

    def test_a_budget_smaller_than_the_cut_note_never_hangs(self):
        # Issue 17 regression: with a budget that can't fit the cut note, the
        # retry pass computed a negative keep and _cut_body never returned. The
        # empty-head path must return the note instead. Each call runs behind a
        # join timeout so a regression fails the test rather than hanging it.
        body = "\n".join(f"line {i}" for i in range(1000))
        for budget in list(range(0, 130)) + [500, 1000, 5000, 6001]:
            with self.subTest(budget=budget):
                result = {}
                thread = threading.Thread(
                    target=lambda: result.update(text=snapshot._cut_body(body, "global/profile.md", budget)),
                    daemon=True,
                )
                thread.start()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), f"_cut_body hung at budget {budget}")
                self.assertIn("text", result, f"_cut_body returned nothing at budget {budget}")
                self.assertTrue(result["text"])
                self.assertIn("global/profile.md", result["text"])
                self.assertIn("not shown", result["text"])

    def test_listing_fills_the_cap(self):
        # Regression: the trim assumed ~40 characters a line and cut the listing far below the cap.
        for i in range(1000):
            self.put(f"global/topics/t-{i}.md",
                     doc(f"t-{i}", f"what the user thinks about topic number {i} and how it relates to the rest"))
        self.index.reconcile()
        snap = self.build(snapshot_max_chars=12000)
        self.assertLessEqual(len(snap.text), 12000)
        self.assertGreater(len(snap.text), 12000 - 200, "the listing should fill the space up to the cap")

    def test_a_cap_the_full_text_meets_drops_nothing(self):
        # Regression: the search skipped "keep every line" (which also drops the
        # "… and N more" line), then dropped files and still ended up over the cap.
        for i in range(2):
            self.put(f"global/topics/t-{i}.md", doc(f"t-{i}", f"topic {i}"))
        self.index.reconcile()
        full = self.build(snapshot_max_chars=100_000).text
        for cap in (len(full), len(full) + 1):
            with self.subTest(cap=cap):
                snap = self.build(snapshot_max_chars=cap)
                self.assertEqual(snap.text, full)
                self.assertIn("global/preferences.md", snap.paths)

    def test_a_drop_that_grows_the_text_is_skipped(self):
        # A file shorter than the notice that would name it isn't worth dropping.
        self.put("global/preferences.md", doc("preferences", "How to behave", "- x\n"))
        for i in range(300):
            self.put(f"global/topics/t-{i}.md", doc(f"t-{i}", f"what the user thinks about topic {i}"))
        self.index.reconcile()
        snap = self.build(snapshot_max_chars=4000)
        self.assertIn("global/preferences.md", snap.paths)
        self.assertNotIn("too long to show", snap.text)
        self.assertLessEqual(len(snap.text), 4000)

    def test_global_preferences_drop_before_the_projects_own(self):
        # The settled order (drop_order): the global preferences first, the
        # project's own preferences next, its profile last, the global profile never.
        self.put("global/preferences.md", doc("preferences", "How to behave", "- y\n" * 1500))
        self.put("proj-1/preferences.md", doc("preferences", "How to work on proj-1", "- run the linter first\n"))
        self.index.reconcile()
        snap = self.build(project="proj-1", snapshot_max_chars=9000)
        self.assertLessEqual(len(snap.text), 9000)
        self.assertIn("### Project: proj-1", snap.text)
        self.assertIn("memory_read when they are relevant: global/preferences.md\n", snap.text)
        self.assertNotIn("global/preferences.md", snap.paths)
        self.assertIn("proj-1/preferences.md", snap.paths, "the project's own preferences outlast the global ones")
        self.assertIn("### proj-1/profile.md", snap.text)
        # Both too big: both go, the profile still stays.
        self.put("proj-1/preferences.md", doc("preferences", "How to work on proj-1", "- x\n" * 1500))
        self.index.reconcile()
        snap = self.build(project="proj-1", snapshot_max_chars=9000)
        self.assertLessEqual(len(snap.text), 9000)
        self.assertIn("relevant: global/preferences.md, proj-1/preferences.md", snap.text)
        self.assertIn("### proj-1/profile.md", snap.text, "the profile is kept the longest")

    def test_inbox_note_names_the_skill(self):
        self.assertNotIn("skill_view", self.build().text)
        self.put("global/inbox.md", doc("inbox", "Imported memory waiting to be sorted"))
        self.index.reconcile()
        self.assertIn('skill_view("memory-buckets:sort-imports")', self.build().text)
        self.assertIn("global/inbox.md holds imported memory waiting to be sorted.", self.build().text)
        with mock.patch.object(snapshot, "SORT_SKILL", None):
            # "the prompt", not "the output": the output also holds steps meant for the user.
            self.assertIn("- To sort it, ask the user to paste the prompt from "
                          "`hermes memory-buckets import-prompt`.", self.build().text)

    def test_the_snapshot_records_the_bucket_it_was_built_for(self):
        self.assertEqual(self.build(project="proj-1").project, "proj-1")
        self.assertIsNone(self.build().project)

    def switch(self, project, previous, context="primary", **cfg):
        c = config.Config.from_dict(cfg)
        return snapshot.switch_note(self.store, scopes.resolve(c, project, context), c, previous)

    def test_switch_note_carries_the_new_buckets_rules_and_files(self):
        note = self.switch("proj-1", None)
        self.assertEqual(note.project, "proj-1")
        self.assertEqual(note.paths, {"proj-1/profile.md", "proj-1/preferences.md"})
        self.assertIn("This session was not in a project. It is now in the project proj-1.", note.text)
        # The same routing text the frozen block would have carried for proj-1.
        self.assertIn(snapshot.SHARED.format(p="proj-1"), note.text)
        self.assertIn("### proj-1/profile.md\n- a web app", note.text)
        self.assertIn("### proj-1/preferences.md\n- run the linter first", note.text)
        self.assertNotIn("- Sam, they/them", note.text, "the global files are already in the frozen block")
        self.assertNotIn("is now another project", note.text, "nothing to demote from an unscoped session")
        self.assertIn("proj-1 is now this session's own project.", note.text)

    def test_switch_note_demotes_the_old_bucket_to_add_only(self):
        text = self.switch("proj-1", "proj-0").text
        self.assertIn("This session was in the project proj-0. It is now in the project proj-1.", text)
        self.assertIn("proj-0 is now another project. You may add to its files with memory_append", text)
        # That is what the scope allows: add and create in the old bucket, no edits or deletes.
        old = scopes.resolve(config.Config(), "proj-1", buckets=["proj-0", "proj-1"])
        old.check_write("proj-0/topics/x.md", append=True)
        old.check_write("proj-0/topics/x.md", create=True)
        with self.assertRaises(StoreError):
            old.check_write("proj-0/topics/x.md")

    def test_switch_note_out_of_a_project_uses_the_unscoped_rules(self):
        note = self.switch(None, "proj-1")
        self.assertIsNone(note.project)
        self.assertNotIn("own project", note.text)
        self.assertEqual(note.paths, set())
        self.assertIn("This session was in the project proj-1. It is no longer in a project.", note.text)
        self.assertIn(snapshot.UNSCOPED, note.text)
        self.assertNotIn("### proj-1/", note.text)

    def test_switch_note_is_read_only_when_the_session_is(self):
        text = self.switch("proj-1", "proj-0", context="cron").text
        self.assertIn("Memory is read-only in this session (cron context). Read it; do not try to save.", text)
        self.assertNotIn("Save each fact once", text)
        self.assertNotIn("is now another project", text)
        self.assertIn("### proj-1/profile.md", text, "read-only sessions still read the new bucket's files")

    def test_switch_note_skips_missing_and_empty_files(self):
        self.put("proj-2/preferences.md", doc("preferences", "How to work on proj-2", "\n"))
        note = self.switch("proj-2", "proj-1")
        self.assertEqual(note.paths, set())
        self.assertIn("Save each fact once", note.text)

    def test_switch_note_drops_long_files_under_the_cap_in_build_order(self):
        self.put("proj-1/preferences.md", doc("preferences", "How to work on proj-1", "- x\n" * 1500))
        note = self.switch("proj-1", "proj-0", snapshot_max_chars=4000)
        self.assertLessEqual(len(note.text), 4000)
        self.assertIn("memory_read when they are relevant: proj-1/preferences.md", note.text)
        self.assertEqual(note.paths, {"proj-1/profile.md"}, "the preferences go first, the profile stays")
        self.put("proj-1/profile.md", doc("profile", "What proj-1 is", "- y\n" * 1500))
        note = self.switch("proj-1", "proj-0", snapshot_max_chars=4000)
        self.assertIn("relevant: proj-1/preferences.md, proj-1/profile.md", note.text)
        self.assertEqual(note.paths, set())

    def test_empty_store(self):
        snap = self.build()
        self.assertIn("## Memory", snap.text)


class EmptySnapshotTest(StoreCase):
    def test_no_files_at_all(self):
        ix = index.Index(self.store)
        self.addCleanup(ix.close)
        c = config.Config()
        snap = snapshot.build(self.store, ix, scopes.resolve(c), c)
        self.assertTrue(snap.text.startswith("## Memory"))
        self.assertEqual(snap.paths, set())


if __name__ == "__main__":
    unittest.main()
