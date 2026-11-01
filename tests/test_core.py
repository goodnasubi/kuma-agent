from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.memory import Memory
from kuma_agent.persona import DEFAULT_SOUL, load_persona
from kuma_agent.tools import ToolBox


def test_persona_default_when_no_files(tmp_path: Path):
    assert load_persona(tmp_path) == DEFAULT_SOUL


def test_persona_reads_files(tmp_path: Path):
    (tmp_path / "SOUL.md").write_text("# 魂\nテスト人格", encoding="utf-8")
    assert "テスト人格" in load_persona(tmp_path)


def test_memory_roundtrip(tmp_path: Path):
    mem = Memory(tmp_path)
    mem.remember("好きな食べ物は寿司")
    assert "寿司" in mem.load_long_term()
    assert mem.search("寿司")
    assert mem.search("ラーメン") == []


def test_toolbox_file_roundtrip(tmp_path: Path):
    box = ToolBox(tmp_path)
    assert "wrote" in box.call("write_file", {"path": "a.txt", "content": "hi"})
    assert box.call("read_file", {"path": "a.txt"}) == "hi"


def test_toolbox_blocks_escape(tmp_path: Path):
    box = ToolBox(tmp_path)
    assert box.call("read_file", {"path": "../secret"}).startswith("error:")


def test_config_paths(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    assert cfg.memory_file == tmp_path / "MEMORY.md"
