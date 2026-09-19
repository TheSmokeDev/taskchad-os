"""Readers cannot see an incompletely initialized learning database."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from personas.learning.models import LearningTarget
from personas.learning.service import LearningService


def test_initial_schema_is_published_atomically(tmp_path, monkeypatch):
    service = LearningService(LearningTarget("sales", tmp_path / "memory", tmp_path / "data", tmp_path / "state", tmp_path / "skills"))
    entered, release = Event(), Event()
    original = service.store._build_initial_database

    def slow_build(path):
        entered.set()
        assert release.wait(5)
        original(path)

    monkeypatch.setattr(service.store, "_build_initial_database", slow_build)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.capture_experience, "same", "test", "price discovery")
        assert entered.wait(5)
        try:
            assert service.store.get("missing") is None
            assert service.summary()["initialized"] is False
        finally:
            release.set()
        assert future.result()["kind"] == "experience"
    assert service.summary()["initialized"] is True
