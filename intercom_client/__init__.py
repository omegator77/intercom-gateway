"""Intercom client: joins several lines with push-to-talk, latch and a volume per line.

Built on the gateway's modules (login, WebRTC connection, reconnecting line worker); the audio side is
one microphone and one output shared by all lines, see hub.py."""
__version__ = "0.1.1"
