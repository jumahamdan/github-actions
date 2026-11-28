import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shared_ci.common import PipelineError
from shared_ci.pr_docs import collect, configuration, evaluate, main


CONFIG = {"version": 1, "code_paths": ["src/**", "api/**"], "doc_paths": ["docs/**", "README.md"],
          "release_note_paths": ["CHANGELOG.md"]}
NO_BEHAVIOR = "Behavior changed: no — Internal refactoring preserves the existing public behavior."
NO_DOCS = "Docs updated (which) / none needed (why): none needed — Existing documentation already describes the unchanged behavior."
HEAD = "a" * 40
BASE = "b" * 40
REPO = "example/project"


def changed(name, status="modified", **extra):
    return {"filename": name, "status": status, **extra}


def release(text="- Fix sign-in error handling."):
    return changed("CHANGELOG.md", patch="@@ -1 +1,2 @@\n # Unreleased\n+" + text,
                   new_content="# Unreleased\n" + text + "\n")


class CoverageTests(unittest.TestCase):
    def result(self, files, body="", config=None):
        return evaluate(config or CONFIG, body, files)

    def test_code_with_actual_docs_and_no_behavior_change(self):
        result = self.result([changed("src/app.py"), changed("docs/usage.md")], NO_BEHAVIOR)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["code_paths_changed"], 1)
        self.assertEqual(result["doc_paths_changed"], 1)

    def test_specific_no_docs_reason_satisfies_coverage(self):
        for reason in [NO_DOCS, NO_DOCS.replace(" — ", ": "), NO_DOCS.replace(" — ", " (") + ")"]:
            with self.subTest(reason=reason):
                self.assertEqual(self.result([changed("src/app.py")], reason + "\n" + NO_BEHAVIOR)["status"], "passed")

    def test_missing_and_placeholder_docs_reasons_fail(self):
        for value in ["", "none needed", "none needed (why)", "none needed: N/A", "none needed: TODO explain the reason here",
                      "none needed: <write your detailed reason here>", "none needed: short"]:
            with self.subTest(value=value):
                body = f"Docs updated (which) / none needed (why): {value}\n{NO_BEHAVIOR}"
                result = self.result([changed("src/app.py")], body)
                self.assertEqual(result["status"], "failed")
                self.assertIn("Code changed: update", result["errors"][0])

    def test_behavior_must_be_explicit_and_no_needs_reason(self):
        for declaration in ["", "Behavior changed: no", "Behavior changed: maybe", "Behavior changed: no — TBD"]:
            with self.subTest(declaration=declaration):
                result = self.result([changed("src/app.py"), changed("README.md")], declaration)
                self.assertEqual(result["status"], "failed")

    def test_behavior_change_requires_actual_added_release_note(self):
        files = [changed("src/app.py"), changed("docs/usage.md"), release()]
        result = self.result(files, "Behavior changed: yes — Improve sign-in failures.")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["release_note_lines_added"], 1)
        self.assertEqual(self.result(files[:-1], "Behavior changed: yes")["status"], "failed")

    def test_blank_heading_comment_placeholder_and_code_are_not_release_notes(self):
        for text in ["", "   ", "## Release 2.0", "<!-- Fix sign-in failures. -->", "- TODO write real release notes.", "```example"]:
            with self.subTest(text=text):
                self.assertEqual(self.result([changed("src/app.py"), changed("README.md"), release(text)], "Behavior changed: yes")["status"], "failed")

    def test_added_line_inside_preexisting_comment_or_fence_is_not_release_note(self):
        for opener, closer in [("<!--", "-->"), ("```text", "```")]:
            note = changed("CHANGELOG.md", patch="@@ -2 +2,2 @@\n+Fix a real looking thing.\n " + closer,
                           new_content=opener + "\nFix a real looking thing.\n" + closer + "\n")
            self.assertEqual(self.result([note], "Behavior changed: yes")["status"], "failed")

    def test_setext_headings_and_indented_code_do_not_supply_release_prose(self):
        for text in ["Release heading\n===============", "Release heading\n---------------", "Release heading\ncontinued heading\n===============", "    Fix the error message.", "\tFix the error message.", "> Fix the error message."]:
            note = changed("CHANGELOG.md", patch="@@ -0,0 +1,2 @@\n+" + text.replace("\n", "\n+"), new_content=text + "\n")
            with self.subTest(text=text):
                self.assertEqual(self.result([note], "Behavior changed: yes")["status"], "failed")

    def test_fence_closer_needs_matching_marker_length_and_no_info_string(self):
        for opener, invalid_close in [("```text", "```example"), ("````text", "```"), ("~~~text", "```"), ("~~~text", "~~~example")]:
            content = opener + "\n" + invalid_close + "\nFix the error message.\n" + opener[:len(opener) - 4] + "\n"
            note = changed("CHANGELOG.md", patch="@@ -2,0 +2,2 @@\n+" + invalid_close + "\n+Fix the error message.", new_content=content)
            with self.subTest(opener=opener, invalid_close=invalid_close):
                self.assertEqual(self.result([note], "Behavior changed: yes")["status"], "failed")
        note = changed("CHANGELOG.md", patch="@@ -4,0 +4 @@\n+Fix the error message.", new_content="```text\nExample\n```\nFix the error message.\n")
        self.assertEqual(self.result([note], "Behavior changed: yes")["status"], "passed")

    def test_nested_fenced_examples_cannot_supply_release_notes(self):
        for text in ["- ```text\n  Fix the error message.\n  ```", "> ```text\n> Fix the error message.\n> ```", "    ```text\n    Fix the error message.\n    ```"]:
            note = changed("CHANGELOG.md", patch="@@ -0,0 +1,3 @@\n+" + text.replace("\n", "\n+"), new_content=text + "\n")
            with self.subTest(text=text):
                self.assertEqual(self.result([note], "Behavior changed: yes")["status"], "failed")

    def test_multiline_comment_preserves_release_diff_line_numbers(self):
        note = changed("CHANGELOG.md", patch="@@ -4 +4,2 @@\n # Unreleased\n+- Improve the public error message.",
                       new_content="<!--\nExample only\n-->\n# Unreleased\n- Improve the public error message.\n")
        self.assertEqual(self.result([note], "Behavior changed: yes")["status"], "passed")

    def test_missing_diff_or_removed_release_notes_cannot_satisfy_yes(self):
        for note in [changed("CHANGELOG.md"), changed("CHANGELOG.md", "removed"), changed("CHANGELOG.md", "renamed", previous_filename="OLD.md")]:
            with self.subTest(note=note):
                self.assertEqual(self.result([note], "Behavior changed: yes")["status"], "failed")

    def test_mismatched_or_malformed_release_diff_fails_closed(self):
        for note in [release() | {"new_content": "Different contents\n"}, release() | {"patch": "+No hunk here"},
                     release() | {"patch": "@@ -1 +99 @@\n+Line outside file"}]:
            with self.subTest(note=note), self.assertRaises(PipelineError):
                self.result([note], "Behavior changed: yes")

    def test_automatic_exemptions_and_docs_only_need_no_declaration(self):
        for names in [["tests/app.py"], ["src/app.test.ts"], ["api/index_test.ts"], [".github/workflows/ci.yml"],
                      [".ci/checks.json"], ["docs/evidence/run.json"], ["tests/app.py", ".ci/checks.json"], ["docs/usage.md"]]:
            with self.subTest(names=names):
                result = self.result([changed(name) for name in names])
                self.assertEqual(result["status"], "passed")
                self.assertIsNotNone(result["exemption"])

    def test_mixed_exempt_and_code_changes_are_not_wholly_exempt(self):
        for other in ["tests/app.py", ".github/workflows/ci.yml", "docs/evidence/run.json"]:
            with self.subTest(other=other):
                result = self.result([changed("src/app.py"), changed(other)], NO_BEHAVIOR)
                self.assertEqual(result["status"], "failed")
                self.assertIsNone(result["exemption"])
                self.assertEqual(result["doc_paths_changed"], 0)

    def test_declared_behavior_change_overrides_file_only_exemption(self):
        self.assertEqual(self.result([changed(".ci/config.json")], "Behavior changed: yes")["status"], "failed")
        self.assertEqual(self.result([changed(".ci/config.json"), release()], "Behavior changed: yes")["status"], "passed")

    def test_extra_exemptions_are_repository_local_and_mixed_safe(self):
        config = copy.deepcopy(CONFIG)
        config["exemptions"] = {"additional": ["src/generated/**"]}
        self.assertEqual(self.result([changed("src/generated/types.py")], config=config)["status"], "passed")
        self.assertEqual(self.result([changed("src/generated/types.py"), changed("src/main.py")], config=config)["status"], "failed")

    def test_rename_checks_both_old_and_new_paths(self):
        for name, previous in [("archive/app.py", "src/app.py"), ("src/app.py", "tests/app.py")]:
            with self.subTest(name=name):
                result = self.result([changed(name, "renamed", previous_filename=previous)])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["code_paths_changed"], 1)

    def test_dynamic_route_and_unicode_paths_remain_valid(self):
        files = [changed("src/[id]/page.ts"), changed("docs/使用.md")]
        self.assertEqual(self.result(files, NO_BEHAVIOR)["status"], "passed")

    def test_template_examples_and_comments_do_not_supply_declarations(self):
        for body in [f"<!--\n{NO_DOCS}\n{NO_BEHAVIOR}\n-->", f"```markdown\n{NO_DOCS}\n{NO_BEHAVIOR}\n```"]:
            self.assertEqual(self.result([changed("src/app.py")], body)["status"], "failed")

    def test_markdown_checklist_and_bold_labels_are_supported(self):
        body = "- [x] **" + NO_DOCS.replace(":", ":**", 1) + "\n- **" + NO_BEHAVIOR.replace(":", ":**", 1)
        self.assertEqual(self.result([changed("src/app.py")], body)["status"], "passed")

    def test_duplicate_declarations_are_ambiguous(self):
        for declaration in [NO_DOCS, NO_BEHAVIOR]:
            with self.assertRaisesRegex(PipelineError, "exactly one"):
                self.result([changed("src/app.py")], declaration + "\n" + declaration)

    def test_invalid_configuration_is_not_an_exemption(self):
        for override in [{"version": True}, {"code_paths": []}, {"doc_paths": ["../docs/**"]}, {"release_note_paths": ["/outside"]},
                         {"exemptions": {"everything": ["**"]}}, {"exemptions": {"tests": []}}, {"typo": True}]:
            with self.subTest(override=override), self.assertRaises(PipelineError):
                configuration(CONFIG | override)

    def test_invalid_or_duplicate_file_metadata_fails(self):
        for files in [[changed("src/app.py"), changed("src/app.py")], [changed("../secret")], [changed("src/app.py", "renamed")],
                      [changed("src/app.py", "unknown")], [changed("src/app.py\n::error::unsafe")]]:
            with self.subTest(files=files), self.assertRaises(PipelineError):
                self.result(files)

    def test_failure_output_does_not_include_raw_body_or_file_paths(self):
        secret = "private-fixture-value-not-for-logs"
        result = self.result([changed("src/" + secret + ".py")], "Unrelated prose: " + secret)
        self.assertNotIn(secret, json.dumps(result))


class GitHubCollectionTests(unittest.TestCase):
    def setUp(self):
        self.event = {"number": 7, "repository": {"full_name": REPO}, "pull_request": {"head": {"sha": HEAD}}}
        self.pr = {"number": 7, "head": {"sha": HEAD}, "base": {"sha": BASE, "repo": {"full_name": REPO}},
                   "body": NO_DOCS + "\n" + NO_BEHAVIOR, "changed_files": 1}
        self.files = [changed("src/app.py")]
        self.calls = []
        self.reads = 0
        self.final_pr = None

    def api(self, endpoint):
        self.calls.append(endpoint)
        if "/files?" in endpoint:
            page = int(endpoint.rsplit("=", 1)[-1])
            return copy.deepcopy(self.files[(page - 1) * 100:page * 100])
        self.reads += 1
        return copy.deepcopy(self.final_pr if self.final_pr is not None and self.reads > 1 else self.pr)

    def test_complete_authenticated_file_pages(self):
        self.files = [changed(f"src/file-{number}.py") for number in range(201)]
        self.pr["changed_files"] = len(self.files)
        body, files, identity = collect(self.event, REPO, self.api)
        self.assertEqual(len(files), 201)
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(identity, {"repository": REPO, "pull_request": 7, "head_sha": HEAD, "base_sha": BASE})
        self.assertEqual(evaluate(CONFIG, body, files)["status"], "passed")

    def test_short_page_and_duplicate_metadata_fail(self):
        self.pr["changed_files"] = 2
        with self.assertRaisesRegex(PipelineError, "complete"):
            collect(self.event, REPO, self.api)
        self.files *= 2
        with self.assertRaisesRegex(PipelineError, "Duplicate"):
            collect(self.event, REPO, self.api)

    def test_large_pr_and_different_head_are_refused(self):
        for change in [{"changed_files": 3001}, {"head": {"sha": "c" * 40}}]:
            with self.subTest(change=change), self.assertRaises(PipelineError):
                with patch.object(self, "pr", self.pr | change):
                    collect(self.event, REPO, self.api)

    def test_body_base_and_file_count_changes_during_read_fail(self):
        for change in [{"body": "New declaration"}, {"changed_files": 0}, {"base": {"sha": "c" * 40, "repo": {"full_name": REPO}}}]:
            self.reads = 0
            self.final_pr = self.pr | change
            with self.subTest(change=change), self.assertRaisesRegex(PipelineError, "changed during"):
                collect(self.event, REPO, self.api)

    def test_current_body_is_read_again_for_edited_events(self):
        self.event["pull_request"]["body"] = "Old body in event"
        body, files, _ = collect(self.event, REPO, self.api)
        self.assertEqual(body, self.pr["body"])
        self.assertEqual(evaluate(CONFIG, body, files)["status"], "passed")

    def test_full_release_document_is_read_at_exact_head(self):
        note = release()
        self.files = [{key: value for key, value in note.items() if key != "new_content"}]
        self.pr["body"] = "Behavior changed: yes"

        def api(endpoint):
            if "/contents/" in endpoint:
                self.assertEqual(endpoint, f"/repos/{REPO}/contents/CHANGELOG.md?ref={HEAD}")
                raw = note["new_content"].encode()
                return {"type": "file", "path": "CHANGELOG.md", "encoding": "base64", "size": len(raw),
                        "content": base64.b64encode(raw).decode()}
            return self.api(endpoint)

        body, files, _ = collect(self.event, REPO, api, CONFIG["release_note_paths"])
        self.assertEqual(evaluate(CONFIG, body, files)["status"], "passed")

    def test_invalid_release_content_is_not_accepted(self):
        self.files = [release()]
        for result in [{"type": "dir"}, {"type": "file", "path": "OTHER.md", "encoding": "base64", "size": 1, "content": "eA=="},
                       {"type": "file", "path": "CHANGELOG.md", "encoding": "base64", "size": 2, "content": "eA=="}]:
            with self.subTest(result=result), self.assertRaises(PipelineError):
                collect(self.event, REPO, lambda endpoint: result if "/contents/" in endpoint else self.api(endpoint), CONFIG["release_note_paths"])

    def test_no_pull_request_target_or_local_code_execution_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "policy.json"
            config.write_text(json.dumps(CONFIG))
            with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}, clear=True), \
                    patch("sys.argv", ["pr-docs", "--root", directory, "--config", "policy.json"]), \
                    patch("sys.stderr"):
                self.assertEqual(main(), 1)

    def test_invalid_event_repository_and_head_fail(self):
        for event in [[], {}, self.event | {"number": True}, self.event | {"repository": {"full_name": "other/project"}},
                      self.event | {"pull_request": {"head": {"sha": "main"}}}]:
            with self.subTest(event=event), self.assertRaises(PipelineError):
                collect(event, REPO, self.api)


if __name__ == "__main__":
    unittest.main()
