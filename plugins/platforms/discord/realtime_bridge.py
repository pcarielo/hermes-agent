"""Discord voice ↔ OpenAI Realtime bridge primitives.

This module intentionally stays small and dependency-light.  The Discord
adapter already owns the voice socket, Opus decode/encode, user authorization,
and text/Hermes callback pipeline.  These helpers provide the reusable pieces
needed to wire Discord's native 48 kHz stereo PCM stream into OpenAI Realtime's
24 kHz mono PCM stream, and to translate Realtime audio deltas back to Discord
PCM for the existing VoiceMixer.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


REALTIME_WS_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_REALTIME_MODEL = "gpt-realtime-2"
DEFAULT_REALTIME_VOICE = "marin"


JsonSender = Callable[[dict[str, Any]], None]


def _sync_ws_connect():
    """Return websockets.sync.client.connect with a clear optional-dep error."""
    try:
        from websockets.sync.client import connect
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("websockets package is required for OpenAI Realtime") from exc
    return connect


def resolve_realtime_api_key() -> Optional[str]:
    """Return the API key for OpenAI Realtime.

    Use a dedicated key when configured, otherwise fall back to the standard
    OpenAI API key.  A single regular OpenAI Project API key is enough for the
    Realtime API; separate env vars are only for operational separation.
    """

    return os.getenv("OPENAI_REALTIME_API_KEY") or os.getenv("OPENAI_API_KEY")


def build_session_update(
    *,
    instructions: str,
    voice: str = DEFAULT_REALTIME_VOICE,
    turn_detection: str = "semantic_vad",
    reasoning_effort: str = "low",
) -> dict[str, Any]:
    """Build the Realtime v2 session.update payload for speech-to-speech.

    The current OpenAI Realtime API uses nested ``session.audio`` configuration
    for PCM input/output formats, voice, and turn detection.  Discord audio is
    resampled to 24 kHz mono PCM before ``input_audio_buffer.append``.
    """

    if turn_detection == "semantic_vad":
        td: dict[str, Any] = {
            "type": "semantic_vad",
            "eagerness": "medium",
            "create_response": True,
            "interrupt_response": True,
        }
    elif turn_detection == "server_vad":
        td = {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 500,
            "create_response": True,
            "interrupt_response": True,
        }
    else:
        raise ValueError(f"unsupported turn_detection: {turn_detection}")

    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "turn_detection": td,
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": voice,
                },
            },
            "reasoning": {"effort": reasoning_effort},
        },
    }


def build_input_audio_append(pcm24_mono: bytes) -> dict[str, str]:
    """Build an ``input_audio_buffer.append`` event from 24 kHz PCM16 bytes."""

    return {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(pcm24_mono).decode("ascii"),
    }


def decode_response_audio_delta(event: dict[str, Any]) -> Optional[bytes]:
    """Decode a Realtime ``response.audio.delta`` event to PCM bytes."""

    if event.get("type") != "response.audio.delta":
        return None
    encoded = event.get("delta") or event.get("audio")
    if not encoded:
        return b""
    try:
        return base64.b64decode(encoded)
    except (TypeError, ValueError):
        return b""


def run_realtime_pcm_turn(
    *,
    api_key: str,
    pcm24_mono: bytes,
    instructions: str,
    model: str = DEFAULT_REALTIME_MODEL,
    voice: str = DEFAULT_REALTIME_VOICE,
    turn_detection: str = "semantic_vad",
    reasoning_effort: str = "low",
    timeout: float = 30.0,
) -> bytes:
    """Run one server-side Realtime audio turn over WebSocket.

    Sends one already-resampled user utterance to OpenAI Realtime, commits the
    input buffer, requests an audio+text response, and returns the concatenated
    24 kHz mono PCM16 audio deltas. This maps safely to the existing Discord
    silence-detected utterance loop while leaving room for later continuous
    full-duplex streaming on the same primitives.
    """

    if not api_key:
        raise ValueError("api_key is required")
    if not pcm24_mono:
        return b""

    connect = _sync_ws_connect()
    url = f"{REALTIME_WS_URL}?model={model}"
    headers = [
        ("Authorization", f"Bearer {api_key}"),
        ("OpenAI-Beta", "realtime=v1"),
    ]

    try:
        ws_ctx = connect(url, additional_headers=headers)
    except TypeError:
        ws_ctx = connect(url, extra_headers=headers)

    def _send(ws: Any, payload: dict[str, Any]) -> None:
        ws.send(json.dumps(payload))

    def _recv(ws: Any) -> Any:
        try:
            return ws.recv(timeout=timeout)
        except TypeError:
            return ws.recv()

    chunks: list[bytes] = []
    with ws_ctx as ws:
        _send(
            ws,
            build_session_update(
                instructions=instructions,
                voice=voice,
                turn_detection=turn_detection,
                reasoning_effort=reasoning_effort,
            ),
        )
        _send(ws, build_input_audio_append(pcm24_mono))
        _send(ws, {"type": "input_audio_buffer.commit"})
        _send(ws, {"type": "response.create", "response": {"modalities": ["audio", "text"]}})

        while True:
            raw = _recv(ws)
            if raw is None:
                break
            try:
                event = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "error":
                raise RuntimeError(f"realtime error: {event.get('error') or event}")
            pcm = decode_response_audio_delta(event)
            if pcm:
                chunks.append(pcm)
            if event.get("type") in {"response.done", "response.completed", "response.cancelled"}:
                break

    return b"".join(chunks)


class AudioResampler:
    """ffmpeg-backed PCM16 resamplers for Discord ↔ Realtime audio."""

    @staticmethod
    def _ffmpeg_pcm(
        pcm: bytes,
        *,
        in_rate: int,
        in_channels: int,
        out_rate: int,
        out_channels: int,
        timeout: float = 10.0,
    ) -> bytes:
        if not pcm:
            return b""
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(in_rate),
            "-ac",
            str(in_channels),
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-ar",
            str(out_rate),
            "-ac",
            str(out_channels),
            "pipe:1",
        ]
        result = subprocess.run(
            cmd,
            input=pcm,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=timeout,
        )
        return result.stdout

    @staticmethod
    def discord_to_realtime_pcm(pcm48_stereo: bytes) -> bytes:
        """Convert Discord-native 48 kHz stereo PCM16 to 24 kHz mono PCM16."""

        return AudioResampler._ffmpeg_pcm(
            pcm48_stereo,
            in_rate=48000,
            in_channels=2,
            out_rate=24000,
            out_channels=1,
        )

    @staticmethod
    def realtime_to_discord_pcm(pcm24_mono: bytes) -> bytes:
        """Convert Realtime 24 kHz mono PCM16 to Discord 48 kHz stereo PCM16."""

        return AudioResampler._ffmpeg_pcm(
            pcm24_mono,
            in_rate=24000,
            in_channels=1,
            out_rate=48000,
            out_channels=2,
        )


@dataclass
class RealtimeDiscordBridge:
    """Small translation layer between Discord PCM and Realtime events.

    ``send_json`` is injected so the same class can be unit-tested without a
    live WebSocket and then used by an adapter-owned async/sync websocket loop.
    """

    api_key: str
    send_json: JsonSender
    model: str = DEFAULT_REALTIME_MODEL
    voice: str = DEFAULT_REALTIME_VOICE
    instructions: str = "You are Jarbas talking with Paulo in pt-BR. Be concise and useful."
    turn_detection: str = "semantic_vad"
    reasoning_effort: str = "low"
    started: bool = field(default=False, init=False)

    def start(self) -> None:
        """Send initial session configuration."""

        self.send_json(
            build_session_update(
                instructions=self.instructions,
                voice=self.voice,
                turn_detection=self.turn_detection,
                reasoning_effort=self.reasoning_effort,
            )
        )
        self.started = True

    def send_discord_pcm(self, pcm48_stereo: bytes) -> None:
        """Resample and send Discord PCM to OpenAI Realtime."""

        pcm24_mono = AudioResampler.discord_to_realtime_pcm(pcm48_stereo)
        if pcm24_mono:
            self.send_json(build_input_audio_append(pcm24_mono))

    def handle_server_event(self, raw_event: str | bytes | dict[str, Any]) -> Optional[bytes]:
        """Return Discord-ready PCM for Realtime audio delta events.

        Non-audio events are ignored here; the adapter can still inspect them
        separately for transcripts, errors, rate limits, or tool-call events.
        """

        if isinstance(raw_event, (str, bytes, bytearray)):
            event = json.loads(raw_event)
        else:
            event = raw_event
        pcm24_mono = decode_response_audio_delta(event)
        if pcm24_mono is None:
            return None
        if not pcm24_mono:
            return b""
        return AudioResampler.realtime_to_discord_pcm(pcm24_mono)
