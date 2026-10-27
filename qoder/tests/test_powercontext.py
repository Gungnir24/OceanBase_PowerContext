import tempfile
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT  # noqa: F401
from app.powercontext import PowerContextClient, PowerContextError
from tests.support import FakePowerContext


class PowerContextClientTest(unittest.TestCase):
    def setUp(self):
        self.server = FakePowerContext().start()
        self.state_dir = Path(tempfile.mkdtemp(prefix="rn-pc-"))
        self.client = PowerContextClient(self.server.url, state_dir=self.state_dir)

    def tearDown(self):
        self.server.stop()

    def test_ping(self):
        self.assertTrue(self.client.ping())

    def test_scope_created_and_reused(self):
        scope_id = self.client.resolve_scope("测试 Scope", "用于单元测试")
        self.assertEqual(scope_id, "scope-test")
        state_file = self.state_dir / "powercontext-scope.json"
        self.assertTrue(state_file.is_file())

        second = PowerContextClient(self.server.url, state_dir=self.state_dir)
        self.assertEqual(second.resolve_scope("测试 Scope", "用于单元测试"), "scope-test")
        creates = [
            call for call in self.server.calls
            if call["path"] == "/v1/scopes" and call["method"] == "POST"
        ]
        self.assertEqual(len(creates), 1)

    def test_remember_and_search(self):
        self.client.resolve_scope("测试 Scope", "摘要")
        result = self.client.remember("preference", "回答先给结论", reason="用户要求")
        self.assertEqual(result["entry"]["citation"]["entry_id"], "entry-1")
        hits = self.client.search("结论")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["text"], "回答先给结论")
        self.assertEqual(hits[0]["entry_id"], "entry-1")

    def test_list_and_retire(self):
        self.client.resolve_scope("测试 Scope", "摘要")
        self.client.remember("fact", "事实一")
        entries = self.client.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "fact")
        result = self.client.retire(entries[0]["citation"], reason="过期")
        self.assertTrue(result["retired"])

    def test_prepare(self):
        self.client.resolve_scope("测试 Scope", "摘要")
        prepared = self.client.prepare("周报", max_bytes=2000)
        self.assertEqual(prepared["status"], "ready")
        self.assertIn("用户偏好", prepared["content"])

    def test_capture_source(self):
        self.client.resolve_scope("测试 Scope", "摘要")
        result = self.client.capture_source("weekly-1", "# 周报", {"kind": "weekly"})
        self.assertEqual(result["source"]["source_id"], "weekly-1")

    def test_call_log_records_operations(self):
        self.client.resolve_scope("测试 Scope", "摘要")
        self.client.remember("fact", "一条事实")
        calls = self.client.read_calls(limit=10)
        self.assertTrue(any("创建 Scope" in call["op"] for call in calls))
        self.assertTrue(any("写入记忆" in call["op"] for call in calls))
        self.assertTrue(all(call["status"] in {200, 201} for call in calls))

    def test_error_mapping(self):
        with self.assertRaises(PowerContextError) as context:
            self.client._request("GET", "/v1/nope")
        self.assertEqual(context.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
