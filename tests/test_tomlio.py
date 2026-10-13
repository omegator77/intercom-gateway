import tomllib

from gateway import tomlio


def test_roundtrip_of_all_config_shapes(tmp_path):
    data = {
        "server": {"url": "https://x.test", "ice_servers": ["stun:a", "stun:b"]},
        "audio": {"backend": "portaudio", "playout_ms": 60},
        "line": [
            {"name": 'Re"gie\1', "production_id": "4", "line_id": "1", "username": "ü gw", "password_env": "PW",
             "input_device": "DVS Receive 1-2", "input_channels": [0, 1], "in_gain_db": -12.5, "enabled": False},
            {"name": "B", "production_id": "4", "line_id": "2", "username": "gw2", "output_channels": [0]},
        ],
    }
    p = tmp_path / "gateway.toml"
    tomlio.save(p, data)
    assert tomllib.loads(p.read_text(encoding="utf-8")) == data


def test_none_values_are_omitted_and_backup_is_kept(tmp_path):
    p = tmp_path / "gateway.toml"
    tomlio.save(p, {"server": {"url": "a"}, "line": [{"name": "A", "input_device": None}]})
    assert "input_device" not in p.read_text()
    tomlio.save(p, {"server": {"url": "b"}, "line": [{"name": "A"}]})
    assert tomllib.loads((tmp_path / "gateway.toml.bak").read_text())["server"]["url"] == "a"
    assert tomllib.loads(p.read_text())["server"]["url"] == "b"


def test_missing_file_gives_skeleton(tmp_path):
    raw = tomlio.load_raw(tmp_path / "nope.toml")
    assert raw["line"] == [] and raw["audio"]["backend"] == "portaudio"
