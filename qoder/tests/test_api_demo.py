import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

from tests.support import PROJECT_ROOT


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DemoApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        cls.process = subprocess.Popen(
            [sys.executable, "app/server.py", "--demo", "--port", str(cls.port)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                cls.get("/api/health")
                return
            except Exception:
                if cls.process.poll() is not None:
                    break
                time.sleep(0.3)
        output = cls.process.stdout.read().decode("utf-8", errors="replace") if cls.process.stdout else ""
        raise RuntimeError(f"demo server 未启动：\n{output}")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.process.kill()

    @classmethod
    def get(cls, path):
        with urllib.request.urlopen(f"{cls.base}{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    @classmethod
    def post(cls, path, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{cls.base}{path}", data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_01_health(self):
        health = self.get("/api/health")
        self.assertTrue(health["demo"])
        self.assertTrue(health["seekdb"]["ok"])
        self.assertTrue(health["powercontext"]["ok"])

    def test_02_papers_seeded(self):
        papers = self.get("/api/papers")["papers"]
        self.assertGreaterEqual(len(papers), 4)
        self.assertTrue(all(paper["chunk_count"] > 0 for paper in papers))

    def test_03_ask(self):
        result = self.post(
            "/api/ask",
            {"question": "RRF 融合的 k 值怎么选？有实验数据吗？", "top_k": 6},
        )
        self.assertTrue(result["answer"])
        self.assertGreaterEqual(len(result["citations"]), 1)
        self.assertGreaterEqual(len(result["memories"]), 1)
        self.assertIn("Recall@10", result["answer"])

    def test_04_memory_roundtrip(self):
        created = self.post(
            "/api/memories",
            {"text": "测试记忆：每周五整理组会周报", "kind": "preference", "reason": "测试"},
        )
        self.assertTrue(created["saved"])
        memories = self.get("/api/memories")["memories"]
        self.assertTrue(any("周五" in item["text"] for item in memories))

    def test_05_weekly(self):
        result = self.get("/api/weekly?days=7")
        self.assertIn("科研周报", result["markdown"])
        self.assertIn("关键发现", result["markdown"])
        self.assertGreaterEqual(len(result["citations"]), 1)

    def test_06_logs(self):
        logs = self.get("/api/logs?limit=10")
        self.assertGreaterEqual(len(logs["retrieval"]), 1)
        self.assertGreaterEqual(len(logs["powercontext"]), 1)
        self.assertTrue(any("检索" in call["op"] or "记忆" in call["op"] for call in logs["powercontext"]))

    def test_07_paper_lifecycle(self):
        content = "# 临时测试文档\n\n## 摘要\n\n这是一篇用于接口测试的临时文档。\n"
        created = self.post("/api/papers", {"name": "temp-test.md", "content": content})
        self.assertGreaterEqual(created["chunks"], 1)
        paper = self.get(f"/api/papers/{created['paper_id']}")
        self.assertEqual(paper["title"], "临时测试文档")
        request = urllib.request.Request(
            f"{self.base}/api/papers/{created['paper_id']}", method="DELETE"
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            deleted = json.loads(response.read().decode("utf-8"))
        self.assertTrue(deleted["deleted"])

    def test_08_unknown_api(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.get("/api/not-exist")
        self.assertEqual(context.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
