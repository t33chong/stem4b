import os
import stat

import pytest

from stem4b.cli import main, parser
from stem4b.config import Config, load_config
from stem4b.setup import diagnose, initialize
from stem4b.storage import read_json


@pytest.fixture(autouse=True)
def isolated_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in os.environ:
        if key.startswith(("LLM_", "TTS_")):
            monkeypatch.delenv(key)


def test_no_arguments_shows_help(capsys):
    assert main([]) == 0
    help_text = capsys.readouterr().out
    assert "stem4b init" in help_text
    assert "--until narrate" in help_text


@pytest.mark.parametrize(
    "command", ["init", "doctor", "convert", "synthesize", "repair-toc", "repair-cover"]
)
def test_subcommand_help(command, capsys):
    with pytest.raises(SystemExit) as exc:
        main([command, "--help"])
    assert exc.value.code == 0
    assert f"stem4b {command}" in capsys.readouterr().out


def test_init_installs_valid_templates(tmp_path, capsys):
    assert main(["init"]) == 0
    config = load_config(tmp_path / "stem4b.toml")
    assert config.llm.model == "your-vision-model"
    assert config.tts.voice == "your-voice"
    assert "TTS_API_KEY=" in (tmp_path / ".env").read_text()
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert "doctor" in capsys.readouterr().out


@pytest.mark.parametrize("existing", [".env", "stem4b.toml"])
def test_init_never_overwrites_or_partially_initializes(tmp_path, existing):
    (tmp_path / existing).write_text("private original")
    with pytest.raises(ValueError, match="Nothing written"):
        initialize(tmp_path)
    assert list(tmp_path.iterdir()) == [tmp_path / existing]
    assert (tmp_path / existing).read_text() == "private original"


def test_init_rejects_dangling_symlink(tmp_path):
    (tmp_path / ".env").symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError, match="Nothing written"):
        initialize(tmp_path)
    assert not (tmp_path / "absent").exists()


def test_init_new_directory(tmp_path):
    assert main(["init", "nested/config"]) == 0
    assert (tmp_path / "nested/config/stem4b.toml").is_file()


def test_doctor_reports_missing_settings_and_binaries(monkeypatch):
    monkeypatch.setattr("stem4b.setup.shutil.which", lambda _: None)
    messages, ready = diagnose(Config())
    assert not ready
    assert sum(message.startswith("FAIL") for message in messages) == 4
    assert "Offline checks only" in messages[0]


def test_doctor_extract_needs_no_keys_models_or_ffmpeg(monkeypatch, capsys):
    monkeypatch.setattr("stem4b.setup.shutil.which", lambda _: None)
    assert main(["doctor", "--stage", "extract"]) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_doctor_narration_does_not_require_tts(monkeypatch):
    monkeypatch.setattr("stem4b.setup.shutil.which", lambda _: None)
    config = Config()
    config.llm.model = "vision"
    messages, ready = diagnose(config, "narrate")
    assert ready
    assert any(message.startswith("WARN llm") for message in messages)


def test_doctor_checks_only_files_needed_for_the_stage():
    config = Config()
    config.llm.model = "vision"
    config.narration.instructions_file = "missing-instructions.md"
    assert diagnose(config, "extract")[1]
    messages, ready = diagnose(config, "narrate")
    assert not ready
    assert "narration.instructions_file" in "\n".join(messages)


def test_doctor_hides_keys_and_checks_voice(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "secret-do-not-print")
    monkeypatch.setattr("stem4b.setup.shutil.which", lambda _: "/bin/mock")
    config = Config()
    config.llm.model = "vision"
    config.tts.model = "speech"
    config.tts.voice = "your-voice"
    messages, ready = diagnose(config)
    assert not ready
    assert "secret-do-not-print" not in "\n".join(messages)
    assert "tts.voice" in "\n".join(messages)


def test_invalid_configuration_has_field_path_not_input_dump(tmp_path, caplog):
    config = tmp_path / "broken.toml"
    config.write_text('[llm]\nmodel="vision"\nretries="not-a-number"\n')
    assert main(["doctor", "-c", str(config)]) == 1
    assert "llm.retries" in caplog.text
    assert "not-a-number" not in caplog.text


def test_malformed_toml_names_file(tmp_path):
    config = tmp_path / "broken.toml"
    config.write_text("[llm")
    with pytest.raises(ValueError, match="broken.toml"):
        load_config(config)


def test_endpoint_table_error_is_actionable(tmp_path, monkeypatch):
    config = tmp_path / "broken.toml"
    config.write_text('llm="vision"')
    monkeypatch.setenv("LLM_MODEL", "test")
    with pytest.raises(ValueError, match="TOML table"):
        load_config(config)


def test_config_and_env_precedence(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "settings"
    initialize(folder)
    (folder / ".env").write_text("LLM_MODEL=from-file\nLLM_API_KEY=private-value\n")
    monkeypatch.setenv("LLM_MODEL", "from-shell")
    # Track this variable before dotenv mutates it, so monkeypatch restores it on teardown.
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.delenv("LLM_API_KEY")
    assert main(["doctor", "-c", str(folder / "stem4b.toml"), "--stage", "narrate"]) == 0
    assert os.environ["LLM_MODEL"] == "from-shell"
    assert "private-value" not in capsys.readouterr().out


def test_default_and_legacy_config_discovery(tmp_path, caplog):
    (tmp_path / "audiobook.toml").write_text('[llm]\nmodel="old-model"')
    assert main(["doctor", "--stage", "narrate"]) == 0
    assert "legacy audiobook.toml" in caplog.text
    caplog.clear()
    (tmp_path / "stem4b.toml").write_text('[llm]\nmodel="new-model"')
    assert main(["doctor", "--stage", "narrate"]) == 0
    assert "legacy" not in caplog.text


def test_convert_default_output(pdf_book, tmp_path):
    assert main(["convert", str(pdf_book), "--until", "extract"]) == 0
    assert (pdf_book.with_suffix(".work") / "plan.json").is_file()
    assert not pdf_book.with_suffix(".m4b").exists()


def test_synthesis_still_requires_explicit_output():
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(["synthesize", "narration.txt"])
    assert exc.value.code == 2


def test_corrupt_cache_identifies_file(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text('{"incomplete":')
    with pytest.raises(ValueError, match=r"Invalid JSON in .*cache.json.*line 1"):
        read_json(cache)
