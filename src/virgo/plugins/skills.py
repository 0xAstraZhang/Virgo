"""Skills disclosed through the originating task's file access interface.

User skills are trusted instruction configuration. Project skills and forced
skill reads use the captured resource context, including during child setup.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from ..harness.commands import parse_frontmatter
from ..harness.extensions import ExtensionAPI, Handled


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    access: object = None

    def body(self) -> str:
        data = self.access.read_bytes(self.path) if self.access else self.path.read_bytes()
        if data is None:
            raise OSError('Skill resource is unavailable')
        return parse_frontmatter(data.decode('utf-8'))[1].strip()


def discover(roots: list[Path], *, access=None) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for root in roots:
        try:
            files = (p for p, _ in access.walk_files(root, set(), max_depth=2)
                     if p.name == 'SKILL.md' and len(p.relative_to(root).parts) == 2) if access else root.glob('*/SKILL.md')
            files = sorted(files)
        except OSError:
            continue
        real_root = root.resolve()
        for file in files:
            try:
                file.resolve().relative_to(real_root)
                data = access.read_bytes(file) if access else file.read_bytes()
                if data is None:
                    continue
                meta, body = parse_frontmatter(data.decode('utf-8'))
            except (OSError, ValueError):
                continue
            name = str(meta.get('name') or file.parent.name).strip()
            description = ' '.join(str(meta.get('description') or '').split())
            if not description:
                description = next((line.strip('# ').strip() for line in body.splitlines() if line.strip()), '')
            skills[name] = Skill(name, description, file, access)
    return skills


def setup(api: ExtensionAPI) -> None:
    trusted = discover([api.paths.user / 'skills'])
    api.services['skills'] = trusted

    def catalog(access):
        workspace = access.workspace
        available = {**trusted, **discover([workspace / '.virgo' / 'skills', workspace / '.agents' / 'skills'], access=access)}
        api.services['skills'] = available  # Derived display state; never an authority source.
        return available

    def section(context):
        available = catalog(context.execution_context)
        if not available:
            return None
        items = '\n'.join(
            f'<skill><name>{escape(skill.name)}</name><description>{escape(skill.description)}</description>'
            f'<location>{escape(str(skill.path))}</location></skill>' for skill in available.values()
        )
        return ('<skills>\nSkills provide optional specialized instructions. Read a skill\'s SKILL.md before using it. '
                'Paths mentioned inside a skill are relative to the skill\'s directory.\n'
                f'<available_skills>\n{items}\n</available_skills>\n</skills>')

    api.add_runtime_system_section('skills', section, order=40)

    def on_input(text, session):
        if not text.startswith('/skill:'):
            return None
        head, _, args = text[len('/skill:'):].partition(' ')
        available = catalog(session.main.execution_context)
        skill = available.get(head.strip())
        if skill is None:
            return Handled(api.t('features.skills.unknown', name=head,
                                 available=', '.join(available) or api.t('features.skills.none')))
        try:
            body = skill.body()
        except OSError as error:
            return Handled(f'Skill resource denied: {error}')
        block = f'<skill name="{skill.name}" location="{skill.path}">\nPaths are relative to {skill.path.parent}.\n\n{body}\n</skill>'
        return f'{block}\n\n{args.strip()}' if args.strip() else block

    api.on('input', on_input, priority=10)

    def command(session, args):
        available = catalog(session.main.execution_context)
        if not available:
            api.ui.print(api.t('features.skills.empty'))
            return
        api.ui.print('\n'.join(f'  {skill.name:<20} {skill.description}' for skill in available.values()))

    api.register_command('skills', command, 'list available skills (/skill:name to use one)', concurrent=True,
                         description_key='features.commands.skills')
