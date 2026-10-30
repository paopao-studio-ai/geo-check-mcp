"""MCP server (stdio) exposing geo_check as tools.

Template pattern: keep the business logic in a plain module (geo_check.py) with no MCP imports,
then wrap each function as a small, typed tool here. Swap geo_check for any REST API client or
CSV/Airtable reader to build a different MCP server.
"""
from __future__ import annotations

import anyio

from . import geo_check

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

mcp = _Server("geo-check")


@mcp.tool()
def list_ai_crawlers() -> dict:
    """List the AI / search crawler user-agents this server checks, with operator and purpose."""
    return {"crawlers": [{"user_agent": k, "operator": v[0], "purpose": v[1]} for k, v in geo_check.AI_BOTS.items()],
            "citation_critical": geo_check.CITATION_CRITICAL}


@mcp.tool()
def check_robots(robots_txt: str, path: str = "/", user_agents: list[str] | None = None) -> dict:
    """Evaluate robots.txt text (no network) for AI crawlers.

    Args:
        robots_txt: full robots.txt content.
        path: URL path to test, e.g. "/" or "/blog/post".
        user_agents: crawler tokens to test; defaults to the built-in AI crawler list.
    """
    rb = geo_check.Robots.parse(robots_txt)
    uas = user_agents or list(geo_check.AI_BOTS)
    return {"path": path, "declared_sitemaps": rb.sitemaps, "results": {ua: rb.check(ua, path) for ua in uas}}


@mcp.tool()
async def check_site(url: str, format: str = "json") -> dict:
    """Run the full GEO technical check on a public website (robots.txt AI-crawler access,
    homepage JSON-LD types, /llms.txt, sitemap, title/description/canonical/h1).

    Makes about 4 polite requests (1 s apart, declared User-Agent, obeys robots.txt).

    Args:
        url: site URL, e.g. https://example.com
        format: "json" (structured report) or "markdown" (adds a human-readable report).
    """
    rep = await anyio.to_thread.run_sync(geo_check.run_check, url)
    rep.pop("request_log", None)
    if format == "markdown":
        rep["markdown"] = geo_check.to_markdown(rep)
    return rep


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
