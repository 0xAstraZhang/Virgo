"""Single-column, terminal-native fullscreen Virgo application."""

from __future__ import annotations

import time
from collections import deque

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.theme import Theme
from textual.widgets import Button, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ... import __version__
from ...harness.store import SessionStore
from .bridge import FullscreenUI
from .controller import RunController
from .dialogs import ApprovalScreen, SessionPicker
from .editor import ChatEditor, InputHistory
from .transcript import Transcript

PURPLE = "#7C3AED"


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
            "scrollbar": "ansi_magenta",
            "scrollbar-hover": "ansi_magenta",
            "scrollbar-active": "ansi_magenta",
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


class VirgoApp(App[int]):
    ENABLE_COMMAND_PALETTE = False
    TITLE = "Virgo"
    CSS = """
    Screen { background: $background; color: $foreground; }
    #brand { height: 1; margin: 0 1; }
    #body { height: 1fr; }
    Transcript { height: 1fr; }
    #dock { height: auto; padding: 0 1; background: $background; }
    #pending { height: auto; max-height: 3; text-style: dim; }
    #jump { height: 1; min-height: 1; border: none; width: auto; padding: 0 1;
        color: #7C3AED; background: $background; }
    #completions { height: auto; max-height: 6; border: solid #7C3AED;
        padding: 0; background: $background; }
    #editor { height: 3; min-height: 3; border: none; border-top: solid #7C3AED;
        border-bottom: solid #7C3AED; padding: 0; background: $background; color: $foreground; }
    #editor:focus { border: none; border-top: solid #7C3AED; border-bottom: solid #7C3AED; }
    TextArea > .text-area--cursor { text-style: reverse; background: $background; color: $foreground; }
    TextArea > .text-area--selection { text-style: reverse; background: $background; color: $foreground; }
    #status { height: auto; max-height: 2; text-style: dim; }
    #hints { height: 1; text-style: dim; }
    OptionList, Button { background: $background; color: $foreground; }
    OptionList > .option-list--option-highlighted { background: $background;
        color: $foreground; text-style: reverse; }
    ModalScreen { align: center middle; background: $background; }
    .dialog { width: 76; max-width: 95%; height: auto; max-height: 90%;
        padding: 1 2; border: round #7C3AED; background: $background; }
    .dialog > Static { height: auto; margin-bottom: 1; }
    .question { height: auto; max-height: 12; margin-bottom: 1; }
    .question Static { height: auto; }
    .dialog-buttons { height: auto; }
    .dialog-buttons Button { width: 1fr; min-width: 8; padding: 0; }
    Button:focus { text-style: reverse; }
    #sessions { height: auto; max-height: 16; margin-bottom: 1; }
    """
    BINDINGS = [
        Binding("ctrl+c", "interrupt", priority=True, show=False),
        Binding("ctrl+d", "eof", priority=True, show=False),
        Binding("escape", "escape", priority=True, show=False),
        Binding("ctrl+o", "tools", priority=True, show=False),
        Binding("ctrl+t", "thinking", priority=True, show=False),
        Binding("pageup", "page(-1)", priority=True, show=False),
        Binding("pagedown", "page(1)", priority=True, show=False),
        Binding("ctrl+home", "top", priority=True, show=False),
        Binding("ctrl+end", "latest", priority=True, show=False),
    ]

    def __init__(self, session, ui: FullscreenUI, initial: str | None = None, pick_session=False):
        super().__init__(ansi_color=True)
        self.session = session
        self.ui = ui
        self.controller = RunController(session, ui)
        self.initial = initial
        self.pick_session = pick_session
        self.exit_code = 0
        self.history = InputHistory(session.paths.user)
        self.register_theme(terminal_theme())
        self.theme = "virgo-terminal"
        self.confirmations = deque()
        self.active_confirmation = None
        self.completion_names: list[str] = []
        self.completion_suppressed: str | None = None
        self.last_interrupt = 0.0
        self.last_status = 0.0
        self._polling = False

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble(("V_", f"bold {PURPLE}"), (f" Virgo {__version__}", "dim")), id="brand")
        with Vertical(id="body"):
            yield Transcript(id="transcript")
        with Vertical(id="dock"):
            yield Button("↓ Latest message · Ctrl+End", id="jump")
            yield Static("", id="pending", markup=False)
            yield OptionList(id="completions")
            yield ChatEditor(self.history)
            yield Static("", id="status", markup=False)
            yield Static("Enter send · Ctrl+J newline · Esc interrupt · / commands", id="hints", markup=False)

    @property
    def editor(self) -> ChatEditor:
        return self.screen_stack[0].query_one(ChatEditor)

    @property
    def transcript(self) -> Transcript:
        return self.screen_stack[0].query_one(Transcript)

    @property
    def has_modal(self) -> bool:
        return len(self.screen_stack) > 1

    def on_mount(self) -> None:
        for selector in ("#jump", "#pending", "#completions"):
            self.screen_stack[0].query_one(selector).display = False
        self.editor.focus()
        self._poll_timer = self.set_interval(1 / 30, self.poll)
        self.update_status()
        if self.pick_session:
            items = SessionStore.list(self.session.paths.sessions)
            self.push_screen(SessionPicker(items), self.session_picked)
        else:
            self.controller.start(self.initial)

    def session_picked(self, path) -> None:
        if path is None:
            self.exit_code = 1
            self.controller.request_close()
            return
        try:
            self.session.open_session(path)
        except (OSError, ValueError) as error:
            self.ui.print(f"Cannot resume session: {error}", "error")
            self.initial = None
        self.controller.start(self.initial)
        self.editor.focus()

    async def poll(self) -> None:
        if self._polling or not self.is_running:
            return
        self._polling = True
        try:
            for update in self.controller.poll():
                if not self.is_running:
                    return
                if update.kind == "reset":
                    await self.transcript.load_history(update.payload)
                elif update.kind == "event":
                    await self.transcript.consume(update.payload)
                elif update.kind == "print":
                    await self.transcript.add_notice(*update.payload)
                elif update.kind == "confirmation":
                    self.confirmations.append(update.payload)
                elif update.kind == "confirmation_cancelled":
                    self.confirmations = deque(c for c in self.confirmations if c.id != update.payload)
                    if self.active_confirmation == update.payload and isinstance(self.screen, ApprovalScreen):
                        self.screen.dismiss(None)
                elif update.kind == "closed":
                    self.exit(self.exit_code)
                    return
            if not self.is_running:
                return
            self.show_next_confirmation()
            if not self.screen_stack[0].query(Transcript):
                return
            self.screen_stack[0].query_one("#jump").display = (
                not self.transcript.follow_latest and self.transcript.display
            )
            pending = self.controller.pending
            pending_widget = self.screen_stack[0].query_one("#pending", Static)
            pending_widget.display = bool(pending)
            pending_widget.update(
                Text("\n".join(f"Queued · {text.splitlines()[0]}" for text in pending[-3:]))
            )
            if time.monotonic() - self.last_status >= 0.5:
                self.update_status()
        finally:
            self._polling = False

    def show_next_confirmation(self) -> None:
        if self.controller.closing or self.has_modal or not self.confirmations:
            return
        confirmation = self.confirmations.popleft()
        self.active_confirmation = confirmation.id

        def answered(answer: str | None) -> None:
            self.ui.resolve_confirmation(confirmation.id, answer)
            self.active_confirmation = None
            self.editor.focus()

        self.push_screen(ApprovalScreen(confirmation), answered)

    def update_status(self) -> None:
        self.last_status = time.monotonic()
        status = "closing…" if self.controller.closing else "working…" if self.controller.busy else "ready"
        if self.active_confirmation is not None:
            status = "approval needed"
        if self.controller.closing:
            self.screen_stack[0].query_one("#hints", Static).update(
                "Closing services… Ctrl+C to return to the terminal immediately"
            )
        width = max(1, self.size.width - 2)
        model = self.session.main.model.spec
        tail = f" · {self.session.effort} · {self.session.permission_mode} · {status}"
        if self.size.width < 64:
            model = self.session.main.model.id
            tail = f" · {status} · {self.session.permission_mode}"
        primary = Text(model, no_wrap=True, overflow="ellipsis")
        primary.truncate(max(1, width - cell_len(tail)), overflow="ellipsis")
        primary.append(tail)
        primary.truncate(width, overflow="ellipsis")
        if self.size.width < 64:
            footer = primary
        else:
            usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
            for entry in self.session.store.branch():
                msg = entry.get("message", {})
                if msg.get("role") == "assistant":
                    for key in usage:
                        usage[key] += msg.get("usage", {}).get(key, 0)
            tokens = self.session.context_tokens()
            window = self.session.main.model.context_window
            context = f"~{tokens / max(window, 1):.1%}/{short_number(window)}"
            used = f"↑{short_number(usage['input'])} ↓{short_number(usage['output'])}"
            cache = usage["cache_read"] + usage["cache_write"]
            if cache:
                used += f" cache {short_number(cache)}"
            cwd = str(self.session.cwd)
            space = max(1, width - cell_len(used) - cell_len(context) - 4)
            if cell_len(cwd) > space:
                while cwd and cell_len(cwd) > space - 1:
                    cwd = cwd[1:]
                cwd = "…" + cwd
            footer = Text(f"{cwd}  {used}  {context}", no_wrap=True, overflow="ellipsis")
            footer.truncate(width, overflow="ellipsis")
            footer.append("\n")
            footer.append(primary)
        self.screen_stack[0].query_one("#status", Static).update(footer)

    @on(ChatEditor.Submitted)
    def submitted(self, event: ChatEditor.Submitted) -> None:
        self.hide_completions()
        self.controller.submit(event.text)
        self.update_status()

    @on(TextArea.Changed, "#editor")
    def editor_changed(self) -> None:
        self.resize_editor()
        self.update_completions()

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
        self.screen_stack[0].query_one("#hints").display = self.size.height >= 20

    def hide_completions(self) -> None:
        self.completion_names = []
        self.screen_stack[0].query_one("#completions").display = False

    def update_completions(self) -> None:
        text = self.editor.text
        if not text.startswith("/") or any(c.isspace() for c in text) or text == self.completion_suppressed:
            self.hide_completions()
            return
        commands = {f"/{c.name}": c.description for c in self.session.commands.all()}
        commands.update(
            {f"/{name}": description for name, (description, _) in self.session.templates.items()}
        )
        commands["/exit"] = "exit Virgo"
        names = sorted(name for name in commands if name.startswith(text))
        self.completion_names = names
        options = self.screen_stack[0].query_one("#completions", OptionList)
        options.clear_options()
        options.add_options(Option(Text(f"{name:<18} {commands[name]}"), id=name) for name in names)
        options.display = bool(names)
        options.styles.max_height = max(3, min(6, self.size.height // 4))
        options.highlighted = 0 if names else None

    def accept_completion(self) -> bool:
        if not self.completion_names or not self.screen_stack[0].query_one("#completions").display:
            return False
        options = self.screen_stack[0].query_one("#completions", OptionList)
        index = options.highlighted or 0
        self.editor.load_text(self.completion_names[index] + " ")
        self.editor.move_cursor(self.editor.document.end)
        self.hide_completions()
        self.editor.focus()
        return True

    def move_completion(self, direction: int) -> bool:
        if not self.completion_names:
            return False
        options = self.screen_stack[0].query_one("#completions", OptionList)
        options.highlighted = ((options.highlighted or 0) + direction) % len(self.completion_names)
        return True

    @on(OptionList.OptionSelected, "#completions")
    def completion_selected(self) -> None:
        self.accept_completion()

    def action_escape(self) -> None:
        if isinstance(self.screen, ApprovalScreen):
            self.screen.dismiss("n")
        elif isinstance(self.screen, SessionPicker):
            self.screen.dismiss(None)
        elif self.completion_names:
            self.completion_suppressed = self.editor.text
            self.hide_completions()
        elif self.controller.busy:
            self.controller.abort()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if self.has_modal and action in {"page", "top", "latest", "tools", "thinking"}:
            return False
        return True

    def action_interrupt(self) -> None:
        if self.controller.closing:
            self.exit_code = 130
            self.exit(self.exit_code)
        elif self.controller.busy:
            self.controller.abort()
        elif self.has_modal:
            self.action_escape()
        elif self.editor.text:
            self.editor.clear()
            self.history.reset()
            self.last_interrupt = 0
        elif time.monotonic() - self.last_interrupt < 2:
            self.controller.request_close()
        else:
            self.last_interrupt = time.monotonic()
            self.screen_stack[0].query_one("#hints", Static).update(
                "Ctrl+C again to exit · /exit also closes Virgo"
            )

    def action_eof(self) -> None:
        if not self.editor.text:
            self.controller.request_close()
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
        if direction < 0:
            self.transcript.action_page_up()
        else:
            self.transcript.action_page_down()

    def action_top(self) -> None:
        if not self.has_modal:
            self.transcript.action_scroll_home()

    def action_latest(self) -> None:
        if not self.has_modal:
            self.transcript.go_latest()

    @on(Button.Pressed, "#jump")
    def jump_pressed(self) -> None:
        self.action_latest()
        self.editor.focus()

    def on_unmount(self) -> None:
        if hasattr(self, "_poll_timer"):
            self._poll_timer.stop()
        self.controller.request_close()
