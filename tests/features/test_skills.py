"""Skills are listed in the system prompt and can be forced with /skill."""

from __future__ import annotations

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
