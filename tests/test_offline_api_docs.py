"""Actual API docs work without a browser script, CDN, database or secrets."""

from copy import deepcopy
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from korsik import main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            testing=True,
            database_url="sqlite://",
            product_mode="CORE",
            generation_enabled=False,
        ),
    )

    def no_business_database():
        raise AssertionError("API_DOCS_MUST_NOT_READ_BUSINESS_DATA")

    monkeypatch.setattr(main, "SessionLocal", no_business_database)
    with TestClient(main.app) as value:
        yield value


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.assets = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.assets.extend(value for key, value in attrs if key in {"href", "src"})


def test_local_catalogue_and_same_origin_schema_need_no_external_assets(client):
    before = deepcopy(main.app.openapi())
    response = client.get("/api/docs")
    assert response.status_code == 200 and response.headers["content-type"].startswith(
        "text/html"
    )
    markup = Markup()
    markup.feed(response.text)
    assert "script" not in markup.tags and "iframe" not in markup.tags
    assert markup.assets == ["/api/openapi.json"]
    assert (
        "/api/v1/employees" in response.text and "Полная схема OpenAPI" in response.text
    )
    schema = client.get("/api/openapi.json")
    assert schema.status_code == 200 and schema.headers["content-type"].startswith(
        "application/json"
    )
    assert schema.json() == before == main.app.openapi()
    assert "/api/docs" not in schema.json()["paths"]
    assert client.get("/redoc").status_code == 404
    assert client.get("/docs").status_code == 404


def test_schema_and_operation_metadata_are_escaped_as_text(client, monkeypatch):
    hostile = '</pre><script src="https://example.invalid/x.js">alert(1)</script><img src="https://example.invalid/x">'
    schema = {
        "openapi": "3.1.0",
        "info": {"title": hostile, "version": hostile},
        "paths": {
            "/api/test/<img src=x onerror=alert(1)>": {"get": {"summary": hostile}},
        },
        "components": {"schemas": {"Example": {"description": hostile}}},
    }
    monkeypatch.setattr(main.app, "openapi", lambda: schema)
    response = client.get("/api/docs")
    markup = Markup()
    markup.feed(response.text)
    assert response.status_code == 200
    assert not {"script", "img", "iframe"} & set(markup.tags)
    assert markup.assets == ["/api/openapi.json"]
    assert "&lt;/pre&gt;&lt;script" in response.text
    assert "<script" not in response.text and "<img" not in response.text
    assert client.get("/api/openapi.json").json() == schema
