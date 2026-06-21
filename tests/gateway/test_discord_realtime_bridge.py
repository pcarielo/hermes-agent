"""Tests for the Discord ↔ OpenAI Realtime voice bridge helpers."""

import base64
import json

import pytest


def test_realtime_api_key_prefers_specific_env(monkeypatch):
    from plugins.platforms.discord.realtime_bridge import resolve_realtime_api_key

    monkeypatch.setenv("OPENAI_API_KEY", "sk-general")
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "sk-realtime")

    assert resolve_realtime_api_key() == "sk-realtime"


def test_realtime_api_key_falls_back_to_openai_key(monkeypatch):
    from plugins.platforms.discord.realtime_bridge import resolve_realtime_api_key

    monkeypatch.delenv("OPENAI_REALTIME_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-general")

    assert resolve_realtime_api_key() == "sk-general"


def test_session_update_uses_realtime_v2_audio_shape():
    from plugins.platforms.discord.realtime_bridge import build_session_update

    event = build_session_update(
        instructions="Talk to Paulo in pt-BR.",
        voice="marin",
        turn_detection="semantic_vad",
        reasoning_effort="low",
    )

    assert event["type"] == "session.update"
    session = event["session"]
    assert session["type"] == "realtime"
    assert session["instructions"] == "Talk to Paulo in pt-BR."
    assert session["audio"]["input"]["format"]["type"] == "audio/pcm"
    assert session["audio"]["input"]["format"]["rate"] == 24000
    assert session["audio"]["output"]["format"]["type"] == "audio/pcm"
    assert session["audio"]["output"]["format"]["rate"] == 24000
    assert session["audio"]["output"]["voice"] == "marin"
    assert session["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
    assert session["reasoning"]["effort"] == "low"


def test_input_audio_append_base64_encodes_pcm():
    from plugins.platforms.discord.realtime_bridge import build_input_audio_append

    payload = build_input_audio_append(b"\x01\x02pcm")

    assert payload == {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(b"\x01\x02pcm").decode("ascii"),
    }


def test_response_audio_delta_decodes_delta_and_audio_fields():
    from plugins.platforms.discord.realtime_bridge import decode_response_audio_delta

    encoded = base64.b64encode(b"pcm-data").decode("ascii")

    assert decode_response_audio_delta({"type": "response.audio.delta", "delta": encoded}) == b"pcm-data"
    assert decode_response_audio_delta({"type": "response.audio.delta", "audio": encoded}) == b"pcm-data"
    assert decode_response_audio_delta({"type": "response.text.delta", "delta": encoded}) is None


def test_resampler_invokes_ffmpeg_for_discord_to_realtime(monkeypatch):
    from plugins.platforms.discord.realtime_bridge import AudioResampler

    calls = []

    def fake_run(cmd, input, stdout, stderr, check, timeout):
        calls.append({"cmd": cmd, "input": input, "timeout": timeout})
        class Result:
            stdout = b"out-pcm"
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    out = AudioResampler.discord_to_realtime_pcm(b"discord-pcm")

    assert out == b"out-pcm"
    cmd = calls[0]["cmd"]
    assert cmd[:8] == ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", "48000"]
    assert "24000" in cmd
    assert calls[0]["input"] == b"discord-pcm"


def test_resampler_invokes_ffmpeg_for_realtime_to_discord(monkeypatch):
    from plugins.platforms.discord.realtime_bridge import AudioResampler

    calls = []

    def fake_run(cmd, input, stdout, stderr, check, timeout):
        calls.append({"cmd": cmd, "input": input})
        class Result:
            stdout = b"discord-pcm"
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    out = AudioResampler.realtime_to_discord_pcm(b"rt-pcm")

    assert out == b"discord-pcm"
    cmd = calls[0]["cmd"]
    assert "24000" in cmd
    assert "48000" in cmd
    assert cmd[-1] == "pipe:1"


def test_bridge_translates_openai_audio_delta_to_discord_pcm(monkeypatch):
    from plugins.platforms.discord.realtime_bridge import RealtimeDiscordBridge

    encoded = base64.b64encode(b"rt-pcm").decode("ascii")
    monkeypatch.setattr(
        "plugins.platforms.discord.realtime_bridge.AudioResampler.realtime_to_discord_pcm",
        staticmethod(lambda pcm: b"discord-" + pcm),
    )

    bridge = RealtimeDiscordBridge(api_key="sk-test", send_json=lambda _payload: None)

    assert bridge.handle_server_event(json.dumps({"type": "response.audio.delta", "delta": encoded})) == b"discord-rt-pcm"
    assert bridge.handle_server_event({"type": "response.done"}) is None


def test_bridge_sends_resampled_discord_audio(monkeypatch):
    from plugins.platforms.discord.realtime_bridge import RealtimeDiscordBridge

    sent = []
    monkeypatch.setattr(
        "plugins.platforms.discord.realtime_bridge.AudioResampler.discord_to_realtime_pcm",
        staticmethod(lambda pcm: b"rt-" + pcm),
    )

    bridge = RealtimeDiscordBridge(api_key="sk-test", send_json=sent.append)
    bridge.send_discord_pcm(b"discord")

    assert sent == [{
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(b"rt-discord").decode("ascii"),
    }]


def test_run_realtime_pcm_turn_sends_audio_and_collects_response(monkeypatch):
    from plugins.platforms.discord import realtime_bridge

    sent = []
    encoded = base64.b64encode(b"reply-pcm").decode("ascii")

    class FakeWebSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def send(self, payload):
            sent.append(json.loads(payload))

        def recv(self, timeout=None):
            if not hasattr(self, "_events"):
                self._events = iter([
                    json.dumps({"type": "session.updated"}),
                    json.dumps({"type": "response.audio.delta", "delta": encoded}),
                    json.dumps({"type": "response.done"}),
                ])
            return next(self._events)

    def fake_connect(url, **kwargs):
        assert "model=gpt-realtime-2" in url
        assert kwargs["additional_headers"][0] == ("Authorization", "Bearer sk-test")
        return FakeWebSocket()

    monkeypatch.setattr(realtime_bridge, "_sync_ws_connect", lambda: fake_connect)

    out = realtime_bridge.run_realtime_pcm_turn(
        api_key="sk-test",
        pcm24_mono=b"input-pcm",
        instructions="Speak pt-BR.",
        timeout=5,
    )

    assert out == b"reply-pcm"
    assert sent[0]["type"] == "session.update"
    assert sent[1] == {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(b"input-pcm").decode("ascii"),
    }
    assert sent[2] == {"type": "input_audio_buffer.commit"}
    assert sent[3] == {"type": "response.create", "response": {"modalities": ["audio", "text"]}}
