import hashlib
import hmac

from fastapi.testclient import TestClient

from src.config.settings import get_settings
from src.web.app import create_app


def _auth_cookie_value() -> str:
    settings = get_settings()
    secret = settings.webui_secret_key.get_secret_value().encode("utf-8")
    password = settings.webui_access_password.get_secret_value().encode("utf-8")
    return hmac.new(secret, password, hashlib.sha256).hexdigest()


def test_index_contains_openai_and_grok_tabs():
    client = TestClient(create_app())
    client.cookies.set("webui_auth", _auth_cookie_value())

    response = client.get("/")

    assert response.status_code == 200
    assert 'id="tab-openai"' in response.text
    assert 'id="tab-grok"' in response.text
    assert 'id="grok-form"' in response.text
    assert 'id="registration-form"' in response.text


def test_health_routes_support_get_and_head():
    client = TestClient(create_app())

    get_response = client.get("/healthz")
    head_response = client.head("/healthz")
    root_head_response = client.head("/")

    assert get_response.status_code == 200
    assert get_response.json() == {"status": "ok"}
    assert head_response.status_code == 200
    assert head_response.text == ""
    assert root_head_response.status_code == 200
    assert root_head_response.text == ""


def test_grok_route_redirects_to_index_tab():
    client = TestClient(create_app())
    client.cookies.set("webui_auth", _auth_cookie_value())

    response = client.get("/grok", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/?tab=grok"
