"""The intercom client's core without any network or sound card: config, hotkeys, talk state and the audio hub."""
import asyncio

import numpy as np
import pytest

from gateway.audio import FRAME, SR
from intercom_client.config import ClientAudio, ConfigError, parse_client_config
from intercom_client.hotkeys import Combo, HotkeyError, HotkeyMatcher, normalize_key, parse_combo, pynput_key_name
from intercom_client.hub import BLOCK, AudioHub, LineControl, SharedLineAudio
from intercom_client.talk import TalkControl

BASE = {
    "server": {"url": "https://intercom.example/", "username": "anna"},
    "audio": {"backend": "synthetic"},
    "line": [
        {"name": "Regie", "production_id": 4, "line_id": 1, "ptt_key": "f1", "latch_key": "shift+f1"},
        {"name": "Technik", "production_id": 4, "line_id": 2, "volume": 80},
    ],
}


def cfg(**over):
    data = {**BASE, **over}
    return parse_client_config(data, env={"INTERCOM_CLIENT_PASSWORD": "pw"})


# ---------------------------------------------------------------- config

def test_config_defaults_and_values():
    c = cfg()
    assert c.server_url == "https://intercom.example" and c.username == "anna"
    assert c.password == "pw"
    assert [l.name for l in c.lines] == ["Regie", "Technik"]
    assert c.lines[0].volume_pct == 100 and c.lines[1].volume_pct == 80
    assert c.audio == ClientAudio(backend="synthetic")
    assert set(c.hotkey_bindings()) == {"ptt:Regie", "latch:Regie"}


def test_config_password_comes_from_the_store_and_an_old_display_name_key_is_ignored():
    data = {**BASE, "server": {"url": "http://x", "username": "anna", "display_name": "Anna"}}
    c = parse_client_config(data, env={}, lookup_password=lambda url, user: "stored")
    assert c.password == "stored"
    with pytest.raises(ConfigError, match="password"):
        parse_client_config(data, env={}, lookup_password=lambda url, user: None)


@pytest.mark.parametrize("change, message", [
    ({"server": {"url": "ftp://x", "username": "a"}}, "http"),
    ({"server": {"url": "http://x"}}, "username"),
    ({"audio": {"backend": "alsa"}}, "backend"),
    ({"audio": {"playout_ms": 5}}, "playout_ms"),
    ({"audio": {"mic_gain_db": 99}}, "mic_gain_db"),
    ({"audio": {"input_channel": -1}}, "input_channel"),
    ({"audio": {"output_channels": []}}, "output_channels"),
    ({"line": []}, "no .*line"),
    ({"line": [{"name": "A", "production_id": 1}]}, "line_id"),
    ({"line": [{"name": "A", "production_id": 1, "line_id": 1, "volume": 200}]}, "volume"),
    ({"line": [{"name": "A", "production_id": 1, "line_id": 1, "ptt_key": "ctrl+"}]}, "ptt_key"),
    ({"line": [{"name": "A", "production_id": 1, "line_id": 1}, {"name": "A", "production_id": 1, "line_id": 2}]},
     "duplicate"),
    ({"companion": {"url": "http://x"}}, "ws://"),
])
def test_config_rejects_bad_values(change, message):
    with pytest.raises(ConfigError, match=message):
        cfg(**change)


def test_config_rejects_one_key_for_two_things():
    lines = [{"name": "A", "production_id": 1, "line_id": 1, "ptt_key": "F2"},
             {"name": "B", "production_id": 1, "line_id": 2, "latch_key": "f2"}]
    with pytest.raises(ConfigError, match="same key"):
        cfg(line=lines)
    with pytest.raises(ConfigError, match="same key"):
        cfg(hotkeys={"global_mute": "f2"}, line=lines[:1])


# ---------------------------------------------------------------- hotkeys

def test_parse_combo():
    assert parse_combo("Ctrl+Shift+F1") == Combo(frozenset({"ctrl", "shift"}), "f1")
    assert parse_combo("Control + Return") == Combo(frozenset({"ctrl"}), "enter")
    assert str(parse_combo("f1+SHIFT+alt")) == "alt+shift+f1"
    for bad in ("", "ctrl", "ctrl+", "a+b"):
        with pytest.raises(ValueError):
            parse_combo(bad)
    assert normalize_key("Ctrl_L") == "ctrl" and normalize_key("Escape") == "esc"


def matcher():
    return HotkeyMatcher({"ptt:A": parse_combo("f1"), "latch:A": parse_combo("shift+f1"),
                          "global_mute": parse_combo("ctrl+alt+m")})


def test_hotkey_down_and_up_for_a_plain_key():
    m = matcher()
    assert m.press("f1") == [("ptt:A", "down")]
    assert m.release("f1") == [("ptt:A", "up")]
    assert m.press("f2") == [] and m.release("f2") == []


def test_hotkey_autorepeat_is_ignored_until_the_key_is_released():
    m = matcher()
    assert m.press("f1") == [("ptt:A", "down")]
    assert m.press("f1") == [] and m.press("f1") == []
    assert m.release("f1") == [("ptt:A", "up")]
    assert m.press("f1") == [("ptt:A", "down")]


def test_hotkey_modifiers_decide_which_binding_fires():
    m = matcher()
    m.press("shift_l")
    assert m.press("f1") == [("latch:A", "down")]
    assert m.release("f1") == [("latch:A", "up")]
    m.release("shift_l")
    assert m.press("f1") == [("ptt:A", "down")]  # without shift it is the push-to-talk again


def test_hotkey_ends_when_a_modifier_is_let_go_first():
    m = matcher()
    for k in ("ctrl_l", "alt_l"):
        m.press(k)
    assert m.press("m") == [("global_mute", "down")]
    assert m.release("alt_l") == [("global_mute", "up")]
    assert m.release("m") == []  # already ended, no second "up"


def test_hotkey_release_all_ends_everything_that_is_held():
    m = matcher()
    m.press("f1")
    assert m.release_all() == [("ptt:A", "up")]
    assert m.release_all() == []
    assert m.press("f1") == [("ptt:A", "down")]


def test_hotkey_same_combo_twice_is_an_error():
    with pytest.raises(HotkeyError):
        HotkeyMatcher({"a": parse_combo("f1"), "b": parse_combo("F1")})


def test_pynput_names():
    class K:
        def __init__(self, vk=None, name=None, char=None):
            self.vk, self.name, self.char = vk, name, char

    assert pynput_key_name(K(vk=0x4D, char="\r")) == "m"  # with Ctrl held pynput reports a control character
    assert pynput_key_name(K(vk=0x31)) == "1"
    assert pynput_key_name(K(vk=0x61)) == "num1"
    assert pynput_key_name(K(vk=112, name="f1")) == "f1"
    assert pynput_key_name(K(name="ctrl_l")) == "ctrl"
    assert pynput_key_name(K(char="Ä")) == "ä"
    assert pynput_key_name(K()) is None


# ---------------------------------------------------------------- talk state

def test_talk_hold_and_latch():
    t = TalkControl()
    assert not t.transmitting
    t.press()
    assert t.transmitting
    t.release()
    assert not t.transmitting
    t.toggle_latch()
    assert t.transmitting and t.latched
    t.press()
    t.release()
    assert t.transmitting  # releasing the key does not end a latch
    t.toggle_latch()
    assert not t.transmitting
    t.press()
    t.toggle_latch()
    t.stop_all()
    assert not t.transmitting and not t.held and not t.latched


# ---------------------------------------------------------------- audio hub

def make_hub(loop, **kw):
    hub = AudioHub(ClientAudio(backend="synthetic", **kw))
    lines = {}
    for name in ("Regie", "Technik"):
        la = SharedLineAudio(LineControl(name), hub, loop, playout_ms=60)
        hub.attach(la)
        lines[name] = la
    return hub, lines


def drain(track):
    """What the line would send: the 10 ms blocks pushed so far as one array."""
    out = []
    while True:
        got = track._take()
        if got is None:
            return np.concatenate(out) if out else np.zeros(0, np.float32)
        out.append(got)


def test_mic_goes_only_to_lines_that_are_being_talked_into():
    async def go():
        hub, lines = make_hub(asyncio.get_running_loop())
        tone = np.full(BLOCK, 0.2, np.float32)
        lines["Regie"].control.talk.press()
        for _ in range(4):
            hub.process_mic(tone)
        assert np.allclose(drain(lines["Regie"].track), 0.2)
        silent = drain(lines["Technik"].track)
        assert len(silent) > 0 and not np.any(silent)  # still fed, so the connection keeps its pace
    asyncio.run(go())


def test_latch_keeps_talking_after_the_key_is_released_and_global_mute_overrides_everything():
    async def go():
        hub, lines = make_hub(asyncio.get_running_loop())
        tone = np.full(BLOCK, 0.2, np.float32)
        c = lines["Regie"].control
        c.talk.press(); c.talk.toggle_latch(); c.talk.release()
        for _ in range(2):
            hub.process_mic(tone)
        assert np.allclose(drain(lines["Regie"].track), 0.2)
        hub.global_mute = True
        for _ in range(2):
            hub.process_mic(tone)
        assert not np.any(drain(lines["Regie"].track))
        hub.global_mute = False
        for _ in range(2):
            hub.process_mic(tone)
        assert np.allclose(drain(lines["Regie"].track), 0.2)
    asyncio.run(go())


def test_mic_gain_is_applied_and_loud_input_is_limited_not_clipped():
    async def go():
        hub, lines = make_hub(asyncio.get_running_loop(), mic_gain_db=6.0)
        lines["Regie"].control.talk.press()
        hub.process_mic(np.full(BLOCK, 0.1, np.float32))
        hub.process_mic(np.full(BLOCK, 0.9, np.float32))
        got = drain(lines["Regie"].track)
        assert abs(got[0] - 0.1 * 10 ** (6 / 20)) < 1e-4
        assert np.all(np.abs(got) <= 1.0) and got[-1] > 0.9  # 0.9 +6 dB would be 1.8: soft limited
    asyncio.run(go())


def feed(line, value, ms=400):
    n = SR * ms // 1000
    line.on_remote_pcm(np.full(n, value, np.float32), 1)


def test_output_mixes_lines_with_their_own_volume_and_mute():
    async def go():
        hub, lines = make_hub(asyncio.get_running_loop())
        feed(lines["Regie"], 0.2)
        feed(lines["Technik"], 0.1)
        hub.mix_output(BLOCK)  # lets the playout buffers start
        out = hub.mix_output(BLOCK)
        assert np.allclose(out, 0.3, atol=1e-3)

        lines["Regie"].control.volume_pct = 50
        out = hub.mix_output(BLOCK)
        assert np.allclose(out, 0.1 + 0.1, atol=1e-3)  # Regie at 50 %: 0.1, Technik 0.1

        lines["Technik"].control.listen_mute = True
        out = hub.mix_output(BLOCK)
        assert np.allclose(out, 0.1, atol=1e-3)
        assert lines["Technik"].out_buf.fill_ms < 400  # a muted line still drains, it does not pile up
    asyncio.run(go())


def test_output_sum_is_limited_below_full_scale():
    async def go():
        hub, lines = make_hub(asyncio.get_running_loop())
        feed(lines["Regie"], 0.9)
        feed(lines["Technik"], 0.9)
        hub.mix_output(BLOCK)
        out = hub.mix_output(BLOCK)
        assert np.all(out <= 1.0) and out[0] > 0.9
    asyncio.run(go())


def test_a_line_can_be_replaced_while_running_and_detach_stops_feeding_it():
    async def go():
        hub, lines = make_hub(asyncio.get_running_loop())
        old = lines["Regie"]
        new = SharedLineAudio(old.control, hub, asyncio.get_running_loop(), playout_ms=60)
        await new.start()  # a reconnect: same line name, fresh audio object
        assert len(hub._lines) == 2 and new in hub._lines and old not in hub._lines
        await new.stop()
        assert new not in hub._lines
        old.control.talk.press()
        hub.process_mic(np.full(BLOCK, 0.2, np.float32))
        assert drain(new.track).size == 0
    asyncio.run(go())


def test_stats_and_levels_have_the_keys_the_worker_reports():
    async def go():
        hub, lines = make_hub(asyncio.get_running_loop())
        la = lines["Regie"]
        assert set(la.stats()) >= {"talkers", "fill_ms", "ratio", "underruns", "overruns", "in_stalls"}
        assert len(la.levels()) == 2
    asyncio.run(go())
