"""Detect and surface actionable open work in final assistant responses.

This guard is intentionally conservative: it does not execute follow-up work
itself (that would require a new agent turn), but it prevents a response that
mentions non-user-blocked unfinished work from looking like a clean delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


@dataclass(frozen=True)
class OpenWorkFinding:
    detected: bool
    excerpt: str = ""
    requires_user_input: bool = False


_OPEN_WORK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"\b(?:falta|faltam|faltou)\s+(?:fechar|resolver|corrigir|validar|investigar|implementar|registrar|gerar|rodar|executar|concluir)\b[^\n.?!]*(?:[.?!]|$)",
        r"\b(?:pend[eê]ncia|pend[eê]ncias|pendente|pendentes)\b[^\n.?!]*(?:corrigir|resolver|validar|investigar|implementar|registrar|gerar|rodar|executar|concluir)[^\n.?!]*(?:[.?!]|$)",
        r"\b(?:remaining work|still needs?|left to do|not done|not completed)\b[^\n.?!]*(?:fix|resolve|validate|investigate|implement|record|generate|run|execute|complete)[^\n.?!]*(?:[.?!]|$)",
        r"\b(?:todo|to-do|next step)\b[^\n.?!]*(?:fix|resolve|validate|investigate|implement|record|generate|run|execute|complete|corrigir|resolver|validar|investigar|implementar|registrar|gerar|rodar|executar|concluir)[^\n.?!]*(?:[.?!]|$)",
    )
)

_USER_BLOCKED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"\b(?:preciso|necessito)\s+(?:da\s+)?(?:sua\s+)?(?:confirma[cç][aã]o|aprova[cç][aã]o|autoriza[cç][aã]o)\b",
        r"\b(?:aguard(?:o|ando)|esperando)\s+(?:sua\s+)?(?:confirma[cç][aã]o|aprova[cç][aã]o|autoriza[cç][aã]o|resposta|decis[aã]o)\b",
        r"\b(?:blocked|waiting)\s+(?:on|for)\s+(?:your|user)\s+(?:confirmation|approval|authorization|decision|input)\b",
        r"\b(?:sem|without)\s+(?:essa\s+)?(?:confirma[cç][aã]o|aprova[cç][aã]o|autoriza[cç][aã]o|approval|authorization)\b",
    )
)

_FOOTER_MARKER = "Pendência acionável detectada"


def _candidate_excerpts(text: str) -> Iterable[str]:
    for pattern in _OPEN_WORK_PATTERNS:
        for match in pattern.finditer(text or ""):
            excerpt = " ".join(match.group(0).split())
            if excerpt:
                yield excerpt[:300]


def _requires_user_input(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _USER_BLOCKED_PATTERNS)


def detect_open_work(response_text: str) -> OpenWorkFinding:
    """Return a conservative finding for actionable open work.

    User-blocked work is deliberately ignored: when the assistant explicitly
    needs confirmation/approval/input, the correct behavior is to wait, not to
    auto-continue.
    """

    if not response_text or _FOOTER_MARKER in response_text:
        return OpenWorkFinding(False)

    requires_user_input = _requires_user_input(response_text)
    if requires_user_input:
        return OpenWorkFinding(False, requires_user_input=True)

    for excerpt in _candidate_excerpts(response_text):
        return OpenWorkFinding(True, excerpt=excerpt, requires_user_input=False)

    return OpenWorkFinding(False)


def format_open_work_footer(finding: OpenWorkFinding) -> str:
    if not finding.detected:
        return ""
    detail = f" Trecho: “{finding.excerpt}”" if finding.excerpt else ""
    return (
        "⚠️ Pendência acionável detectada: esta resposta menciona trabalho "
        "ainda aberto; a pendência não foi tratada como entrega concluída. "
        "Vou continuar/corrigir automaticamente quando houver ferramentas e "
        "escopo suficientes; se depender de aprovação sua, vou pedir de forma "
        f"explícita.{detail}"
    )


def append_open_work_footer(response_text: str) -> str:
    finding = detect_open_work(response_text)
    footer = format_open_work_footer(finding)
    if not footer:
        return response_text
    return response_text.rstrip() + "\n\n" + footer


def apply_open_work_guard(response_text: str, *, completed: bool = True) -> tuple[str, bool, OpenWorkFinding]:
    """Append the footer and clear completed when actionable work remains."""

    finding = detect_open_work(response_text)
    footer = format_open_work_footer(finding)
    if not footer:
        return response_text, completed, finding
    guarded = response_text.rstrip() + "\n\n" + footer
    return guarded, False, finding
