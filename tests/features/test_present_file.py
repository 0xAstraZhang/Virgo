from pathlib import Path

from virgo.ai.providers import faux

from tests.support.harness import tool_results


def test_present_verified_path_and_refuse_typo_or_directory(broker_session, project):
    paper = project / "Long paper title [v1]_KV-Cache-Migration.pdf"
    paper.write_bytes(b"%PDF-test")
    paths = [paper.name, paper.name.replace("_", "-"), "."]
    session = broker_session(faux.reply(tools=[("present_file", {"path": path}) for path in paths]),
                             faux.reply("Done"), persist=True)
    session.prompt("Deliver the downloaded paper.")
    success, missing, directory = tool_results(session)
    assert not success.is_error, success.text()
    assert success.details["presented_file"] == str(paper.resolve())
    assert Path(success.text()).read_bytes() == b"%PDF-test"
    for failure in (missing, directory):
        assert failure.is_error and "presented_file" not in (failure.details or {})


def test_present_file_obeys_protected_reads(broker_session):
    session = broker_session()
    target = session.paths.user / "auth.json"
    faux.push("test", faux.reply(tools=[("present_file", {"path": str(target)})]), faux.reply("Done"))
    session.prompt("Present this file.")
    result, = tool_results(session)
    assert result.is_error
    assert result.details["boundary"]["dimension"] == "protected_path"
    assert "presented_file" not in result.details
