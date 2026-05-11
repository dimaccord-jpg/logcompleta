from app.user_area import _checkout_feedback_downgrade_pendente


def test_checkout_feedback_pro_para_starter_formata_texto_e_data_br():
    out = _checkout_feedback_downgrade_pendente(
        plano_atual="pro",
        plano_pendente="starter",
        efetivar_em="2026-05-10T13:07:08",
        data_vencimento_iso=None,
    )
    assert out["nivel"] == "success"
    assert out["mensagem"] == (
        "Sua alteração para o plano Starter foi registrada. "
        "Ela entrará em vigor em 10/05/2026."
    )


def test_checkout_feedback_pro_para_free_formata_texto_e_data_br():
    out = _checkout_feedback_downgrade_pendente(
        plano_atual="pro",
        plano_pendente="free",
        efetivar_em="2026-05-10T13:07:08",
        data_vencimento_iso=None,
    )
    assert out["nivel"] == "success"
    assert out["mensagem"] == (
        "Sua assinatura do plano Pro foi cancelada. "
        "Essa alteração entrará em vigor em 10/05/2026."
    )


def test_checkout_feedback_starter_para_free_formata_texto_e_data_br():
    out = _checkout_feedback_downgrade_pendente(
        plano_atual="starter",
        plano_pendente="free",
        efetivar_em="2026-05-10T13:07:08",
        data_vencimento_iso=None,
    )
    assert out["nivel"] == "success"
    assert out["mensagem"] == (
        "Sua assinatura do plano Starter foi cancelada. "
        "Essa alteração entrará em vigor em 10/05/2026."
    )
