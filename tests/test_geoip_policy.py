from app.services.geoip import is_country_allowed


def test_allowlist_allows_only_listed_country():
    assert is_country_allowed(
        mode="allowlist",
        allow_countries=["US", "CA"],
        block_countries=None,
        country_code="US",
    )
    assert not is_country_allowed(
        mode="allowlist",
        allow_countries=["US", "CA"],
        block_countries=None,
        country_code="JP",
    )


def test_blocklist_blocks_listed_country():
    assert not is_country_allowed(
        mode="blocklist",
        allow_countries=None,
        block_countries=["CN", "RU"],
        country_code="RU",
    )
    assert is_country_allowed(
        mode="blocklist",
        allow_countries=None,
        block_countries=["CN", "RU"],
        country_code="US",
    )
