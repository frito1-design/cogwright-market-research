from cogwright_research.robots import RobotsRules, path_of

UA = "Cogwright-Research/1.0 (+https://cogwright.com/research; contact@cogwright.com)"


def test_wildcard_group_applies_when_no_specific_match():
    rules = RobotsRules.parse("User-agent: *\nDisallow: /admin\n")
    assert rules.can_fetch(UA, "/products.json")
    assert not rules.can_fetch(UA, "/admin/orders")


def test_named_group_beats_wildcard():
    rules = RobotsRules.parse(
        "User-agent: *\nDisallow: /\n\nUser-agent: Cogwright-Research\nAllow: /\nDisallow: /cart\n"
    )
    assert rules.can_fetch(UA, "/products.json")
    assert not rules.can_fetch(UA, "/cart")


def test_longest_match_wins_and_allow_breaks_ties():
    rules = RobotsRules.parse("User-agent: *\nDisallow: /a\nAllow: /a/b\n")
    assert not rules.can_fetch(UA, "/a/x")
    assert rules.can_fetch(UA, "/a/b/c")

    tie = RobotsRules.parse("User-agent: *\nDisallow: /x\nAllow: /x\n")
    assert tie.can_fetch(UA, "/x")


def test_empty_disallow_means_allow_all():
    rules = RobotsRules.parse("User-agent: *\nDisallow:\n")
    assert rules.can_fetch(UA, "/anything")


def test_wildcards_and_end_anchor():
    rules = RobotsRules.parse("User-agent: *\nDisallow: /*.json$\n")
    assert not rules.can_fetch(UA, "/products.json")
    assert rules.can_fetch(UA, "/products.json?page=2")
    assert rules.can_fetch(UA, "/products.html")


def test_crawl_delay_is_read_from_the_matching_group():
    rules = RobotsRules.parse(
        "User-agent: *\nCrawl-delay: 10\n\nUser-agent: Cogwright-Research\nCrawl-delay: 4\n"
    )
    assert rules.crawl_delay(UA) == 4.0
    assert rules.crawl_delay("SomeOtherBot") == 10.0


def test_comments_and_blank_lines_are_ignored():
    rules = RobotsRules.parse("# hello\nUser-agent: *  # all\nDisallow: /private # secret\n")
    assert not rules.can_fetch(UA, "/private")
    assert rules.can_fetch(UA, "/public")


def test_consecutive_user_agents_share_one_group():
    rules = RobotsRules.parse("User-agent: A\nUser-agent: Cogwright-Research\nDisallow: /nope\n")
    assert not rules.can_fetch(UA, "/nope")


def test_path_of_keeps_query_string():
    assert path_of("https://x.com/products.json?limit=250") == "/products.json?limit=250"
    assert path_of("https://x.com") == "/"
