from __future__ import annotations

from types import SimpleNamespace

from virgo.harness import prompt
from virgo.harness.settings import Paths

from .conftest import RecordingUI


def test_system_parts_preserve_prompt_text_and_classify_sources(project, tmp_path, monkeypatch):
    paths = Paths(project, tmp_path / "prompt-home")
    paths.user.mkdir()
    paths.project.mkdir()
    (paths.user / "SYSTEM.md").write_text("User identity")
    (paths.project / "SYSTEM.md").write_text("  Project identity\n")
    (paths.project / "APPEND_SYSTEM.md").write_text(" Extra system text\n")
    instructions = project / "AGENTS.md"
    monkeypatch.setattr(prompt, "context_files", lambda cwd, user: [(instructions, " Project rules\n")])
    monkeypatch.setattr(prompt.platform, "system", lambda: "TestOS")
    monkeypatch.setattr(prompt.platform, "release", lambda: "1")
    monkeypatch.setattr(prompt.time, "strftime", lambda fmt: "2026-11-11")
    session = SimpleNamespace(
        cwd=project,
        paths=paths,
        tools_for=lambda role: [SimpleNamespace(name="read", snippet="read files", description="unused")],
        sections=[
            (50, "custom", lambda role: " Custom rules "),
            (45, "memory", lambda role: "<memory>index</memory>"),
            (40, "skills", lambda role: "<skills>catalog</skills>"),
            (35, "mcp", lambda role: "<mcp>server instructions</mcp>"),
        ],
        ui=RecordingUI(),
    )
    expected = [
        "Project identity",
        "<tools>\n- read: read files\n</tools>",
        prompt.GUIDELINES,
        "Runtime extra",
        "<mcp>server instructions</mcp>",
        "<skills>catalog</skills>",
        "<memory>index</memory>",
        "Custom rules",
        f'<project_context>\n<project_instructions path="{instructions}">\nProject rules\n'
        "</project_instructions>\n</project_context>",
        "Extra system text",
        f"<environment>\nWorking directory: {project}\nPlatform: TestOS 1\nDate: 2026-11-11\n</environment>",
    ]

    parts = prompt.build_system_parts(session, extra=" Runtime extra ")
    assert [part.text for part in parts] == expected
    assert [part.category for part in parts] == [
        "System prompt", "System prompt", "System prompt", "System prompt", "MCP server instruction",
        "Skills", "Memory files", "System prompt", "Memory files", "System prompt", "System prompt",
    ]
    assert prompt.build_system(session, extra=" Runtime extra ") == "\n\n".join(expected)


def test_system_parts_keep_role_filters_and_section_errors(project, tmp_path, monkeypatch):
    paths = Paths(project, tmp_path / "prompt-home")
    paths.user.mkdir()
    (paths.user / "SYSTEM.md").write_text("Main identity only")
    monkeypatch.setattr(prompt, "context_files", lambda cwd, user: [])

    def failing_section(role):
        raise ValueError("broken")

    session = SimpleNamespace(
        cwd=project,
        paths=paths,
        tools_for=lambda role: [],
        sections=[
            (40, "skills", lambda role: "skills" if role == "main" else None),
            (45, "memory", lambda role: None),
            (50, "broken", failing_section),
        ],
        ui=RecordingUI(),
    )
    parts = prompt.build_system_parts(session, role="subagent")
    assert parts[0].text == prompt.IDENTITY["subagent"]
    assert all(part.category == "System prompt" for part in parts)
    assert len(parts) == 3
    assert "[system section broken failed: broken]" in session.ui.text
