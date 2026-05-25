from __future__ import annotations

import json as json_module
import zipfile
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from spec2event.config import get_settings
from spec2event.db import Base, get_db
from spec2event.main import app


def test_builder_workbench_demo_flow(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RUNS_ROOT", str(tmp_path / "generated-runs"))
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("ENABLE_LOCAL_WORKER", "false")
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.test")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.setenv("LITELLM_MODEL", "claude-sonnet-4-6")
    get_settings.cache_clear()

    litellm_calls: list[str] = []

    def mock_litellm_post(url, headers, json: dict, timeout):  # noqa: ANN001
        del url, headers, timeout
        prompt = json["messages"][1]["content"]
        prompt_payload = json_module.loads(prompt)
        litellm_calls.append(prompt_payload["intent"])
        language = prompt_payload["transformLanguage"]
        file_path = {
            "dataweave": "src/main/resources/transforms/transform.dwl",
            "groovy": "src/main/resources/transforms/transform.groovy",
            "java_sdk": "src/main/resources/transforms/transform.md",
        }[language]
        file_content = {
            "dataweave": "%dw 2.0\noutput application/json\n---\npayload",
            "groovy": (
                "def transform(Map payload) { "
                "return payload + [sourceTopic: 'orders/created/v1'] }"
            ),
            "java_sdk": (
                "Use Jackson ObjectNode to map the captured payload "
                "to the canonical event."
            ),
        }[language]
        draft = {
            "transformLanguage": language,
            "intentSummary": "Map the order payload to a canonical order-created event.",
            "assistantMessage": "Claude Code designed the micro integration.",
            "qualifyingQuestions": [
                "Should invalid order payloads be rejected or routed to an error topic?"
            ],
            "topicMapping": {
                "inputTopic": "orders/created/v1",
                "outputTopic": "orders/canonical/created/v1",
            },
            "sampleOutput": {
                "sourceTopic": "orders/created/v1",
                "orderId": "ORD-1001",
                "status": "created",
            },
            "files": [
                {
                    "path": file_path,
                    "purpose": "Generated transform artifact",
                    "content": file_content,
                }
            ],
            "validationNotes": ["Mocked LiteLLM draft for tests."],
        }

        class MockResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"choices": [{"message": {"content": json_module.dumps(draft)}}]}

        return MockResponse()

    monkeypatch.setattr("spec2event.services.builder_service.httpx.post", mock_litellm_post)

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    testing_session_local = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    Base.metadata.create_all(engine)

    def override_get_db() -> Iterator[Session]:
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    admin_headers = {"X-Demo-Admin-Password": "test-password"}

    try:
        subscription_response = client.post(
            "/api/solace/subscriptions",
            headers=admin_headers,
            json={
                "brokerUrl": "demo://sample",
                "vpn": "default",
                "username": "demo",
                "password": "demo",
                "topicFilter": "orders/>",
            },
        )
        assert subscription_response.status_code == 200
        subscription_id = subscription_response.json()["id"]

        events_response = client.get(
            f"/api/solace/subscriptions/{subscription_id}/captured-events"
        )
        assert events_response.status_code == 200
        captured_event = events_response.json()[0]
        assert captured_event["topicName"] == "orders/created/v1"

        session_response = client.post(
            "/api/builder/sessions",
            headers=admin_headers,
            json={
                "capturedEventId": captured_event["id"],
                "transformLanguage": "dataweave",
            },
        )
        assert session_response.status_code == 200
        builder_session = session_response.json()
        assert builder_session["status"] == "awaiting_design"
        assert builder_session["draft"] is None

        design_response = client.post(
            f"/api/builder/sessions/{builder_session['id']}/design",
            headers=admin_headers,
            json={
                "content": "Design this as a DataWeave mapping first.",
                "transformLanguage": "dataweave",
            },
        )
        assert design_response.status_code == 200
        builder_session = design_response.json()
        assert builder_session["draft"]["transformLanguage"] == "dataweave"
        assert 2 <= len(builder_session["draft"]["qualifyingQuestions"]) <= 3
        assert builder_session["messages"] == []

        message_response = client.post(
            f"/api/builder/sessions/{builder_session['id']}/messages",
            headers=admin_headers,
            json={
                "content": "Map the order payload to a canonical order-created event.",
                "transformLanguage": "groovy",
            },
        )
        assert message_response.status_code == 200
        assert message_response.json()["transformLanguage"] == "groovy"

        preview_response = client.post(
            f"/api/builder/sessions/{builder_session['id']}/preview",
            headers=admin_headers,
        )
        assert preview_response.status_code == 200
        assert preview_response.json()["sampleOutput"]["sourceTopic"] == "orders/created/v1"
        assert litellm_calls == ["live_design", "chat_turn"]

        generate_response = client.post(
            f"/api/builder/sessions/{builder_session['id']}/generate",
            headers=admin_headers,
        )
        assert generate_response.status_code == 200
        assert litellm_calls == ["live_design", "chat_turn"]
        body = generate_response.json()
        assert body["run"]["status"] == "completed"
        assert body["workerJob"]["status"] == "pending"
        assert body["session"]["draft"]["generationMode"] == "fast_template_single_transform_file"

        artifact_response = client.get(f"/api/runs/{body['run']['id']}/artifacts")
        assert artifact_response.status_code == 200
        artifact_paths = {artifact["path"] for artifact in artifact_response.json()}
        assert "ai/claude-code-plan.json" in artifact_paths
        assert ".dockerignore" in artifact_paths
        assert ".env.example" in artifact_paths
        assert "docs/micro-integration.md" in artifact_paths
        assert "docs/event-portal-schema.md" in artifact_paths
        assert "docs/operations.md" in artifact_paths
        assert "event-portal/event-api.json" in artifact_paths
        assert "helm/templates/pdb.yaml" in artifact_paths
        assert "orders-canonical-created-v1.code-workspace" in artifact_paths
        assert ".vscode/settings.json" in artifact_paths
        assert ".vscode/extensions.json" in artifact_paths
        assert ".vscode/launch.json" in artifact_paths
        assert "src/main/resources/transforms/transform.groovy" in artifact_paths
        assert "src/main/resources/transforms/sample-output.json" in artifact_paths
        archive_response = client.get(f"/api/runs/{body['run']['id']}/workspace.zip")
        assert archive_response.status_code == 200
        with zipfile.ZipFile(BytesIO(archive_response.content)) as archive:
            names = set(archive.namelist())
        assert "pom.xml" in names
        assert "Dockerfile" in names
        assert ".dockerignore" in names
        assert ".env.example" in names
        assert "docs/micro-integration.md" in names
        assert "event-portal/event-api.json" in names
        assert "orders-canonical-created-v1.code-workspace" in names
        assert "src/main/resources/transforms/transform.groovy" in names
    finally:
        app.dependency_overrides.clear()


def test_builder_requires_litellm(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RUNS_ROOT", str(tmp_path / "generated-runs"))
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("ENABLE_LOCAL_WORKER", "false")
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_MODEL", raising=False)
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    testing_session_local = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    Base.metadata.create_all(engine)

    def override_get_db() -> Iterator[Session]:
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    admin_headers = {"X-Demo-Admin-Password": "test-password"}

    try:
        subscription_response = client.post(
            "/api/solace/subscriptions",
            headers=admin_headers,
            json={
                "brokerUrl": "demo://sample",
                "vpn": "default",
                "username": "demo",
                "password": "demo",
                "topicFilter": "orders/>",
            },
        )
        captured_event = client.get(
            f"/api/solace/subscriptions/{subscription_response.json()['id']}/captured-events"
        ).json()[0]
        session_response = client.post(
            "/api/builder/sessions",
            headers=admin_headers,
            json={
                "capturedEventId": captured_event["id"],
                "transformLanguage": "java_sdk",
            },
        )
        builder_session = session_response.json()
        message_response = client.post(
            f"/api/builder/sessions/{builder_session['id']}/messages",
            headers=admin_headers,
            json={"content": "Build a transformer.", "transformLanguage": "java_sdk"},
        )
        assert message_response.status_code == 400
        assert "LiteLLM is required" in message_response.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("language", "expected_path"),
    [
        ("java_sdk", "src/main/resources/transforms/transform.md"),
        ("groovy", "src/main/resources/transforms/transform.groovy"),
        ("dataweave", "src/main/resources/transforms/transform.dwl"),
    ],
)
def test_builder_generates_three_transform_options(
    monkeypatch, tmp_path: Path, language: str, expected_path: str
) -> None:
    monkeypatch.setenv("RUNS_ROOT", str(tmp_path / f"generated-runs-{language}"))
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("ENABLE_LOCAL_WORKER", "false")
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.test")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.setenv("LITELLM_MODEL", "claude-sonnet-4-6")
    get_settings.cache_clear()

    seen_schema_contexts: list[str | None] = []
    litellm_intents: list[str] = []

    def mock_litellm_post(url, headers, json: dict, timeout):  # noqa: ANN001
        del url, headers, timeout
        prompt_payload = json_module.loads(json["messages"][1]["content"])
        seen_schema_contexts.append(prompt_payload.get("schemaContext"))
        litellm_intents.append(prompt_payload["intent"])
        language_value = prompt_payload["transformLanguage"]
        content = {
            "java_sdk": "Map keep fields with Jackson ObjectNode.",
            "groovy": (
                "def transform(Map payload) { "
                "return payload.subMap(['orderId','customer','items','total']) }"
            ),
            "dataweave": (
                "%dw 2.0\n"
                "output application/json\n"
                "---\n"
                "{ orderId: payload.orderId, customer: payload.customer, "
                "items: payload.items, total: payload.total }"
            ),
        }[language_value]
        draft = {
            "transformLanguage": language_value,
            "intentSummary": (
                "Strip sensitive order fields and keep customer, items, total, orderId."
            ),
            "assistantMessage": "Claude Code generated the transform-only artifact.",
            "qualifyingQuestions": [],
            "topicMapping": {
                "inputTopic": "orders/created/v1",
                "outputTopic": "orders/public/created/v1",
            },
            "sampleOutput": {
                "orderId": "ORD-1001",
                "customer": {"id": "C-100"},
                "items": [{"sku": "SKU-1"}],
                "total": 42.5,
            },
            "files": [
                {
                    "path": expected_path,
                    "purpose": "Generated transform artifact",
                    "content": content,
                }
            ],
            "validationNotes": ["Schema keep rule respected."],
        }

        class MockResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"choices": [{"message": {"content": json_module.dumps(draft)}}]}

        return MockResponse()

    monkeypatch.setattr("spec2event.services.builder_service.httpx.post", mock_litellm_post)

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    testing_session_local = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    Base.metadata.create_all(engine)

    def override_get_db() -> Iterator[Session]:
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    admin_headers = {"X-Demo-Admin-Password": "test-password"}

    try:
        subscription_response = client.post(
            "/api/solace/subscriptions",
            headers=admin_headers,
            json={
                "brokerUrl": "demo://sample",
                "vpn": "default",
                "username": "demo",
                "password": "demo",
                "topicFilter": "orders/>",
            },
        )
        captured_event = client.get(
            f"/api/solace/subscriptions/{subscription_response.json()['id']}/captured-events"
        ).json()[0]
        session_response = client.post(
            "/api/builder/sessions",
            headers=admin_headers,
            json={
                "capturedEventId": captured_event["id"],
                "transformLanguage": language,
            },
        )
        builder_session = session_response.json()
        design_response = client.post(
            f"/api/builder/sessions/{builder_session['id']}/design",
            headers=admin_headers,
            json={
                "content": (
                    "Strip out sensitive data from ORDER and only return customer, "
                    "items and total."
                ),
                "transformLanguage": language,
                "schemaContext": '{"keep": ["customer", "items", "total", "orderId"]}',
            },
        )
        assert design_response.status_code == 200
        assert design_response.json()["draft"]["schemaContext"] is not None
        assert 2 <= len(design_response.json()["draft"]["qualifyingQuestions"]) <= 3
        generate_response = client.post(
            f"/api/builder/sessions/{builder_session['id']}/generate",
            headers=admin_headers,
        )
        assert generate_response.status_code == 200
        body = generate_response.json()
        assert litellm_intents == ["live_design"]
        assert body["session"]["draft"]["generationMode"] == "fast_template_single_transform_file"
        artifact_response = client.get(f"/api/runs/{body['run']['id']}/artifacts")
        artifact_paths = {artifact["path"] for artifact in artifact_response.json()}
        assert expected_path in artifact_paths
        assert any(context and "keep" in context for context in seen_schema_contexts)
    finally:
        app.dependency_overrides.clear()
