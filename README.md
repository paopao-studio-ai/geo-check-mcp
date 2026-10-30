# geo-check-mcp

An MCP server that checks whether a website is technically ready to be read and cited by AI search
(ChatGPT search, Perplexity, Copilot, Gemini, Claude). It runs over stdio and has no API keys.

**AI answers change over time; this tool does not and cannot guarantee that a site will be recommended.**
It checks the technical factors a site owner controls.

## Tools

| Tool | Network | What it does |
|---|---|---|
| `list_ai_crawlers` | no | Lists the 16 crawler user-agents checked (GPTBot, OAI-SearchBot, ChatGPT-User, PerplexityBot, Perplexity-User, ClaudeBot, Claude-SearchBot, Claude-User, Google-Extended, Googlebot, Bingbot, Applebot-Extended, Amazonbot, Meta-ExternalAgent, CCBot, Bytespider) with operator and purpose. |
| `check_robots` | no | Evaluates pasted robots.txt text for each crawler and path. Follows RFC 9309: user-agent group merging, a specific group beating `*`, longest match, Allow winning ties, and `*` / `$` wildcards. |
| `check_site` | yes | Full check of a public site: AI-crawler access, homepage JSON-LD types, `/llms.txt` (with soft-404 detection), sitemap, title, meta description, canonical, H1, lang, noindex and Open Graph. Returns JSON, and Markdown with `format="markdown"`. |

`check_site` is polite. It makes about 4 GET requests 1 second apart, sends a declared User-Agent
(`GeoCheckBot/0.1 (+mailto:...)`, which you can change in `geo_check.py`), obeys robots.txt for its own
user-agent, and fetches only the homepage. It does not spoof crawler user-agents, so CDN or firewall bot
blocking (for example Cloudflare "Block AI bots") is reported as "not checked".

## Install and run

```bash
pip install -e .            # needs Python 3.10+, mcp>=1.2 (works with SDK 1.x FastMCP and 2.x MCPServer)
geo-check-mcp               # stdio server
# or: python -m geo_check_mcp
```

Claude Desktop / Cursor config:

```json
{
  "mcpServers": {
    "geo-check": { "command": "geo-check-mcp" }
  }
}
```

Try it with MCP Inspector:

```bash
npx @modelcontextprotocol/inspector --cli geo-check-mcp --method tools/list
npx @modelcontextprotocol/inspector --cli geo-check-mcp --method tools/call --tool-name check_site --tool-arg url=https://example.com
```

The checker also works without MCP: `python src/geo_check_mcp/geo_check.py https://example.com --md out.md --json out.json`.

## Tests

```bash
pip install -e '.[test]'
pytest -q                                            # offline: robots parser + stdio protocol self-test
GEO_LIVE_URL=https://example.com pytest -q           # also runs one live check_site call
```

## Use this as a template for your own MCP server

The layout keeps business logic and the MCP layer apart:

```
src/geo_check_mcp/
  geo_check.py   # plain Python, no MCP imports; unit-testable
  server.py      # ~60 lines: one decorated function per tool
tests/
  test_robots.py         # logic tests
  test_server_stdio.py   # starts the server, sends raw JSON-RPC: initialize, tools/list, tools/call
```

To wrap a REST API, a CSV or Airtable base, or an internal service instead:

1. Replace `geo_check.py` with a client module (read secrets from environment variables, never hard-code them).
2. In `server.py`, write one small `@mcp.tool()` function per action, with type hints and a docstring. The SDK builds the JSON schema from them.
3. Run slow or blocking I/O in a thread (`anyio.to_thread.run_sync`) so the stdio loop stays responsive.
4. Update `test_server_stdio.py` with the new tool names and one offline call each.

## License

MIT, see [LICENSE](LICENSE).
