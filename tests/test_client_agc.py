"""The speech leveller: does it bring quiet and loud talkers to one level, and leave noise and pauses alone?"""
import numpy as np
import pytest

from intercom_client.agc import SR, SpeechLeveller

BLOCK = 480


def speech_like(seconds, seed=1, peak_db=-12.0):
    r = np.random.default_rng(seed)
    n = int(SR * seconds)
    t = np.arange(n) / SR
    noise = np.convolve(r.standard_normal(n), np.ones(6) / 6, mode="same")
    env = np.clip(np.sin(2 * np.pi * 3.3 * t + r.uniform(0, 6)) + 0.4, 0, 1) ** 1.5  # syllables with gaps
    f0 = 110 + 25 * np.sin(2 * np.pi * 0.7 * t)
    harm = sum(np.sin(2 * np.pi * np.cumsum(f0 * k) / SR) / k for k in range(1, 8))
    x = (0.6 * noise + 0.5 * harm) * env
    return (10 ** (peak_db / 20) * x / np.max(np.abs(x))).astype(np.float32)


def run(agc, x, block=BLOCK):
    """-> output, gain (dB) after every block, and whether each block was taken for speech."""
    out = np.zeros_like(x)
    gains, speaking = [], []
    for i in range(0, len(x) - block + 1, block):
        out[i:i + block] = agc.process(x[i:i + block])
        gains.append(agc.gain_db)
        speaking.append(agc.speaking)
    return out, np.array(gains), np.array(speaking)


def block_rms_db(x, block=BLOCK):
    n = len(x) // block
    r = np.sqrt(np.mean(np.square(x[: n * block].reshape(n, block)), axis=1))
    return 20 * np.log10(r + 1e-9)


def speech_level(x, speaking, from_s=6.0):
    """Mean level (dB) of the blocks the leveller took for speech, after it has settled."""
    d = block_rms_db(x)
    k = int(from_s * 100)
    return float(d[k:][speaking[k:]].mean())


@pytest.mark.parametrize("peak_db", [-30, -22, -14, -6])
def test_every_talker_ends_up_at_the_target_level(peak_db):
    x = speech_like(12, peak_db=peak_db)
    out, _, speaking = run(SpeechLeveller(target_dbfs=-20.0), x)
    assert speech_level(out, speaking) == pytest.approx(-20.0, abs=2.0)


def test_the_difference_between_a_quiet_and_a_loud_talker_is_mostly_gone():
    quiet, loud = speech_like(12, peak_db=-30), speech_like(12, seed=2, peak_db=-4)
    qo, _, qs = run(SpeechLeveller(), quiet)
    lo, _, ls = run(SpeechLeveller(), loud)
    before = speech_level(loud, ls) - speech_level(quiet, qs)
    after = abs(speech_level(lo, ls) - speech_level(qo, qs))
    assert before > 20 and after < 3  # more than 20 dB apart before, within 3 dB after


@pytest.mark.parametrize("peak_db", [-38, -48])
def test_a_microphone_too_quiet_for_the_maximum_gain_gets_all_of_it_and_no_more(peak_db):
    x = speech_like(12, peak_db=peak_db)
    agc = SpeechLeveller(target_dbfs=-20.0, max_gain_db=25.0)
    out, gains, speaking = run(agc, x)
    assert gains[-1] == pytest.approx(25.0, abs=0.3)
    assert speech_level(out, speaking) == pytest.approx(speech_level(x, speaking) + 25.0, abs=1.0)


def test_the_first_speech_is_levelled_at_once_not_after_seconds():
    x = speech_like(3, peak_db=-30)
    out, gains, speaking = run(SpeechLeveller(), x)
    assert gains[30] > 12  # within 0.3 s most of the gain is already there
    d_in, d_out = block_rms_db(x)[100:], block_rms_db(out)[100:]
    assert d_out[speaking[100:]].mean() > d_in[speaking[100:]].mean() + 12


def test_room_noise_alone_is_not_pulled_up():
    r = np.random.default_rng(3)
    noise = (10 ** (-60 / 20) * r.standard_normal(SR * 10)).astype(np.float32)  # about -60 dBFS rms
    out, gains, speaking = run(SpeechLeveller(), noise)
    assert np.max(np.abs(gains)) < 1.0 and not speaking[100:].any()
    assert abs(block_rms_db(out).mean() - block_rms_db(noise).mean()) < 1.0


def test_the_gain_is_held_in_pauses_so_the_noise_is_not_pumped_up():
    r = np.random.default_rng(4)
    speech = speech_like(6, peak_db=-30)
    pause = (10 ** (-62 / 20) * r.standard_normal(SR * 6)).astype(np.float32)
    out, gains, speaking = run(SpeechLeveller(), np.concatenate([speech, pause, speech]))
    at_pause_start, at_pause_end = gains[int(6.2 * 100)], gains[int(11.8 * 100)]
    assert at_pause_start > 8
    assert abs(at_pause_end - at_pause_start) < 1.0  # held, not drifting up into the noise
    assert abs(speech_level(out, speaking, from_s=15.0) - (-20.0)) < 3.0  # and the talker is at the target again


def test_speech_over_a_noisy_room_is_levelled():
    r = np.random.default_rng(5)
    room = (10 ** (-52 / 20) * r.standard_normal(SR * 12)).astype(np.float32)
    x = speech_like(12, peak_db=-28) + room
    agc = SpeechLeveller()
    out, gains, speaking = run(agc, x)
    assert 5 < gains[-1] <= agc.max_gain_db
    assert speech_level(out, speaking) == pytest.approx(-20.0, abs=4.0)


def test_a_sudden_loud_burst_never_clips_and_the_gain_comes_back_slowly():
    quiet = speech_like(5, peak_db=-35)
    burst = (0.95 * np.sign(np.sin(2 * np.pi * 200 * np.arange(SR // 5) / SR))).astype(np.float32)  # 200 ms square
    after = speech_like(4, seed=7, peak_db=-35)
    out, gains, _ = run(SpeechLeveller(), np.concatenate([quiet, burst, after]))
    assert np.max(np.abs(out)) <= 1.0
    b = slice(5 * SR, 5 * SR + SR // 5)
    assert np.max(np.abs(out[b])) <= 0.95  # the burst itself was already turned down, from its first sample
    g_before, g_burst = gains[int(4.9 * 100)], gains[int(5.15 * 100)]
    assert g_burst < g_before - 5  # gain was cut at once
    assert gains[int(8.9 * 100)] > g_burst + 3  # and given back afterwards (slowly)


def test_the_gain_moves_smoothly_for_a_steady_talker():
    x = speech_like(10, peak_db=-30)
    _, gains, _ = run(SpeechLeveller(), x)
    assert np.abs(np.diff(gains[300:])).max() < 1.0  # under 1 dB per 10 ms once settled


def test_the_gain_stays_between_its_limits():
    _, g1, _ = run(SpeechLeveller(max_gain_db=10.0, min_gain_db=-6.0), speech_like(8, peak_db=-45))
    assert g1.max() <= 10.0 + 1e-6
    out, g2, _ = run(SpeechLeveller(max_gain_db=10.0, min_gain_db=-6.0), speech_like(8, peak_db=-3))
    assert np.max(np.abs(out)) <= 1.0


def test_odd_block_sizes_and_empty_blocks_are_fine():
    assert len(SpeechLeveller().process(np.zeros(0, np.float32))) == 0
    x = speech_like(3, peak_db=-30)
    for size in (441, 960, 7):
        agc = SpeechLeveller()
        out = np.concatenate([agc.process(x[i:i + size]) for i in range(0, len(x) - size + 1, size)])
        assert len(out) == (len(x) // size) * size and np.all(np.isfinite(out))


def test_silence_stays_silence():
    agc = SpeechLeveller()
    out, gains, _ = run(agc, np.zeros(SR * 2, np.float32))
    assert not np.any(out) and np.all(gains == 0.0) and not agc.speaking
