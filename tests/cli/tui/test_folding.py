"""Whole-block folds distinguish clicks, selections and nested event blocks."""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.events import MouseDown, MouseMove, MouseUp
from textual.widgets import Static

from virgo.cli.tui.folding import Foldable, FoldHeader
from virgo.cli.tui.selection import SelectionCopyMixin


class ExampleFold(Foldable):
    DEFAULT_CSS = """
    ExampleFold { height: auto; padding: 0 1 1 1; }
    ExampleFold > Static { height: auto; }
    """

    def __init__(self, *, nested: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.expanded = True
        self.toggles = 0
        self.header = FoldHeader("Header words", markup=False)
        self.body = Static("Selectable body words\nSecond line", markup=False)
        self.inner = ExampleFold(id="inner") if nested else None

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.body
        if self.inner is not None:
            yield self.inner

    def action_toggle(self) -> None:
        self.toggles += 1
        self.expanded = not self.expanded
        self.body.display = self.expanded
        if self.inner is not None:
            self.inner.display = self.expanded


class FoldingApp(SelectionCopyMixin, App):
    def __init__(self, *, nested: bool = False) -> None:
        super().__init__()
        self.CLICK_CHAIN_TIME_THRESHOLD = 10
        self.fold = ExampleFold(nested=nested, id="outer")
        self.copied: list[str] = []

    def compose(self) -> ComposeResult:
        yield self.fold

    def copy_to_clipboard(self, text: str) -> None:
        self.copied.append(text)


@pytest.mark.parametrize("target", ["header", "body", "padding"])
def test_only_header_click_toggles_immediately(target):
    async def scenario():
        app = FoldingApp()
        async with app.run_test(size=(80, 24)) as pilot:
            fold = app.fold
            if target == "padding":
                await pilot.click(fold, offset=(0, fold.size.height - 1))
            else:
                await pilot.click(getattr(fold, target), offset=(3, 0))
            await pilot.pause(0.05)
            if target != "header":
                assert fold.expanded and fold.toggles == 0
                return
            assert not fold.expanded and not fold.body.display
            assert fold.toggles == 1
            await pilot.click(fold.header, offset=(3, 0))
            await pilot.pause(0.05)
            assert fold.expanded and fold.body.display
            assert fold.toggles == 2

    asyncio.run(scenario())


def test_nested_block_mouse_and_keyboard_do_not_toggle_parent():
    async def scenario():
        app = FoldingApp(nested=True)
        async with app.run_test(size=(80, 24)) as pilot:
            outer, inner = app.fold, app.fold.inner
            await pilot.click(inner.header, offset=(3, 0))
            await pilot.pause(0.05)
            assert not inner.expanded and inner.toggles == 1
            assert outer.expanded and outer.toggles == 0
            inner.header.focus()
            await pilot.press("enter")
            assert inner.expanded and inner.toggles == 2
            await pilot.press("space")
            assert not inner.expanded and inner.toggles == 3
            assert outer.expanded and outer.toggles == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("gesture", ["double_click", "triple_click"])
def test_multiclick_selects_and_copies_without_folding(gesture):
    async def scenario():
        app = FoldingApp(nested=True)
        async with app.run_test(size=(80, 24)) as pilot:
            inner = app.fold.inner
            await getattr(pilot, gesture)(inner.body, offset=(3, 0))
            await pilot.pause(0.05)
            assert app.copied
            assert "Selectable body words" in app.copied[-1]
            assert inner.expanded and inner.toggles == 0
            assert app.fold.expanded and app.fold.toggles == 0

    asyncio.run(scenario())


def test_real_drag_click_copies_selection_without_folding():
    async def scenario():
        app = FoldingApp()
        async with app.run_test(size=(80, 24)) as pilot:
            fold = app.fold
            x, y = fold.body.region.offset

            def send(event_type, column):
                app.post_message(
                    event_type(
                        widget=fold.body,
                        x=x + column,
                        y=y,
                        delta_x=column,
                        delta_y=0,
                        button=1,
                        shift=False,
                        meta=False,
                        ctrl=False,
                        screen_x=x + column,
                        screen_y=y,
                    )
                )

            # App generates Click after this MouseUp because both ends target
            # the same widget; Pilot's mouse_down/up alone do not exercise it.
            send(MouseDown, 0)
            await pilot.pause()
            send(MouseMove, 15)
            await pilot.pause()
            send(MouseUp, 15)
            await pilot.pause(0.05)
            assert app.copied and "Selectable" in app.copied[-1]
            assert fold.expanded and fold.toggles == 0

    asyncio.run(scenario())
