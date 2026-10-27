"""SCRUM-259: preparação, perfil, analisar.v1, adaptador e fila, com transporte falso.

Não abre o cliente Anthropic, não grava migration e não liga o worker.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiDataset,
    AgenteBiExecucao,
    Franquia,
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
    utcnow_naive,
)
from app.run_agente_bi_worker import main, processo_habilitado
from app.services.agente_bi_anthropic_service import (
    RETRIES,
    TIMEOUT_PADRAO_SEGUNDOS,
    TIMEOUT_TETO_SEGUNDOS,
    ErroTransporte,
)
from app.services.agente_bi_analisar_service import (
    AnalisarErro,
    analisar,
    contexto_de_transferencia,
    montar_contexto,
    provedor_simulado,
)
from app.services.agente_bi_execucao_service import (
    LEASE_MAX_SEGUNDOS,
    LEASE_PADRAO_SEGUNDOS,
    ExecucaoErro,
    adquirir,
    confirmar,
    executar_um_ciclo,
    listar_eventos,
    recuperar_leases,
)
import app.services.agente_bi_integracao_service as integracao
from app.services.agente_bi_integracao_service import (
    MAX_TOKENS_SIMULADO,
    IntegracaoErro,
    abrir_operacao,
    autorizar_transferencia,
    executar_operacao,
    processar_transporte_simulado,
    validar_resposta_simulada,
)
from app.services.agente_bi_perfil_semantico_service import perfilar_dataset
from app.services.agente_bi_persistencia_service import escopo_do_usuario, obter_analise
from app.services.cleiton_ai_data_governance import DECISION_BLOCK, GovernanceResult
from tests.test_agente_bi_execucao import _seed_financeiro
from tests.test_agente_bi_ingestao import _ingerir

SENTINELA = "NF8842109"
FORMULA = "=SUM(A1:A9)"
MARCADOR = "CELULA-SENTINELA-XZ"


def _habilitar(monkeypatch):
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENTE_BI_ANTHROPIC_MAX_TOKENS", raising=False)
    monkeypatch.delenv("AGENTE_BI_ANTHROPIC_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AGENTE_BI_ANTHROPIC_MODEL", raising=False)


def _csv() -> bytes:
    return (
        f"alfa,beta\n{SENTINELA},{MARCADOR}\nK200,sul\n".encode("utf-8")
    )


def _pedido():
    return {"modalidades": ["C"], "texto": f"soma {FORMULA} {SENTINELA}"}


def _foto(franquia_id):
    franquia = db.session.get(Franquia, franquia_id)
    db.session.refresh(franquia)
    return Decimal(franquia.consumo_acumulado or 0), Decimal(franquia.reserva_pendente or 0)


def _preparar(email="integracao@test.com", *, financeiro=True):
    if financeiro:
        usuario, franquia, escopo, _analise = _seed_financeiro(email=email)
    else:
        from tests.test_agente_bi_ingestao import _par

        _conta, franquia, usuario = _par(f"conta-{email}", email)
        escopo = escopo_do_usuario(usuario)
    ingerido = _ingerir(usuario, _csv(), "integracao.csv", chave=f"lote-{email.split('@')[0]}")
    return usuario, franquia, escopo, ingerido["dataset"]["id"]


def _resposta(texto):
    return {
        "texto": texto,
        "stop_reason": "end_turn",
        "input_tokens": None,
        "output_tokens": None,
        "recusa": False,
        "truncada": False,
    }


def _transporte_proposta(estado):
    def enviar(pedido):
        estado["chamadas"] = estado.get("chamadas", 0) + 1
        estado["pedido"] = pedido
        payload = json.loads(pedido["messages"][0]["content"])
        proposta = provedor_simulado(payload)
        return _resposta(json.dumps(proposta, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    return enviar


def _sem_vazamento(texto: str):
    assert SENTINELA not in texto
    assert FORMULA not in texto
    assert MARCADOR not in texto
    assert "sul" not in texto


def _linguagem_preservada_sem_dsl(bruto: str):
    """A frase do usuário permanece texto. Célula e fórmula não viram execução."""
    assert MARCADOR not in bruto
    assert "sul" not in bruto
    corpo = json.loads(bruto)
    if isinstance(corpo, dict) and "messages" in corpo:
        corpo = json.loads(corpo["messages"][0]["content"])
    assert corpo["intencao"] == _pedido()["texto"]
    resto = dict(corpo)
    resto.pop("intencao")
    restante = json.dumps(resto, ensure_ascii=False)
    assert FORMULA not in restante
    assert SENTINELA not in restante
    assert "formulas" not in corpo
    assert corpo.get("intencoes") == ["soma"]


def test_limites_de_lease_e_timeout_permanecem():
    assert LEASE_PADRAO_SEGUNDOS == 30
    assert LEASE_MAX_SEGUNDOS == 120
    assert TIMEOUT_PADRAO_SEGUNDOS == 30.0
    assert TIMEOUT_TETO_SEGUNDOS == 120.0
    assert MAX_TOKENS_SIMULADO <= 4096
    assert RETRIES == 0


def test_worker_permanece_desligado(monkeypatch):
    monkeypatch.delenv("AGENTE_BI_WORKER_HABILITADO", raising=False)
    assert processo_habilitado() is False
    assert main([]) == 2


def test_flag_falso_nao_libera_transferencia():
    with pytest.raises(IntegracaoErro) as exc:
        autorizar_transferencia(
            {"governanca": {"apto_anthropic": False}},
            {"apto_anthropic": False, "contrato": "contexto.analisar.v1"},
        )
    assert exc.value.codigo == "contexto_nao_autorizado"


def test_governanca_bloqueada_recusa_mesmo_com_flag_falso(app, monkeypatch):
    def bloqueia(*_args, **_kwargs):
        return GovernanceResult(
            decision=DECISION_BLOCK,
            safe_content=None,
            reason_codes=["conteudo_nao_autorizado"],
            issued_by_cleiton=True,
        )

    monkeypatch.setattr(
        "app.services.agente_bi_integracao_service.govern_outbound_content",
        bloqueia,
    )
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id = _preparar("integracao-block@test.com", financeiro=False)
        perfil = perfilar_dataset(escopo, dataset_id)
        assert perfil["governanca"]["apto_anthropic"] is False
        contexto = montar_contexto(perfil, {"modalidades": ["A"]})
        with pytest.raises(IntegracaoErro) as exc:
            autorizar_transferencia(perfil, contexto)
        assert exc.value.codigo == "contexto_nao_autorizado"
        assert AgenteBiExecucao.query.count() == 0


def test_contexto_com_valor_de_celula_nao_libera(app, monkeypatch):
    real = contexto_de_transferencia

    def sujo(contexto):
        payload = real(contexto)
        payload["colunas"][0]["valor"] = SENTINELA
        return payload

    monkeypatch.setattr("app.services.agente_bi_integracao_service.contexto_de_transferencia", sujo)
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id = _preparar("integracao-sujo@test.com", financeiro=False)
        perfil = perfilar_dataset(escopo, dataset_id)
        contexto = montar_contexto(perfil, {"modalidades": ["A"]})
        assert perfil["governanca"]["apto_anthropic"] is False
        with pytest.raises(IntegracaoErro) as exc:
            autorizar_transferencia(perfil, contexto)
        assert exc.value.codigo == "contexto_nao_autorizado"


def test_desligado_nao_chama_transporte_nem_cobra(app, monkeypatch):
    monkeypatch.delenv("AGENTE_BI_ANTHROPIC_ENABLED", raising=False)
    estado = {"chamadas": 0}
    with app.app_context():
        _usuario, franquia, escopo, dataset_id = _preparar("integracao-off@test.com")
        antes = _foto(franquia.id)
        resultado = executar_operacao(
            escopo,
            dataset_id,
            _pedido(),
            worker_id="worker-off",
            chave_analise="bi-integracao-off",
            transporte=_transporte_proposta(estado),
        )
        execucao = resultado["ciclo"]["execucao"]
        assert execucao["estado"] == "falha_recuperavel"
        assert execucao["ultimo_erro"] == "desabilitado"
        assert estado["chamadas"] == 0
        assert resultado["saida"] is None
        assert IaConsumoEvento.query.count() == 0
        assert IaChamadaTentativa.query.count() == 0
        assert _foto(franquia.id) == antes


def test_fluxo_prepara_perfil_contexto_analise_e_fila(app, monkeypatch, caplog):
    _habilitar(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "chave-que-nao-pode-ser-usada")

    def sdk_proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.montar_cliente_anthropic",
        sdk_proibido,
    )
    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service._transporte_sdk",
        sdk_proibido,
    )
    estado = {"chamadas": 0}
    with app.app_context(), caplog.at_level("INFO"):
        usuario, franquia, escopo, dataset_id = _preparar("integracao-ok@test.com")
        antes = _foto(franquia.id)
        perfil = perfilar_dataset(escopo, dataset_id)
        liberacao = autorizar_transferencia(perfil, montar_contexto(perfil, _pedido()))
        assert liberacao["liberado"] is True
        assert liberacao["motivo"] == "contexto_verificado"
        assert "finalidade_nao_basta" in liberacao["verificacoes"]
        _linguagem_preservada_sem_dsl(json.dumps(liberacao["payload"], ensure_ascii=False))
        resultado = executar_operacao(
            escopo,
            dataset_id,
            _pedido(),
            worker_id="worker-ok",
            chave_analise="bi-integracao-ok",
            transporte=_transporte_proposta(estado),
        )
        assert resultado["ciclo"]["adquirida"] is True
        saida = resultado["saida"]
        assert saida["contrato"] == "analisar.v1"
        assert saida["provedor"] == "simulado"
        assert saida["chamada_anthropic"] is False
        assert saida["apto_anthropic"] is False
        assert saida["contrato_saida_validado"] is True
        _sem_vazamento(json.dumps(saida, ensure_ascii=False))
        _linguagem_preservada_sem_dsl(json.dumps(estado["pedido"], ensure_ascii=False))
        assert estado["pedido"]["max_tokens"] == MAX_TOKENS_SIMULADO
        assert estado["pedido"]["model"] == "claude-sonnet-simulado"
        assert estado["chamadas"] == 1
        execucao = db.session.get(AgenteBiExecucao, resultado["ciclo"]["execucao"]["id"])
        assert execucao.estado == "concluida"
        assert execucao.efeito_externo == "simulado"
        assert execucao.codigo_resultado == "simulado"
        assert execucao.desfecho_financeiro is None
        assert execucao.financeiro_encerrado is False
        assert execucao.reserva_em_andamento is False
        tentativa = db.session.get(IaChamadaTentativa, execucao.ia_chamada_tentativa_id)
        evento = db.session.get(IaConsumoEvento, tentativa.ia_consumo_evento_id)
        assert tentativa.usuario_id == usuario.id
        assert tentativa.conta_id == usuario.conta_id
        assert tentativa.franquia_id == franquia.id
        assert tentativa.reserved_credits == 0
        assert tentativa.reserva_ativa is False
        assert evento.id == tentativa.ia_consumo_evento_id
        assert evento.usuario_id == usuario.id
        assert evento.franquia_id == franquia.id
        assert evento.agent == "agente_bi"
        assert evento.input_tokens is None
        assert evento.output_tokens is None
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes
        analise = db.session.get(AgenteBiAnalise, execucao.analise_id)
        gravado = json.loads(analise.resultado_json)
        assert gravado == saida
        assert gravado["contrato"] == "analisar.v1"
        assert "execucao_simulada.v1" not in analise.resultado_json
        assert SENTINELA not in analise.resultado_json
        assert FORMULA not in analise.resultado_json
        assert SENTINELA not in caplog.text
        assert FORMULA not in caplog.text
        assert "chave-que-nao-pode-ser-usada" not in caplog.text
        repetida = executar_operacao(
            escopo,
            dataset_id,
            _pedido(),
            worker_id="worker-ok",
            chave_analise="bi-integracao-ok",
            transporte=_transporte_proposta(estado),
        )
        assert repetida["ciclo"]["adquirida"] is False
        assert estado["chamadas"] == 1
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes


def test_resposta_invalida_falha_sem_segunda_chamada_nem_cobranca(app, monkeypatch):
    _habilitar(monkeypatch)
    estado = {"chamadas": 0}

    def enviar(pedido):
        estado["chamadas"] += 1
        estado["pedido"] = pedido
        return _resposta('{"ok":true}')

    with app.app_context():
        _usuario, franquia, escopo, dataset_id = _preparar("integracao-ruim@test.com")
        antes = _foto(franquia.id)
        resultado = executar_operacao(
            escopo,
            dataset_id,
            _pedido(),
            worker_id="worker-ruim",
            chave_analise="bi-integracao-ruim",
            transporte=enviar,
        )
        assert resultado["saida"] is None
        assert resultado["ciclo"]["execucao"]["estado"] == "falha_definitiva"
        assert resultado["ciclo"]["execucao"]["ultimo_erro"] == "contrato_saida_invalido"
        assert resultado["ciclo"]["execucao"]["tentativas"] == 1
        analise = db.session.get(AgenteBiAnalise, resultado["abertura"]["analise_id"])
        assert analise.resultado_json is None
        assert analise.estado == "falha_validacao"
        linha = db.session.get(AgenteBiExecucao, resultado["ciclo"]["execucao"]["id"])
        tentativa = db.session.get(IaChamadaTentativa, linha.ia_chamada_tentativa_id)
        assert tentativa.status == IaChamadaTentativa.STATUS_SETTLED
        assert tentativa.ia_consumo_evento_id is not None
        _linguagem_preservada_sem_dsl(json.dumps(estado["pedido"], ensure_ascii=False))
        de_novo = executar_um_ciclo(
            worker_id="worker-ruim-2",
            processador=lambda refs: processar_transporte_simulado(refs, transporte=enviar),
        )
        assert de_novo["adquirida"] is False
        assert estado["chamadas"] == 1
        assert db.session.get(IaChamadaTentativa, tentativa.id).status == IaChamadaTentativa.STATUS_SETTLED
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes


def test_timeout_marca_efeito_incerto_sem_repetir(app, monkeypatch):
    _habilitar(monkeypatch)
    estado = {"chamadas": 0}

    def enviar(_pedido):
        estado["chamadas"] += 1
        raise ErroTransporte("timeout", incerto=True)

    with app.app_context():
        _usuario, franquia, escopo, dataset_id = _preparar("integracao-timeout@test.com")
        antes = _foto(franquia.id)
        resultado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-timeout",
            chave_analise="bi-integracao-timeout",
            transporte=enviar,
        )
        execucao = resultado["ciclo"]["execucao"]
        assert execucao["estado"] == "incerta"
        assert execucao["efeito_externo"] == "incerto"
        assert estado["chamadas"] == 1
        linha = db.session.get(AgenteBiExecucao, execucao["id"])
        assert linha.ia_chamada_tentativa_id is not None
        tentativa = db.session.get(IaChamadaTentativa, linha.ia_chamada_tentativa_id)
        assert tentativa.ia_consumo_evento_id is not None
        assert tentativa.status == IaChamadaTentativa.STATUS_UNCERTAIN
        segundo = executar_um_ciclo(
            worker_id="worker-timeout-2",
            processador=lambda refs: processar_transporte_simulado(refs, transporte=enviar),
        )
        assert segundo["adquirida"] is False
        assert estado["chamadas"] == 1
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes


def test_lease_expirado_nao_entrega_ao_transporte(app, monkeypatch):
    _habilitar(monkeypatch)
    estado = {"chamadas": 0}
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id = _preparar("integracao-lease@test.com")
        abertura = abrir_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            chave_analise="bi-integracao-lease",
        )
        tomada = adquirir(worker_id="worker-lease")
        assert tomada["id"] == abertura["execucao"]["id"]
        expirado = utcnow_naive() + timedelta(seconds=LEASE_PADRAO_SEGUNDOS + 1)
        assert recuperar_leases(agora=expirado) == 1
        assert db.session.get(AgenteBiExecucao, tomada["id"]).estado == "aguardando"
        assert estado["chamadas"] == 0
        resultado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-lease-2",
            chave_analise="bi-integracao-lease",
            transporte=_transporte_proposta(estado),
        )
        assert resultado["ciclo"]["execucao"]["estado"] == "concluida"
        assert estado["chamadas"] == 1
        assert LEASE_PADRAO_SEGUNDOS == 30


def test_outro_escopo_nao_abre_o_dataset(app):
    with app.app_context():
        _usuario, _franquia, _escopo, dataset_id = _preparar("integracao-a@test.com", financeiro=False)
        from tests.test_agente_bi_ingestao import _par

        _conta, _franquia_b, usuario_b = _par("conta-b", "integracao-b@test.com")
        with pytest.raises(IntegracaoErro) as exc:
            abrir_operacao(
                escopo_do_usuario(usuario_b),
                dataset_id,
                {"modalidades": ["A"]},
                chave_analise="bi-alheio",
            )
        assert exc.value.codigo == "nao_encontrado"


def test_contrato_aceita_documento_fechado_e_recusa_flag_promovido(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id = _preparar("integracao-contrato@test.com", financeiro=False)
        perfil = perfilar_dataset(escopo, dataset_id)
        pedido = {"modalidades": ["A"]}
        contexto = montar_contexto(perfil, pedido)
        saida = analisar(perfil, pedido)
        assert validar_resposta_simulada(contexto, saida)["contrato"] == "analisar.v1"
        promovida = dict(saida)
        promovida["apto_anthropic"] = True
        with pytest.raises(AnalisarErro) as exc:
            validar_resposta_simulada(contexto, promovida)
        assert exc.value.codigo == "autorizacao_indevida"


def test_analisar_v1_permanece_apos_nova_sessao(app, monkeypatch):
    _habilitar(monkeypatch)
    with app.app_context():
        usuario, _franquia, escopo, dataset_id = _preparar("integracao-duravel@test.com")
        dataset = db.session.get(AgenteBiDataset, dataset_id)
        versao = dataset.versao
        impressao = dataset.impressao
        usuario_id = usuario.id
        resultado = executar_operacao(
            escopo,
            dataset_id,
            _pedido(),
            worker_id="worker-duravel",
            chave_analise="bi-integracao-duravel",
            transporte=_transporte_proposta({}),
        )
        saida = resultado["saida"]
        execucao_id = resultado["ciclo"]["execucao"]["id"]
        analise_id = resultado["abertura"]["analise_id"]
        fencing = resultado["ciclo"]["execucao"]["fencing"]
        assert resultado["ciclo"]["execucao"]["estado"] == "concluida"
        assert saida["sugestoes"] or saida["descobertas"] or saida["perguntas"]
        db.session.remove()

        vista = obter_analise(escopo, analise_id)
        assert vista["resultado"] == saida
        assert vista["resultado"]["contrato"] == "analisar.v1"
        assert vista["contrato_versao"] == "analisar.v1"
        assert vista["usuario_id"] == usuario_id
        assert vista["dataset_id"] == dataset_id
        assert vista["versao"] == versao
        assert vista["impressao"] == impressao
        assert vista["resultado"]["trilha"]["dataset_id"] == dataset_id
        assert vista["resultado"]["trilha"]["dataset_versao"] == versao
        assert [item["codigo"] for item in listar_eventos(escopo, execucao_id)].count("concluida") == 1

        de_novo = confirmar(
            execucao_id,
            worker_id="worker-duravel",
            fencing=fencing,
            documento=saida,
        )
        assert de_novo["estado"] == "concluida"
        assert de_novo["codigo_resultado"] == "simulado"
        assert [item["codigo"] for item in listar_eventos(escopo, execucao_id)].count("concluida") == 1
        assert obter_analise(escopo, analise_id)["resultado"] == saida

        divergente_valido = json.loads(json.dumps(saida))
        divergente_valido["descobertas"] = []
        divergente_valido["sugestoes"] = []
        divergente_valido["perguntas"] = []
        divergente_valido["critica_referencia"] = []
        assert divergente_valido != saida
        with pytest.raises(ExecucaoErro) as preservado:
            confirmar(
                execucao_id,
                worker_id="worker-duravel",
                fencing=fencing,
                documento=divergente_valido,
            )
        assert preservado.value.codigo == "historico_preservado"
        assert [item["codigo"] for item in listar_eventos(escopo, execucao_id)].count("concluida") == 1

        alterado = json.loads(json.dumps(saida))
        alterado["modalidades"] = ["Z"]
        alterado["contrato_saida_validado"] = True
        with pytest.raises(ExecucaoErro) as divergente:
            confirmar(
                execucao_id,
                worker_id="worker-duravel",
                fencing=fencing,
                documento=alterado,
            )
        assert divergente.value.codigo == "contrato_saida_invalido"
        assert [item["codigo"] for item in listar_eventos(escopo, execucao_id)].count("concluida") == 1
        db.session.remove()
        preservada = obter_analise(escopo, analise_id)
        assert preservada["resultado"] == saida
        assert preservada["usuario_id"] == usuario_id
        assert preservada["dataset_id"] == dataset_id
        assert preservada["versao"] == versao


def test_fencing_invalido_nao_grava_analisar(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id = _preparar("integracao-fence@test.com", financeiro=False)
        perfil = perfilar_dataset(escopo, dataset_id)
        saida = analisar(perfil, {"modalidades": ["A"]})
        abertura = abrir_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            chave_analise="bi-integracao-fence",
        )
        tomada = adquirir(worker_id="worker-fence")
        with pytest.raises(ExecucaoErro) as exc:
            confirmar(
                tomada["id"],
                worker_id="worker-fence",
                fencing=tomada["fencing"] + 9,
                documento=saida,
            )
        assert exc.value.codigo == "fencing_recusado"
        assert db.session.get(AgenteBiAnalise, abertura["analise_id"]).resultado_json is None
        db.session.remove()
        assert db.session.get(AgenteBiAnalise, abertura["analise_id"]).resultado_json is None
        assert db.session.get(AgenteBiExecucao, tomada["id"]).estado == "adquirida"


def _copia_documento(documento):
    copia = json.loads(json.dumps(documento))
    copia["contrato_saida_validado"] = True
    return copia


def _documento_sugestoes_invalidas(documento):
    copia = _copia_documento(documento)
    copia["sugestoes"] = "INVALIDO"
    return copia


def _documento_trilha_forjada(documento):
    copia = _copia_documento(documento)
    copia["trilha"] = dict(copia["trilha"])
    copia["trilha"]["dataset_id"] = 1000
    copia["trilha"]["dataset_versao"] = 999
    copia["trilha"]["dataset_impressao"] = "forjada"
    return copia


def _documento_referencia_inexistente(documento):
    copia = _copia_documento(documento)
    for nome in ("sugestoes", "descobertas", "perguntas"):
        for item in copia[nome]:
            regiao = item.get("regiao")
            if isinstance(regiao, dict) and "regiao" in regiao:
                item["regiao"] = dict(regiao)
                item["regiao"]["regiao"] = 999
                return copia
    pytest.fail("documento legitimo sem referencia interna")


def _tomar(escopo, dataset_id, chave, worker_id, pedido):
    abertura = abrir_operacao(escopo, dataset_id, pedido, chave_analise=chave)
    tomada = adquirir(worker_id=worker_id)
    assert tomada["id"] == abertura["execucao"]["id"]
    assert tomada["estado"] == "adquirida"
    return abertura, tomada


def _recusar(escopo, tomada, analise_id, documento):
    execucao_id = tomada["id"]
    eventos = listar_eventos(escopo, execucao_id)
    estado = db.session.get(AgenteBiExecucao, execucao_id).estado
    resultado = db.session.get(AgenteBiAnalise, analise_id).resultado_json
    with pytest.raises(ExecucaoErro) as exc:
        confirmar(
            execucao_id,
            worker_id=tomada["lease_worker"],
            fencing=tomada["fencing"],
            documento=documento,
        )
    db.session.expire_all()
    linha = db.session.get(AgenteBiExecucao, execucao_id)
    analise = db.session.get(AgenteBiAnalise, analise_id)
    assert linha.estado == estado
    assert analise.resultado_json == resultado
    assert listar_eventos(escopo, execucao_id) == eventos
    assert analise.estado != "concluida" or resultado is not None
    return exc.value.codigo


def test_confirmacao_rejeita_documento_invalido_e_trilha_forjada(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id = _preparar("integracao-fronteira@test.com", financeiro=False)
        pedido = {"modalidades": ["A"]}
        saida = analisar(perfilar_dataset(escopo, dataset_id), pedido)
        assert saida["contrato"] == "analisar.v1"
        assert saida["contrato_saida_validado"] is True
        assert saida["trilha"]["dataset_id"] == dataset_id
        assert saida["trilha"]["dataset_impressao"] != "forjada"
        invalido = _documento_sugestoes_invalidas(saida)
        forjada = _documento_trilha_forjada(saida)

        abertura, tomada = _tomar(escopo, dataset_id, "bi-fronteira-invalido", "worker-fronteira", pedido)
        assert _recusar(escopo, tomada, abertura["analise_id"], invalido) == "contrato_saida_invalido"
        linha = db.session.get(AgenteBiExecucao, tomada["id"])
        assert linha.estado == "adquirida"
        assert db.session.get(AgenteBiAnalise, abertura["analise_id"]).resultado_json is None
        eventos = listar_eventos(escopo, tomada["id"])
        assert any(item["codigo"] == "adquirida" for item in eventos)
        assert all(item["codigo"] != "concluida" and item["estado_novo"] != "concluida" for item in eventos)

        gravada = confirmar(
            tomada["id"],
            worker_id="worker-fronteira",
            fencing=tomada["fencing"],
            documento=saida,
        )
        assert gravada["estado"] == "concluida"
        assert gravada["codigo_resultado"] == "simulado"
        db.session.remove()
        assert obter_analise(escopo, abertura["analise_id"])["resultado"] == saida
        repetida = confirmar(
            tomada["id"],
            worker_id="worker-fronteira",
            fencing=tomada["fencing"],
            documento=saida,
        )
        assert repetida["estado"] == "concluida"
        assert [item["codigo"] for item in listar_eventos(escopo, tomada["id"])].count("concluida") == 1
        assert _recusar(escopo, tomada, abertura["analise_id"], invalido) == "contrato_saida_invalido"
        assert _recusar(escopo, tomada, abertura["analise_id"], forjada) == "identidade_inconsistente"
        preservada = db.session.get(AgenteBiAnalise, abertura["analise_id"]).resultado_json
        assert json.loads(preservada) == saida
        assert "INVALIDO" not in preservada
        assert "forjada" not in preservada
        assert [item["codigo"] for item in listar_eventos(escopo, tomada["id"])].count("concluida") == 1

        abertura_trilha, tomada_trilha = _tomar(
            escopo,
            dataset_id,
            "bi-fronteira-trilha",
            "worker-fronteira",
            pedido,
        )
        assert _recusar(escopo, tomada_trilha, abertura_trilha["analise_id"], forjada) == "identidade_inconsistente"
        assert db.session.get(AgenteBiExecucao, tomada_trilha["id"]).estado == "adquirida"
        assert db.session.get(AgenteBiAnalise, abertura_trilha["analise_id"]).resultado_json is None
        eventos_trilha = listar_eventos(escopo, tomada_trilha["id"])
        assert any(item["codigo"] == "adquirida" for item in eventos_trilha)
        assert all(item["codigo"] != "concluida" and item["estado_novo"] != "concluida" for item in eventos_trilha)


def test_confirmacao_rejeita_referencia_interna_inconsistente(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id = _preparar("integracao-referencia@test.com", financeiro=False)
        pedido = {"modalidades": ["A"]}
        saida = analisar(perfilar_dataset(escopo, dataset_id), pedido)
        inconsistente = _documento_referencia_inexistente(saida)
        assert set(inconsistente) == set(saida)
        abertura, tomada = _tomar(escopo, dataset_id, "bi-fronteira-referencia", "worker-fronteira", pedido)
        assert _recusar(escopo, tomada, abertura["analise_id"], inconsistente) == "referencia_inexistente"
        assert db.session.get(AgenteBiExecucao, tomada["id"]).estado == "adquirida"
        assert db.session.get(AgenteBiAnalise, abertura["analise_id"]).resultado_json is None
        eventos = listar_eventos(escopo, tomada["id"])
        assert any(item["codigo"] == "adquirida" for item in eventos)
        assert all(item["codigo"] != "concluida" and item["estado_novo"] != "concluida" for item in eventos)


def _ids_de_dataset(valor, achados: list) -> None:
    if isinstance(valor, dict):
        if "dataset_id" in valor:
            achados.append(valor["dataset_id"])
        for item in valor.values():
            _ids_de_dataset(item, achados)
    elif isinstance(valor, list):
        for item in valor:
            _ids_de_dataset(item, achados)


def _transporte_contando(estado):
    def enviar(pedido):
        estado["chamadas"] = estado.get("chamadas", 0) + 1
        payload = json.loads(pedido["messages"][0]["content"])
        proposta = provedor_simulado(payload)
        return _resposta(json.dumps(proposta, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    return enviar


def test_executar_b_nao_processa_nem_devolve_a(app, monkeypatch):
    """A aberta antes de B não pode ser o resultado de executar B."""
    _habilitar(monkeypatch)

    def sdk_proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.montar_cliente_anthropic",
        sdk_proibido,
    )
    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service._transporte_sdk",
        sdk_proibido,
    )
    estado = {"chamadas": 0}
    with app.app_context():
        usuario_a, franquia_a, escopo_a, dataset_a = _preparar("iso-a@test.com")
        usuario_b, franquia_b, escopo_b, dataset_b = _preparar("iso-b@test.com")
        usuario_a_id = usuario_a.id
        assert usuario_a_id != usuario_b.id
        assert usuario_a.conta_id != usuario_b.conta_id
        assert usuario_a.franquia_id != usuario_b.franquia_id
        assert dataset_a != dataset_b
        antes_a = _foto(franquia_a.id)
        antes_b = _foto(franquia_b.id)
        pedido_a = {"modalidades": ["A"]}
        pedido_b = {"modalidades": ["C"]}
        abertura_a = abrir_operacao(escopo_a, dataset_a, pedido_a, chave_analise="bi-iso-a")
        abertura_b = abrir_operacao(escopo_b, dataset_b, pedido_b, chave_analise="bi-iso-b")
        id_a = abertura_a["execucao"]["id"]
        id_b = abertura_b["execucao"]["id"]
        assert id_a < id_b
        assert abertura_a["execucao"]["estado"] == "aguardando"
        assert abertura_b["execucao"]["estado"] == "aguardando"

        resultado = executar_operacao(
            escopo_b,
            dataset_b,
            pedido_b,
            worker_id="worker-b",
            chave_analise="bi-iso-b",
            transporte=_transporte_contando(estado),
        )
        saida = resultado["saida"]
        assert resultado["ciclo"]["adquirida"] is True
        assert resultado["ciclo"]["execucao"]["id"] == id_b
        assert resultado["ciclo"]["execucao"]["analise_id"] == abertura_b["analise_id"]
        assert resultado["ciclo"]["execucao"]["usuario_id"] == usuario_b.id
        assert resultado["ciclo"]["execucao"]["conta_id"] == usuario_b.conta_id
        assert resultado["ciclo"]["execucao"]["franquia_id"] == usuario_b.franquia_id
        assert resultado["abertura"]["analise_id"] == abertura_b["analise_id"]
        assert saida["contrato"] == "analisar.v1"
        assert saida["modalidades"] == ["C"]
        assert saida["chamada_anthropic"] is False
        assert saida["trilha"]["dataset_id"] == dataset_b
        _sem_vazamento(json.dumps(saida, ensure_ascii=False))
        referencias = []
        _ids_de_dataset(saida, referencias)
        assert dataset_a not in referencias
        assert dataset_b in referencias
        assert estado["chamadas"] == 1
        assert not hasattr(integracao, "saida_validada")
        assert not hasattr(integracao, "_saida_validada")
        fencing = resultado["ciclo"]["execucao"]["fencing"]
        assert fencing >= 1
        assert resultado["ciclo"]["execucao"]["lease_worker"] is None
        confirmada = confirmar(
            id_b,
            worker_id="worker-b",
            fencing=fencing,
            documento=saida,
        )
        assert confirmada["estado"] == "concluida"
        with pytest.raises(ExecucaoErro) as fence:
            confirmar(
                id_b,
                worker_id="worker-b",
                fencing=fencing + 9,
                documento=saida,
            )
        assert fence.value.codigo == "fencing_recusado"

        linha_a = db.session.get(AgenteBiExecucao, id_a)
        linha_b = db.session.get(AgenteBiExecucao, id_b)
        analise_a = db.session.get(AgenteBiAnalise, abertura_a["analise_id"])
        analise_b = db.session.get(AgenteBiAnalise, abertura_b["analise_id"])
        assert linha_a.estado == "aguardando"
        assert linha_a.tentativas == 0
        assert linha_a.fencing == 0
        assert linha_a.lease_worker is None
        assert linha_a.usuario_id == usuario_a.id
        assert linha_a.analise_id == abertura_a["analise_id"]
        assert linha_a.ia_chamada_tentativa_id is None
        assert analise_a.resultado_json is None
        assert analise_a.estado == "aguardando_execucao"
        assert linha_b.estado == "concluida"
        assert linha_b.codigo_resultado == "simulado"
        assert linha_b.fencing >= 1
        assert linha_b.usuario_id == usuario_b.id
        assert linha_b.analise_id == analise_b.id
        assert linha_b.analise_id != linha_a.analise_id
        assert linha_b.operacao_id != linha_a.operacao_id
        assert json.loads(analise_b.resultado_json) == saida
        assert analise_b.usuario_id == usuario_b.id
        assert analise_b.dataset_id == dataset_b
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia_a.id) == antes_a
        assert _foto(franquia_b.id) == antes_b

        repetida = executar_operacao(
            escopo_b,
            dataset_b,
            pedido_b,
            worker_id="worker-b",
            chave_analise="bi-iso-b",
            transporte=_transporte_contando(estado),
        )
        assert repetida["ciclo"]["adquirida"] is False
        assert repetida["saida"] == saida
        assert repetida["abertura"]["analise_id"] == abertura_b["analise_id"]
        assert estado["chamadas"] == 1
        assert db.session.get(AgenteBiExecucao, id_a).estado == "aguardando"

        db.session.remove()
        recuperada = executar_operacao(
            escopo_b,
            dataset_b,
            pedido_b,
            worker_id="worker-b-sessao",
            chave_analise="bi-iso-b",
            transporte=_transporte_contando(estado),
        )
        assert recuperada["ciclo"]["adquirida"] is False
        assert recuperada["saida"] == saida
        assert recuperada["saida"]["trilha"]["dataset_id"] == dataset_b
        assert estado["chamadas"] == 1
        assert obter_analise(escopo_b, abertura_b["analise_id"])["resultado"] == saida
        assert obter_analise(escopo_a, abertura_a["analise_id"])["resultado"] is None
        assert db.session.get(AgenteBiExecucao, id_a).estado == "aguardando"
        assert db.session.get(AgenteBiExecucao, id_a).usuario_id == usuario_a_id


def test_b_em_andamento_nao_consome_a(app, monkeypatch):
    _habilitar(monkeypatch)

    def sdk_proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.montar_cliente_anthropic",
        sdk_proibido,
    )
    estado = {"chamadas": 0}
    with app.app_context():
        usuario_a, franquia_a, escopo_a, dataset_a = _preparar("iso-hold-a@test.com")
        _usuario_b, franquia_b, escopo_b, dataset_b = _preparar("iso-hold-b@test.com")
        antes_a = _foto(franquia_a.id)
        antes_b = _foto(franquia_b.id)
        abertura_a = abrir_operacao(
            escopo_a,
            dataset_a,
            {"modalidades": ["A"]},
            chave_analise="bi-iso-hold-a",
        )
        abertura_b = abrir_operacao(
            escopo_b,
            dataset_b,
            {"modalidades": ["C"]},
            chave_analise="bi-iso-hold-b",
        )
        id_a = abertura_a["execucao"]["id"]
        id_b = abertura_b["execucao"]["id"]
        tomada = adquirir(worker_id="worker-outro", execucao_id=id_b)
        assert tomada["id"] == id_b
        assert tomada["fencing"] == 1
        assert tomada["lease_worker"] == "worker-outro"
        assert db.session.get(AgenteBiExecucao, id_b).lease_ate > utcnow_naive()
        assert adquirir(worker_id="worker-b", execucao_id=id_b) is None
        resultado = executar_operacao(
            escopo_b,
            dataset_b,
            {"modalidades": ["C"]},
            worker_id="worker-b",
            chave_analise="bi-iso-hold-b",
            transporte=_transporte_contando(estado),
        )
        assert resultado["ciclo"]["adquirida"] is False
        assert resultado["ciclo"]["em_andamento"] is True
        assert resultado["ciclo"]["execucao"]["id"] == id_b
        assert resultado["ciclo"]["execucao"]["estado"] == "adquirida"
        assert resultado["ciclo"]["execucao"]["lease_worker"] == "worker-outro"
        assert resultado["ciclo"]["execucao"]["usuario_id"] == escopo_b.usuario_id
        assert resultado["saida"] is None
        assert resultado["abertura"]["analise_id"] == abertura_b["analise_id"]
        assert estado["chamadas"] == 0
        linha_a = db.session.get(AgenteBiExecucao, id_a)
        linha_b = db.session.get(AgenteBiExecucao, id_b)
        assert linha_a.estado == "aguardando"
        assert linha_a.tentativas == 0
        assert linha_a.usuario_id == usuario_a.id
        assert linha_b.estado == "adquirida"
        assert linha_b.lease_worker == "worker-outro"
        assert linha_b.fencing == tomada["fencing"]
        assert db.session.get(AgenteBiAnalise, abertura_a["analise_id"]).resultado_json is None
        assert db.session.get(AgenteBiAnalise, abertura_b["analise_id"]).resultado_json is None
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia_a.id) == antes_a
        assert _foto(franquia_b.id) == antes_b


def test_saida_estruturada_respeita_modelo_e_sdk():
    from app.services.agente_bi_analisar_service import esquema_rascunho_analisar, instrucao_de_rascunho
    from app.services.agente_bi_anthropic_service import (
        modelo_aceita_saida_estruturada,
        preparar_saida_estruturada,
    )

    assert modelo_aceita_saida_estruturada("claude-sonnet-simulado") is False
    assert modelo_aceita_saida_estruturada("claude-sonnet-4-5") is True
    esquema = esquema_rascunho_analisar()
    assert preparar_saida_estruturada("claude-sonnet-simulado", esquema) is None
    instrucao = instrucao_de_rascunho()
    assert "descobertas" in instrucao
    assert "sugestoes" in instrucao
    assert "Nao invente" in instrucao
    assert SENTINELA not in instrucao
    config = preparar_saida_estruturada("claude-sonnet-4-5", esquema)
    if config is not None:
        assert config["format"]["type"] == "json_schema"
        assert set(config["format"]["schema"]["required"]) == {"descobertas", "sugestoes"}


def test_caminho_autorizado_persiste_proveniencia_sem_chamar_sdk(app, monkeypatch):
    from app.services.agente_bi_analisar_service import AnalisarErro, validar_saida
    from app.services.agente_bi_anthropic_service import TETO_MAX_TOKENS

    _habilitar(monkeypatch)
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-4-5")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def sdk_proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", sdk_proibido)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", sdk_proibido)
    estado = {"chamadas": 0}

    def enviar(pedido):
        estado["chamadas"] += 1
        estado["pedido"] = pedido
        payload = json.loads(pedido["messages"][0]["content"])
        proposta = provedor_simulado(payload)
        return {
            "texto": json.dumps(proposta, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "stop_reason": "end_turn",
            "input_tokens": 1000,
            "output_tokens": 1000,
            "recusa": False,
            "truncada": False,
        }

    with app.app_context():
        usuario, franquia, escopo, dataset_id = _preparar("integracao-real@test.com")
        _usuario_b, _franquia_b, escopo_b, dataset_b = _preparar("integracao-real-b@test.com")
        antes = _foto(franquia.id)
        abertura_b = abrir_operacao(
            escopo_b,
            dataset_b,
            {"modalidades": ["A"]},
            chave_analise="bi-real-b",
        )
        perfil = perfilar_dataset(escopo, dataset_id)
        assert perfil["governanca"]["apto_anthropic"] is False
        resultado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-real",
            chave_analise="bi-integracao-real",
            transporte=enviar,
            autorizado=True,
            lease_segundos=120,
        )
        saida = resultado["saida"]
        assert saida["contrato"] == "analisar.v1"
        assert saida["provedor"] == "anthropic"
        assert saida["chamada_anthropic"] is True
        assert saida["apto_anthropic"] is False
        assert perfilar_dataset(escopo, dataset_id)["governanca"]["apto_anthropic"] is False
        pedido = estado["pedido"]
        assert pedido["model"] == "claude-sonnet-4-5"
        assert pedido["max_tokens"] == TETO_MAX_TOKENS
        assert "descobertas" in pedido["system"]
        _sem_vazamento(pedido["system"])
        _sem_vazamento(pedido["messages"][0]["content"])
        _sem_vazamento(json.dumps(saida, ensure_ascii=False))
        execucao = db.session.get(AgenteBiExecucao, resultado["ciclo"]["execucao"]["id"])
        assert execucao.estado == "concluida"
        assert execucao.codigo_resultado == "anthropic"
        assert execucao.efeito_externo == "simulado"
        tentativa = db.session.get(IaChamadaTentativa, execucao.ia_chamada_tentativa_id)
        evento = db.session.get(IaConsumoEvento, tentativa.ia_consumo_evento_id)
        assert tentativa.provider == "anthropic"
        assert tentativa.model == "claude-sonnet-4-5"
        assert int(execucao.ia_chamada_tentativa_id) == int(tentativa.id)
        assert evento.provider == "anthropic"
        assert evento.model == "claude-sonnet-4-5"
        assert evento.usuario_id == usuario.id
        assert evento.franquia_id == franquia.id
        assert IaConsumoAbatimento.query.count() == 1
        cobrado = _foto(franquia.id)
        assert cobrado[0] == antes[0] + Decimal("2")
        assert cobrado[1] == antes[1]
        assert estado["chamadas"] == 1
        linha_b = db.session.get(AgenteBiExecucao, abertura_b["execucao"]["id"])
        assert linha_b.estado == "aguardando"
        assert linha_b.tentativas == 0
        assert linha_b.usuario_id == escopo_b.usuario_id
        repetida = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-real-2",
            chave_analise="bi-integracao-real",
            transporte=enviar,
            autorizado=True,
        )
        assert repetida["ciclo"]["adquirida"] is False
        assert estado["chamadas"] == 1
        assert IaConsumoAbatimento.query.count() == 1
        assert _foto(franquia.id) == cobrado
        incoerente = json.loads(json.dumps(saida))
        incoerente["chamada_anthropic"] = False
        with pytest.raises(AnalisarErro) as recusa:
            validar_saida(montar_contexto(perfil, {"modalidades": ["A"]}), incoerente)
        assert recusa.value.codigo == "contrato_saida_invalido"
        if saida["sugestoes"]:
            inventada = json.loads(json.dumps(saida))
            inventada["sugestoes"][0]["operacao"] = "inventada"
            with pytest.raises(AnalisarErro):
                validar_saida(montar_contexto(perfil, {"modalidades": ["A"]}), inventada)


def test_modelo_ausente_antes_da_chamada_segue_recuperavel(app, monkeypatch):
    _habilitar(monkeypatch)
    estado = {"chamadas": 0}
    with app.app_context():
        _usuario, franquia, escopo, dataset_id = _preparar("integracao-modelo@test.com")
        antes = _foto(franquia.id)
        recusado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-modelo",
            chave_analise="bi-integracao-modelo",
            transporte=_transporte_proposta(estado),
            autorizado=True,
        )
        assert recusado["ciclo"]["execucao"]["estado"] == "falha_recuperavel"
        assert recusado["ciclo"]["execucao"]["ultimo_erro"] == "modelo_ausente"
        assert recusado["saida"] is None
        assert estado["chamadas"] == 0
        assert IaChamadaTentativa.query.count() == 0
        assert _foto(franquia.id) == antes
        monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-4-5")
        recuperado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-modelo-2",
            chave_analise="bi-integracao-modelo",
            transporte=_transporte_proposta(estado),
            autorizado=True,
        )
        assert recuperado["saida"]["contrato"] == "analisar.v1"
        assert recuperado["saida"]["provedor"] == "anthropic"
        assert recuperado["saida"]["chamada_anthropic"] is True
        assert estado["chamadas"] == 1


def test_resposta_invalida_com_uso_nao_readquire_nem_repete_abatimento(app, monkeypatch):
    _habilitar(monkeypatch)
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-4-5")
    estado = {"chamadas": 0}

    def enviar(pedido):
        estado["chamadas"] += 1
        estado["pedido"] = pedido
        return {
            "texto": '{"ok":true}',
            "stop_reason": "end_turn",
            "input_tokens": 1000,
            "output_tokens": 1000,
            "recusa": False,
            "truncada": False,
        }

    with app.app_context():
        _usuario, franquia, escopo, dataset_id = _preparar("integracao-cobrada@test.com")
        antes = _foto(franquia.id)
        resultado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-cobrada",
            chave_analise="bi-integracao-cobrada",
            transporte=enviar,
            autorizado=True,
        )
        assert resultado["saida"] is None
        assert resultado["ciclo"]["execucao"]["estado"] == "falha_definitiva"
        assert resultado["ciclo"]["execucao"]["ultimo_erro"] == "contrato_saida_invalido"
        assert estado["chamadas"] == 1
        assert IaConsumoAbatimento.query.count() == 1
        cobrado = _foto(franquia.id)
        assert cobrado[0] == antes[0] + Decimal("2")
        linha = db.session.get(AgenteBiExecucao, resultado["ciclo"]["execucao"]["id"])
        tentativa = db.session.get(IaChamadaTentativa, linha.ia_chamada_tentativa_id)
        evento_id = tentativa.ia_consumo_evento_id
        assert tentativa.status == IaChamadaTentativa.STATUS_SETTLED
        for indice in range(2):
            ciclo = executar_um_ciclo(
                worker_id=f"worker-cobrada-{indice}",
                processador=lambda refs: processar_transporte_simulado(refs, transporte=enviar, autorizado=True),
            )
            assert ciclo["adquirida"] is False
        assert estado["chamadas"] == 1
        assert IaConsumoAbatimento.query.count() == 1
        assert db.session.get(IaConsumoEvento, evento_id).id == evento_id
        assert _foto(franquia.id) == cobrado
