"""The listening buffer that grows after glitches, the log lines that explain them, and the settings for it."""
import asyncio
import logging
import os
import time
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gateway.audio as audio_mod  # noqa: E402
from gateway.audio import DriftBuffer, MixedPlayout  # noqa: E402
from intercom_client.config import ConfigError, parse_client_config  # noqa: E402
from intercom_client.hub import AudioHub, LineControl, SharedLineAudio  # noqa: E402
from intercom_client.config import ClientAudio  # noqa: E402
from test_client_engine import CFG, FakeWorker, fakes, make, wait_for  # noqa: E402,F401
from test_client_live import changed  # noqa: E402

CHUNK = np.zeros(480, np.float32)  # 10 ms


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(audio_mod, "time", SimpleNamespace(monotonic=c.monotonic, sleep=time.sleep))
    return c


def run(buf, clock, steps, feed=True):
    """`steps` x 10 ms: one chunk in (if `feed`) and one chunk read per step."""
    for _ in range(steps):
        clock.t += 0.010
        if feed:
            buf.write(CHUNK)
        buf.read(480)


def glitch(buf, clock, silent_steps=8):
    run(buf, clock, 10)  # the buffer is full and playing
    run(buf, clock, silent_steps, feed=False)  # the network delivers nothing for a moment ...
    run(buf, clock, 2)  # ... and then goes on


def test_a_glitch_makes_the_buffer_deeper_until_its_limit_and_quiet_minutes_take_it_back(clock):
    seen = []
    buf = DriftBuffer(target_ms=60, adapt_to_ms=100, on_underrun=lambda gap, target: seen.append((round(gap), round(target))))
    run(buf, clock, 20)
    assert buf.underruns == 0 and buf.target_ms == pytest.approx(60)
    glitch(buf, clock)
    assert buf.underruns == 1 and buf.target_ms == pytest.approx(80)
    assert seen and seen[0][1] == 80 and seen[0][0] > 0
    glitch(buf, clock)
    glitch(buf, clock)
    assert buf.target_ms == pytest.approx(100)  # never beyond the limit
    clock.t += 125  # two quiet minutes ...
    buf.read(480)
    assert buf.target_ms == pytest.approx(90)
    clock.t += 31
    buf.read(480)
    assert buf.target_ms == pytest.approx(80)
    clock.t += 31
    buf.read(480)
    clock.t += 31
    buf.read(480)
    assert buf.target_ms == pytest.approx(60)  # and never below what was configured


def test_a_fixed_buffer_stays_where_it_is(clock):
    buf = DriftBuffer(target_ms=60)
    run(buf, clock, 20)
    glitch(buf, clock)
    glitch(buf, clock)
    assert buf.underruns == 2 and buf.target_ms == pytest.approx(60)


def test_the_end_of_a_talk_spurt_is_not_a_glitch(clock):
    buf = DriftBuffer(target_ms=60, adapt_to_ms=200)
    run(buf, clock, 20)
    run(buf, clock, 40, feed=False)  # somebody stopped talking: 400 ms of nothing
    run(buf, clock, 10)
    assert buf.underruns == 0 and buf.target_ms == pytest.approx(60)


def test_the_mixed_playout_hands_the_settings_on_and_can_be_reconfigured_while_running(clock):
    reports = []
    mp = MixedPlayout(target_ms=60, adapt_to_ms=120, on_underrun=lambda g, t: reports.append(t))
    assert mp.target_ms == 60
    for _ in range(20):
        clock.t += 0.010
        mp.write(CHUNK, 7)
        mp.read(480)
    for _ in range(8):
        clock.t += 0.010
        mp.read(480)
    for _ in range(3):
        clock.t += 0.010
        mp.write(CHUNK, 7)
    assert reports == [80.0] and mp.target_ms == pytest.approx(80)
    mp.configure(100, 0)  # now fixed at a deeper buffer
    assert mp.target_ms == pytest.approx(100)
    mp.write(CHUNK, 8)
    assert mp._streams[8].target_ms == pytest.approx(100)


def test_a_line_reports_its_glitches_to_the_log_at_most_once_a_second(caplog, clock):
    async def go():
        hub = AudioHub(ClientAudio(backend="synthetic"))
        line = SharedLineAudio(LineControl("Regie"), hub, asyncio.get_running_loop(), 60, 200)
        line.out_buf.write(CHUNK, 1)
        return line

    line = asyncio.run(go())
    with caplog.at_level(logging.INFO, logger="client.hub"):
        for _ in range(3):
            for _ in range(10):
                clock.t += 0.010
                line.out_buf.write(CHUNK, 1)
                line.out_buf.read(480)
            for _ in range(8):
                clock.t += 0.010
                line.out_buf.read(480)
            for _ in range(2):
                clock.t += 0.010
                line.out_buf.write(CHUNK, 1)
    lines = [r.getMessage() for r in caplog.records if "Aussetzer" in r.getMessage()]
    assert len(lines) == 1 and lines[0].startswith("[Regie] Aussetzer:") and "Puffer jetzt" in lines[0]
    # every glitch was counted and answered with a deeper buffer; the third one no longer got through the deeper buffer
    assert line.stats()["underruns"] == 2 and line.stats()["target_ms"] == 100


def test_sound_card_trouble_is_counted_and_logged_now_and_then(caplog):
    hub = AudioHub(ClientAudio(backend="synthetic"))
    with caplog.at_level(logging.WARNING, logger="client.hub"):
        hub.note_device_status("Ausgabe", "output underflow")
        hub.note_device_status("Ausgabe", "output underflow")
    assert hub.device_glitches == 2
    assert len([r for r in caplog.records if "Soundkarte (Ausgabe)" in r.getMessage()]) == 1


# ---------------------------------------------------------------- settings

def cfg(**audio):
    return parse_client_config({"server": {"url": "https://intercom.example", "username": "anna"},
                                "audio": {"backend": "synthetic", **audio},
                                "line": [{"name": "Regie", "production_id": "4", "line_id": "1"}]},
                               env={"INTERCOM_CLIENT_PASSWORD": "pw"})


def test_the_buffer_settings_have_defaults_and_limits():
    c = cfg()
    assert (c.audio.playout_ms, c.audio.playout_max_ms) == (60, 250)
    assert cfg(playout_ms=120, playout_max_ms=0).audio.playout_max_ms == 0
    for bad in (1001, -1, True, "big"):
        with pytest.raises(ConfigError, match="playout_max_ms"):
            cfg(playout_max_ms=bad)


def test_a_new_buffer_setting_applies_to_the_lines_that_are_connected_without_reconnecting_them():
    cfg0, eng = make()
    eng.start()
    try:
        assert wait_for(lambda: len(eng.hub._lines) == 2 and len(FakeWorker.instances) == 2)
        before = {n: id(w) for n, w in eng.workers.items()}
        result = eng.apply_config(changed(audio={"backend": "synthetic", "playout_ms": 140, "playout_max_ms": 300}))
        assert result.live and {n: id(w) for n, w in eng.workers.items()} == before
        assert all(l.out_buf.target_ms == 140 for l in eng.hub._lines)
        assert eng.hub.cfg.playout_ms == 140 and eng.hub.cfg.playout_max_ms == 300
    finally:
        eng.stop()


def test_the_engine_notices_when_its_loop_stands_still(caplog):
    cfg0, eng = make()
    caplog.set_level(logging.WARNING, logger="client.engine")
    eng.start()
    try:
        assert wait_for(lambda: len(eng.hub._lines) == 2)

        async def block():
            time.sleep(0.25)  # a long stall of everything that runs on the engine's loop

        asyncio.run_coroutine_threadsafe(block(), eng._loop).result(5)
        assert wait_for(lambda: eng.snapshot()["loop_lag_max_ms"] >= 150)
        assert wait_for(lambda: any("nicht reagiert" in r.getMessage() for r in caplog.records), timeout=3)
    finally:
        eng.stop()


def test_the_settings_dialog_has_the_buffer_fields_and_keeps_the_file_free_of_defaults(tmp_path):
    from PySide6.QtWidgets import QApplication

    from gateway import tomlio
    from intercom_client.ui.settings import ClientSettingsDialog

    app = QApplication.instance() or QApplication([])
    path = tmp_path / "client.toml"
    tomlio.save(path, {"server": {"url": "https://intercom.example", "username": "anna"}, "audio": {"backend": "synthetic"},
                       "line": [{"name": "Regie", "production_id": "4", "line_id": "1"}]})
    dlg = ClientSettingsDialog(path, device_lists=([], []))
    try:
        assert (dlg.playout.value(), dlg.playout_max.value()) == (60, 250)
        data = dlg._build()
        assert "playout_ms" not in data["audio"] and "playout_max_ms" not in data["audio"]
        dlg.playout.setValue(120)
        dlg.playout_max.setValue(0)
        data = dlg._build()
        assert data["audio"]["playout_ms"] == 120 and data["audio"]["playout_max_ms"] == 0
    finally:
        dlg.close()
        dlg.deleteLater()
        app.processEvents()
