from agent.open_work_guard import (
    append_open_work_footer,
    apply_open_work_guard,
    detect_open_work,
)


def test_detects_actionable_open_work_in_portuguese_final_response():
    text = (
        "Conclusão: parte foi feita.\n"
        "Falta fechar a parte de governança/correção do restart do gateway e Evidence."
    )

    finding = detect_open_work(text)

    assert finding.detected is True
    assert "Falta fechar" in finding.excerpt
    assert finding.requires_user_input is False


def test_does_not_flag_user_blocked_pending_work():
    text = (
        "Pendência: preciso da sua confirmação antes de reiniciar o gateway. "
        "Sem essa aprovação explícita, não vou executar."
    )

    finding = detect_open_work(text)

    assert finding.detected is False


def test_footer_makes_open_work_visible_and_non_final():
    text = "Fiz A. Falta fechar B."

    updated = append_open_work_footer(text)

    assert updated.startswith(text)
    assert "Pendência acionável detectada" in updated
    assert "não foi tratada como entrega concluída" in updated


def test_footer_is_not_duplicated():
    text = append_open_work_footer("Fiz A. Falta fechar B.")

    updated = append_open_work_footer(text)

    assert updated.count("Pendência acionável detectada") == 1


def test_apply_guard_clears_completed_for_actionable_open_work():
    updated, completed, finding = apply_open_work_guard("Fiz A. Falta fechar B.", completed=True)

    assert completed is False
    assert finding.detected is True
    assert "Pendência acionável detectada" in updated


def test_apply_guard_preserves_completed_for_user_blocked_work():
    updated, completed, finding = apply_open_work_guard(
        "Pendência: preciso da sua aprovação antes de executar.",
        completed=True,
    )

    assert completed is True
    assert finding.detected is False
    assert "Pendência acionável detectada" not in updated
