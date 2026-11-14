"""Skills: folders with a SKILL.md, disclosed progressively.

Only name, description and location appear in the system prompt; the model reads the file with
`read` when a task matches. Users can force a skill with ``/skill:name [args]``.
Locations: ``~/.virgo/skills``, ``.virgo/skills``, ``.agents/skills`` (each ``<dir>/<skill>/SKILL.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from ..harness.commands import parse_frontmatter
from ..harness.extensions import ExtensionAPI


@dataclass
class Skill:
    name: str
    description: str
    path: Path

    def body(self) -> str:
        return parse_frontmatter(self.path.read_text(encoding="utf-8"))[1].strip()


def discover(roots: list[Path]) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for root in roots:
        if not root.is_dir():
            continue
        real_root = root.resolve()
        for f in sorted(root.glob("*/SKILL.md")):
            try:
                rf = f.resolve()
                rf.relative_to(real_root)  # ignore symlinks that escape the skills dir
            except ValueError:
                continue
            meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
            name = str(meta.get("name") or f.parent.name).strip()
            desc = " ".join(str(meta.get("description") or "").split())
            if not desc:
                first = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
                desc = first
            skills[name] = Skill(name, desc, rf)  # later roots (project) override earlier (user)
    return skills


def setup(api: ExtensionAPI) -> None:
    roots = [*api.paths.both("skills"), api.cwd / ".agents" / "skills"]
    skills = discover(roots)
    api.services["skills"] = skills

    def section(role: str):
        if not skills:
            return None
        items = "\n".join(
            f"<skill><name>{escape(s.name)}</name><description>{escape(s.description)}</description>"
            f"<location>{escape(str(s.path))}</location></skill>"
            for s in skills.values()
        )
        return (
            "<skills>\nSkills provide optional specialized instructions. Read a skill's SKILL.md before using it. "
            "Paths mentioned inside a skill are "
            "relative to the skill's directory.\n"
            f"<available_skills>\n{items}\n</available_skills>\n</skills>"
        )

    api.add_system_section("skills", section, order=40)

    def on_input(text: str, session):
        if not text.startswith("/skill:"):
            return None
        head, _, args = text[len("/skill:") :].partition(" ")
        sk = skills.get(head.strip())
        if sk is None:
            from ..harness.extensions import Handled

            return Handled(
                api.t(
                    "features.skills.unknown",
                    name=head,
                    available=", ".join(skills) or api.t("features.skills.none"),
                )
            )
        block = f'<skill name="{sk.name}" location="{sk.path}">\nPaths are relative to {sk.path.parent}.\n\n{sk.body()}\n</skill>'
        return f"{block}\n\n{args.strip()}" if args.strip() else block

    api.on("input", on_input, priority=10)

    def cmd(session, args):
        if not skills:
            api.ui.print(api.t("features.skills.empty"))
            return
        api.ui.print("\n".join(f"  {s.name:<20} {s.description}" for s in skills.values()))

    api.register_command(
        "skills",
        cmd,
        "list available skills (/skill:name to use one)",
        concurrent=True,
        description_key="features.commands.skills",
    )
