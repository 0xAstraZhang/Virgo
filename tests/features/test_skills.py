"""Skills are listed in the system prompt and can be forced with /skill."""

from __future__ import annotations

import pytest

from virgo.ai.providers import faux

from tests.support.harness import calls, last_message_text


def test_skills_listed_and_forced(make_session, project):
    d = project / ".virgo" / "skills" / "pdf"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: pdf\ndescription: Work with PDF files\n---\n# PDF\nUse pypdf.")
    s = make_session(faux.reply("ok"), faux.reply("ok"))
    s.prompt("hi")
    assert "<name>pdf</name>" in calls()[0]["system"] and str(d / "SKILL.md") in calls()[0]["system"]
    s.prompt("/skill:pdf merge a.pdf b.pdf")
    assert "Use pypdf." in last_message_text(calls()[1]) and "merge a.pdf" in last_message_text(calls()[1])


@pytest.mark.parametrize('resource', ['skills', 'agents-skills', 'prompts'])
def test_project_resource_aliases_do_not_read_outside_task(broker_session, project, resource):
    outside = project.parent / 'private-resources'
    outside.mkdir()
    if resource == 'prompts':
        (outside/'leak.md').write_text('---\ndescription: private-resource-canary\n---\nprivate body')
        root = project/'.virgo'/'prompts'
    else:
        (outside/'leak').mkdir()
        (outside/'leak'/'SKILL.md').write_text('---\nname: leak\ndescription: private-resource-canary\n---\nprivate body')
        root = project/('.agents' if resource == 'agents-skills' else '.virgo')/'skills'
    root.parent.mkdir(exist_ok=True)
    root.symlink_to(outside, target_is_directory=True)
    session = broker_session(faux.reply('done'))
    session.prompt('Describe the available project instructions')
    assert 'private-resource-canary' not in calls()[0]['system']
    assert 'leak' not in session.templates
