"""Metron UPC lookup + aggregator UPC fan-out."""

from __future__ import annotations

import asyncio

import httpx
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import metron
from app.services.aggregator import lookup_full


def _client() -> TestClient:
    return TestClient(create_app())


METRON_LIST_PAYLOAD = {
    "count": 1,
    "next": None,
    "results": [{"id": 4242, "number": "1", "name": "Probed Issue"}],
}

METRON_ISSUE_PAYLOAD = {
    "id": 4242,
    "name": "Probed Issue",
    "number": "1",
    "image": "https://m/x.jpg",
    "series": {"name": "Probed Series", "publisher": {"name": "Probed Publisher"}},
    "credits": [],
    "upc": "76194131234500111",
}


@respx.mock
def test_metron_search_upc_round_trips_through_get_issue():
    """The list endpoint returns just IDs; we re-fetch each through the
    detail endpoint so creators / arcs / images are populated."""
    upc = "76194131234500111"
    respx.get(f"https://metron.cloud/api/issue/?upc={upc}").mock(
        return_value=httpx.Response(200, json=METRON_LIST_PAYLOAD)
    )
    respx.get("https://metron.cloud/api/issue/4242/").mock(
        return_value=httpx.Response(200, json=METRON_ISSUE_PAYLOAD)
    )
    with _client():
        pass
    out = asyncio.run(metron.search_upc(upc))
    assert len(out) == 1
    assert out[0].source == "metron"
    assert out[0].title == "Probed Issue"


@respx.mock
def test_metron_search_upc_falls_back_to_12_digit_prefix():
    upc_full = "76194131234500111"  # 17 digits
    upc_short = upc_full[:12]        # 761941312345

    # Full UPC misses, 12-digit prefix hits.
    respx.get(f"https://metron.cloud/api/issue/?upc={upc_full}").mock(
        return_value=httpx.Response(200, json={"count": 0, "next": None, "results": []})
    )
    respx.get(f"https://metron.cloud/api/issue/?upc={upc_short}").mock(
        return_value=httpx.Response(200, json=METRON_LIST_PAYLOAD)
    )
    respx.get("https://metron.cloud/api/issue/4242/").mock(
        return_value=httpx.Response(200, json=METRON_ISSUE_PAYLOAD)
    )
    with _client():
        pass
    out = asyncio.run(metron.search_upc(upc_full))
    assert len(out) == 1


@respx.mock
def test_aggregator_upc_fans_out_to_metron_and_wookieepedia():
    upc = "76194131234500111"
    respx.get(f"https://metron.cloud/api/issue/?upc={upc}").mock(
        return_value=httpx.Response(200, json=METRON_LIST_PAYLOAD)
    )
    respx.get("https://metron.cloud/api/issue/4242/").mock(
        return_value=httpx.Response(200, json=METRON_ISSUE_PAYLOAD)
    )
    # Wookieepedia returns no hits for this non-SW UPC.
    respx.get("https://starwars.fandom.com/api.php").mock(
        return_value=httpx.Response(200, json={"query": {"search": []}})
    )
    with _client():
        pass
    result = asyncio.run(lookup_full(upc))
    sources = [c.source for c in result.candidates]
    assert "metron" in sources
    assert not result.rate_limited


@respx.mock
def test_aggregator_upc_handles_metron_throttled():
    # Distinct UPC so cached responses from earlier tests don't bleed in.
    upc = "76194131234500999"
    respx.get(f"https://metron.cloud/api/issue/?upc={upc}").mock(
        return_value=httpx.Response(429, json={})
    )
    respx.get("https://starwars.fandom.com/api.php").mock(
        return_value=httpx.Response(200, json={"query": {"search": []}})
    )
    with _client():
        pass
    result = asyncio.run(lookup_full(upc))
    assert "metron" in result.rate_limited
