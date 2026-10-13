import pytest

from gateway.config import ConfigError, parse_config

BASE = {
    "server": {"url": "https://example.test/"},
    "audio": {"backend": "synthetic"},
    "line": [{"name": "Regie", "production_id": 2, "line_id": 1, "username": "gw", "password_env": "PW"}],
}


def cfg(**over):
    d = {**BASE, **over}
    return parse_config(d, env={"PW": "secret"})


def test_minimal_config_and_defaults():
    c = cfg()
    assert c.server.url == "https://example.test"  # trailing slash stripped
    line = c.lines[0]
    assert (line.production_id, line.line_id, line.password) == ("2", "1", "secret")
    assert line.input_channels == (0,) and line.output_channels == (0, 1)
    assert c.audio.playout_ms == 60


def test_missing_env_password_is_an_error():
    with pytest.raises(ConfigError, match="PW"):
        parse_config(BASE, env={})


def test_password_fallback_and_requirement():
    line = {"name": "A", "production_id": 1, "line_id": 1, "username": "u"}
    with pytest.raises(ConfigError, match="password"):
        parse_config({**BASE, "line": [line]}, env={})
    c = parse_config({**BASE, "line": [{**line, "password": "plain"}]}, env={})
    assert c.lines[0].password == "plain"


@pytest.mark.parametrize("bad", [
    {"server": {"url": "ftp://x"}},
    {"audio": {"backend": "asio"}},
    {"audio": {"backend": "synthetic", "playout_ms": 5}},
    {"line": []},
])
def test_invalid_sections_rejected(bad):
    with pytest.raises(ConfigError):
        cfg(**bad)


def test_line_validation():
    base = BASE["line"][0]
    with pytest.raises(ConfigError, match="duplicate"):
        cfg(line=[base, base])
    with pytest.raises(ConfigError, match="mixed"):
        cfg(line=[{**base, "relay_type": "ssrc-rewrite"}])
    with pytest.raises(ConfigError, match="in_gain_db"):
        cfg(line=[{**base, "in_gain_db": 99}])
    with pytest.raises(ConfigError, match="input_channels"):
        cfg(line=[{**base, "input_channels": []}])
    with pytest.raises(ConfigError, match="production_id"):
        cfg(line=[{**base, "production_id": ""}])


def test_disabled_line_is_skipped_and_portaudio_needs_a_device():
    other = {**BASE["line"][0], "name": "B", "enabled": False}
    assert len(cfg(line=[BASE["line"][0], other]).lines) == 1
    with pytest.raises(ConfigError, match="input_device"):
        parse_config({**BASE, "audio": {"backend": "portaudio"}}, env={"PW": "x"})
