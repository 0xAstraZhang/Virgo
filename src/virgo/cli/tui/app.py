"""Single-column, terminal-native fullscreen Virgo application."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import Click
from textual.theme import Theme
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ... import __version__
from ...ai.types import Usage
from ...harness.permissions import MODES, normalize_permission_mode, permission_mode_label
from ...harness.preferences import PreferenceChoice
from ...harness.session import AgentSession
from ...harness.store import SessionStore
from ..i18n import tr
from .activity import RunActivity
from .bridge import FullscreenUI
from .choices import ChoiceList, CommandChoice
from .controller import RunController
from .dialogs import ApprovalPicker, SessionPicker
from .editor import ChatEditor, InputHistory
from .forms import MultilineInput
from .preferences import PreferencePicker
from .schedules import ScheduleWorkspace
from .selection import SelectionCopyMixin
from .sessions import workspace_name
from .transcript import EventView, Transcript, update_static

PURPLE = "#7C3AED"
PERMISSION_COLORS = {"auto-review": "#B8860B", "accept-all": "#DC2626", "plan": "#2563EB"}


def terminal_theme() -> Theme:
    return Theme(
        "virgo-terminal",
        primary=PURPLE,
        accent=PURPLE,
        secondary="ansi_cyan",
        warning="ansi_yellow",
        error="ansi_red",
        success="ansi_green",
        foreground="ansi_default",
        background="ansi_default",
        surface="ansi_default",
        panel="ansi_default",
        boost="ansi_default",
        ansi=True,
        variables={
            "ansi-background": "ansi_default",
            "ansi-foreground": "ansi_default",
            "foreground-muted": "ansi_default",
            "foreground-disabled": "ansi_default",
            "input-cursor-background": "ansi_default",
            "input-cursor-foreground": "ansi_default",
            "input-cursor-text-style": "reverse",
            "input-selection-background": "ansi_default",
            "input-selection-foreground": "ansi_default",
            "block-cursor-background": "ansi_default",
            "block-cursor-foreground": "ansi_default",
            "block-cursor-text-style": "reverse",
            "border": PURPLE,
            "border-blurred": PURPLE,
            "scrollbar": PURPLE,
            "scrollbar-hover": PURPLE,
            "scrollbar-active": PURPLE,
            "scrollbar-background": "ansi_default",
            "scrollbar-background-hover": "ansi_default",
            "scrollbar-background-active": "ansi_default",
            "scrollbar-corner-color": "ansi_default",
        },
    )


def short_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def shortcut_hints(text: str) -> Text:
    hint = Text(text)
    for key in re.finditer(
        r"(?<!\S)(?:Ctrl\+[A-Z]|Shift\+(?:Enter|Tab)|Enter|Esc|Backspace|↑↓|←→|/exit|/|s)(?=\s|$)", text
    ):
        hint.stylize("bold", key.start(), key.end())
    return hint


def input_hints(owner=None) -> Text:
    newline = "Ctrl+J" if os.environ.get("TERM_PROGRAM") == "Apple_Terminal" else "Shift+Enter"
    return shortcut_hints(tr(owner, "ui.hints.input", newline=newline))


class ConversationPane(Vertical):
    """Keep a live conversation's widgets and buffered updates when it is hidden."""

    def __init__(self, controller: RunController, history: InputHistory):
        super().__init__()
        self.controller = controller
        self.history = history
        self.state: dict = {}
        self.updates = deque()

    @property
    def key(self):
        store = self.controller.session.store
        return store.path if store.path is not None else f"memory:{store.id}"

    def compose(self) -> ComposeResult:
        yield Static("", id="cwd", markup=False)
        yield Static(input_hints(self), id="hints", markup=False)
        yield ScheduleWorkspace(id="schedule-workspace")
        with Vertical(id="body"):
            yield Transcript(id="transcript")
        with Vertical(id="dock"):
            yield Static(tr(self, "ui.latest"), id="jump")
            yield Static("", id="pending", markup=False)
            yield ApprovalPicker(id="approval")
            yield PreferencePicker(id="preferences")
            yield Static("", id="auth-question", markup=False)
            with VerticalScroll(id="input-detail-scroll"):
                yield Static("", id="input-details", markup=False)
            yield ChoiceList(id="completions")
            yield Input(id="auth-value")
            yield MultilineInput(id="input-multiline")
            yield Static("", id="metrics", markup=False)
            yield ChatEditor(self.history)
            yield Static("", id="status", markup=False)


class VirgoApp(SelectionCopyMixin, App[int]):
    ENABLE_COMMAND_PALETTE = False
    TITLE = "Virgo"
    CSS = """
    * { scrollbar-size-vertical: 0; }
    Screen { background: $background; color: $foreground; }
    #brand { height: 1; margin: 0 1; }
    #cwd { height: 1; margin: 0 1; text-style: dim; }
    ConversationPane { width: 100%; height: 1fr; }
    #body { height: 1fr; }
    Transcript { height: 1fr; }
    #dock { height: auto; padding: 0 1; background: $background; }
    #pending { height: auto; max-height: 3; text-style: dim; }
    #jump { height: 1; min-height: 1; border: none; width: auto; padding: 0 1;
        color: #7C3AED; background: $background; text-style: bold; }
    #completions { height: auto; max-height: 6; border: solid #7C3AED;
        padding: 0; background: $background; }
    #completions > .option-list--option-highlighted,
    #completions:focus > .option-list--option-highlighted {
        background: #7C3AED; color: #FFFFFF; text-style: bold; }
    #auth-question { height: auto; max-height: 3; color: #7C3AED; text-style: bold; }
    #auth-value { height: 3; border: solid #7C3AED; padding: 0;
        background: $background; color: $foreground; }
    #auth-value:focus { border: solid #7C3AED; }
    #input-detail-scroll { display: none; height: 30vh; min-height: 3; max-height: 12;
        border: solid #7C3AED; padding: 0 1; scrollbar-size-vertical: 1; }
    #input-details { height: auto; }
    #input-multiline { display: none; height: 7; max-height: 35vh; min-height: 3;
        border: solid #7C3AED; padding: 0; background: $background; color: $foreground;
        scrollbar-size-vertical: 1; }
    #editor { height: 3; min-height: 3; border: none; border-top: solid #7C3AED;
        border-bottom: solid #7C3AED; padding: 0; background: $background; color: $foreground; }
    #editor:focus { border: none; border-top: solid #7C3AED; border-bottom: solid #7C3AED; }
    TextArea > .text-area--cursor { text-style: reverse; background: $background; color: $foreground; }
    TextArea > .text-area--selection { text-style: reverse; background: $background; color: $foreground; }
    #metrics { height: 1; text-align: right; text-style: dim; }
    #status { height: 1; text-align: left; }
    #hints { height: auto; max-height: 2; margin: 0 1; text-style: dim; }
    ToastRack { dock: top; margin: 0 1; }
    Toast { width: auto; max-width: 50%; padding: 0 1; margin: 0;
        border: none; color: #FFFFFF; background: #7C3AED; }
    OptionList { background: $background; color: $foreground; }
    OptionList > .option-list--option-highlighted { background: $background;
        color: $foreground; text-style: reverse; }
    #schedule-list > .option-list--option-highlighted,
    #schedule-list:focus > .option-list--option-highlighted,
    #schedule-options > .option-list--option-highlighted,
    #schedule-options:focus > .option-list--option-highlighted {
        background: #7C3AED; color: #FFFFFF; text-style: bold; }
    #approval-options > .option-list--option-highlighted,
    #approval-options:focus > .option-list--option-highlighted {
        background: $background; color: #7C3AED; text-style: bold; }
    ModalScreen { align: center middle; background: $background; }
    """
    # State associated with the visible pane; widgets keep drafts, selections,
    # scroll positions and partially completed forms without answering them.
    CONVERSATION_STATE = (
        "activity",
        "history",
        "confirmations",
        "active_confirmation",
        "input_requests",
        "active_input",
        "input_request",
        "completion_names",
        "completion_query",
        "completion_suppressed",
        "last_interrupt",
    )
    BINDINGS = [
        Binding("ctrl+c", "interrupt", priority=True, show=False),
        Binding("ctrl+d", "eof", priority=True, show=False),
        Binding("escape", "escape", priority=True, show=False),
        Binding("ctrl+o", "tools", priority=True, show=False),
        Binding("ctrl+t", "thinking", priority=True, show=False),
        Binding("pageup", "page(-1)", priority=True, show=False),
        Binding("pagedown", "page(1)", priority=True, show=False),
        Binding("ctrl+home", "top", priority=True, show=False),
        Binding("shift+tab", "cycle_permissions", priority=True, show=False),
        Binding("ctrl+r", "sessions", priority=True, show=False),
    ]

    def __init__(self, session, ui: FullscreenUI, initial: str | None = None, pick_session=False):
        super().__init__(ansi_color=True)
        self.session = session
        self.ui = ui
        self.controller = RunController(session, ui)
        self.activity = RunActivity(session.translator)
        self.initial = initial
        self.pick_session = pick_session
        self.session_grouped = session.settings.get("tui.sessionGrouping", "workspace") != "none"
        self._session_picker: SessionPicker | None = None
        self.exit_code = 0
        self.history = InputHistory(session.paths.user)
        self.register_theme(terminal_theme())
        self.theme = "virgo-terminal"
        self.confirmations = deque()
        self.active_confirmation = None
        self.input_requests = deque()
        self.active_input = None
        self.input_request = None
        self.completion_names: list[str] = []
        self.completion_query: str | None = None
        self.completion_suppressed: str | None = None
        self.last_interrupt = 0.0
        self.last_status = 0.0
        self._polling = False
        self._pane = ConversationPane(self.controller, self.history)
        self._panes = [self._pane]
        self._pending_switch = None

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble(("V_", f"bold {PURPLE}"), (f" Virgo {__version__}", "dim")), id="brand")
        yield self._pane

    @property
    def editor(self) -> ChatEditor:
        return self._pane.query_one(ChatEditor)

    @property
    def transcript(self) -> Transcript:
        return self._pane.query_one(Transcript)

    @property
    def has_modal(self) -> bool:
        return len(self.screen_stack) > 1

    def on_mount(self) -> None:
        for selector in ("#jump", "#pending", "#completions", "#auth-question", "#auth-value"):
            self._pane.query_one(selector).display = False
        self.editor.focus()
        self._poll_timer = self.set_interval(1 / 30, self.poll)
        self.update_status()
        self.controller.start(None if self.pick_session else self.initial)
        if self.pick_session:
            self.action_sessions()

    def action_sessions(self) -> None:
        if self.controller.closing or self.has_modal:
            return
        if self.active_input is None:
            self.hide_completions()
        self.controller.browsing = True
        picker = SessionPicker(
            self.session_browser_items(),
            current_path=self._pane.key,
            grouped=self.session_grouped,
            current_status=self.session_browser_status(),
        )
        self._session_picker = picker
        self.push_screen(picker, self.session_picked)

    def session_browser_items(self) -> list[dict]:
        items = SessionStore.list_all(self.session.paths.user / "sessions")
        retained = {
            store.path: store.describe()
            for pane in self._panes
            for store in pane.controller.session.retained_stores()
        }
        items = [item for item in items if item["path"] not in retained] + list(retained.values())
        for pane in self._panes:
            controller = pane.controller
            path = controller.session.store.path
            if self.pick_session and pane is self._pane:
                continue
            item = next((item for item in items if path is not None and item["path"] == path), None)
            if item is None:
                item = controller.session.store.describe()
                items.append(item)
            elif controller.session.store.save_error is not None:
                item.update(controller.session.store.describe())
            item["status"] = self.pane_status(pane)
        statuses = self.session_browser_statuses()
        for item in items:
            item["status"] = statuses.get(str(item["path"]), item.get("status", tr(self, "ui.state.saved")))
        return items

    def session_browser_statuses(self) -> dict[str, str]:
        statuses = {
            str(store.path): tr(self, "ui.state.unsaved")
            if store.save_error is not None
            else tr(self, "ui.state.saved")
            for pane in self._panes
            for store in pane.controller.session.retained_stores()
        }
        statuses.update({str(pane.key): self.pane_status(pane) for pane in self._panes})
        return statuses

    def can_change_session(self) -> bool:
        return (
            self.controller.can_switch
            and not self.ui.has_pending_requests
            and self.active_input is None
            and self.active_confirmation is None
            and not self.input_requests
            and not self.confirmations
            and not self.session.has_save_failure
            and not any(self.session.bus.emit("pending_work", self.session))
        )

    def session_browser_status(self) -> str:
        if self.session.store.save_error is not None:
            return tr(self, "ui.state.unsaved")
        if (
            self.active_input is not None
            or self.active_confirmation is not None
            or self.input_requests
            or self.confirmations
        ):
            return tr(self, "ui.state.waiting")
        return tr(self, "ui.state.ready") if self.controller.can_switch else tr(self, "ui.state.running")

    def pane_status(self, pane: ConversationPane) -> str:
        if pane.controller.session.store.save_error is not None:
            return tr(self, "ui.state.unsaved")
        if pane is self._pane:
            return self.session_browser_status()
        if pane.controller.ui.has_pending_requests:
            return tr(self, "ui.state.waiting")
        return tr(self, "ui.state.ready") if pane.controller.can_switch else tr(self, "ui.state.running")

    @on(SessionPicker.GroupingChanged)
    def session_grouping_changed(self, event: SessionPicker.GroupingChanged) -> None:
        self.session_grouped = event.grouped
        try:
            self.session.settings.save_user(
                {"tui": {"sessionGrouping": "workspace" if event.grouped else "none"}}
            )
        except (OSError, ValueError) as error:
            self.notify(tr(self, "ui.session.view_save_error", error=error), severity="error")

    @on(SessionPicker.DeleteRequested)
    def session_delete_requested(self, event: SessionPicker.DeleteRequested) -> None:
        picker = self._session_picker
        if picker is None:
            return
        owner = next(
            (pane for pane in self._panes if pane.controller.session.live_store(event.path) is not None), None
        )
        if owner is not None and (
            not owner.controller.can_switch or owner.controller.ui.has_pending_requests
        ):
            self.notify(tr(self, "ui.session.active"))
            return
        current = event.path == self.session.store.path
        try:
            (owner.controller.session if owner is not None else self.session).trash_session(event.path)
        except (OSError, ValueError) as error:
            self.notify(tr(self, "ui.session.trash_error", error=error), severity="error")
            return
        picker.current_path = self._pane.key
        picker.remove_session(event.path)
        if current:
            self.history.reset()

    def session_picked(self, path) -> None:
        if self._session_picker is not None:
            self.session_grouped = self._session_picker.grouped
            self._session_picker = None
        startup = self.pick_session
        self.pick_session = False
        self.controller.browsing = False
        if path is None:
            if startup:
                self.exit_code = 1
                self.request_close()
            else:
                self.focus_composer()
            return
        initial = self.initial if startup else None
        self.initial = None
        if not startup and path == self._pane.key:
            self.focus_composer()
            return
        self._pending_switch = (path, initial)

    async def change_conversation(self, path, initial=None) -> None:
        target = next((pane for pane in self._panes if pane.key == path), None)
        if target is None and path != "new":
            owner = next(
                (pane for pane in self._panes if pane.controller.session.live_store(path) is not None), None
            )
            if owner is not None:
                # Never create a disk-backed duplicate of a retained conversation.
                if owner.controller.ui.has_pending_requests or not owner.controller.open_session(
                    path, initial=initial
                ):
                    self.notify(tr(self, "ui.session.other_active"), severity="warning")
                    return
                owner.query_one(ChatEditor).clear()
                owner.history.reset()
                target = owner
        if target is None and not self.can_change_session():
            ui = FullscreenUI()
            previous = self.session

            def construct():
                cwd = previous.cwd
                if path != "new":
                    store = SessionStore.load(path)
                    store.build_context()
                    cwd = store.cwd
                    if not cwd.is_dir():
                        raise FileNotFoundError(tr(self, "ui.session.missing_workspace", path=cwd))
                storage = None if path == "new" and previous.store.path is None else path
                return AgentSession(cwd, ui=ui, session=storage, **previous.session_options)

            try:
                candidate = await asyncio.to_thread(construct)
            except Exception as error:  # noqa: BLE001 - retain the current live conversation
                self.notify(tr(self, "ui.session.open_error", error=error), severity="error")
                return
            if self.controller.closing or not self.is_running:
                await asyncio.to_thread(candidate.shutdown)
                return
            target = ConversationPane(RunController(candidate, ui), self.history)
            target.display = False
            self._panes.append(target)
            await self.screen_stack[0].mount(target)
            for selector in ("#jump", "#pending", "#completions", "#auth-question", "#auth-value"):
                target.query_one(selector).display = False
            target.controller.start(initial)
        if target is not None:
            self._pane.state = {name: getattr(self, name) for name in self.CONVERSATION_STATE}
            self._pane.display = False
            self._pane = target
            self.controller = target.controller
            self.session = self.controller.session
            self.ui = self.controller.ui
            defaults = dict(
                activity=RunActivity(self.session.translator),
                history=target.history,
                confirmations=deque(),
                active_confirmation=None,
                input_requests=deque(),
                active_input=None,
                input_request=None,
                completion_names=[],
                completion_query=None,
                completion_suppressed=None,
                last_interrupt=0.0,
            )
            for name, value in (target.state or defaults).items():
                setattr(self, name, value)
            target.display = True
            self.update_status()
            self.focus_composer()
            return
        if path == "new":
            changed = self.controller.new_session(initial=initial)
        else:
            changed = self.controller.open_session(path, initial=initial)
        if changed:
            self.editor.clear()
            self.history.reset()
        self.focus_composer()

    async def poll(self) -> None:
        if self._polling or not self.is_running:
            return
        self._polling = True
        try:
            if self._pending_switch is not None:
                path, initial = self._pending_switch
                self._pending_switch = None
                if not self.controller.closing:
                    await self.change_conversation(path, initial)
            for pane in self._panes:
                if pane is not self._pane:
                    pane.updates.extend(pane.controller.poll())
            if self.controller.closing:
                self.request_close()
            updates = list(self._pane.updates)
            self._pane.updates.clear()
            updates.extend(self.controller.poll())
            for update in updates:
                if not self.is_running:
                    return
                if update.kind == "language_changed":
                    self.refresh_language()
                elif update.kind == "session_changed":
                    self.session = update.payload
                    self.activity.translator = self.session.translator
                    self.refresh_language()
                elif update.kind == "reset":
                    self.activity.reset()
                    await self.transcript.load_history(update.payload)
                elif update.kind == "run_start":
                    self.activity.begin(update.created_at, self.session.effort)
                elif update.kind == "done":
                    summary = self.activity.finish(update.payload, update.created_at)
                    self.transcript.finish_activity()
                    if summary is not None:
                        await self.transcript.add_notice(summary, "dim")
                elif update.kind == "event":
                    self.activity.consume(update.payload)
                    await self.transcript.consume(update.payload)
                elif update.kind == "print":
                    await self.transcript.add_notice(*update.payload)
                    workspace = self._pane.query_one(ScheduleWorkspace)
                    if workspace.display:
                        workspace.notice(*update.payload)
                elif update.kind == "schedule_workspace":
                    self.set_schedule_workspace(update.payload)
                elif update.kind == "confirmation":
                    self.confirmations.append(update.payload)
                elif update.kind == "confirmation_cancelled":
                    self.confirmations = deque(c for c in self.confirmations if c.id != update.payload)
                    self.finish_confirmation(None, update.payload)
                elif update.kind == "input":
                    self.input_requests.append(update.payload)
                elif update.kind == "input_cancelled":
                    self.input_requests = deque(c for c in self.input_requests if c.id != update.payload)
                    if self.active_input == update.payload:
                        self.finish_input(None)
                elif update.kind == "closed":
                    self.exit(self.exit_code)
                    return
            if not self.is_running:
                return
            self.show_next_confirmation()
            self.show_next_input()
            if self._session_picker is not None and self._session_picker.is_mounted:
                if self._session_picker.current_path != self._pane.key:
                    self._session_picker.refresh_sessions(self.session_browser_items(), self._pane.key)
                self._session_picker.set_current_status(self.session_browser_status())
                self._session_picker.set_statuses(self.session_browser_statuses())
            self.activity.waiting = (
                tr(self, "ui.state.closing")
                if self.controller.closing
                else tr(self, "ui.state.approval")
                if self.active_confirmation is not None
                else tr(self, "ui.state.input")
                if self.active_input is not None
                else None
            )
            self.activity.tick()
            self.transcript.show_activity(
                self.activity.content if self.activity.display else None, waiting=self.activity.waiting
            )
            # Not query(...): its truthiness matches every widget in the conversation,
            # which made this 30 fps poll cost grow with the transcript length.
            if self._pane.query_one_optional(Transcript) is None:
                return
            self._pane.query_one("#jump").display = (
                self.transcript.max_scroll_y > 0
                and not self.transcript.follow_latest
                and self.transcript.display
                and self.active_confirmation is None
                and not (self.input_request and self.input_request.preferences)
            )
            pending = self.controller.pending
            pending_widget = self._pane.query_one("#pending", Static)
            pending_widget.display = bool(pending)
            queued = "\n".join(tr(self, "ui.queued", text=text.splitlines()[0]) for text in pending[-3:])
            update_static(pending_widget, Text(queued))  # runs every frame: skip no-op relayouts
            if self.input_request is not None and self.input_request.preferences is not None:
                self._pane.query_one(PreferencePicker).resize_panel()
            if time.monotonic() - self.last_status >= 0.5:
                self.update_status()
        finally:
            self._polling = False

    def show_next_confirmation(self) -> None:
        if (
            self.controller.closing
            or self.has_modal
            or self.active_confirmation is not None
            or self.active_input is not None
        ):
            return
        # The workspace hides the approval dock, including between input steps.
        if self._pane.query_one(ScheduleWorkspace).display:
            return
        while self.confirmations:
            confirmation = self.confirmations.popleft()
            if self.ui.is_confirmation_pending(confirmation.id):
                break
        else:
            return
        self.active_confirmation = confirmation.id
        self.hide_completions()
        root = self._pane
        for selector in ("#editor", "#metrics", "#status", "#hints", "#jump"):
            root.query_one(selector).display = False
        root.query_one(ApprovalPicker).open(confirmation)
        self.update_status()

    def finish_confirmation(self, answer: str | None, confirmation_id: int | None = None) -> None:
        request_id = self.active_confirmation
        if request_id is None or (confirmation_id is not None and request_id != confirmation_id):
            return
        self.active_confirmation = None
        root = self._pane
        root.query_one(ApprovalPicker).close()
        for selector in ("#editor", "#metrics", "#status", "#hints"):
            root.query_one(selector).display = True
        self.ui.resolve_confirmation(request_id, answer)
        self.update_completions()
        self.focus_composer()
        self.update_status()

    @on(ApprovalPicker.Answered)
    def approval_answered(self, event: ApprovalPicker.Answered) -> None:
        event.stop()
        self.finish_confirmation(event.answer, event.confirmation.id)

    def show_next_input(self) -> None:
        if (
            self.controller.closing
            or self.has_modal
            or self.active_input is not None
            or self.active_confirmation is not None
        ):
            return
        while self.input_requests:
            request = self.input_requests.popleft()
            if self.ui.is_input_pending(request.id):
                break
        else:
            return
        self.hide_completions()
        self.active_input = request.id
        self.input_request = request
        root = self._pane
        if request.schedule_workspace:
            root.query_one(ScheduleWorkspace).open(request)
            self.update_status()
            return
        if request.preferences is not None:
            self.editor.display = False
            root.query_one(PreferencePicker).open(request.preferences)
            self.focus_composer()
            self.update_status()
            return
        question = root.query_one("#auth-question", Static)
        question.update(request.question)
        question.display = True
        self.editor.display = False
        details = root.query_one("#input-detail-scroll", VerticalScroll)
        details.display = request.details is not None
        if request.details is not None:
            root.query_one("#input-details", Static).update(Text(request.details))
            details.scroll_home(animate=False)
        options = root.query_one("#completions", ChoiceList)
        field = root.query_one("#auth-value", Input)
        if request.options is not None:
            options.clear_options()
            options.add_options(Option(Text(label), id=str(i)) for i, label in enumerate(request.options))
            options.highlighted = 0
            options.display = True
            options.styles.max_height = max(3, min(6, self.size.height // 4))
        elif request.multiline:
            multiline = root.query_one("#input-multiline", MultilineInput)
            multiline.load_text(request.default)
            multiline.display = True
        else:
            field.password = request.password
            field.value = request.default
            field.display = True
        self.focus_composer()
        self.update_status()

    def focus_composer(self) -> None:
        if self.has_modal:
            return
        if self.active_confirmation is not None:
            self._pane.query_one(ApprovalPicker).focus_control()
        elif self._pane.query_one(ScheduleWorkspace).display:
            self._pane.query_one(ScheduleWorkspace).focus_control()
        elif self.input_request is None:
            self.editor.focus()
        elif self.input_request.preferences is not None:
            self._pane.query_one(PreferencePicker).focus_control()
        elif self.input_request.options is not None:
            self._pane.query_one("#completions").focus()
        elif self.input_request.multiline:
            self._pane.query_one("#input-multiline", MultilineInput).focus()
        else:
            self._pane.query_one("#auth-value").focus()

    def finish_input(self, answer: str | PreferenceChoice | None) -> None:
        request_id = self.active_input
        if request_id is None:
            return
        if self.input_request is not None and self.input_request.schedule_workspace:
            self.active_input = None
            self.input_request = None
            self._pane.query_one(ScheduleWorkspace).close_request()
            self.ui.resolve_input(request_id, answer)
            self.update_status()
            return
        self.active_input = None
        self.input_request = None
        root = self._pane
        root.query_one(PreferencePicker).close()
        root.query_one("#input-detail-scroll").display = False
        root.query_one("#input-details", Static).update("")
        multiline = root.query_one("#input-multiline", MultilineInput)
        multiline.clear()
        multiline.display = False
        field = root.query_one("#auth-value", Input)
        field.value = ""
        field.password = False
        field.display = False
        question = root.query_one("#auth-question", Static)
        question.update("")
        question.display = False
        root.query_one("#completions", ChoiceList).clear_options()
        self.editor.display = True
        self.hide_completions()
        self.update_completions()
        self.focus_composer()
        self.ui.resolve_input(request_id, answer)
        self.update_status()

    def set_schedule_workspace(self, visible: bool) -> None:
        workspace = self._pane.query_one(ScheduleWorkspace)
        if visible:
            workspace.begin()
        else:
            workspace.end()
        for selector in ("#cwd", "#hints", "#body", "#dock"):
            self._pane.query_one(selector).display = not visible
        self.focus_composer()
        self.update_status()

    @on(ScheduleWorkspace.Answered)
    def schedule_answered(self, event: ScheduleWorkspace.Answered) -> None:
        event.stop()
        if (
            event.workspace is self._pane.query_one(ScheduleWorkspace)
            and event.request_id == self.active_input
        ):
            self.finish_input(event.answer)

    @on(Input.Submitted, "#auth-value")
    def input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self.input_request is not None and self.input_request.options is None:
            self.finish_input(event.value)

    @on(MultilineInput.Submitted)
    def multiline_submitted(self, event: MultilineInput.Submitted) -> None:
        event.stop()
        if self.input_request is not None and self.input_request.multiline:
            self.finish_input(event.value)

    @on(PreferencePicker.Answered)
    def preference_answered(self, event: PreferencePicker.Answered) -> None:
        event.stop()
        if self.input_request is not None and self.input_request.preferences is event.request:
            self.finish_input(event.choice)

    def update_status(self) -> None:
        """Refresh the dock labels; unchanged labels are left alone (see ``update_static``)."""
        self.last_status = time.monotonic()
        root = self._pane
        workspace = root.query_one(ScheduleWorkspace)
        brand = Text.assemble(("V_", f"bold {PURPLE}"), " Virgo")
        if workspace.display:
            brand.append(tr(self, "ui.brand.schedules"), style="dim")
            width = max(1, self.size.width - 2)
            path = Text(
                workspace_name(self.session.cwd, self), style="dim", no_wrap=True, overflow="ellipsis"
            )
            path.truncate(max(0, width - brand.cell_len - 2), overflow="ellipsis")
            if path.cell_len:
                brand.append(" " * max(2, width - brand.cell_len - path.cell_len))
                brand.append_text(path)
        else:
            brand.append(f" {__version__}", style="dim")
        update_static(self.screen_stack[0].query_one("#brand", Static), brand)
        if self.controller.closing:
            hints = shortcut_hints(tr(self, "ui.hints.closing"))
        elif self.input_request is not None:
            if self.input_request.preferences is not None:
                if self.input_request.preferences.kind == "model":
                    picker = self._pane.query_one(PreferencePicker)
                    hint = (
                        tr(self, "ui.hints.providers")
                        if picker.provider is None
                        else tr(self, "ui.hints.models")
                    )
                else:
                    hint = tr(self, "ui.hints.effort")
            elif self.input_request.multiline:
                hint = tr(self, "ui.hints.multiline")
            elif self.input_request.details is not None:
                hint = tr(self, "ui.hints.details")
            else:
                hint = (
                    tr(self, "ui.hints.select")
                    if self.input_request.options is not None
                    else tr(self, "ui.hints.confirm")
                )
            hints = shortcut_hints(hint)
        elif not self.controller.busy and 0 < time.monotonic() - self.last_interrupt < 2:
            hints = shortcut_hints(tr(self, "ui.hints.exit"))
        else:
            hints = input_hints(self)
        update_static(root.query_one("#hints", Static), hints)
        width = max(1, self.size.width - 2)
        cwd = str(self.session.cwd)
        if cell_len(cwd) > width:
            while cwd and cell_len(cwd) > width - 1:
                cwd = cwd[1:]
            cwd = "…" + cwd
        update_static(root.query_one("#cwd", Static), Text(cwd, no_wrap=True))
        usage = Usage(**self.session.store.total_usage())
        window = self.session.main.model.context_window
        if self.session.main.messages:
            tokens = self.session.context_tokens()
            context = f"~{tokens / max(window, 1):.1%}/{short_number(window)}"
        else:
            context = f"0.0%/{short_number(window)}"
        used = tr(
            self,
            "ui.metrics",
            input=short_number(usage.input_total),
            output=short_number(usage.output),
            cache=usage.cache_hit_rate,
        )
        metrics = Text(f"{used}  {context}", no_wrap=True, overflow="ellipsis")
        metrics.truncate(width, overflow="ellipsis")
        update_static(root.query_one("#metrics", Static), metrics)
        mode = normalize_permission_mode(self.session.permission_mode)
        footer = Text(no_wrap=True, overflow="ellipsis")
        if self.session.store.save_error is not None:
            footer.append(tr(self, "ui.state.unsaved_footer"), style="bold red")
        footer.append(permission_mode_label(mode, self.session.t), style=PERMISSION_COLORS[mode])
        effort = f" · {self.session.effort}"
        available = max(1, width - footer.cell_len - 2)
        model = Text(self.session.main.model.id, style="dim", no_wrap=True, overflow="ellipsis")
        model.truncate(max(1, available - cell_len(effort)), overflow="ellipsis")
        model.append(effort)
        model.truncate(available, overflow="ellipsis")
        footer.append(" " * max(2, width - footer.cell_len - model.cell_len))
        footer.append(model)
        footer.truncate(width, overflow="ellipsis")
        update_static(root.query_one("#status", Static), footer)

    def refresh_language(self) -> None:
        """Refresh presentation chrome without altering stored conversation text."""
        self.activity.translator = self.session.translator
        update_static(self._pane.query_one("#jump", Static), tr(self, "ui.latest"))
        self._pane.query_one(ScheduleWorkspace).refresh_language()
        for event in self.transcript.query(EventView):
            event.render_header()
        self.completion_query = None
        self.update_completions()
        self.editor.refresh()
        self.update_status()

    @on(ChatEditor.Submitted)
    def submitted(self, event: ChatEditor.Submitted) -> None:
        self.hide_completions()
        if event.text.strip() == "/sessions":
            self.action_sessions()
        elif event.text.strip() == "/new":
            self._pending_switch = ("new", None)
        else:
            self.controller.submit(event.text)
        self.update_status()

    @on(TextArea.Changed, "#editor")
    def editor_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is not self.editor:
            return
        self.resize_editor()
        self.sync_completions()

    def on_resize(self) -> None:
        if self.is_mounted:
            self.call_after_refresh(self.resize_editor)
            self.call_after_refresh(self.update_status)

    def resize_editor(self) -> None:
        if not self.is_running:
            return
        self.editor.styles.height = min(
            max(3, self.editor.wrapped_document.height + 2), max(3, self.size.height // 3)
        )
        self._pane.query_one("#completions").styles.max_height = max(3, min(6, self.size.height // 4))

    def hide_completions(self) -> None:
        self.completion_names = []
        self._pane.query_one("#completions").display = False

    def sync_completions(self) -> None:
        # TextArea.Changed is queued; a quick Enter/Tab/arrow can arrive first.
        if self.completion_query != self.editor.text:
            self.update_completions()

    def update_completions(self) -> None:
        if self.active_input is not None or self.active_confirmation is not None:
            return
        text = self.editor.text
        self.completion_query = text
        if text != self.completion_suppressed:
            self.completion_suppressed = None
        if not text.startswith("/") or any(c.isspace() for c in text) or text == self.completion_suppressed:
            self.hide_completions()
            return
        commands = {f"/{c.name}": c.description for c in self.session.commands.all()}
        commands.update(
            {f"/{name}": description for name, (description, _) in self.session.templates.items()}
        )
        commands["/exit"] = tr(self, "ui.command.exit")
        names = sorted(name for name in commands if name.startswith(text))
        self.completion_names = names
        options = self._pane.query_one("#completions", ChoiceList)
        options.clear_options()
        options.add_options(Option(CommandChoice(name, commands[name]), id=name) for name in names)
        options.display = bool(names)
        options.styles.max_height = max(3, min(6, self.size.height // 4))
        options.highlighted = 0 if names else None

    def accept_completion(self) -> bool:
        self.sync_completions()
        if not self.completion_names or not self._pane.query_one("#completions").display:
            return False
        options = self._pane.query_one("#completions", OptionList)
        index = options.highlighted or 0
        self.editor.load_text(self.completion_names[index] + " ")
        self.editor.move_cursor(self.editor.document.end)
        self.hide_completions()
        self.editor.focus()
        return True

    def move_completion(self, direction: int) -> bool:
        self.sync_completions()
        if not self.completion_names or self.active_input is not None:
            return False
        options = self._pane.query_one("#completions", ChoiceList)
        return options.move_selection(direction)

    @on(OptionList.OptionSelected, "#completions")
    def completion_selected(self, event: OptionList.OptionSelected) -> None:
        if self.input_request is not None and self.input_request.options is not None:
            self.finish_input(self.input_request.options[event.option_index])
        else:
            self.accept_completion()

    def action_escape(self) -> None:
        if isinstance(self.screen, SessionPicker):
            self.screen.dismiss(None)
        elif self.active_confirmation is not None:
            self.finish_confirmation("n")
        elif self.active_input is not None:
            self.finish_input(None)
        elif self.completion_names:
            self.completion_suppressed = self.editor.text
            self.hide_completions()
        elif self.controller.busy:
            self.controller.abort()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if (
            not self.has_modal
            and self.input_request is not None
            and self.input_request.details is not None
            and action in {"page", "top"}
        ):
            return True
        if (
            self.has_modal or self.active_input is not None or self.active_confirmation is not None
        ) and action in {"page", "top", "tools", "thinking", "cycle_permissions"}:
            return False
        return True

    def action_interrupt(self) -> None:
        if self.controller.closing:
            self.exit_code = 130
            self.exit(self.exit_code)
        elif self.controller.busy:
            self.controller.abort()
        elif self.has_modal or self.active_confirmation is not None:
            self.action_escape()
        elif self.editor.text:
            self.editor.clear()
            self.history.reset()
            self.last_interrupt = 0
        elif time.monotonic() - self.last_interrupt < 2:
            self.request_close()
        else:
            self.last_interrupt = time.monotonic()
            update_static(
                self._pane.query_one("#hints", Static),
                shortcut_hints(tr(self, "ui.hints.exit")),
            )

    def action_cycle_permissions(self) -> None:
        if self.controller.closing:
            return
        mode = normalize_permission_mode(self.session.permission_mode)
        self.session.permission_mode = MODES[(MODES.index(mode) + 1) % len(MODES)]
        self.update_status()

    def action_eof(self) -> None:
        if self.active_confirmation is not None or not self.editor.text:
            self.request_close()
        elif not self.has_modal:
            self.editor.action_delete_right()

    def action_tools(self) -> None:
        if not self.has_modal:
            self.transcript.toggle_tools()

    def action_thinking(self) -> None:
        if not self.has_modal:
            self.transcript.toggle_thinking()

    def action_page(self, direction: int) -> None:
        if self.has_modal:
            return
        if self.input_request is not None and self.input_request.details is not None:
            selector = (
                "#schedule-detail-scroll" if self.input_request.schedule_workspace else "#input-detail-scroll"
            )
            details = self._pane.query_one(selector, VerticalScroll)
            if direction < 0:
                details.action_page_up()
            else:
                details.action_page_down()
            return
        if direction < 0:
            self.transcript.action_page_up()
        else:
            self.transcript.action_page_down()

    def action_top(self) -> None:
        if not self.has_modal and self.input_request is not None and self.input_request.details is not None:
            selector = (
                "#schedule-detail-scroll" if self.input_request.schedule_workspace else "#input-detail-scroll"
            )
            self._pane.query_one(selector, VerticalScroll).scroll_home(animate=False)
        elif not self.has_modal:
            self.transcript.action_scroll_home()

    @on(Click, "#jump")
    def jump_clicked(self) -> None:
        if not self.has_modal:
            self.transcript.go_latest()
            self.focus_composer()

    def on_unmount(self) -> None:
        if hasattr(self, "_poll_timer"):
            self._poll_timer.stop()
        self.request_close()

    def request_close(self) -> None:
        for pane in self._panes:
            pane.controller.request_close()

    def wait_closed(self, timeout: float = 5) -> bool:
        deadline = time.monotonic() + timeout
        results = [pane.controller.wait_closed(max(0, deadline - time.monotonic())) for pane in self._panes]
        return all(results)
