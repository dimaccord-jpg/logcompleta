"""SCRUM-259 correção 3: lease, timeout e resultado externo sem documento.

Transporte falso. Não abre o cliente Anthropic.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import httpx

from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiExecucao,
    Franquia,
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
    utcnow_naive,
)
from app.services.agente_bi_analisar_service import provedor_simulado
from app.services.agente_bi_anthropic_service import (
    FASES_DO_FLOAT,
    FASE_CONNECT_SEGUNDOS,
    FASE_POOL_SEGUNDOS,
    FASE_WRITE_SEGUNDOS,
    LEITURA_MINIMA_SEGUNDOS,
    MARGEM_CONFIRMACAO_SEGUNDOS,
    TIMEOUT_PADRAO_SEGUNDOS,
    TIMEOUT_TETO_SEGUNDOS,
    chamar_anthropic_governado,
    fases_para_orcamento,
    materializar_prazo,
)
from app.services.agente_bi_execucao_service import (
    LEASE_MAX_SEGUNDOS,
    LEASE_PADRAO_SEGUNDOS,
    ExecucaoErro,
    adquirir,
    enfileirar,
    executar_um_ciclo,
    recuperar_leases,
)
from app.services.agente_bi_anthropic_service import ErroTransporte
from app.services.agente_bi_integracao_service import executar_operacao, processar_transporte_simulado
from app.services.agente_bi_persistencia_service import AgenteBiErro, escopo_do_usuario, obter_analise
from tests.test_agente_bi_ingestao import _par
from tests.test_agente_bi_integracao import _habilitar, _preparar


def _foto(franquia_id):
    franquia = db.session.get(Franquia, franquia_id)
    db.session.refresh(franquia)
    return Decimal(franquia.consumo_acumulado or 0), Decimal(franquia.reserva_pendente or 0)


def _resposta(texto, *, entrada=None, saida=None):
    return {
        "texto": texto,
        "stop_reason": "end_turn",
        "input_tokens": entrada,
        "output_tokens": saida,
        "recusa": False,
        "truncada": False,
    }


def _proposta(pedido):
    payload = json.loads(pedido["messages"][0]["content"])
    return provedor_simulado(payload)


def _transporte_ok(estado, *, entrada=None, saida=None, ao_enviar=None):
    def enviar(pedido):
        estado["chamadas"] = estado.get("chamadas", 0) + 1
        if ao_enviar is not None:
            ao_enviar()
        texto = json.dumps(
            _proposta(pedido),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _resposta(texto, entrada=entrada, saida=saida)

    return enviar


def _expirar_lease_adquirido():
    linha = AgenteBiExecucao.query.filter_by(estado="adquirida").one()
    linha.lease_ate = utcnow_naive() - timedelta(seconds=1)
    db.session.commit()


def _assert_sem_documento(execucao_id):
    linha = db.session.get(AgenteBiExecucao, int(execucao_id))
    analise = db.session.get(AgenteBiAnalise, linha.analise_id)
    db.session.refresh(analise)
    assert linha.estado == "incerta"
    assert linha.efeito_externo == "incerto"
    assert analise.resultado_json is None
    assert linha.codigo_resultado is None


def test_float_do_transporte_nao_e_prazo_total_e_o_lease_nao_sobe():
    prazo = httpx.Timeout(TIMEOUT_PADRAO_SEGUNDOS)
    assert (prazo.connect, prazo.read, prazo.write, prazo.pool) == (30.0, 30.0, 30.0, 30.0)
    parede = prazo.connect + prazo.read + prazo.write + prazo.pool
    assert parede == 120.0
    assert parede > LEASE_PADRAO_SEGUNDOS
    assert LEASE_PADRAO_SEGUNDOS == 30
    assert LEASE_MAX_SEGUNDOS == 120
    assert TIMEOUT_PADRAO_SEGUNDOS == 30.0
    assert TIMEOUT_TETO_SEGUNDOS == 120.0
    orcamento = LEASE_PADRAO_SEGUNDOS - MARGEM_CONFIRMACAO_SEGUNDOS
    fases = fases_para_orcamento(TIMEOUT_PADRAO_SEGUNDOS, orcamento)
    assert fases is not None
    assert fases["connect"] == FASE_CONNECT_SEGUNDOS
    assert fases["write"] == FASE_WRITE_SEGUNDOS
    assert fases["pool"] == FASE_POOL_SEGUNDOS
    assert LEITURA_MINIMA_SEGUNDOS <= fases["read"] <= TIMEOUT_PADRAO_SEGUNDOS
    assert sum(fases.values()) <= orcamento
    assert fases_para_orcamento(TIMEOUT_PADRAO_SEGUNDOS, 10.0) is None
    minimo = (
        FASE_CONNECT_SEGUNDOS
        + FASE_WRITE_SEGUNDOS
        + FASE_POOL_SEGUNDOS
        + LEITURA_MINIMA_SEGUNDOS
        + MARGEM_CONFIRMACAO_SEGUNDOS
    )
    assert minimo < LEASE_PADRAO_SEGUNDOS


def test_materializa_float_conservador_sem_httpx2(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "httpx2", None)
    fases = fases_para_orcamento(30.0, 25.0)
    prazo = materializar_prazo(fases)
    assert isinstance(prazo, float)
    assert FASES_DO_FLOAT * prazo <= 25.0 + 1e-9


def test_lease_suficiente_conclui_e_persiste(app, monkeypatch):
    _habilitar(monkeypatch)
    estado = {"chamadas": 0}
    with app.app_context():
        _usuario, franquia, escopo, dataset_id = _preparar("lease-ok@test.com")
        antes = _foto(franquia.id)
        resultado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-lease-ok",
            chave_analise="bi-lease-ok",
            transporte=_transporte_ok(estado),
        )
        assert resultado["ciclo"]["execucao"]["estado"] == "concluida"
        assert resultado["saida"]["contrato"] == "analisar.v1"
        assert estado["chamadas"] == 1
        linha = db.session.get(AgenteBiExecucao, resultado["ciclo"]["execucao"]["id"])
        assert linha.efeito_externo == "simulado"
        gravado = json.loads(db.session.get(AgenteBiAnalise, linha.analise_id).resultado_json)
        assert gravado == resultado["saida"]
        assert IaChamadaTentativa.query.count() == 1
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes


def test_orcamento_insuficiente_nao_cria_tentativa_nem_cobra(app, monkeypatch):
    _habilitar(monkeypatch)
    estado = {"chamadas": 0}
    with app.app_context():
        _usuario, franquia, escopo, dataset_id = _preparar("lease-curto@test.com")
        antes = _foto(franquia.id)

        def relogio_curto():
            linha = AgenteBiExecucao.query.filter_by(estado="adquirida").one()
            return linha.lease_ate - timedelta(seconds=2)

        monkeypatch.setattr(
            "app.services.agente_bi_anthropic_service.utcnow_naive",
            relogio_curto,
        )
        recusado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-lease-curto",
            chave_analise="bi-lease-curto",
            transporte=_transporte_ok(estado),
        )
        assert recusado["ciclo"]["execucao"]["estado"] == "falha_recuperavel"
        assert recusado["ciclo"]["execucao"]["ultimo_erro"] == "orcamento_temporal"
        assert recusado["saida"] is None
        assert estado["chamadas"] == 0
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoEvento.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes
        assert db.session.get(AgenteBiAnalise, recusado["abertura"]["analise_id"]).resultado_json is None

        from app.models import utcnow_naive as relogio_real

        monkeypatch.setattr(
            "app.services.agente_bi_anthropic_service.utcnow_naive",
            relogio_real,
        )
        recuperado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-lease-curto-2",
            chave_analise="bi-lease-curto",
            transporte=_transporte_ok(estado),
        )
        assert recuperado["ciclo"]["execucao"]["estado"] == "concluida"
        assert recuperado["saida"]["contrato"] == "analisar.v1"
        assert estado["chamadas"] == 1
        assert IaChamadaTentativa.query.count() == 1
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes


def _perder_confirmacao(estado, *, ao_enviar=None):
    return executar_operacao(
        estado["escopo"],
        estado["dataset_id"],
        {"modalidades": ["A"]},
        worker_id="worker-lease-perdido",
        chave_analise=estado["chave"],
        transporte=_transporte_ok(
            estado,
            entrada=1000,
            saida=1000,
            ao_enviar=ao_enviar or _expirar_lease_adquirido,
        ),
    )


def _preparar_perda(email, chave):
    _usuario, franquia, escopo, dataset_id = _preparar(email)
    return {
        "chamadas": 0,
        "franquia_id": franquia.id,
        "antes": _foto(franquia.id),
        "escopo": escopo,
        "dataset_id": dataset_id,
        "usuario": _usuario,
        "chave": chave,
    }


def test_lease_expira_durante_transporte_nao_repete(app, monkeypatch):
    _habilitar(monkeypatch)
    with app.app_context():
        estado = _preparar_perda("lease-durante@test.com", "bi-lease-durante")
        resultado = _perder_confirmacao(estado)
        execucao = resultado["ciclo"]["execucao"]
        assert execucao["estado"] == "incerta"
        assert resultado["saida"] is None
        assert estado["chamadas"] == 1
        _assert_sem_documento(execucao["id"])
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        cobrado = _foto(estado["franquia_id"])
        assert cobrado[0] == Decimal("2")
        assert cobrado[1] == estado["antes"][1]
        de_novo = executar_um_ciclo(
            worker_id="worker-lease-durante-2",
            processador=lambda refs: processar_transporte_simulado(
                refs,
                transporte=_transporte_ok(estado, entrada=1000, saida=1000),
            ),
        )
        assert de_novo["adquirida"] is False
        assert estado["chamadas"] == 1
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        assert _foto(estado["franquia_id"]) == cobrado


def test_resposta_contabilizada_com_confirmacao_recusada(app, monkeypatch):
    _habilitar(monkeypatch)
    with app.app_context():
        estado = _preparar_perda("lease-contabil@test.com", "bi-lease-contabil")
        resultado = _perder_confirmacao(estado)
        linha = db.session.get(AgenteBiExecucao, resultado["ciclo"]["execucao"]["id"])
        tentativa = db.session.get(IaChamadaTentativa, linha.ia_chamada_tentativa_id)
        assert tentativa.status == IaChamadaTentativa.STATUS_SETTLED
        assert tentativa.ia_consumo_evento_id is not None
        assert resultado["saida"] is None
        _assert_sem_documento(linha.id)
        assert Decimal(db.session.get(Franquia, estado["franquia_id"]).consumo_acumulado) == Decimal("2")
        assert [item for item in (linha.codigo_resultado, linha.estado) if item == "concluida"] == []


def test_reentrada_da_mesma_attempt_key_nao_chama_de_novo(app, monkeypatch):
    _habilitar(monkeypatch)
    with app.app_context():
        estado = _preparar_perda("lease-reentrada@test.com", "bi-lease-reentrada")
        resultado = _perder_confirmacao(estado)
        linha = db.session.get(AgenteBiExecucao, resultado["ciclo"]["execucao"]["id"])
        tentativa = db.session.get(IaChamadaTentativa, linha.ia_chamada_tentativa_id)
        evento_id = tentativa.ia_consumo_evento_id
        extra = {"chamadas": 0}
        repetida = chamar_anthropic_governado(
            usuario_id=estado["usuario"].id,
            conta_id=estado["usuario"].conta_id,
            franquia_id=estado["franquia_id"],
            mensagem="Frete SP 10 kg",
            chave_idempotencia=linha.chave_idempotencia,
            execucao_id=linha.id,
            worker_id="worker-lease-reentrada",
            fencing=int(linha.fencing),
            transporte=_transporte_ok(extra, entrada=1000, saida=1000),
            model="claude-sonnet-simulado",
            max_tokens=128,
        )
        assert repetida["provedor_chamado"] is False
        assert repetida["incerto"] is True
        assert repetida["ok"] is False
        assert extra["chamadas"] == 0
        assert estado["chamadas"] == 1
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
        assert db.session.get(IaChamadaTentativa, tentativa.id).ia_consumo_evento_id == evento_id
        assert IaConsumoAbatimento.query.count() == 1
        assert Decimal(db.session.get(Franquia, estado["franquia_id"]).consumo_acumulado) == Decimal("2")
        _assert_sem_documento(linha.id)


def test_dois_workers_com_lease_expirado_durante_a_operacao(app, monkeypatch):
    _habilitar(monkeypatch)
    with app.app_context():
        estado = _preparar_perda("lease-dois@test.com", "bi-lease-dois")
        recuperadas = {"total": 0}

        def durante():
            _expirar_lease_adquirido()
            recuperadas["total"] = recuperar_leases()

        resultado = _perder_confirmacao(estado, ao_enviar=durante)
        assert recuperadas["total"] == 1
        assert estado["chamadas"] == 1
        _assert_sem_documento(resultado["ciclo"]["execucao"]["id"])
        segundo = executar_um_ciclo(
            worker_id="worker-lease-dois-b",
            processador=lambda refs: processar_transporte_simulado(
                refs,
                transporte=_transporte_ok(estado, entrada=1000, saida=1000),
            ),
        )
        assert segundo["adquirida"] is False
        assert estado["chamadas"] == 1
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
        assert IaConsumoAbatimento.query.count() == 1


def test_timeout_com_lease_expirado_fica_incerto(app, monkeypatch):
    _habilitar(monkeypatch)
    estado = {"chamadas": 0}
    with app.app_context():
        _usuario, franquia, escopo, dataset_id = _preparar("lease-timeout@test.com")
        antes = _foto(franquia.id)

        def enviar(_pedido):
            estado["chamadas"] += 1
            _expirar_lease_adquirido()
            raise ErroTransporte("timeout", incerto=True)

        resultado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-lease-timeout",
            chave_analise="bi-lease-timeout",
            transporte=enviar,
        )
        execucao = resultado["ciclo"]["execucao"]
        assert execucao["estado"] == "incerta"
        assert execucao["efeito_externo"] == "incerto"
        assert estado["chamadas"] == 1
        linha = db.session.get(AgenteBiExecucao, execucao["id"])
        tentativa = db.session.get(IaChamadaTentativa, linha.ia_chamada_tentativa_id)
        assert tentativa.status == IaChamadaTentativa.STATUS_UNCERTAIN
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes
        _assert_sem_documento(linha.id)
        segundo = executar_um_ciclo(
            worker_id="worker-lease-timeout-2",
            processador=lambda refs: processar_transporte_simulado(refs, transporte=enviar),
        )
        assert segundo["adquirida"] is False
        assert estado["chamadas"] == 1


def test_recuperacao_nao_repete_ciclo_inutil(app, monkeypatch):
    _habilitar(monkeypatch)
    with app.app_context():
        estado = _preparar_perda("lease-ciclo@test.com", "bi-lease-ciclo")
        resultado = _perder_confirmacao(estado)
        linha = db.session.get(AgenteBiExecucao, resultado["ciclo"]["execucao"]["id"])
        tentativas = int(linha.tentativas)
        for indice in range(3):
            assert recuperar_leases() == 0
            ciclo = executar_um_ciclo(
                worker_id=f"worker-lease-ciclo-{indice}",
                processador=lambda refs: processar_transporte_simulado(
                    refs,
                    transporte=_transporte_ok(estado, entrada=1000, saida=1000),
                ),
            )
            assert ciclo["adquirida"] is False
        atual = db.session.get(AgenteBiExecucao, linha.id)
        assert atual.estado == "incerta"
        assert int(atual.tentativas) == tentativas
        assert estado["chamadas"] == 1
        assert IaConsumoAbatimento.query.count() == 1
        _assert_sem_documento(linha.id)


def test_regressao_analisar_persiste_e_outro_escopo_nao_le(app, monkeypatch):
    _habilitar(monkeypatch)
    estado = {"chamadas": 0}
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id = _preparar("lease-a@test.com")
        resultado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-lease-a",
            chave_analise="bi-lease-a",
            transporte=_transporte_ok(estado),
        )
        analise_id = resultado["abertura"]["analise_id"]
        documento = resultado["saida"]
        assert documento["contrato"] == "analisar.v1"
        assert obter_analise(escopo, analise_id)["resultado"] == documento
        _conta, _franquia_b, usuario_b = _par("conta-lease-b", "lease-b@test.com")
        try:
            obter_analise(escopo_do_usuario(usuario_b), analise_id)
        except AgenteBiErro as exc:
            assert exc.codigo == "nao_encontrado"
        else:
            raise AssertionError("outro escopo leu a analise")
        ciclo_b = executar_um_ciclo(
            worker_id="worker-lease-b",
            processador=lambda refs: processar_transporte_simulado(refs, transporte=_transporte_ok(estado)),
        )
        assert ciclo_b["adquirida"] is False
        assert estado["chamadas"] == 1
        assert json.loads(db.session.get(AgenteBiAnalise, analise_id).resultado_json) == documento


def _vencer_lease_na_confirmacao(monkeypatch, visto, *, ceder: bool) -> None:
    """Vence o lease depois do evento e do abatimento, antes do update de confirmar()."""
    import app.services.agente_bi_execucao_service as fila

    original = fila.confirmar

    def confirmar(execucao_id, **kwargs):
        linha = db.session.get(AgenteBiExecucao, int(execucao_id))
        db.session.refresh(linha)
        tentativa = db.session.get(IaChamadaTentativa, linha.ia_chamada_tentativa_id)
        assert tentativa is not None
        assert tentativa.status == IaChamadaTentativa.STATUS_SETTLED
        assert tentativa.ia_consumo_evento_id is not None
        assert IaConsumoAbatimento.query.count() == 1
        visto["efeito_na_confirmacao"] = linha.efeito_externo
        visto["tentativas_na_confirmacao"] = int(linha.tentativas)
        linha.lease_ate = utcnow_naive() - timedelta(seconds=1)
        db.session.commit()
        if ceder:
            visto["recuperadas_concorrente"] = fila.recuperar_leases()
            concorrente = db.session.get(AgenteBiExecucao, int(execucao_id))
            db.session.refresh(concorrente)
            visto["estado_apos_concorrente"] = concorrente.estado
            visto["efeito_apos_concorrente"] = concorrente.efeito_externo
        return original(execucao_id, **kwargs)

    monkeypatch.setattr(fila, "confirmar", confirmar)


def fotografar_janela_pos_liquidacao(
    monkeypatch,
    *,
    escopo,
    dataset_id,
    usuario,
    franquia_id,
    antes,
    chave: str,
    worker: str,
    ceder: bool,
) -> dict:
    """Chamada contabilizada, documento ainda ausente, lease morto antes de confirmar()."""
    visto = {}
    chamadas = {"chamadas": 0}
    _vencer_lease_na_confirmacao(monkeypatch, visto, ceder=ceder)
    erro = None
    try:
        executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id=worker,
            chave_analise=chave,
            transporte=_transporte_ok(chamadas, entrada=1000, saida=1000),
        )
    except ExecucaoErro as exc:
        erro = exc.codigo
    recuperar_leases()
    adquiridas = 0
    for indice in range(3):
        ciclo = executar_um_ciclo(
            worker_id=f"{worker}-depois-{indice}",
            processador=lambda refs: processar_transporte_simulado(
                refs,
                transporte=_transporte_ok(chamadas, entrada=1000, saida=1000),
            ),
        )
        if ciclo.get("adquirida"):
            adquiridas += 1
    linha = AgenteBiExecucao.query.one()
    db.session.refresh(linha)
    analise = db.session.get(AgenteBiAnalise, linha.analise_id)
    db.session.refresh(analise)
    extra = {"chamadas": 0}
    repetida = chamar_anthropic_governado(
        usuario_id=usuario.id,
        conta_id=usuario.conta_id,
        franquia_id=usuario.franquia_id,
        mensagem="Frete SP 10 kg",
        chave_idempotencia=linha.chave_idempotencia,
        execucao_id=linha.id,
        worker_id=f"{worker}-reentrada",
        fencing=int(linha.fencing),
        transporte=_transporte_ok(extra, entrada=1000, saida=1000),
        model="claude-sonnet-simulado",
        max_tokens=128,
    )
    consumo, reserva = _foto(franquia_id)
    foto = {
        "erro": erro,
        "estado": linha.estado,
        "efeito": linha.efeito_externo,
        "efeito_na_confirmacao": visto.get("efeito_na_confirmacao"),
        "tentativas": int(linha.tentativas),
        "tentativas_na_confirmacao": visto.get("tentativas_na_confirmacao"),
        "adquiridas_posteriores": adquiridas,
        "chamadas": chamadas["chamadas"],
        "reentrada_chamadas": extra["chamadas"],
        "eventos": IaConsumoEvento.query.filter_by(provider="anthropic").count(),
        "abatimentos": IaConsumoAbatimento.query.count(),
        "tentativas_ia": IaChamadaTentativa.query.count(),
        "reentrada_incerta": repetida.get("incerto") is True and repetida.get("provedor_chamado") is False,
        "documento": analise.resultado_json,
        "codigo_resultado": linha.codigo_resultado,
        "consumo": consumo,
        "reserva": reserva,
        "reserva_inicial": antes[1],
    }
    if ceder:
        foto["recuperadas_concorrente"] = visto.get("recuperadas_concorrente")
        foto["estado_apos_concorrente"] = visto.get("estado_apos_concorrente")
        foto["efeito_apos_concorrente"] = visto.get("efeito_apos_concorrente")
    return foto


def _foto_esperada_pos_liquidacao(*, ceder: bool, reserva_inicial) -> dict:
    foto = {
        "erro": None,
        "estado": "incerta",
        "efeito": "incerto",
        "efeito_na_confirmacao": "incerto",
        "tentativas": 1,
        "tentativas_na_confirmacao": 1,
        "adquiridas_posteriores": 0,
        "chamadas": 1,
        "reentrada_chamadas": 0,
        "eventos": 1,
        "abatimentos": 1,
        "tentativas_ia": 1,
        "reentrada_incerta": True,
        "documento": None,
        "codigo_resultado": None,
        "consumo": Decimal("2"),
        "reserva": reserva_inicial,
        "reserva_inicial": reserva_inicial,
    }
    if ceder:
        foto["recuperadas_concorrente"] = 1
        foto["estado_apos_concorrente"] = "incerta"
        foto["efeito_apos_concorrente"] = "incerto"
    return foto


def test_expiracao_apos_liquidacao_nao_recoloca_nem_reenvia(app, monkeypatch):
    _habilitar(monkeypatch)
    with app.app_context():
        estado = _preparar_perda("lease-janela@test.com", "bi-lease-janela")
        foto = fotografar_janela_pos_liquidacao(
            monkeypatch,
            escopo=estado["escopo"],
            dataset_id=estado["dataset_id"],
            usuario=estado["usuario"],
            franquia_id=estado["franquia_id"],
            antes=estado["antes"],
            chave="bi-lease-janela",
            worker="worker-janela",
            ceder=False,
        )
        assert foto == _foto_esperada_pos_liquidacao(ceder=False, reserva_inicial=estado["antes"][1])


def test_dois_workers_apos_liquidacao_nao_refilam(app, monkeypatch):
    _habilitar(monkeypatch)
    with app.app_context():
        estado = _preparar_perda("lease-janela-2@test.com", "bi-lease-janela-2")
        foto = fotografar_janela_pos_liquidacao(
            monkeypatch,
            escopo=estado["escopo"],
            dataset_id=estado["dataset_id"],
            usuario=estado["usuario"],
            franquia_id=estado["franquia_id"],
            antes=estado["antes"],
            chave="bi-lease-janela-2",
            worker="worker-janela-2",
            ceder=True,
        )
        assert foto == _foto_esperada_pos_liquidacao(ceder=True, reserva_inicial=estado["antes"][1])


def _processador_sem_chamada(_refs):
    raise RuntimeError("sem-chamada")


def cenario_efeito_liberado(escopo, analise_id: int) -> dict:
    """Estado deixado por _liberar_efeito: tentativa liquidada, efeito nenhum, lease morto."""
    criada = enfileirar(escopo, analise_id)
    adquirir(worker_id="worker-liberado")
    linha = db.session.get(AgenteBiExecucao, criada["id"])
    tentativa = IaChamadaTentativa(
        attempt_key=linha.chave_idempotencia,
        conta_id=linha.conta_id,
        franquia_id=linha.franquia_id,
        usuario_id=linha.usuario_id,
        origem_sistema=False,
        agent="agente_bi",
        flow_type="bi_logistico",
        operation="messages",
        provider="anthropic",
        model="claude-sonnet-simulado",
        reserved_credits=0,
        status=IaChamadaTentativa.STATUS_SETTLED,
        created_at=utcnow_naive(),
        settled_at=utcnow_naive(),
    )
    db.session.add(tentativa)
    db.session.flush()
    linha.ia_chamada_tentativa_id = tentativa.id
    linha.efeito_externo = "nenhum"
    linha.lease_ate = utcnow_naive() - timedelta(seconds=1)
    db.session.commit()
    tentativas = int(linha.tentativas)
    assert recuperar_leases() == 1
    adquiridas = 0
    for indice in range(3):
        ciclo = executar_um_ciclo(
            worker_id=f"worker-liberado-{indice}",
            processador=_processador_sem_chamada,
        )
        if ciclo.get("adquirida"):
            adquiridas += 1
    atual = db.session.get(AgenteBiExecucao, linha.id)
    return {
        "estado": atual.estado,
        "efeito": atual.efeito_externo,
        "tentativas": int(atual.tentativas),
        "tentativas_iniciais": tentativas,
        "adquiridas": adquiridas,
        "tentativas_ia": IaChamadaTentativa.query.count(),
        "documento": db.session.get(AgenteBiAnalise, atual.analise_id).resultado_json,
    }


def test_recuperacao_de_efeito_liberado_nao_incrementa_tentativas(app):
    from tests.test_agente_bi_execucao import _preparar as preparar_execucao

    with app.app_context():
        _usuario, escopo, _dataset, analise = preparar_execucao("lease-liberado@test.com")
        assert cenario_efeito_liberado(escopo, analise["id"]) == {
            "estado": "incerta",
            "efeito": "incerto",
            "tentativas": 1,
            "tentativas_iniciais": 1,
            "adquiridas": 0,
            "tentativas_ia": 1,
            "documento": None,
        }
