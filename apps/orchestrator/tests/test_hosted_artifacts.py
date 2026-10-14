"""Hosted runner local files are ingested only from harness workdirs."""

from __future__ import annotations

from artifacts import hosted_local_files, safe_hosted_artifact_path


def test_safe_path_rejects_outside_workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_WORKDIR", str(tmp_path / "ws"))
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    assert safe_hosted_artifact_path(str(outside)) is None


def test_hosted_local_files_reads_workdir_artifact(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("HARNESS_WORKDIR", str(ws))
    f = ws / "t1" / "hermes" / "result.md"
    f.parent.mkdir(parents=True)
    f.write_text("# hosted\n", encoding="utf-8")
    files = hosted_local_files({"hosted": True, "artifact": str(f)})
    assert len(files) == 1
    name, body, mime = files[0]
    assert name == "result.md"
    assert b"# hosted" in body
    assert "markdown" in mime
