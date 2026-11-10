from __future__ import annotations

import threading
from pathlib import Path

import pytest

from virgo.ai.providers import faux
from virgo.harness.session import AgentSession
from virgo.harness.ui import UI


class RecordingUI(UI):
    """Collects output; answers permission questions from a list (default: deny)."""

    def __init__(self, answers=None):
        self.lines: list[str] = []
        self.events = []
        self.questions: list[str] = []
        self.answers = list(answers or [])
        self.woken = threading.Event()
        self._lock = threading.Lock()

    def on_event(self, ev):
        with self._lock:
            self.events.append(ev)

    def print(self, text, style=None):
        with self._lock:
            self.lines.append(text)

    def confirm(self, question, agent="main"):
        with self._lock:
            self.questions.append(question)
            return self.answers.pop(0) if self.answers else "n"

    def wake(self):
        self.woken.set()

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("VIRGO_HOME", str(home))
    monkeypatch.delenv("VIRGO_MODEL", raising=False)
    faux.reset()
    yield
    faux.reset()


@pytest.fixture
def project(tmp_path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.fixture
def make_session(project):
    made = []

    def factory(*responses, ui=None, mode="yolo", persist=False, settings=None, **kw) -> AgentSession:
        faux.script("test", *responses)
        s = AgentSession(
            kw.pop("cwd", project),
            ui=ui or RecordingUI(),
            model="faux/test",
            session=kw.pop("session", "new" if persist else None),
            permission_mode=mode,
            settings=settings,
            **kw,
        )
        made.append(s)
        return s

    yield factory
    for s in made:
        s.shutdown()


def calls(model_id: str = "test") -> list[dict]:
    return faux.CALLS[model_id]
