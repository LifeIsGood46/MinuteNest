"""J. Requests from other web pages (TEST_PLAN.md).

The app listens on 127.0.0.1, and any page the user has open can make the
browser send requests there: an <img src=...>, a form, a fetch. A page whose
domain is pointed at 127.0.0.1 (DNS rebinding) can even read the answers. Each
test sends exactly what such a page makes the browser send.
"""

import urllib.error
import urllib.request

import pytest

FROM_ANOTHER_SITE = [
    {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},    # a current browser
    {"Origin": "https://evil.example"},                                      # an older one
]


def _status(app, path: str, method: str = "GET", headers: dict | None = None) -> int:
    req = urllib.request.Request(f"{app.url}{path}", method=method, headers=headers or {},
                                 data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code                                   # the refusal is the answer under test


@pytest.mark.parametrize("headers", FROM_ANOTHER_SITE)
def test_another_sites_page_cannot_shut_the_app_down(app, headers):
    assert _status(app, "/api/shutdown", "POST", headers) == 403
    assert _status(app, "/api/version") == 200            # still running


def test_another_sites_page_cannot_make_the_app_fetch_anything(app, ollama_api):
    """An <img src> is enough: the app would list models at the page's address."""
    path = f"/api/llm/models?provider=ollama&host={ollama_api.base_url}"
    assert _status(app, path, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}) == 403
    assert ollama_api.authorizations == []                # nothing was asked of that address


def test_a_domain_pointed_at_this_pc_gets_no_answer(app):
    """DNS rebinding: the page's own domain now resolves to 127.0.0.1."""
    assert _status(app, "/api/settings", headers={"Host": f"evil.example:{app.port}"}) == 403


def test_the_app_still_answers_its_own_addresses_and_tools(app):
    assert _status(app, "/api/version") == 200                                       # scripts, the CLI
    assert _status(app, "/api/version", headers={"Sec-Fetch-Site": "same-origin"}) == 200
    assert _status(app, "/api/version", headers={"Host": f"localhost:{app.port}"}) == 200


def test_a_flood_of_refused_requests_does_not_flood_the_log(app):
    """A page can fire thousands of requests a second; the 2 MB log must keep the
    lines a bug report needs."""
    for _ in range(40):
        assert _status(app, "/api/shutdown", "POST", FROM_ANOTHER_SITE[0]) == 403
    log = (app.data_dir / "logs" / "minutenest.log").read_text(encoding="utf-8")
    assert log.count("refused POST") <= 20
