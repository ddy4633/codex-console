from contextlib import contextmanager
from pathlib import Path

from src.database.models import Account, Base
from src.database.session import DatabaseSessionManager
from src.web.routes import grok as grok_routes


def test_task_to_response_splits_logs():
    task = type(
        "Task",
        (),
        {
            "id": 1,
            "task_uuid": "task-1",
            "status": "completed",
            "email_service_id": 9,
            "proxy": "http://127.0.0.1:7890",
            "logs": "line-1\nline-2",
            "result": {"success": True},
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "completed_at": None,
        },
    )()

    response = grok_routes.task_to_response(task)

    assert response.task_uuid == "task-1"
    assert response.logs == ["line-1", "line-2"]
    assert response.result == {"success": True}


def test_recent_grok_accounts_only_returns_grok(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "grok_routes.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add(
            Account(
                email="grok@example.com",
                password="secret",
                email_service="grok",
                source="grok_register",
                status="active",
            )
        )
        session.add(
            Account(
                email="openai@example.com",
                password="secret",
                email_service="tempmail",
                source="register",
                status="active",
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(grok_routes, "get_db", fake_get_db)

    result = __import__("asyncio").run(grok_routes.get_recent_grok_accounts(limit=10))

    assert len(result["accounts"]) == 1
    assert result["accounts"][0]["email"] == "grok@example.com"
    assert result["accounts"][0]["source"] == "grok_register"


def test_get_grok_defaults_uses_database_and_global_config(monkeypatch):
    class DummySetting:
        def __init__(self, value):
            self.value = value

    settings = type("Settings", (), {"registration_default_password_length": 16})

    @contextmanager
    def fake_get_db():
        yield None

    monkeypatch.setattr(grok_routes, "get_db", fake_get_db)
    monkeypatch.setattr(grok_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(grok_routes, "get_proxy_for_grok_registration", lambda _db: ("http://127.0.0.1:7890", None))

    values = {
        "grok.default_password": "db-password",
        "grok.vibemail_user_jwt": "jwt-token",
        "grok.vibemail_api": "https://tmpmail.example.com",
    }

    def _fake_setting(_db, key, default=""):
        return values.get(key, default)

    monkeypatch.setattr(grok_routes, "_get_db_setting", lambda _, key, default="": _fake_setting(None, key, default))

    result = __import__("asyncio").run(grok_routes.get_grok_defaults())

    assert result["proxy"] == "http://127.0.0.1:7890"
    assert result["default_password"] == "db-password"
    assert result["vibemail_user_jwt"] == "jwt-token"
    assert result["vibemail_api"] == "https://tmpmail.example.com"
    assert result["password_length"] == 16
