"""Opus encoder settings for the audio the gateway sends into a line.

aiortc always encodes with libopus at 96 kbit/s, optimised for speech ("voip"), stereo layout. That is fine
for talk-back but not for programme sound from a desk. This swaps in an encoder with the line's own mode and
bitrate. The server only relays the stream, so nothing has to change there; browsers and the gateway decode
mono and stereo Opus alike.
"""
from __future__ import annotations

import logging

from aiortc.codecs.opus import OpusEncoder

log = logging.getLogger("gateway.opus")

DEFAULT_MODE = "voip"
DEFAULT_KBPS = 96  # what aiortc uses on its own

# aiortc keeps the encoder in a private attribute and creates it lazily on the first packet only when it is
# still empty. Putting ours there before media flows is the only hook it offers; the check below makes a
# future aiortc that renames it fail loudly in the log instead of silently ignoring the setting.
_ENCODER_ATTR = "_RTCRtpSender__encoder"


class ConfiguredOpusEncoder(OpusEncoder):
    def __init__(self, mode: str, bitrate_kbps: int) -> None:
        super().__init__()
        self.mode, self.bitrate_kbps = mode, bitrate_kbps
        # libopus is opened on the first frame, so these still apply
        self.codec.bit_rate = bitrate_kbps * 1000
        self.codec.options = {"application": mode}


def is_default(mode: str, bitrate_kbps: int) -> bool:
    return mode == DEFAULT_MODE and bitrate_kbps == DEFAULT_KBPS


def install(sender, mode: str, bitrate_kbps: int) -> bool:
    """Makes `sender` (an aiortc RTCRtpSender) encode with these settings. True if it was changed."""
    if is_default(mode, bitrate_kbps):
        return False
    if not hasattr(sender, _ENCODER_ATTR):
        log.warning("this aiortc version has no %s: Opus mode/bitrate setting ignored", _ENCODER_ATTR)
        return False
    setattr(sender, _ENCODER_ATTR, ConfiguredOpusEncoder(mode, bitrate_kbps))
    return True
