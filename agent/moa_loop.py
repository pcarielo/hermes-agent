"""Mixture-of-Agents runtime helpers for /moa turns.

The slash command is deliberately not a model tool. It marks one user turn as
MoA-enabled; the normal Hermes agent loop still owns tool calling and turn
termination, while this module gathers reference-model context before each model
iteration.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.auxiliary_client import call_llm
from agent.transports import get_transport

logger = logging.getLogger(__name__)


def _slot_label(slot: dict[str, str]) -> str:
    return f"{slot.get('provider', '').strip()}:{slot.get('model', '').strip()}"


def _reference_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build an advisory-safe view of the conversation for reference models.

    Reference calls are advisory: they never call tools and never emit the
    ``tool_calls`` the main model did. Replaying the full transcript verbatim
    (a) re-bills the ~8K-token Hermes system prompt per reference per
    iteration and (b) risks 400s from strict providers (Mistral, Fireworks)
    that reject orphan ``tool`` messages or ``tool_calls`` the reference never
    produced. We keep only the user/assistant *text* turns, dropping the
    system prompt, any ``tool``-role messages, and any ``tool_calls`` payloads.
    """
    trimmed: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            # Drop system prompt and tool-result messages.
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            # Skip non-text (multimodal/tool-call-only) assistant turns.
            if not content:
                continue
        text = content if isinstance(content, str) else ""
        if role == "assistant" and not text.strip():
            # Assistant turn that was purely tool calls — nothing advisory.
            continue
        trimmed.append({"role": role, "content": text})
    if not trimmed:
        # Degenerate case (e.g. first turn was stripped): fall back to a
        # minimal user turn so the reference still has something to answer.
        for msg in reversed(messages):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                return [{"role": "user", "content": msg["content"]}]
    return trimmed



def _extract_text(response: Any) -> str:
    try:
        transport = get_transport("chat_completions")
        if transport is None:
            raise RuntimeError("chat_completions transport unavailable")
        normalized = transport.normalize_response(response)
        text = (normalized.content or "").strip()
        if text:
            return text
    except Exception:
        pass
    try:
        content = response.choices[0].message.content
        return (content or "").strip()
    except Exception:
        return ""


def aggregate_moa_context(
    *,
    user_prompt: str,
    api_messages: list[dict[str, Any]],
    reference_models: list[dict[str, str]],
    aggregator: dict[str, str],
    temperature: float = 0.6,
    aggregator_temperature: float = 0.4,
    max_tokens: int = 4096,
) -> str:
    """Run configured reference models and synthesize their advice.

    Failures are returned as model-specific notes instead of aborting the normal
    agent loop; the main model can still act with partial context.
    """
    reference_outputs: list[tuple[str, str]] = []
    ref_messages = _reference_messages(api_messages)
    for slot in reference_models:
        label = _slot_label(slot)
        try:
            response = call_llm(
                task="moa_reference",
                provider=slot["provider"],
                model=slot["model"],
                messages=ref_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            text = _extract_text(response)
            reference_outputs.append((label, text or "(empty response)"))
        except Exception as exc:
            logger.warning("MoA reference model %s failed: %s", label, exc)
            reference_outputs.append((label, f"[failed: {exc}]"))

    joined = "\n\n".join(
        f"Reference {idx} — {label}:\n{text}"
        for idx, (label, text) in enumerate(reference_outputs, start=1)
    )
    synth_prompt = (
        "You are the aggregator in a Mixture of Agents process. Synthesize the "
        "reference responses into concise, actionable guidance for the main "
        "Hermes agent. Focus on next steps, tool-use strategy, risks, and any "
        "disagreements. Do not answer the user directly unless that is all that "
        "is needed; produce context the main agent should use in its normal loop.\n\n"
        f"Original user prompt:\n{user_prompt}\n\n"
        f"Reference responses:\n{joined}"
    )

    agg_label = _slot_label(aggregator)
    try:
        response = call_llm(
            task="moa_aggregator",
            provider=aggregator["provider"],
            model=aggregator["model"],
            messages=[{"role": "user", "content": synth_prompt}],
            temperature=aggregator_temperature,
            max_tokens=max_tokens,
        )
        synthesis = _extract_text(response)
    except Exception as exc:
        logger.warning("MoA aggregator model %s failed: %s", agg_label, exc)
        synthesis = ""

    if not synthesis:
        synthesis = joined

    return (
        "[Mixture of Agents context — use this as private guidance for the "
        "normal Hermes agent loop. You may call tools, continue reasoning, or "
        "finish normally.]\n"
        f"Aggregator: {agg_label}\n"
        f"References: {', '.join(_slot_label(slot) for slot in reference_models)}\n\n"
        f"{synthesis.strip()}"
    )


class MoAChatCompletions:
    """OpenAI-chat-compatible facade where the aggregator is the acting model."""

    def __init__(self, preset_name: str):
        self.preset_name = preset_name or "default"

    def create(self, **api_kwargs: Any) -> Any:
        from hermes_cli.config import load_config
        from hermes_cli.moa_config import resolve_moa_preset

        preset = resolve_moa_preset(load_config().get("moa") or {}, self.preset_name)
        messages = list(api_kwargs.get("messages") or [])
        reference_models = preset.get("reference_models") or []
        aggregator = preset.get("aggregator") or {}
        max_tokens = int(preset.get("max_tokens", api_kwargs.get("max_tokens") or 4096) or 4096)
        temperature = float(preset.get("reference_temperature", 0.6) or 0.6)
        aggregator_temperature = float(preset.get("aggregator_temperature", api_kwargs.get("temperature") or 0.4) or 0.4)

        # When the preset is disabled, skip the reference fan-out and let the
        # configured aggregator act alone — it is the preset's acting model, so
        # a disabled MoA preset is simply "use the aggregator directly."
        if not preset.get("enabled", True):
            reference_models = []

        reference_outputs: list[tuple[str, str]] = []
        ref_messages = _reference_messages(messages)
        for slot in reference_models:
            if slot.get("provider") == "moa":
                reference_outputs.append((_slot_label(slot), "[skipped: MoA presets cannot recursively reference MoA]"))
                continue
            try:
                response = call_llm(
                    task="moa_reference",
                    provider=slot["provider"],
                    model=slot["model"],
                    messages=ref_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                reference_outputs.append((_slot_label(slot), _extract_text(response) or "(empty response)"))
            except Exception as exc:
                logger.warning("MoA reference model %s failed: %s", _slot_label(slot), exc)
                reference_outputs.append((_slot_label(slot), f"[failed: {exc}]"))

        agg_messages = [dict(m) for m in messages]
        if reference_outputs:
            joined = "\n\n".join(
                f"Reference {idx} — {label}:\n{text}"
                for idx, (label, text) in enumerate(reference_outputs, start=1)
            )
            guidance = (
                "[Mixture of Agents reference context]\n"
                f"Preset: {self.preset_name}\n"
                f"Aggregator/acting model: {_slot_label(aggregator)}\n"
                f"References: {', '.join(label for label, _ in reference_outputs)}\n\n"
                "Use the reference responses below as private context. You are the aggregator and acting model: "
                "answer the user directly or call tools as needed.\n\n"
                f"{joined}"
            )
            for msg in reversed(agg_messages):
                if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                    msg["content"] = msg["content"] + "\n\n" + guidance
                    break
            else:
                agg_messages.append({"role": "user", "content": guidance})

        if aggregator.get("provider") == "moa":
            raise RuntimeError("MoA aggregator cannot be another MoA preset")
        agg_kwargs = dict(api_kwargs)
        agg_kwargs["messages"] = agg_messages
        agg_kwargs["model"] = aggregator.get("model")
        agg_kwargs["temperature"] = aggregator_temperature
        return call_llm(
            task="moa_aggregator",
            provider=aggregator.get("provider"),
            model=aggregator.get("model"),
            messages=agg_messages,
            temperature=aggregator_temperature,
            max_tokens=agg_kwargs.get("max_tokens"),
            tools=agg_kwargs.get("tools"),
            extra_body=agg_kwargs.get("extra_body"),
        )


class MoAClient:
    def __init__(self, preset_name: str):
        self.chat = type("_MoAChat", (), {})()
        self.chat.completions = MoAChatCompletions(preset_name)
