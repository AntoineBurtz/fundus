import importlib

import pytest


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Frische Datenbank + Datenordner pro Test, Worker aus."""
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "fundus.db")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "RUN_WORKER", False)
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")
    from app import db

    db.init()
    return tmp_path


@pytest.fixture()
def client(app_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
