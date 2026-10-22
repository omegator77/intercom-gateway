"""Echo/noise processing and dimming of the programme line in the client: config, timing, hub and engine."""
import asyncio
import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import intercom_client.engine as engine_mod  # noqa: E402
import intercom_client.hub as hub_mod  # noqa: E402
from gateway.audio import SR  # noqa: E402
from intercom_client.config import ClientAudio, ClientDim, ConfigError, parse_client_config  # noqa: E402
from intercom_client.hub import BLOCK, AudioHub, Dimmer, LineControl, SharedLineAudio  # noqa: E402
from intercom_client.processing import FRAME, MicProcessor, ProcessingUnavailable  # noqa: E402

BASE = {
    "server": {"url": "https://intercom.example", "username": "anna"},
    "audio": {"backend": "synthetic"},
    "line": [{"name": "Regie", "production_id": 4, "line_id": 1}],
}


def cfg(**over):
    return parse_client_config({**BASE, **over}, env={"INTERCOM_CLIENT_PASSWORD": "pw"})


# ---------------------------------------------------------------- config

def test_new_settings_default_to_the_old_behaviour():
    c = cfg()
    assert (c.audio.echo_cancellation, c.audio.noise_suppression, c.audio.auto_gain) == (False, 0, False)
    assert c.dim == ClientDim(amount_db=-14, when_addressed=True, while_speaking=True, delay_ms=1000)


def test_processing_and_dim_settings_are_read():
    c = cfg(audio={"backend": "synthetic", "echo_cancellation": True, "noise_suppression": 3, "auto_gain": True},
            dim={"amount_db": -20, "when_addressed": False, "while_speaking": True, "delay_ms": 250})
    assert (c.audio.echo_cancellation, c.audio.noise_suppression, c.audio.auto_gain) == (True, 3, True)
    assert c.dim == ClientDim(-20, False, True, 250)


@pytest.mark.parametrize("change, message", [
    ({"audio": {"noise_suppression": 5}}, "noise_suppression"),
    ({"audio": {"noise_suppression": True}}, "noise_suppression"),
    ({"audio": {"echo_cancellation": "yes"}}, "echo_cancellation"),
    ({"audio": {"auto_gain": 1}}, "auto_gain"),
    ({"dim": {"amount_db": 3}}, "amount_db"),
    ({"dim": {"amount_db": -41}}, "amount_db"),
    ({"dim": {"delay_ms": -1}}, "delay_ms"),
    ({"dim": {"delay_ms": 9000}}, "delay_ms"),
    ({"dim": {"when_addressed": "no"}}, "when_addressed"),
])
def test_bad_processing_and_dim_settings_are_rejected(change, message):
    with pytest.raises(ConfigError, match=message):
        cfg(**change)


# ---------------------------------------------------------------- the dimmer

def run_dimmer(d, pattern, step=0.01):
    """pattern: list of (speaking, seconds); returns (time, dimmed, gain) per step."""
    out, t = [], 0.0
    for speaking, seconds in pattern:
        for _ in range(round(seconds / step)):
            g0, g1 = d.step(speaking, step)
            t += step
            out.append((t, d.dimmed, g1))
    return out


def test_dimmer_waits_for_the_delay_both_ways_like_the_web_app():
    d = Dimmer(ClientDim(amount_db=-14, delay_ms=1000))
    trace = run_dimmer(d, [(True, 2.5), (False, 2.5)])
    start = next(t for t, dimmed, _ in trace if dimmed)
    assert 0.95 <= start <= 1.06  # speech has to last a second before dimming starts
    end = next(t for t, dimmed, _ in trace if t > 2.5 and not dimmed)
    assert 3.45 <= end <= 3.56  # and the dim stays a second after the speech stopped
    assert min(g for *_, g in trace) == pytest.approx(10 ** (-14 / 20), abs=1e-3)  # -14 dB = x0.2
    assert trace[-1][2] == 1.0


def test_a_pause_shorter_than_the_delay_does_not_release_the_dim():
    d = Dimmer(ClientDim(delay_ms=1000))
    trace = run_dimmer(d, [(True, 1.5), (False, 0.6), (True, 1.0), (False, 0.5)])
    assert all(dimmed for t, dimmed, _ in trace if t > 1.2)  # the 0.6 s gap and the 0.5 s tail never reach 1 s


def test_dimmer_glides_instead_of_jumping():
    d = Dimmer(ClientDim(amount_db=-20, delay_ms=0))
    gains = [g1 for *_, g1 in run_dimmer(d, [(True, 0.5)])]
    assert gains[0] > 0.8  # first 10 ms: only a tenth of the way down
    steps = np.abs(np.diff([1.0, *gains]))
    assert steps.max() <= (1 - 0.1) * 0.01 / 0.1 + 1e-9
    assert gains[-1] == pytest.approx(0.1, abs=1e-6)  # -20 dB


def test_dimmer_with_zero_delay_reacts_at_once_and_zero_amount_never_dims():
    d = Dimmer(ClientDim(amount_db=-14, delay_ms=0))
    d.step(True, 0.01)
    assert d.dimmed
    off = Dimmer(ClientDim(amount_db=0, delay_ms=0))
    for _ in range(50):
        assert off.step(True, 0.01) == (1.0, 1.0)
    assert not off.dimmed


# ---------------------------------------------------------------- dimming in the hub

def hub_with(loop, dim, **kw):
    hub = AudioHub(ClientAudio(backend="synthetic", **kw))
    hub.set_dim(dim)
    talk = SharedLineAudio(LineControl("Regie"), hub, loop, playout_ms=60)
    prog = SharedLineAudio(LineControl("Programm", program=True), hub, loop, playout_ms=60)
    other = SharedLineAudio(LineControl("Technik"), hub, loop, playout_ms=60)
    for la in (talk, prog, other):
        hub.attach(la)
    return hub, talk, prog, other


def play(hub, seconds, feeds, mic=0.0, step_ms=10):
    """Feed every line at its level (and the microphone), and play it, in 10 ms steps."""
    n = step_ms * SR // 1000
    for _ in range(round(seconds * 1000 / step_ms)):
        for la, value in feeds.items():
            la.on_remote_pcm(np.full(n, value, np.float32), 1)
        if mic:
            hub.process_mic(np.full(n, mic, np.float32))
        hub.mix_output(n)


def program_gain(hub, prog):
    """Measure the programme line's present gain: its out level against its raw level."""
    raw, out = prog.raw_level.rms, prog.out_level.rms
    return out / raw if raw > 0 else 1.0


def test_programme_line_is_dimmed_when_somebody_speaks_on_another_line_and_comes_back():
    async def go():
        hub, talk, prog, other = hub_with(asyncio.get_running_loop(), ClientDim(delay_ms=500))
        feeds = {prog: 0.1, talk: 0.1}
        play(hub, 0.3, feeds)
        assert program_gain(hub, prog) == pytest.approx(1.0, abs=0.01)  # not yet: the delay has not passed
        play(hub, 1.1, feeds)
        assert hub.dimmed and program_gain(hub, prog) == pytest.approx(0.2, abs=0.02)  # -14 dB
        play(hub, 1.0, {prog: 0.1, talk: 0.0})  # the talker is quiet again
        assert not hub.dimmed and program_gain(hub, prog) == pytest.approx(1.0, abs=0.02)
    asyncio.run(go())


def test_speech_on_the_programme_line_itself_never_dims_it():
    async def go():
        hub, talk, prog, other = hub_with(asyncio.get_running_loop(), ClientDim(delay_ms=0))
        play(hub, 1.0, {prog: 0.2})
        assert not hub.dimmed and program_gain(hub, prog) == pytest.approx(1.0, abs=0.01)
    asyncio.run(go())


def test_dim_when_addressed_can_be_switched_off_and_the_amount_is_followed():
    async def go():
        hub, talk, prog, other = hub_with(asyncio.get_running_loop(), ClientDim(when_addressed=False, delay_ms=0))
        play(hub, 0.8, {prog: 0.1, talk: 0.1})
        assert not hub.dimmed  # somebody speaks to me, but that is switched off
        hub.set_dim(ClientDim(amount_db=-6, delay_ms=0))
        play(hub, 0.8, {prog: 0.1, talk: 0.1})
        assert program_gain(hub, prog) == pytest.approx(0.5, abs=0.02)
    asyncio.run(go())


def test_dim_while_i_speak_needs_real_speech_on_a_line_that_is_not_the_programme_line():
    async def go():
        hub, talk, prog, other = hub_with(asyncio.get_running_loop(), ClientDim(when_addressed=False, delay_ms=0))
        play(hub, 0.5, {prog: 0.1}, mic=0.2)
        assert not hub.dimmed  # the microphone is loud, but nothing is being talked into
        talk.control.talk.press()
        play(hub, 0.5, {prog: 0.1}, mic=0.001)
        assert not hub.dimmed  # key down but silence: not speaking
        play(hub, 0.5, {prog: 0.1}, mic=0.2)
        assert hub.dimmed
        talk.control.talk.release()
        prog.control.talk.press()  # talking into the programme line itself does not dim it either
        play(hub, 1.0, {prog: 0.1}, mic=0.2)
        assert not hub.dimmed
        hub.set_dim(ClientDim(when_addressed=False, while_speaking=False, delay_ms=0))
        talk.control.talk.press()
        play(hub, 0.5, {prog: 0.1}, mic=0.2)
        assert not hub.dimmed
    asyncio.run(go())


def test_a_muted_or_quiet_line_still_counts_as_speech_because_the_level_is_measured_before_the_volume():
    async def go():
        hub, talk, prog, other = hub_with(asyncio.get_running_loop(), ClientDim(delay_ms=0))
        talk.control.volume_pct = 0  # not audible, but somebody is speaking to me
        play(hub, 0.8, {prog: 0.1, talk: 0.1})
        assert hub.dimmed
    asyncio.run(go())


# ---------------------------------------------------------------- echo and noise processing

def speech_like(seconds, seed):
    r = np.random.default_rng(seed)
    n = int(SR * seconds)
    t = np.arange(n) / SR
    noise = np.convolve(r.standard_normal(n), np.ones(6) / 6, mode="same")
    env = np.clip(np.sin(2 * np.pi * 3.3 * t + r.uniform(0, 6)) + 0.4, 0, 1) ** 1.5
    f0 = 110 + 25 * np.sin(2 * np.pi * 0.7 * t)
    harm = sum(np.sin(2 * np.pi * np.cumsum(f0 * k) / SR) / k for k in range(1, 8))
    x = (0.6 * noise + 0.5 * harm) * env
    return (0.3 * x / np.max(np.abs(x))).astype(np.float32)


def echo_of(far, delay_ms=60, gain=0.45):
    d = int(SR * delay_ms / 1000)
    ir = np.zeros(d + 400)
    ir[d], ir[d + 90], ir[d + 250] = gain, gain * 0.35, gain * 0.15
    return np.convolve(far, ir)[: len(far)].astype(np.float32)


def rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


def test_processor_refuses_to_start_with_everything_off():
    with pytest.raises(ValueError):
        MicProcessor(False, 0, False)


def test_echo_cancellation_takes_the_far_end_out_of_the_microphone():
    far = speech_like(10, 1)
    mic = echo_of(far)
    p = MicProcessor(echo_cancellation=True, noise_suppression=0, auto_gain=False)
    out = np.zeros_like(mic)
    for i in range(0, len(mic) - FRAME + 1, FRAME):
        p.reverse(far[i:i + FRAME])
        out[i:i + FRAME] = p.process(mic[i:i + FRAME])
    seg = slice(SR * 5, SR * 10)  # after the filter has adapted
    assert 20 * np.log10(rms(mic[seg]) / rms(out[seg])) > 12  # at least 12 dB of the echo gone


def test_noise_suppression_alone_keeps_speech_and_needs_no_far_end():
    near = speech_like(5, 2)
    p = MicProcessor(echo_cancellation=False, noise_suppression=2, auto_gain=False)
    assert not p.needs_reverse
    p.reverse(near[:FRAME])  # ignored, must not fail
    out = np.concatenate([p.process(near[i:i + FRAME]) for i in range(0, len(near) - FRAME + 1, FRAME)])
    assert len(out) == len(near) // FRAME * FRAME
    assert rms(out[SR:]) > 0.3 * rms(near[SR:])  # not silenced (the module returns silence when misconfigured)


def test_blocks_of_other_sizes_come_out_the_same_length():
    p = MicProcessor(echo_cancellation=True, noise_suppression=1, auto_gain=False)
    x = speech_like(2, 3)
    got = 0
    for i in range(0, len(x) - 441, 441):
        out = p.process(x[i:i + 441])
        assert len(out) == 441
        got += len(out)
    assert got > SR


def test_the_hub_runs_the_microphone_through_the_processor_and_feeds_it_the_output(monkeypatch):
    calls = []

    class Fake:
        needs_reverse = True

        def process(self, block):
            calls.append(("mic", len(block)))
            return block * 0.5

        def reverse(self, block):
            calls.append(("out", len(block)))

    async def go():
        hub, talk, prog, other = hub_with(asyncio.get_running_loop(), ClientDim(amount_db=0))
        hub._proc = Fake()
        talk.control.talk.press()
        for _ in range(2):  # the line sends 20 ms packets: two 10 ms blocks
            hub.process_mic(np.full(BLOCK, 0.4, np.float32))
        got = talk.track._take()
        assert np.allclose(got, 0.2)  # the processed signal is what the line receives
        hub.mix_output(BLOCK)
        assert calls == [("mic", BLOCK), ("mic", BLOCK), ("out", BLOCK)]
        assert hub.processing == "on"
    asyncio.run(go())


def test_missing_processing_library_is_reported_and_audio_runs_unprocessed(monkeypatch):
    def boom(*a, **k):
        raise ProcessingUnavailable("Echo-/Rauschunterdrückung nicht verfügbar (ImportError: x)")

    monkeypatch.setattr(hub_mod, "MicProcessor", boom)

    async def go():
        hub = AudioHub(ClientAudio(backend="synthetic", echo_cancellation=True))
        assert hub._proc is None and "nicht verfügbar" in hub.processing
        la = SharedLineAudio(LineControl("Regie"), hub, asyncio.get_running_loop(), 60)
        hub.attach(la)
        la.control.talk.press()
        for _ in range(2):
            hub.process_mic(np.full(BLOCK, 0.3, np.float32))
        assert np.allclose(la.track._take(), 0.3)
    asyncio.run(go())


def test_hub_without_any_processing_setting_has_no_processor():
    assert AudioHub(ClientAudio(backend="synthetic")).processing == "off"
    assert AudioHub(ClientAudio(backend="synthetic", noise_suppression=2)).processing == "on"


# ---------------------------------------------------------------- engine and window

class FakeWorker:
    program = {"Programm"}

    def __init__(self, cfg, server, audio_cfg, factory, http, debug=None, auth=None):
        self.cfg, self._factory, self._audio_cfg = cfg, factory, audio_cfg
        self._stop = asyncio.Event()
        self.program_line = cfg.name in FakeWorker.program

    async def run(self):
        audio = self._factory(self.cfg, self._audio_cfg, asyncio.get_running_loop())
        await audio.start()
        await self._stop.wait()
        await audio.stop()

    def stop(self):
        self._stop.set()

    def status(self):
        return {"state": "connected", "uptime_s": 3, "last_error": None, "in_dbfs": -40.0, "out_dbfs": -40.0,
                "participants": [], "audio": {}, "program_line": self.program_line}


@pytest.fixture
def fakes(monkeypatch):
    class FakeHttp:
        async def aclose(self):
            pass

    async def fake_client(url):
        return FakeHttp()

    monkeypatch.setattr(engine_mod, "LineWorker", FakeWorker)
    monkeypatch.setattr(engine_mod, "create_client", fake_client)


def wait_for(cond, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_the_engine_marks_the_line_the_server_calls_the_programme_line(fakes):
    c = cfg(line=[{"name": "Regie", "production_id": 4, "line_id": 1},
                  {"name": "Programm", "production_id": 4, "line_id": 2}],
            dim={"delay_ms": 0})
    eng = engine_mod.ClientEngine(c, enable_hotkeys=False)
    eng.start()
    try:
        assert wait_for(lambda: eng.controls["Programm"].program)
        assert not eng.controls["Regie"].program
        snap = eng.snapshot()
        assert [l["program"] for l in snap["lines"]] == [False, True]
        assert snap["dim_db"] == -14 and snap["processing"] == "off" and not snap["dimmed"]
        assert eng.hub._dimmer.cfg == c.dim  # the configured values reach the audio
    finally:
        eng.stop()


def test_a_card_shows_programme_and_dimmed(fakes, tmp_path):
    from PySide6.QtWidgets import QApplication
    from intercom_client.ui.window import LineCard

    QApplication.instance() or QApplication([])

    class Eng:  # the card only calls the engine when a button is clicked
        def toggle_line(self, name): ...
        def ptt(self, name, down): ...
        def toggle_latch(self, name): ...
        def set_listen_mute(self, name, mute): ...
        def set_volume(self, name, pct): ...

    card = LineCard(Eng(), "Programm", None, None)
    base = {"state": "connected", "uptime_s": 1, "participants": [], "talking": False, "held": False, "latched": False,
            "listen_mute": False, "volume_pct": 100, "in_dbfs": -120.0, "out_dbfs": -30.0, "ptt_key": None,
            "latch_key": None, "last_error": None, "audio": {}, "enabled": True}
    card.refresh({**base, "program": False, "dimmed": False})
    assert card.tag.isHidden()
    card.refresh({**base, "program": True, "dimmed": False})
    assert not card.tag.isHidden() and card.tag.text() == "Programm"
    card.refresh({**base, "program": True, "dimmed": True})
    assert card.tag.text() == "Programm · gedimmt"
    card.show_off()
    assert card.tag.isHidden()


def test_the_hub_runs_for_a_while_with_the_real_processing_switched_on():
    async def go():
        hub = AudioHub(ClientAudio(backend="synthetic", echo_cancellation=True, noise_suppression=2, auto_gain=True))
        assert hub.processing == "on" and hub._proc.needs_reverse
        la = SharedLineAudio(LineControl("Regie"), hub, asyncio.get_running_loop(), 60)
        hub.attach(la)
        la.control.talk.press()
        far, near = speech_like(3, 5), speech_like(3, 6)
        for i in range(0, len(far) - BLOCK + 1, BLOCK):
            la.on_remote_pcm(far[i:i + BLOCK], 1)
            out = hub.mix_output(BLOCK)
            assert out.shape == (BLOCK,) and np.all(np.isfinite(out))
            hub.process_mic(near[i:i + BLOCK] + 0.3 * out)  # near-end speech plus what comes back from the speakers
        assert hub.mic_level.dbfs > -80  # the microphone path keeps delivering signal
    asyncio.run(go())
