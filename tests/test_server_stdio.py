"""Protocol-level self-test: start the server over stdio, initialize, list tools, call tools.
Uses raw JSON-RPC so it does not depend on the MCP client API version."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Client:
    def __init__(self):
        env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
        self.p = subprocess.Popen([sys.executable, "-m", "geo_check_mcp"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.i = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.i += 1
            msg["id"] = self.i
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        while True:
            line = self.p.stdout.readline()
            assert line, self.p.stderr.read()
            resp = json.loads(line)
            if resp.get("id") == self.i:
                return resp

    def close(self):
        self.p.stdin.close()
        self.p.terminate()
        self.p.wait(timeout=10)


@pytest.fixture(scope="module")
def client():
    c = Client()
    r = c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "selftest", "version": "0"}})
    assert "result" in r, r
    c.send("notifications/initialized", notify=True)
    yield c
    c.close()


def _payload(resp):
    res = resp["result"]
    assert not res.get("isError"), res
    if res.get("structuredContent") is not None:
        sc = res["structuredContent"]
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(res["content"][0]["text"])


def test_list_tools(client):
    names = {t["name"] for t in client.send("tools/list", {})["result"]["tools"]}
    assert {"check_site", "check_robots", "list_ai_crawlers"} <= names


def test_call_list_ai_crawlers(client):
    data = _payload(client.send("tools/call", {"name": "list_ai_crawlers", "arguments": {}}))
    assert any(c["user_agent"] == "OAI-SearchBot" for c in data["crawlers"])


def test_call_check_robots(client):
    txt = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\nSitemap: https://e.com/s.xml"
    data = _payload(client.send("tools/call", {"name": "check_robots", "arguments": {"robots_txt": txt}}))
    assert data["results"]["GPTBot"]["allowed"] is False
    assert data["results"]["PerplexityBot"]["allowed"] is True
    assert data["declared_sitemaps"] == ["https://e.com/s.xml"]


@pytest.mark.skipif(not os.environ.get("GEO_LIVE_URL"), reason="set GEO_LIVE_URL to run a live check")
def test_call_check_site_live(client):
    data = _payload(client.send("tools/call", {"name": "check_site", "arguments": {"url": os.environ["GEO_LIVE_URL"]}}))
    assert data["robots"]["status"] is not None and data["findings"]
