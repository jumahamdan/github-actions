import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci import stores
from shared_ci.common import PipelineError, load_json
from test_stores import TRUST


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {"schema": 1, "operation": "play-inventory", "product": "example-app", "allowed_refs": ["refs/heads/staging"],
                       "token_env": "STORE_TOKEN", "remote_highest_output": "remote-highest.json", "play": {"package": "org.example.app"}}
        self.env = patch.dict(os.environ, TRUST, clear=True)
        self.env.start()
        self.calls = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def play(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST":
            return {"id": "inventory-edit"}
        if path.endswith("/bundles"):
            return {"bundles": [{"versionCode": 100}, {"versionCode": 80}]}
        if path.endswith("/apks"):
            return {"apks": [{"versionCode": 101}]}
        if path.endswith("/tracks"):
            return {"tracks": [{"track": "production", "releases": [{"versionCodes": ["90"]}]},
                               {"track": "internal", "releases": [{"status": "draft", "versionCodes": ["120"]}]},
                               {"track": "beta", "releases": [{"status": "halted", "versionCodes": ["119"]}]}]}
        if method == "DELETE":
            return {}
        raise AssertionError((method, path))

    def test_play_all_tracks_bundles_apks_and_no_commit(self):
        result = stores.execute(self.config, self.root, api=self.play)
        self.assertEqual(result["highest"], 120)
        remote = load_json(self.root / "remote-highest.json")
        self.assertEqual(set(remote), {"product", "highest", "checked_at"})
        self.assertEqual(remote["highest"], 120)
        self.assertEqual(self.calls[-1][0], "DELETE")
        self.assertFalse(any(":commit" in path for _, path, _ in self.calls))

    def test_play_error_deletes_uncommitted_edit_and_no_receipt(self):
        def failing(method, path, body=None):
            if path.endswith("/bundles"):
                raise PipelineError("fixture inventory failure")
            return self.play(method, path, body)
        with self.assertRaisesRegex(PipelineError, "inventory failure"):
            stores.execute(self.config, self.root, api=failing)
        self.assertEqual(self.calls[-1][0], "DELETE")
        self.assertFalse((self.root / "remote-highest.json").exists())

    def apple_config(self):
        self.config.pop("play")
        self.config.update(operation="apple-inventory", apple={"app_id": "100", "bundle_id": "org.example.app", "platform": "IOS", "version": "1.2.3"})

    def apple(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/v1/apps/100":
            return {"data": {"id": "100", "attributes": {"bundleId": "org.example.app"}}}
        if "cursor=second" in path:
            return {"data": [{"id": "build2", "attributes": {"version": "155", "processingState": "FAILED", "expired": True}}]}
        return {"data": [{"id": "build1", "attributes": {"version": "120"}}], "links": {"next": "https://api.appstoreconnect.apple.com/v1/builds?cursor=second"}}

    def test_apple_all_pages_including_failed_expired_builds(self):
        self.apple_config()
        result = stores.execute(self.config, self.root, api=self.apple)
        self.assertEqual(result["highest"], 155)
        request = self.calls[1][1]
        self.assertIn("filter%5BpreReleaseVersion.platform%5D=IOS", request)
        self.assertIn("filter%5BpreReleaseVersion.version%5D=1.2.3", request)
        self.assertNotIn("expired", request)
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))

    def test_apple_dotted_build_refuses_integer_allocator(self):
        self.apple_config()
        def dotted(method, path, body=None):
            result = self.apple(method, path, body)
            if "data" in result and isinstance(result["data"], list):
                result["data"][0]["attributes"]["version"] = "1.2.3"
            return result
        with self.assertRaisesRegex(PipelineError, "integer allocation"):
            stores.execute(self.config, self.root, api=dotted)

    def test_apple_wrong_app_fails(self):
        self.apple_config()
        def wrong(method, path, body=None):
            return {"data": {"id": "100", "attributes": {"bundleId": "wrong.app"}}}
        with self.assertRaisesRegex(PipelineError, "identity mismatch"):
            stores.execute(self.config, self.root, api=wrong)

    def test_inventory_untrusted_context_fails(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}), self.assertRaises(PipelineError):
            stores.execute(self.config, self.root, api=self.play)
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
