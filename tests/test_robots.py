from geo_check_mcp.geo_check import Robots, schema_types


def chk(txt, ua, path="/"):
    return Robots.parse(txt).check(ua, path)["allowed"]


def test_empty_allows_all():
    assert chk("", "GPTBot")


def test_specific_group_overrides_star():
    txt = "User-agent: *\nDisallow: /\n\nUser-agent: OAI-SearchBot\nAllow: /\n"
    assert chk(txt, "OAI-SearchBot")
    assert not chk(txt, "GPTBot")


def test_star_not_merged_when_specific_exists():
    txt = "User-agent: GPTBot\nDisallow: /private\n\nUser-agent: *\nDisallow: /\n"
    assert chk(txt, "GPTBot", "/")
    assert not chk(txt, "GPTBot", "/private/x")


def test_grouped_user_agents_share_rules():
    txt = "User-agent: GPTBot\nUser-agent: ClaudeBot\nDisallow: /\n"
    assert not chk(txt, "GPTBot") and not chk(txt, "ClaudeBot")
    assert chk(txt, "PerplexityBot")


def test_case_insensitive_and_comments():
    txt = "user-agent: gptbot # openai\nDISALLOW: / # all\n"
    assert not chk(txt, "GPTBot")


def test_longest_match_and_allow_tie():
    txt = "User-agent: *\nDisallow: /blog\nAllow: /blog/public\n"
    assert not chk(txt, "Bingbot", "/blog/x")
    assert chk(txt, "Bingbot", "/blog/public/a")
    tie = "User-agent: *\nDisallow: /a\nAllow: /a\n"
    assert chk(tie, "Bingbot", "/a")


def test_wildcards_and_end_anchor():
    txt = "User-agent: *\nDisallow: /*.pdf$\n"
    assert not chk(txt, "Bingbot", "/files/x.pdf")
    assert chk(txt, "Bingbot", "/files/x.pdf?v=1")


def test_empty_disallow_means_allow():
    assert chk("User-agent: GPTBot\nDisallow:\n", "GPTBot")


def test_merges_repeated_groups_and_sitemaps():
    txt = "User-agent: GPTBot\nAllow: /\n\nSitemap: https://x/s.xml\nUser-agent: GPTBot\nDisallow: /a\n"
    rb = Robots.parse(txt)
    assert rb.sitemaps == ["https://x/s.xml"]
    assert not rb.check("GPTBot", "/a/b")["allowed"]


def test_schema_types_nested_and_errors():
    types, errs = schema_types(['{"@context":"https://schema.org","@graph":[{"@type":"Organization"},{"@type":["WebSite","Thing"]}]}', "{bad"])
    assert types == ["Organization", "WebSite", "Thing"]
    assert len(errs) == 1
