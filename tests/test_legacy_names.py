"""Arquivos salvos quando o programa se chamava TraceCAM continuam abrindo no BrokCAM."""
import json

from brokcam.cam.library import ToolLibrary
from brokcam.ui.project import read_project_file


def test_old_project_format_is_accepted(tmp_path):
    p = tmp_path / "antigo.tcam"
    p.write_text(json.dumps({"format": "tracecam-project", "version": 1, "layers": [], "jobs": []}),
                 encoding="utf-8")
    assert read_project_file(str(p))["format"] == "tracecam-project"


def test_old_tool_library_is_accepted():
    data = {"format": "tracecam-tools", "version": 1, "tools": [{"name": "Broca 0,8", "shape": "drill",
                                                                "diameter": 0.8}]}
    tools = ToolLibrary.parse(data)
    assert tools and tools[0].name == "Broca 0,8"


def test_config_migrates_from_old_folder(tmp_path, monkeypatch):
    from brokcam.ui import settings
    old = tmp_path / "TraceCAM"
    old.mkdir()
    (old / "tools.json").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("BROKCAM_CONFIG_DIR", raising=False)
    monkeypatch.delenv("TRACECAM_CONFIG_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = settings.config_dir()
    assert d.name == "BrokCAM" and (d / "tools.json").exists()
