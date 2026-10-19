import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from ovo_insights.domain import Fuel
from ovo_insights.ovo.client import GRAPHQL_URL, LOGIN_URL, TOKEN_URL, USAGE_BASE, OvoClient
from ovo_insights.ovo.errors import (
    AuthFailed,
    BlockedByUpstream,
    RateLimited,
    ResponseTooLarge,
    Unauthorized,
    UpstreamSchemaChanged,
    UpstreamUnavailable,
)
from tests.ovo.conftest import ACCOUNT, NOW, PASSWORD, Sleeps, fixture, mock_login, token_body

pytestmark = pytest.mark.anyio

DAILY = f"{USAGE_BASE}/daily/{ACCOUNT}"
HALF_HOURLY = f"{USAGE_BASE}/half-hourly-local/{ACCOUNT}"


async def test_login_then_accounts(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    graphql = mock.post(GRAPHQL_URL).respond(200, json=fixture("bootstrap"))

    accounts = await client.accounts()

    assert [a.account_id for a in accounts] == [ACCOUNT]
    assert {p.fuel for p in accounts[0].supply_points} == {Fuel.ELECTRICITY, Fuel.GAS}
    assert client.customer_id == "11111111-2222-3333-4444-555555555555"
    assert client.token_account_ids == (ACCOUNT,)
    sent = graphql.calls.last.request
    assert sent.headers["authorization"].startswith("Bearer ey")
    body = json.loads(sent.content)
    assert body["variables"] == {"customerId": "11111111-2222-3333-4444-555555555555"}
    assert "#" not in body["query"]
    # Data minimisation: we never ask for addresses or other personal fields.
    for field in ("address", "postCode", "name", "email", "phone"):
        assert field.lower() not in body["query"].lower()


async def test_login_sends_expected_body(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)

    await client.login()

    body = json.loads(mock.routes[0].calls.last.request.content)
    assert body == {"username": "someone@example.com", "password": PASSWORD, "rememberMe": True}


async def test_token_request_carries_login_cookies_only_there(
    client: OvoClient, mock: respx.MockRouter
) -> None:
    mock_login(mock)
    daily = mock.get(DAILY).respond(200, json=fixture("daily"))

    await client.daily(ACCOUNT, 2025, 1)

    token_request = mock.routes[1].calls.last.request
    assert "restricted_refresh_token=REFRESH-SECRET" in token_request.headers["cookie"]
    assert token_request.headers["restricted_refresh_token"] == "REFRESH-SECRET"
    data_request = daily.calls.last.request
    assert "cookie" not in data_request.headers
    assert "restricted_refresh_token" not in data_request.headers


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_login_refused(client: OvoClient, mock: respx.MockRouter, status: int) -> None:
    mock.post(LOGIN_URL).respond(status, json={})

    with pytest.raises(AuthFailed, match=str(status)):
        await client.login()


async def test_login_200_with_unknown_code_is_a_failure(
    client: OvoClient, mock: respx.MockRouter
) -> None:
    mock.post(LOGIN_URL).respond(200, json={"code": "Unknown", "message": "nope"})

    with pytest.raises(AuthFailed, match="check email and password"):
        await client.login()


async def test_login_without_refresh_cookie_fails(
    client: OvoClient, mock: respx.MockRouter
) -> None:
    mock.post(LOGIN_URL).respond(200, json=fixture("login"))

    with pytest.raises(AuthFailed, match="no refresh cookie"):
        await client.login()


async def test_login_is_never_retried(client: OvoClient, mock: respx.MockRouter) -> None:
    route = mock.post(LOGIN_URL).respond(503)

    with pytest.raises(UpstreamUnavailable):
        await client.login()
    assert route.call_count == 1


async def test_token_refused(client: OvoClient, mock: respx.MockRouter) -> None:
    mock.post(LOGIN_URL).respond(
        200, json=fixture("login"), headers={"set-cookie": "restricted_refresh_token=R"}
    )
    mock.get(TOKEN_URL).respond(401)

    with pytest.raises(AuthFailed, match="Token request refused"):
        await client.login()


async def test_expiring_token_is_refreshed_before_use(
    client: OvoClient, mock: respx.MockRouter
) -> None:
    mock_login(mock, exp=NOW + timedelta(seconds=30))  # refresh margin: 7.5 s
    mock.get(DAILY).respond(200, json=fixture("daily"))

    await client.daily(ACCOUNT, 2025, 1)
    client._now = lambda: NOW + timedelta(seconds=25)  # now inside the margin
    await client.daily(ACCOUNT, 2025, 1)

    assert mock.routes[0].call_count == 1  # no second login
    assert mock.routes[1].call_count == 2  # initial token + proactive refresh


async def test_401_refreshes_token_then_succeeds(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    route = mock.get(DAILY)
    route.side_effect = [httpx.Response(401), httpx.Response(200, json=fixture("daily"))]

    rows = await client.daily(ACCOUNT, 2025, 1)

    assert rows
    assert mock.routes[0].call_count == 1  # no re-login needed
    assert mock.routes[1].call_count == 2


async def test_persistent_401_relogs_in_once_then_gives_up(
    client: OvoClient, mock: respx.MockRouter
) -> None:
    mock_login(mock)
    route = mock.get(DAILY).respond(401)

    with pytest.raises(Unauthorized):
        await client.daily(ACCOUNT, 2025, 1)

    assert mock.routes[0].call_count == 2  # initial login + exactly one re-login
    assert route.call_count == 3


async def test_cloudflare_challenge_is_blocked_and_not_retried(
    client: OvoClient, mock: respx.MockRouter
) -> None:
    mock_login(mock)
    route = mock.get(DAILY).respond(
        403,
        text="<html>Just a moment...</html>",
        headers={"cf-ray": "abc", "content-type": "text/html"},
    )

    with pytest.raises(BlockedByUpstream):
        await client.daily(ACCOUNT, 2025, 1)
    assert route.call_count == 1


async def test_plain_403_is_treated_as_blocked(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    mock.get(DAILY).respond(403, json={})

    with pytest.raises(BlockedByUpstream):
        await client.daily(ACCOUNT, 2025, 1)


async def test_5xx_is_retried_with_backoff_then_fails(
    client: OvoClient, mock: respx.MockRouter, sleeps: Sleeps
) -> None:
    mock_login(mock)
    route = mock.get(DAILY).respond(502)

    with pytest.raises(UpstreamUnavailable, match="502"):
        await client.daily(ACCOUNT, 2025, 1)
    assert route.call_count == 3
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 1
    assert 0 <= sleeps[1] <= 2


async def test_5xx_then_success(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    mock.get(DAILY).side_effect = [httpx.Response(500), httpx.Response(200, json=fixture("daily"))]

    assert await client.daily(ACCOUNT, 2025, 1)


async def test_timeout_is_retried(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    mock.get(DAILY).side_effect = [
        httpx.ReadTimeout("slow"),
        httpx.Response(200, json=fixture("daily")),
    ]

    assert await client.daily(ACCOUNT, 2025, 1)


async def test_429_with_short_retry_after_is_honoured(
    client: OvoClient, mock: respx.MockRouter, sleeps: Sleeps
) -> None:
    mock_login(mock)
    mock.get(DAILY).side_effect = [
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(200, json=fixture("daily")),
    ]

    assert await client.daily(ACCOUNT, 2025, 1)
    assert sleeps == [7.0]


async def test_a_sixty_second_retry_after_is_waited_out(
    client: OvoClient, mock: respx.MockRouter, sleeps: Sleeps
) -> None:
    # Seen on the first real backfill (2026-10-13).
    mock_login(mock)
    mock.get(DAILY).side_effect = [
        httpx.Response(429, headers={"retry-after": "60"}),
        httpx.Response(200, json=fixture("daily")),
    ]

    assert await client.daily(ACCOUNT, 2025, 1)
    assert sleeps == [60.0]


async def test_short_lived_tokens_are_not_refreshed_before_every_request(
    client: OvoClient, mock: respx.MockRouter
) -> None:
    # A token valid for 60 s used to be "due for refresh" on arrival, doubling every request.
    mock_login(mock, exp=NOW + timedelta(seconds=60))
    mock.get(DAILY).respond(200, json=fixture("daily"))

    for _ in range(3):
        await client.daily(ACCOUNT, 2025, 1)

    assert mock.routes[1].call_count == 1  # the token endpoint, once


async def test_429_slows_every_later_request(
    client: OvoClient, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    mock_login(mock)
    mock.get(DAILY).side_effect = [
        httpx.Response(429, headers={"retry-after": "1"}),
        httpx.Response(200, json=fixture("daily")),
    ]

    assert await client.daily(ACCOUNT, 2025, 1)
    assert client._limiter.min_interval == 1.0
    assert "Retry-After: 1 s); now at most one request every 1 s" in caplog.text


@pytest.mark.parametrize("retry_after", ["3600", "", "Wed, 21 Oct 2025 07:28:00 GMT"])
async def test_429_with_long_or_missing_retry_after_raises(
    client: OvoClient, mock: respx.MockRouter, retry_after: str
) -> None:
    mock_login(mock)
    route = mock.get(DAILY).respond(429, headers={"retry-after": retry_after})

    with pytest.raises(RateLimited) as caught:
        await client.daily(ACCOUNT, 2025, 1)
    assert route.call_count == 1
    assert ("asked to wait 3600 s" in str(caught.value)) == (retry_after == "3600")


async def test_redirects_are_not_followed(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    mock.get(DAILY).respond(302, headers={"location": "https://evil.example/steal"})
    evil = mock.get("https://evil.example/steal").respond(200)

    with pytest.raises(UpstreamSchemaChanged, match="302"):
        await client.daily(ACCOUNT, 2025, 1)
    assert evil.call_count == 0


async def test_404_reports_moved_endpoint(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    mock.get(DAILY).respond(404)

    with pytest.raises(UpstreamSchemaChanged, match="404"):
        await client.daily(ACCOUNT, 2025, 1)


@pytest.mark.parametrize(
    ("content", "content_type", "match"),
    [
        (b"<html></html>", "text/html", "Expected JSON"),
        (b"{not json", "application/json", "not valid JSON"),
        (
            b'{"electricity": {"data": [{"consumption": NaN}]}}',
            "application/json",
            "not valid JSON",
        ),
        (b'{"electricity": {"data": [{"consumption": "1"}]}}', "application/json", "shape"),
    ],
)
async def test_malformed_responses(
    client: OvoClient, mock: respx.MockRouter, content: bytes, content_type: str, match: str
) -> None:
    mock_login(mock)
    mock.get(DAILY).respond(200, content=content, headers={"content-type": content_type})

    with pytest.raises(UpstreamSchemaChanged, match=match):
        await client.daily(ACCOUNT, 2025, 1)


async def test_schema_errors_never_echo_values(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    mock.get(DAILY).respond(
        200, json={"electricity": {"data": [{"consumption": "SECRET-VALUE", "interval": {}}]}}
    )

    with pytest.raises(UpstreamSchemaChanged) as exc_info:
        await client.daily(ACCOUNT, 2025, 1)
    assert "SECRET-VALUE" not in str(exc_info.value)


async def test_oversized_response_is_refused(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    mock.get(DAILY).respond(200, headers={"content-length": str(50 * 1024 * 1024)}, content=b"{}")

    with pytest.raises(ResponseTooLarge):
        await client.daily(ACCOUNT, 2025, 1)


async def test_half_hourly_variants(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    local = mock.get(HALF_HOURLY).respond(200, json=fixture("half_hourly"))
    utc = mock.get(f"{USAGE_BASE}/half-hourly/{ACCOUNT}").respond(200, json=fixture("half_hourly"))

    rows = await client.half_hourly(ACCOUNT, date(2025, 1, 1))  # default: UK-day endpoint
    await client.half_hourly(ACCOUNT, date(2025, 1, 1), variant="half-hourly")

    assert len(rows) == 3
    assert local.calls.last.request.url.params["date"] == "2025-01-01"
    assert utc.call_count == 1


async def test_monthly_and_meter_data(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    mock.get(f"{USAGE_BASE}/monthly/{ACCOUNT}").respond(200, json=fixture("monthly"))
    mock.post(GRAPHQL_URL).respond(200, json=fixture("meter_reads"))

    months = await client.monthly(ACCOUNT, 2025)
    meters = await client.meter_data(ACCOUNT)

    assert len(months) == 3
    assert len(meters.readings) == 3


async def test_unknown_fields_are_logged_once_by_name(
    client: OvoClient, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    mock_login(mock)
    mock.get(DAILY).respond(200, json=fixture("daily"))

    with caplog.at_level(logging.WARNING):
        await client.daily(ACCOUNT, 2025, 1)
        await client.daily(ACCOUNT, 2025, 1)

    warnings = [r.getMessage() for r in caplog.records if "unrecognised field" in r.getMessage()]
    assert any("meterReadings" in w for w in warnings)
    assert not any("rates" in w or ".prev" in w for w in warnings)  # modelled or ignored
    assert len(warnings) == len(set(warnings))


async def test_secrets_never_reach_logs(
    client: OvoClient, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    mock_login(mock)
    route = mock.get(DAILY)
    route.side_effect = [httpx.Response(401), httpx.Response(200, json=fixture("daily"))]

    with caplog.at_level(logging.DEBUG):
        await client.daily(ACCOUNT, 2025, 1)

    text = caplog.text
    assert PASSWORD not in text
    assert "REFRESH-SECRET" not in text
    assert "eyJ" not in text


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda c: c.monthly(ACCOUNT, 1999), ValueError),
        (lambda c: c.daily(ACCOUNT, 2025, 13), ValueError),
        (lambda c: c.daily("../etc", 2025, 1), ValueError),
        (lambda c: c.half_hourly(ACCOUNT, datetime(2025, 1, 1, tzinfo=UTC)), TypeError),
        (lambda c: c.half_hourly(ACCOUNT, date(2025, 1, 1), variant="x"), ValueError),
    ],
)
async def test_bad_arguments_fail_before_any_request(
    client: OvoClient, mock: respx.MockRouter, call: object, error: type[Exception]
) -> None:
    mock_login(mock)
    await client.login()
    before = client.request_count

    with pytest.raises(error):
        await call(client)  # type: ignore[operator]
    assert client.request_count == before


def test_unknown_default_variant_is_rejected() -> None:
    from ovo_insights.ovo.auth import Credentials, Secret

    with pytest.raises(ValueError, match="variant"):
        OvoClient(Credentials("a@b.c", Secret("x")), half_hourly_variant="nope")


async def test_close_clears_tokens(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    await client.login()

    await client.aclose()

    assert client.customer_id is None
    assert client.token_account_ids == ()


def test_token_body_helper_is_a_jwt() -> None:
    assert token_body(NOW)["accessToken"]["value"].startswith("ey")


async def test_unexpected_login_status(client: OvoClient, mock: respx.MockRouter) -> None:
    mock.post(LOGIN_URL).respond(204)

    with pytest.raises(AuthFailed, match="Unexpected login response"):
        await client.login()


async def test_unexpected_token_status(client: OvoClient, mock: respx.MockRouter) -> None:
    mock.post(LOGIN_URL).respond(
        200, json=fixture("login"), headers={"set-cookie": "restricted_refresh_token=R"}
    )
    mock.get(TOKEN_URL).respond(204)

    with pytest.raises(AuthFailed, match="Unexpected token response"):
        await client.login()


async def test_customer_id_falls_back_to_token_subject(
    client: OvoClient, mock: respx.MockRouter
) -> None:
    mock.post(LOGIN_URL).respond(200, json={}, headers={"set-cookie": "restricted_refresh_token=R"})
    mock.get(TOKEN_URL).respond(200, json=token_body(NOW + timedelta(hours=1)))

    await client.login()

    assert client.customer_id == "cust-1"


async def test_login_fails_without_any_customer_id(
    client: OvoClient, mock: respx.MockRouter
) -> None:
    from tests.ovo.conftest import b64

    jwt = f"{b64({'alg': 'x'})}.{b64({'exp': int((NOW + timedelta(hours=1)).timestamp())})}.s"
    mock.post(LOGIN_URL).respond(200, json={}, headers={"set-cookie": "restricted_refresh_token=R"})
    mock.get(TOKEN_URL).respond(200, json={"accessToken": {"value": jwt}})

    with pytest.raises(AuthFailed, match="customer id"):
        await client.login()


async def test_failed_proactive_refresh_falls_back_to_login(
    client: OvoClient, mock: respx.MockRouter
) -> None:
    mock_login(mock, exp=NOW + timedelta(seconds=30))
    mock.get(DAILY).respond(200, json=fixture("daily"))
    await client.daily(ACCOUNT, 2025, 1)
    client._now = lambda: NOW + timedelta(seconds=25)  # inside the 7.5 s refresh margin
    mock.routes[1].side_effect = [
        httpx.Response(401),
        httpx.Response(200, json=token_body(NOW + timedelta(hours=1))),
    ]

    await client.daily(ACCOUNT, 2025, 1)

    assert mock.routes[0].call_count == 2  # refresh refused -> one fresh login


async def test_401_with_refused_refresh_relogs_in_once(
    client: OvoClient, mock: respx.MockRouter
) -> None:
    mock_login(mock)
    await client.login()
    mock.routes[1].side_effect = [
        httpx.Response(401),  # refresh refused
        httpx.Response(200, json=token_body(NOW + timedelta(hours=1))),  # token after re-login
    ]
    route = mock.get(DAILY)
    route.side_effect = [httpx.Response(401), httpx.Response(401)]

    with pytest.raises(Unauthorized):
        await client.daily(ACCOUNT, 2025, 1)
    assert mock.routes[0].call_count == 2
    assert route.call_count == 2


async def test_requests_need_a_session(client: OvoClient) -> None:
    with pytest.raises(AuthFailed, match="Not logged in"):
        await client._refresh_access_token()
    with pytest.raises(AuthFailed, match="Not logged in"):
        await client._send("GET", DAILY)


async def test_body_over_cap_without_content_length(
    client: OvoClient, mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_login(mock)
    await client.login()
    monkeypatch.setattr("ovo_insights.ovo.client.MAX_BODY_BYTES", 10)

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"electricity": null, '
        yield b'"gas": null}'

    # A streamed body has no Content-Length, so only the post-read check can catch it.
    mock.get(DAILY).side_effect = lambda request: httpx.Response(
        200, content=chunks(), headers={"content-type": "application/json"}
    )

    with pytest.raises(ResponseTooLarge):
        await client.daily(ACCOUNT, 2025, 1)
