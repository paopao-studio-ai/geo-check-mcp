#!/usr/bin/env python3
"""geo_check.py - AI-search (GEO) readiness check for one website.

Polite by design:
  * declares a User-Agent with a contact address
  * fetches robots.txt first and obeys it for its own user-agent
  * makes at most ~6 small GET requests, with a delay between them
  * no JavaScript execution, no crawling beyond the homepage

Checks:
  1. robots.txt access for AI crawlers (RFC 9309 group matching + longest-match rules)
  2. homepage JSON-LD schema.org types
  3. /llms.txt presence (with soft-404 detection)
  4. sitemap (declared in robots.txt, else /sitemap.xml)
  5. basic on-page items: title, meta description, canonical, h1, lang, meta robots,
     X-Robots-Tag, Open Graph

Usage:
  python geo_check.py https://example.com --json out.json --md out.md
Standard library only (Python 3.9+).
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser

__version__ = "0.1.0"

DEFAULT_UA = "GeoCheckBot/0.1 (+mailto:paopaomusic@mail.grokbot.com)"
UA_TOKEN = "GeoCheckBot"  # product token we obey in robots.txt
MAX_BYTES = 2_000_000
TIMEOUT = 15

# crawler -> (operator, purpose)
AI_BOTS: dict[str, tuple[str, str]] = {
    "GPTBot": ("OpenAI", "training"),
    "OAI-SearchBot": ("OpenAI", "search index (ChatGPT search)"),
    "ChatGPT-User": ("OpenAI", "user-triggered fetch"),
    "PerplexityBot": ("Perplexity", "search index"),
    "Perplexity-User": ("Perplexity", "user-triggered fetch"),
    "ClaudeBot": ("Anthropic", "training"),
    "Claude-SearchBot": ("Anthropic", "search index"),
    "Claude-User": ("Anthropic", "user-triggered fetch"),
    "Google-Extended": ("Google", "Gemini training/grounding opt-out token"),
    "Googlebot": ("Google", "search index (also AI Overviews)"),
    "Bingbot": ("Microsoft", "search index (Copilot, also feeds ChatGPT search)"),
    "Applebot-Extended": ("Apple", "Apple Intelligence training opt-out token"),
    "Amazonbot": ("Amazon", "search / Alexa"),
    "Meta-ExternalAgent": ("Meta", "training"),
    "CCBot": ("Common Crawl", "open dataset used for training"),
    "Bytespider": ("ByteDance", "training"),
}
# crawlers whose block most directly hurts being *cited* in AI answers
CITATION_CRITICAL = ["OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "Bingbot", "Googlebot", "Claude-SearchBot"]


# --------------------------------------------------------------------------- robots.txt
@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[str, str]] = field(default_factory=list)  # (allow|disallow, pattern)


@dataclass
class Robots:
    groups: list[Group]
    sitemaps: list[str]

    @classmethod
    def parse(cls, text: str) -> "Robots":
        groups: list[Group] = []
        sitemaps: list[str] = []
        cur: Group | None = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, val = line.split(":", 1)
            key, val = key.strip().lower(), val.strip()
            if key == "user-agent":
                if cur is None or not last_was_agent:
                    cur = Group()
                    groups.append(cur)
                cur.agents.append(val)
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if cur is not None:
                    cur.rules.append((key, val))
            elif key == "sitemap":
                sitemaps.append(val)  # sitemap lines are global
            else:
                last_was_agent = False  # crawl-delay etc.
        return cls(groups, sitemaps)

    def _groups_for(self, ua_token: str) -> tuple[list[Group], str]:
        """RFC 9309: merge all groups whose UA matches the token (case-insensitive);
        fall back to '*' groups; otherwise no rules apply."""
        tok = ua_token.lower()
        exact = [g for g in self.groups if any(a.lower() == tok for a in g.agents)]
        if exact:
            return exact, ua_token
        star = [g for g in self.groups if any(a.strip() == "*" for a in g.agents)]
        if star:
            return star, "*"
        return [], "(none)"

    @staticmethod
    def _match_len(pattern: str, path: str) -> int:
        """Return match length (specificity) or -1. Supports * and $."""
        if pattern == "":
            return -1
        regex = "".join(".*" if c == "*" else ("$" if c == "$" and i == len(pattern) - 1 else re.escape(c))
                        for i, c in enumerate(pattern))
        return len(pattern) if re.match(regex, path) else -1

    def check(self, ua_token: str, path: str = "/") -> dict:
        groups, matched = self._groups_for(ua_token)
        best: tuple[int, str, str] | None = None  # (len, kind, pattern)
        for g in groups:
            for kind, pat in g.rules:
                n = self._match_len(pat, path)
                if n < 0:
                    continue
                # longest match wins; on tie, allow wins
                if best is None or n > best[0] or (n == best[0] and kind == "allow" and best[1] != "allow"):
                    best = (n, kind, pat)
        allowed = True if best is None else best[1] == "allow"
        return {
            "allowed": allowed,
            "matched_group": matched,
            "deciding_rule": None if best is None else f"{best[1].capitalize()}: {best[2]}",
        }


# --------------------------------------------------------------------------- HTML parsing
class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self.meta: dict[str, str] = {}
        self.links: list[dict] = []
        self.h1: list[str] = []
        self._in_h1 = False
        self._h1_buf = ""
        self.lang = ""
        self.jsonld: list[str] = []
        self._in_jsonld = False
        self._jsonld_buf = ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link":
            self.links.append(a)
        elif tag == "h1":
            self._in_h1, self._h1_buf = True, ""
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_jsonld, self._jsonld_buf = True, ""

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "h1" and self._in_h1:
            self._in_h1 = False
            self.h1.append(re.sub(r"\s+", " ", self._h1_buf).strip())
        elif tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self.jsonld.append(self._jsonld_buf)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_h1:
            self._h1_buf += data
        if self._in_jsonld:
            self._jsonld_buf += data


def schema_types(blocks: list[str]) -> tuple[list[str], list[str]]:
    types: list[str] = []
    errors: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            t = o.get("@type")
            if isinstance(t, str):
                types.append(t)
            elif isinstance(t, list):
                types.extend(str(x) for x in t)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for i, b in enumerate(blocks):
        try:
            walk(json.loads(b))
        except json.JSONDecodeError as e:
            errors.append(f"block {i + 1}: invalid JSON ({e.msg})")
    seen: list[str] = []
    for t in types:
        if t not in seen:
            seen.append(t)
    return seen, errors


# --------------------------------------------------------------------------- HTTP
class Fetcher:
    def __init__(self, ua: str = DEFAULT_UA, delay: float = 1.0) -> None:
        self.ua, self.delay, self.count, self._last = ua, delay, 0, 0.0
        self.log: list[dict] = []

    def get(self, url: str) -> dict:
        wait = self.delay - (time.time() - self._last)
        if self._last and wait > 0:
            time.sleep(wait)
        self._last = time.time()
        self.count += 1
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept-Encoding": "gzip"})
        out = {"url": url, "status": None, "content_type": "", "final_url": url, "body": "", "headers": {}, "error": None}
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                out["status"] = r.status
                out["final_url"] = r.geturl()
                out["headers"] = {k.lower(): v for k, v in r.headers.items()}
                raw = r.read(MAX_BYTES)
        except urllib.error.HTTPError as e:
            out["status"] = e.code
            out["headers"] = {k.lower(): v for k, v in (e.headers or {}).items()}
            raw = e.read(MAX_BYTES) if hasattr(e, "read") else b""
        except Exception as e:  # network errors
            out["error"] = f"{type(e).__name__}: {e}"
            raw = b""
        if out["headers"].get("content-encoding") == "gzip" and raw:
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        out["content_type"] = out["headers"].get("content-type", "")
        out["body"] = raw.decode("utf-8", errors="replace")
        self.log.append({"url": url, "status": out["status"], "error": out["error"]})
        return out


def looks_like_html(resp: dict) -> bool:
    return "html" in resp["content_type"].lower() or resp["body"].lstrip()[:15].lower().startswith(("<!doctype", "<html"))


# --------------------------------------------------------------------------- main check
def run_check(site: str, ua: str = DEFAULT_UA, delay: float = 1.0) -> dict:
    if not re.match(r"^https?://", site):
        site = "https://" + site
    p = urllib.parse.urlparse(site)
    origin = f"{p.scheme}://{p.netloc}"
    home = site if p.path not in ("",) else origin + "/"
    f = Fetcher(ua, delay)
    findings: list[dict] = []

    def add(sev: str, area: str, msg: str, fix: str = "") -> None:
        findings.append({"severity": sev, "area": area, "finding": msg, "fix": fix})

    report: dict = {
        "tool": f"geo_check.py {__version__}",
        "site": origin,
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user_agent": ua,
    }

    # 1 robots.txt -------------------------------------------------------------
    r = f.get(origin + "/robots.txt")
    robots_ok = r["status"] == 200 and not looks_like_html(r)
    robots = Robots.parse(r["body"] if robots_ok else "")
    rob = {"url": origin + "/robots.txt", "status": r["status"], "present": robots_ok,
           "error": r["error"], "declared_sitemaps": robots.sitemaps, "bots": {}}
    if r["status"] and 500 <= r["status"] < 600:
        add("high", "robots.txt", f"robots.txt returns {r['status']}; crawlers may treat the whole site as disallowed.", "Make /robots.txt return 200 or 404.")
    elif not robots_ok:
        add("info", "robots.txt", "No robots.txt (or it is HTML); all crawlers are allowed by default.", "Optional: add a robots.txt that lists your sitemap.")
    for bot, (op, purpose) in AI_BOTS.items():
        res = robots.check(bot, "/")
        res.update({"operator": op, "purpose": purpose})
        rob["bots"][bot] = res
    blocked_crit = [b for b in CITATION_CRITICAL if not rob["bots"][b]["allowed"]]
    blocked_train = [b for b, v in rob["bots"].items() if not v["allowed"] and b not in CITATION_CRITICAL]
    if blocked_crit:
        add("high", "robots.txt", "Search/answer crawlers blocked from the homepage: " + ", ".join(blocked_crit),
            "Allow these user-agents if you want to be cited in AI answers.")
    else:
        add("ok", "robots.txt", "All search/answer crawlers checked may fetch the homepage (" + ", ".join(CITATION_CRITICAL) + ").")
    if blocked_train:
        add("info", "robots.txt", "Training-only crawlers blocked (a business choice, not an error): " + ", ".join(blocked_train))
    report["robots"] = rob
    self_ok = robots.check(UA_TOKEN, "/")["allowed"]

    def polite_get(url: str) -> dict | None:
        path = urllib.parse.urlparse(url).path or "/"
        if not robots.check(UA_TOKEN, path)["allowed"]:
            f.log.append({"url": url, "status": None, "error": "skipped: disallowed for GeoCheckBot by robots.txt"})
            return None
        return f.get(url)

    # 2 + 5 homepage ---------------------------------------------------------
    h = polite_get(home)
    page: dict = {"url": home}
    if h is None:
        page["skipped"] = "robots.txt disallows our checker on this path"
        add("info", "homepage", "Homepage not fetched: robots.txt disallows GeoCheckBot.")
    else:
        pp = PageParser()
        try:
            pp.feed(h["body"])
        except Exception:
            pass
        types, errs = schema_types(pp.jsonld)
        canon = next((l.get("href", "") for l in pp.links if "canonical" in l.get("rel", "").lower().split()), "")
        hreflang = [l.get("hreflang") for l in pp.links if l.get("hreflang")]
        page.update({
            "status": h["status"], "final_url": h["final_url"], "error": h["error"],
            "title": pp.title.strip(), "title_len": len(pp.title.strip()),
            "meta_description": pp.meta.get("description", ""),
            "meta_description_len": len(pp.meta.get("description", "")),
            "canonical": canon, "h1": pp.h1, "lang": pp.lang,
            "meta_robots": pp.meta.get("robots", ""),
            "x_robots_tag": h["headers"].get("x-robots-tag", ""),
            "og": {k: v for k, v in pp.meta.items() if k.startswith("og:")},
            "hreflang": hreflang,
            "jsonld_blocks": len(pp.jsonld), "schema_types": types, "jsonld_errors": errs,
            "html_bytes": len(h["body"].encode()),
            "visible_faq": bool(re.search(r'id=["\']faq["\']|<details[\s>]|frequently asked', h["body"], re.I)),
            "visible_text_chars": len(re.sub(r"\s+", " ", re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", h["body"], flags=re.S)).strip()),
        })
        if h["status"] != 200:
            add("high", "homepage", f"Homepage returned HTTP {h['status']}.")
        t = page["title_len"]
        if t == 0:
            add("high", "on-page", "Missing <title>.", "Add a descriptive title (30-60 chars).")
        elif t > 65:
            add("low", "on-page", f"Title is {t} chars; may be truncated.", "Aim for 30-60 characters.")
        else:
            add("ok", "on-page", f"Title present ({t} chars).")
        d = page["meta_description_len"]
        if d == 0:
            add("medium", "on-page", "Missing meta description.", "Add a 120-160 char summary of who you serve and what you sell.")
        elif d > 170:
            add("low", "on-page", f"Meta description is {d} chars; may be truncated.", "Aim for 120-160 characters.")
        else:
            add("ok", "on-page", f"Meta description present ({d} chars).")
        if not canon:
            add("low", "on-page", "No canonical link.", "Add <link rel=\"canonical\">.")
        else:
            add("ok", "on-page", f"Canonical: {canon}")
        if len(pp.h1) != 1:
            add("low", "on-page", f"Found {len(pp.h1)} <h1> tags (1 is typical).")
        else:
            add("ok", "on-page", "Exactly one <h1>.")
        if "og:image" not in page["og"]:
            add("low", "on-page", "No og:image; link previews (and some AI answer cards) show no image.", "Add an og:image (1200x630).")
        if not pp.lang:
            add("low", "on-page", "<html> has no lang attribute.")
        if "noindex" in (page["meta_robots"] + page["x_robots_tag"]).lower():
            add("high", "on-page", "Homepage is marked noindex.", "Remove noindex if you want to be found.")
        if not types:
            add("medium", "schema", "No JSON-LD structured data on the homepage.",
                "Add Organization / LocalBusiness / ProfessionalService JSON-LD with name, url, sameAs, contact.")
        else:
            add("ok", "schema", "JSON-LD types: " + ", ".join(types))
            if not any(x in types for x in ("Organization", "LocalBusiness", "ProfessionalService", "Corporation")) and \
               not any(x.endswith("Business") for x in types):
                add("low", "schema", "No Organization/LocalBusiness-type entity found.")
            if "FAQPage" not in types:
                if page["visible_faq"]:
                    add("medium", "schema", "Page has a visible FAQ section but no FAQPage schema.",
                        "Mark the existing questions/answers up as FAQPage JSON-LD (text must match what is visible).")
                else:
                    add("low", "schema", "No FAQPage schema. Q&A content is easy for answer engines to quote.",
                        "If you add a visible FAQ, mark it up as FAQPage.")
        for e in errs:
            add("medium", "schema", "JSON-LD parse error: " + e)
        if "sameas" not in json.dumps(pp.jsonld).lower():
            add("low", "schema", "No sameAs links (profiles that confirm the entity, e.g. LinkedIn, GitHub, directories).",
                "Add sameAs URLs to the Organization JSON-LD.")
        if page["visible_text_chars"] < 500:
            add("medium", "content", "Very little server-rendered text (<500 chars); AI crawlers mostly do not run JavaScript.",
                "Render key copy in the initial HTML.")
    report["homepage"] = page

    # 3 llms.txt -------------------------------------------------------------
    l = polite_get(origin + "/llms.txt")
    llms = {"url": origin + "/llms.txt"}
    if l is None:
        llms["present"] = None
        llms["note"] = "not fetched (robots)"
    else:
        present = l["status"] == 200 and not looks_like_html(l) and len(l["body"].strip()) > 0
        llms.update({"status": l["status"], "content_type": l["content_type"], "present": present,
                     "starts_with_h1": l["body"].lstrip().startswith("# ") if present else False,
                     "bytes": len(l["body"].encode()) if present else 0})
        if l["status"] == 200 and not present:
            llms["note"] = "200 but HTML (soft 404 / SPA fallback)"
        if present:
            add("ok", "llms.txt", "llms.txt present.")
        else:
            add("low", "llms.txt",
                "No /llms.txt. It is an emerging convention, not a standard; no major AI platform has confirmed it uses it for ranking.",
                "Optional, cheap: publish a short Markdown /llms.txt that lists your key pages.")
    report["llms_txt"] = llms

    # 4 sitemap --------------------------------------------------------------
    sm_url = robots.sitemaps[0] if robots.sitemaps else origin + "/sitemap.xml"
    sm = {"url": sm_url, "declared_in_robots": bool(robots.sitemaps)}
    s = polite_get(sm_url) if urllib.parse.urlparse(sm_url).netloc == p.netloc else None
    if s is None:
        sm["present"] = None
        sm["note"] = "not fetched (robots or other host)"
    else:
        body = s["body"]
        is_xml = s["status"] == 200 and ("<urlset" in body or "<sitemapindex" in body)
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body) if is_xml else []
        lastmods = re.findall(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", body) if is_xml else []
        sm.update({"status": s["status"], "present": is_xml, "is_index": "<sitemapindex" in body,
                   "url_count": len(locs), "sample_urls": locs[:10],
                   "latest_lastmod": max(lastmods) if lastmods else None})
        if is_xml:
            add("ok", "sitemap", f"Sitemap found with {len(locs)} URL(s)" + ("" if robots.sitemaps else " (not declared in robots.txt)") + ".")
            if not robots.sitemaps:
                add("low", "sitemap", "Sitemap not declared in robots.txt.", "Add 'Sitemap: <url>' to robots.txt.")
        else:
            add("medium", "sitemap", "No valid XML sitemap found.", "Publish /sitemap.xml and declare it in robots.txt.")
    report["sitemap"] = sm

    # manual items we deliberately do not automate
    report["not_checked"] = [
        "CDN/WAF bot blocking (e.g. Cloudflare 'Block AI bots'): needs the owner's dashboard or server logs; we do not spoof crawler user-agents.",
        "Pages other than the homepage (only the homepage is fetched).",
        "Whether any AI platform actually mentions or cites the brand: see the results log (manual / tool-assisted prompts).",
    ]
    report["requests_made"] = f.count
    report["request_log"] = f.log
    report["findings"] = findings
    report["robots_self_allowed"] = self_ok
    return report


# --------------------------------------------------------------------------- markdown
SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3, "ok": 4}


def to_markdown(rep: dict) -> str:
    L = [f"# GEO technical check: {rep['site']}", "",
         f"- Checked: {rep['checked_at_utc']} (UTC) with `{rep['tool']}`",
         f"- User-Agent: `{rep['user_agent']}`; requests made: {rep['requests_made']}", "",
         "> AI answers change over time; we do not guarantee being recommended. This check covers technical access only.", "",
         "## Findings", "", "| Severity | Area | Finding | Suggested fix |", "|---|---|---|---|"]
    for x in sorted(rep["findings"], key=lambda x: SEV_ORDER.get(x["severity"], 9)):
        L.append(f"| {x['severity']} | {x['area']} | {x['finding'].replace('|', '/')} | {x['fix'].replace('|', '/')} |")
    L += ["", "## AI crawler access (robots.txt, path `/`)", "",
          f"robots.txt: HTTP {rep['robots']['status']}", "",
          "| Crawler | Operator | Purpose | Allowed | Group used | Deciding rule |", "|---|---|---|---|---|---|"]
    for b, v in rep["robots"]["bots"].items():
        L.append(f"| {b} | {v['operator']} | {v['purpose']} | {'yes' if v['allowed'] else '**NO**'} | `{v['matched_group']}` | {v['deciding_rule'] or '(no matching rule → allowed)'} |")
    hp = rep["homepage"]
    if "title" in hp:
        L += ["", "## Homepage basics", "",
              f"- Title ({hp['title_len']}): {hp['title']}",
              f"- Meta description ({hp['meta_description_len']}): {hp['meta_description']}",
              f"- Canonical: {hp['canonical'] or '(none)'}",
              f"- H1: {' / '.join(hp['h1']) or '(none)'}",
              f"- lang: {hp['lang'] or '(none)'}; meta robots: {hp['meta_robots'] or '(none)'}; X-Robots-Tag: {hp['x_robots_tag'] or '(none)'}",
              f"- JSON-LD blocks: {hp['jsonld_blocks']}; schema types: {', '.join(hp['schema_types']) or '(none)'}",
              f"- Open Graph tags: {', '.join(hp['og']) or '(none)'}",
              f"- Server-rendered text: ~{hp['visible_text_chars']} chars"]
    ll = rep["llms_txt"]
    L += ["", "## llms.txt", "", f"- {ll['url']}: present = {ll.get('present')} (HTTP {ll.get('status')}{'; ' + ll['note'] if ll.get('note') else ''})"]
    sm = rep["sitemap"]
    L += ["", "## Sitemap", "", f"- {sm['url']} (declared in robots.txt: {sm['declared_in_robots']}): present = {sm.get('present')}, URLs = {sm.get('url_count')}, latest lastmod = {sm.get('latest_lastmod')}"]
    for u in sm.get("sample_urls", []):
        L.append(f"  - {u}")
    L += ["", "## Not checked automatically", ""] + [f"- {x}" for x in rep["not_checked"]]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("site")
    ap.add_argument("--json", help="write JSON report here")
    ap.add_argument("--md", help="write Markdown report here")
    ap.add_argument("--ua", default=DEFAULT_UA)
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests (min 0.5)")
    a = ap.parse_args(argv)
    rep = run_check(a.site, a.ua, max(0.5, a.delay))
    js = json.dumps(rep, indent=2, ensure_ascii=False)
    if a.json:
        open(a.json, "w", encoding="utf-8").write(js)
    if a.md:
        open(a.md, "w", encoding="utf-8").write(to_markdown(rep))
    if not a.json and not a.md:
        print(js)
    else:
        print(f"ok: {rep['site']} findings={len(rep['findings'])} requests={rep['requests_made']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
