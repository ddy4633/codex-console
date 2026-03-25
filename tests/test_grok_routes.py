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
