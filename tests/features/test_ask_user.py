"""ask_user: options with a recommendation, free text, dismissal, unattended timeouts, and the UIs."""

from __future__ import annotations

import asyncio
import sys
import threading

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.harness.ui import TIMED_OUT

from tests.support.harness import RecordingUI, calls, wait_until
from tests.support.terminal import PtyClient, plain
from tests.support.tui import form_is_ready, wait_for

ASK = {
    "question": "Which database should the service use?",
    "options": [
        {"label": "PostgreSQL", "description": "already used by the other services"},
        {"label": "SQLite"},
        {"label": "MySQL"},
    ],
}


class AskingUI(RecordingUI):
    interactive = True

    def __init__(self, picks=(), typed=()):
        super().__init__()
        self.picks, self.typed = list(picks), list(typed)
        self.asks: list[tuple] = []

    def select(self, question, options, *, timeout=None):
        self.asks.append(("select", question, list(options), timeout))
        pick = self.picks.pop(0)
        return options[pick] if isinstance(pick, int) else pick

    def input(self, question, *, password=False, default="", timeout=None):
        self.asks.append(("input", question, password, timeout))
        return self.typed.pop(0)


def ask_results(s) -> list[ToolResultMessage]:
    return [m for m in s.main.messages if isinstance(m, ToolResultMessage) and m.tool_name == "ask_user"]


def test_options_put_the_recommendation_first_and_add_a_custom_choice(make_session):
    ui = AskingUI(picks=[1])
    s = make_session(faux.reply(tools=[("ask_user", ASK)]), faux.reply("ok"), ui=ui)
    s.prompt("set up storage")
    (kind, question, options, timeout), = ui.asks
    assert kind == "select" and question == ASK["question"] and timeout is None
    assert options == [
        "A. PostgreSQL (recommended) — already used by the other services",
        "B. SQLite",
        "C. MySQL",
        "D. Type my own answer",
    ]
    (result,) = ask_results(s)
    assert not result.is_error and result.text() == "The user chose B: SQLite"
    assert result.details["choice"] == "B" and result.details["answered"] is True


def test_custom_choice_reads_free_text(make_session):
    ui = AskingUI(picks=[3], typed=["  DuckDB  "])
    s = make_session(faux.reply(tools=[("ask_user", ASK)]), faux.reply("ok"), ui=ui)
    s.prompt("set up storage")
    assert [a[0] for a in ui.asks] == ["select", "input"]
    assert ask_results(s)[0].text() == "The user answered: DuckDB"


def test_without_options_the_user_gets_an_input_box(make_session):
    ui = AskingUI(typed=["staging"])
    s = make_session(
        faux.reply(tools=[("ask_user", {"question": "Which environment?"})]), faux.reply("ok"), ui=ui
    )
    s.prompt("deploy")
    assert ui.asks == [("input", "Which environment?", False, None)]
    assert ask_results(s)[0].text() == "The user answered: staging"


def test_fewer_options_move_the_custom_choice_up(make_session):
    ui = AskingUI(picks=[0])
    args = {"question": "Keep the old API?", "options": [{"label": "Yes"}]}
    s = make_session(faux.reply(tools=[("ask_user", args)]), faux.reply("ok"), ui=ui)
    s.prompt("refactor")
    assert ui.asks[0][2] == ["A. Yes (recommended)", "B. Type my own answer"]


def test_dismissed_questions_are_not_errors(make_session):
    ui = AskingUI(picks=[None, 3], typed=["   "])
    s = make_session(
        faux.reply(tools=[("ask_user", ASK)]), faux.reply(tools=[("ask_user", ASK)]), faux.reply("ok"), ui=ui
    )
    s.prompt("set up storage")
    first, second = ask_results(s)
    for r in (first, second):
        assert not r.is_error and "dismissed" in r.text() and r.details["answered"] is False
        assert "choice" not in r.details and "answer" not in r.details


def test_too_many_options_are_rejected_before_asking(make_session):
    ui = AskingUI()
    args = {"question": "Pick", "options": [{"label": x} for x in "abcd"]}
    s = make_session(faux.reply(tools=[("ask_user", args)]), faux.reply("ok"), ui=ui)
    s.prompt("go")
    assert ui.asks == [] and ask_results(s)[0].is_error


def test_noninteractive_sessions_report_no_answer_without_a_prompt(make_session):
    ui = RecordingUI()
    s = make_session(faux.reply(tools=[("ask_user", ASK)]), faux.reply("ok"), ui=ui)
    s.prompt("set up storage")
    (result,) = ask_results(s)
    assert not result.is_error and "not interactive" in result.text()
    assert result.details["answered"] is False
    assert "choice" not in result.details and "answer" not in result.details


def test_only_the_lead_agent_gets_the_tool_and_plan_mode_allows_it(make_session):
    ui = AskingUI(picks=[0])
    s = make_session(faux.reply(tools=[("ask_user", ASK)]), faux.reply("ok"), ui=ui, mode="plan")
    assert s.tool("ask_user") is not None
    assert s.tool("ask_user", "subagent") is None and s.tool("ask_user", "teammate") is None
    s.prompt("plan storage")
    assert ask_results(s)[0].text() == "The user chose A: PostgreSQL"
    assert ui.questions == []  # no permission prompt
    assert "ask_user" in calls()[0]["tools"]
    assert "<asking>" not in calls()[0]["system"]


def test_runs_woken_by_notifications_time_out_without_an_answer(make_session):
    ui = AskingUI(picks=[TIMED_OUT, 0], typed=[TIMED_OUT])
    s = make_session(
        faux.reply(tools=[("ask_user", ASK)]),
        faux.reply(tools=[("ask_user", {"question": "Anything else?"})]),
        faux.reply("done"),
        faux.reply(tools=[("ask_user", ASK)]),
        faux.reply("done"),
        ui=ui,
        settings={"askUser": {"unattendedTimeout": 120}},
    )
    s.notify("check CI", mode="follow_up", source="schedule")
    assert s.run_pending()
    picked, typed = ask_results(s)
    assert ui.asks[0][3] == 120 and ui.asks[1][3] == 120
    assert "within 2 minutes" in picked.text()
    for result in (picked, typed):
        assert "did not answer" in result.text()
        assert result.details["timed_out"] is True
        assert result.details["answered"] is False
        assert "choice" not in result.details and "answer" not in result.details
    # A run the user started waits without a deadline.
    s.prompt("again")
    assert ui.asks[2][3] is None


def test_typing_into_an_unattended_run_removes_the_deadline(make_session):
    ui = AskingUI(picks=[0])
    steered = []

    def ask_while_user_steers(ctx):
        # The user types while the response that calls ask_user is still streaming: the message is
        # only queued, and is delivered to the model after the whole tool batch.
        steered.append(s.steer("I'm here"))
        return faux.reply(tools=[("ask_user", ASK)])

    s = make_session(ask_while_user_steers, faux.reply("ok"), ui=ui)
    s.notify("job finished", source="jobs")
    assert s.run_pending()
    assert steered == [True]
    assert ui.asks[0][3] is None


def test_fullscreen_questions_time_out_and_close_the_form():
    ui = FullscreenUI()
    got = []
    t = threading.Thread(target=lambda: got.append(ui.select("Pick", ["A", "B"], timeout=0.2)))
    t.start()
    t.join(3)
    assert got == [TIMED_OUT]
    kinds = [u.kind for u in ui.drain()]
    assert kinds == ["input", "input_cancelled"]


def test_fullscreen_answers_before_the_deadline_win():
    ui = FullscreenUI()
    got = []
    t = threading.Thread(target=lambda: got.append(ui.input("Name?", timeout=5)))
    t.start()
    requests = []
    wait_until(lambda: requests.extend(u.payload for u in ui.drain() if u.kind == "input") or requests)
    assert ui.resolve_input(requests[0].id, "Ada")
    t.join(3)
    assert got == ["Ada"]


REPL_SCRIPT = """
from virgo.ai.providers import faux
from virgo.cli.console import ConsoleBroker, TerminalUI
from virgo.cli.repl import run_repl
from virgo.harness.session import AgentSession

ask = {"question": "Which database?", "options": [{"label": "PostgreSQL"}, {"label": "SQLite"}]}

def answer(ctx):
    return faux.reply("model saw: " + ctx.messages[-1].text())

faux.script("t", faux.reply(tools=[("ask_user", ask)]), answer, faux.reply(tools=[("ask_user", ask)]), answer)
faux.handler("t", lambda ctx: faux.reply("echo: " + ctx.messages[-1].text()))
broker = ConsoleBroker()
ui = TerminalUI(broker)
session = AgentSession(ui=ui, model="faux/t", session=None, extensions=False, permission_mode="accept-all")
run_repl(session, ui, broker)
"""


@pytest.mark.pty
def test_repl_answers_questions_mid_run_and_keeps_reading_afterwards(project, tmp_path):
    with PtyClient(project, tmp_path / "home", [sys.executable, "-c", REPL_SCRIPT]) as terminal:
        terminal.wait_for(lambda data: " /help for commands" in plain(data))
        start = len(terminal.output)
        terminal.send(b"pick storage\r")
        terminal.wait_for(lambda data: "C. Type my own answer" in plain(data), start=start)
        terminal.send(b"3\r")
        terminal.wait_for(lambda data: "Which database?\n> " in plain(data).replace("\r", ""), start=start)
        terminal.send(b"DuckDB\r")
        terminal.wait_for(lambda data: "model saw: The user answered: DuckDB" in plain(data), start=start)

        # The REPL still reads prompts, and a later question in another run, normally.
        start = len(terminal.output)
        terminal.send(b"again\r")
        terminal.wait_for(lambda data: "C. Type my own answer" in plain(data), start=start)
        terminal.send(b"1\r")
        terminal.wait_for(lambda data: "model saw: The user chose A: PostgreSQL" in plain(data), start=start)
        start = len(terminal.output)
        terminal.send(b"after\r")
        terminal.wait_for(lambda data: "echo: after" in plain(data), start=start)
        terminal.send(b"/exit\r")
        assert terminal.finish() == 0


def test_fullscreen_app_answers_mid_run_and_withdraws_unattended_questions(make_session):
    async def scenario():
        ui = FullscreenUI()
        s = make_session(
            faux.reply(tools=[("ask_user", ASK)]),
            faux.reply("stored"),
            faux.reply(tools=[("ask_user", ASK)]),
            faux.reply("checked"),
            ui=ui,
            settings={"askUser": {"unattendedTimeout": 0.3}},
        )
        app = VirgoApp(s, ui)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                app.controller.submit("set up storage")
                await wait_for(pilot, lambda: form_is_ready(app, ASK["question"]))
                assert app.input_request.options[0].startswith("A. PostgreSQL (recommended)")
                await pilot.press("down", "enter")
                await wait_for(pilot, lambda: s.main.agent.last_text == "stored")
                assert ask_results(s)[0].text() == "The user chose B: SQLite"
                await wait_for(pilot, lambda: app.input_request is None and app.focused is app.editor)

                s.notify("check CI", mode="follow_up", source="schedule")
                await wait_for(pilot, lambda: s.main.agent.last_text == "checked", timeout=10)
                assert ask_results(s)[1].details["timed_out"] is True
                await wait_for(pilot, lambda: app.input_request is None and app.focused is app.editor)
                assert not app.input_requests
        finally:
            ui.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("options", [True, False])
def test_aborted_question_has_no_answer(make_session, options):
    from virgo.agent.types import ToolContext

    signal = threading.Event()
    ui = AskingUI(picks=[None], typed=[None])
    session = make_session(ui=ui)
    signal.set()
    args = ASK if options else {"question": "Where?"}
    result = session.tool("ask_user").execute(args, ToolContext("ask", session.cwd, signal))
    assert result.is_error and result.details["answered"] is False
    assert "choice" not in result.details and "answer" not in result.details


def test_answering_an_unattended_question_removes_future_deadlines(make_session):
    ui = AskingUI(picks=[0], typed=["more context"])
    session = make_session(
        faux.reply(tools=[("ask_user", ASK)]),
        faux.reply(tools=[("ask_user", {"question": "What else?"})]), faux.reply("done"), ui=ui,
    )
    session.notify("check", source="schedule")
    session.run_pending()
    assert ui.asks[0][3] == 300 and ui.asks[1][3] is None


def test_user_steering_keeps_an_already_displayed_question_open(make_session):
    ui = FullscreenUI()
    session = make_session(
        faux.reply(tools=[("ask_user", ASK)]), faux.reply("done"), ui=ui,
        settings={"askUser": {"unattendedTimeout": 0.3}},
    )
    session.notify("check", source="schedule")
    worker = threading.Thread(target=session.run_pending)
    worker.start()
    requests = []
    try:
        wait_until(lambda: requests.extend(u.payload for u in ui.drain() if u.kind == "input") or requests)
        assert session.steer("I'm here, let me decide")
        worker.join(0.5)  # Beyond the original deadline, while the user is deciding.
        assert worker.is_alive() and ui.is_input_pending(requests[0].id)
        assert ui.resolve_input(requests[0].id, requests[0].options[1])
        worker.join(3)
        assert not worker.is_alive()
        assert ask_results(session)[0].details["answer"] == "SQLite"
    finally:
        ui.close()
        worker.join(3)
