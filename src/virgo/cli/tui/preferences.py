"""Inline model and effort selection without changing state until confirmation."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ...harness.preferences import EFFORT_LEVELS, PreferenceChoice, PreferenceRequest, provider_name


class PreferencePicker(Vertical, can_focus=True):
    DEFAULT_CSS = """
    PreferencePicker {
        height: auto; border-top: solid #7C3AED; padding: 1 2;
        background: $background; color: $foreground;
    }
    PreferencePicker > Static { height: auto; }
    PreferencePicker > #preference-title { color: #7C3AED; text-style: bold; }
    PreferencePicker > #preference-description { text-style: dim; margin-bottom: 1; }
    PreferencePicker > #preference-models {
        height: 5; border: none; padding: 0; background: $background;
        scrollbar-size: 0 0; text-wrap: wrap;
    }
    PreferencePicker #preference-models > .option-list--option-highlighted,
    PreferencePicker #preference-models:focus > .option-list--option-highlighted {
        background: $background; color: #7C3AED; text-style: bold;
    }
    PreferencePicker > #preference-more { text-style: dim; }
    PreferencePicker > #preference-effort { margin: 1 0; }
    PreferencePicker > #preference-help { text-style: dim; }
    """
    BINDINGS = [
        Binding("up", "model_key('cursor_up')", show=False, priority=True),
        Binding("down", "model_key('cursor_down')", show=False, priority=True),
        Binding("home", "model_key('first')", show=False, priority=True),
        Binding("end", "model_key('last')", show=False, priority=True),
        Binding("pageup", "model_key('page_up')", show=False, priority=True),
        Binding("pagedown", "model_key('page_down')", show=False, priority=True),
        Binding("left", "effort(-1)", show=False, priority=True),
        Binding("right", "effort(1)", show=False, priority=True),
        Binding("enter", "choose(True)", show=False, priority=True),
        Binding("s", "choose(False)", show=False, priority=True),
        Binding("backspace", "back", show=False, priority=True),
        Binding("escape", "cancel", show=False),
    ]

    class Answered(Message):
        def __init__(self, request: PreferenceRequest, choice: PreferenceChoice | None) -> None:
            self.request = request
            self.choice = choice
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.request: PreferenceRequest | None = None
        self.effort = "medium"
        self._highlighted: int | None = None
        self._answered = False
        self._narrow_models = False
        self.providers: tuple[str, ...] = ()
        self.provider: str | None = None
        self.visible_models = ()
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("", id="preference-title", markup=False)
        yield Static("", id="preference-description", markup=False)
        yield OptionList(id="preference-models")
        yield Static("", id="preference-more", markup=False)
        yield Static("", id="preference-effort", markup=False)
        yield Static("", id="preference-help", markup=False)

    def open(self, request: PreferenceRequest) -> None:
        self.request = request
        self._answered = False
        self.effort = request.effort
        self.display = True
        model_mode = request.kind == "model"
        self.providers = tuple(
            sorted({provider_name(m.spec.split("/", 1)[0]) for m in request.models}, key=str.casefold)
        )
        self.provider = None
        self.visible_models = ()
        models = self.query_one("#preference-models", OptionList)
        self._narrow_models = self.app.size.width < 64
        if model_mode:
            self.show_catalog()
        else:
            self.query_one("#preference-title", Static).update("Effort")
            self.query_one("#preference-description", Static).update("Adjust how much reasoning the model uses.")
        models.display = model_mode
        self.query_one("#preference-more").display = model_mode
        self.resize_panel()
        self.render_effort()
        self.render_more()
        self.focus_control()

    def show_catalog(self, provider: str | None = None) -> None:
        self.provider = provider
        self.visible_models = tuple(
            model for model in self.request.models if provider_name(model.spec.split("/", 1)[0]) == provider
        )
        models = self.query_one("#preference-models", OptionList)
        models.clear_options()
        self._highlighted = None
        if provider is None:
            current_provider = provider_name(self.request.model.split("/", 1)[0])
            current = self.providers.index(current_provider) if current_provider in self.providers else 0
            ids = [f"provider:{name}" for name in self.providers]
            title = "Select provider"
            description = "Choose a connected provider to see its available models."
        else:
            current = next(
                (i for i, model in enumerate(self.visible_models) if model.spec == self.request.model), 0
            )
            ids = [model.spec for model in self.visible_models]
            title = f"Select model · {provider}"
            description = "The current model is marked ✓. Backspace returns to providers."
        self.query_one("#preference-title", Static).update(title)
        self.query_one("#preference-description", Static).update(description)
        models.add_options(Option(self.model_row(i, selected=i == current), id=id_) for i, id_ in enumerate(ids))
        models.highlighted = current if ids else None
        self._highlighted = models.highlighted
        self.query_one("#preference-effort").display = provider is not None
        self.resize_panel()
        self.render_more()
        if hasattr(self.app, "update_status"):
            self.app.update_status()

    def focus_control(self) -> None:
        if self.request is not None and self.request.kind == "model":
            self.query_one("#preference-models").focus()
        else:
            self.focus()

    def close(self) -> None:
        self.display = False
        self.request = None
        self.provider = None
        self.visible_models = ()

    def resize_panel(self) -> None:
        compact = self.app.size.height < 20
        self.styles.padding = (0, 1) if compact else (1, 2)
        self.query_one("#preference-description").display = not compact
        self.query_one("#preference-more").display = bool(
            self.request is not None and self.request.kind == "model" and not compact
        )
        root = self.app.screen_stack[0]
        pending = root.query_one_optional("#pending")  # runs every frame while open: no full DOM walk
        pending_lines = 0
        if pending is not None and pending.display:
            pending_lines = min(3, len(str(pending.content).splitlines()))
        header_height = sum(root.query_one(selector).region.height for selector in ("#brand", "#cwd", "#hints"))
        reserved = (12 if compact else 17) + max(0, header_height - 3)
        self.query_one("#preference-models").styles.height = min(
            3 if compact else 5,
            max(1, self.app.size.height - reserved - pending_lines),
        )
        self.query_one("#preference-effort").styles.margin = (0, 0) if compact else (1, 0)
        help_text = (
            "Enter default · s session · Esc cancel"
            if self.app.size.width < 64
            else "Enter to save as default · s for this session only · Esc to cancel"
        )
        if self.request is not None and self.request.kind == "effort":
            help_text = "←/→ to adjust · " + help_text
            self.query_one("#preference-effort").display = True
        elif self.provider is None:
            help_text = "Enter models · Esc cancel"
        else:
            help_text = (
                "Enter save · s session · ⌫ back · Esc"
                if self.app.size.width < 64
                else "Enter save · s session · Backspace back · Esc cancel"
            )
        self.query_one("#preference-help", Static).update(help_text)

    def on_resize(self) -> None:
        if self.request is not None:
            self.resize_panel()
            self.render_effort()
            narrow = self.app.size.width < 64
            if self.request.kind == "model" and narrow != self._narrow_models:
                self._narrow_models = narrow
                models = self.query_one("#preference-models", OptionList)
                for index in range(models.option_count):
                    models.replace_option_prompt_at_index(
                        index, self.model_row(index, selected=index == models.highlighted)
                    )
                self.call_after_refresh(models.scroll_to_highlight)

    def render_effort(self) -> None:
        if self.request is None:
            return
        if self.request.kind == "model":
            label = Text.assemble(
                (f"◯ {self.effort.capitalize()} effort", "#7C3AED"), ("  ←/→ to adjust", "dim")
            )
        else:
            width = max(24, self.content_size.width - 2)
            positions = [round(i * (width - 6) / 4) for i in range(5)]
            title = "Faster" + " " * max(1, width - 13) + "Smarter"
            bar = ["─"] * width
            selected = EFFORT_LEVELS.index(self.effort)
            for i, position in enumerate(positions):
                bar[position] = "▲" if i == selected else "┬"
            label = Text(title + "\n" + "".join(bar) + "\n", no_wrap=True, overflow="ellipsis")
            for i, effort in enumerate(EFFORT_LEVELS):
                if i:
                    label.append(" " * max(1, positions[i] - positions[i - 1] - len(EFFORT_LEVELS[i - 1])))
                label.append(effort, style="bold #7C3AED" if effort == self.effort else "dim")
        self.query_one("#preference-effort", Static).update(label)

    def render_more(self) -> None:
        if self.request is None or self.request.kind != "model":
            return
        models = self.query_one("#preference-models", OptionList)
        selected = (models.highlighted + 1) if models.highlighted is not None else 0
        count = len(self.providers) if self.provider is None else len(self.visible_models)
        noun = "providers" if self.provider is None else "models"
        self.query_one("#preference-more", Static).update(
            f"{selected} / {count} {noun} · ↑/↓ to choose"
        )

    def model_row(self, index: int, *, selected: bool) -> Table | Text:
        if self.provider is None:
            name = self.providers[index]
            count = sum(provider_name(model.spec.split("/", 1)[0]) == name for model in self.request.models)
            label = Text(f"{'›' if selected else ' '} {index + 1}. {name}")
            label.append(f"  {count} {'model' if count == 1 else 'models'}", style="dim")
            return label
        model = self.visible_models[index]
        label = Text(f"{'›' if selected else ' '} {index + 1}. {model.label}")
        if model.spec == self.request.model:
            label.append(" ✓", style="green")
        if self._narrow_models:
            label.no_wrap = True
            label.overflow = "ellipsis"
            return label
        row = Table.grid(expand=True, padding=(0, 2))
        row.add_column(ratio=1)
        row.add_column(ratio=1)
        row.add_row(label, Text(model.description, style="dim"))
        return row

    @on(OptionList.OptionHighlighted, "#preference-models")
    def model_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if self.request is None or self.request.kind != "model":
            return
        models = self.query_one("#preference-models", OptionList)
        # Ignore queued highlight messages from a previous opening of the panel.
        if (
            event.option_index != models.highlighted
            or event.option.id != models.get_option_at_index(event.option_index).id
        ):
            return
        if self._highlighted is not None and self._highlighted < models.option_count:
            models.replace_option_prompt_at_index(
                self._highlighted, self.model_row(self._highlighted, selected=False)
            )
        models.replace_option_prompt_at_index(
            event.option_index, self.model_row(event.option_index, selected=True)
        )
        self._highlighted = event.option_index
        self.render_more()

    def action_effort(self, direction: int) -> None:
        if self.request is None or self._answered:
            return
        if self.request.kind == "model" and self.provider is None:
            return
        index = EFFORT_LEVELS.index(self.effort)
        self.effort = EFFORT_LEVELS[max(0, min(len(EFFORT_LEVELS) - 1, index + direction))]
        self.render_effort()

    def action_model_key(self, action: str) -> None:
        # Navigation and confirmation share the same queue, so a rapid
        # arrow+Enter sequence commits the newly highlighted model.
        if self.request is None or self.request.kind != "model" or self._answered:
            return
        models = self.query_one("#preference-models", OptionList)
        getattr(models, f"action_{action}")()

    def action_choose(self, persist: bool) -> None:
        if self.request is None or self._answered:
            return
        model = self.request.model
        if self.request.kind == "model":
            options = self.query_one("#preference-models", OptionList)
            if options.highlighted is None:
                return
            if self.provider is None:
                self.show_catalog(self.providers[options.highlighted])
                return
            model = self.visible_models[options.highlighted].spec
        self._answered = True
        self.post_message(self.Answered(self.request, PreferenceChoice(model, self.effort, persist)))

    @on(OptionList.OptionSelected, "#preference-models")
    def model_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if self.request is not None and self.provider is None:
            self.action_choose(True)
        # Clicking a model previews it; Enter or s explicitly commits it.

    def action_back(self) -> None:
        if self.request is not None and self.request.kind == "model" and self.provider is not None:
            previous = self.provider
            self.show_catalog()
            self.query_one("#preference-models", OptionList).highlighted = self.providers.index(previous)

    def action_cancel(self) -> None:
        if self.request is None or self._answered:
            return
        self._answered = True
        self.post_message(self.Answered(self.request, None))
