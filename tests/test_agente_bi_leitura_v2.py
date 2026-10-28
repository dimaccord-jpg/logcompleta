"""SCRUM-269 etapa 3.1: leitura de definir.v2 e contexto interno recuperável.

Não executa o plano, não publica dashboard e não chama a Anthropic.
"""
from __future__ import annotations

import copy
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func

from app.extensions import db
from app.models import AgenteBiAnalise, AgenteBiDashboard, CleitonCostConfig, Conta, IaChamadaTentativa, utcnow_naive
from app.services.agente_bi_calculo_service import CalculoErro, calcular_definicao
from app.services.agente_bi_contrato_v2_service import CONTRATO_DEFINIR_V2, reconhecer_historico
from app.services.agente_bi_dashboard_service import montar_dashboard
from app.services.agente_bi_definir_service import CONTRATO_DEFINIR, definir, montar_contexto_definir, validar_saida
from app.services.agente_bi_interpretacao_v2_service import (
    _vinculos_parametro,
    InterpretacaoErro,
    interpretar_plano,
    recuperar_contexto,
)
from app.services.agente_bi_leitura_service import CONTRATO_CONTEXTO_DEFINICAO_V2, LeituraErro, ler_definicao
from app.services.agente_bi_persistencia_service import EscopoAgenteBi, criar_analise, criar_dashboard, obter_analise
from tests.conftest import seed_cleiton_cost_config, seed_sistema_interno
from tests.test_agente_bi_contrato_v2 import _conjunto
from tests.test_agente_bi_definir import _jornada
from tests.test_agente_bi_evolucao import _evoluir, _painel
from tests.test_agente_bi_interpretacao_v2 import _mensagem, _operacao, _plano, _resposta
from tests.test_agente_bi_calculo import _calcular, _gravar

ROOT = Path(__file__).resolve().parents[1]
FONTE_LEITURA = (ROOT / "app" / "services" / "agente_bi_leitura_service.py").read_text(encoding="utf-8")
FONTE_INTERPRETACAO = (ROOT / "app" / "services" / "agente_bi_interpretacao_v2_service.py").read_text(encoding="utf-8")
_CHAVES_INTERNAS = frozenset(
    {
        "dashboard_id",
        "revisao_base",
        "definicao_anterior_id",
        "identidade",
        "dataset_id",
        "dataset_versao",
        "dataset_impressao",
    }
)


@pytest.fixture(autouse=True)
def _sem_anthropic(monkeypatch):
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-simulado")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", proibido)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", proibido)


def _chaves(valor, saida=None):
    encontradas = set() if saida is None else saida
    if isinstance(valor, dict):
        encontradas.update(str(chave) for chave in valor)
        for item in valor.values():
            _chaves(item, encontradas)
    elif isinstance(valor, list):
        for item in valor:
            _chaves(item, encontradas)
    return encontradas


def _creditar(email, slug):
    franquia, escopo, painel = _painel(email, slug)
    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    if db.session.get(CleitonCostConfig, 1) is None:
        seed_cleiton_cost_config()
    franquia.limite_total = Decimal("100")
    franquia.consumo_acumulado = Decimal("0")
    franquia.reserva_pendente = Decimal("0")
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    return franquia, escopo, painel


def _persistir(escopo, base, *, contrato, resultado, contexto, etapa="definicao"):
    atual = (
        db.session.query(func.max(AgenteBiAnalise.rodada))
        .filter_by(
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            usuario_id=escopo.usuario_id,
            chave=base["chave"],
        )
        .scalar()
    )
    return criar_analise(
        escopo,
        {
            "chave": base["chave"],
            "rodada": int(atual) + 1,
            "dataset_id": base["dataset_id"],
            "versao": base["versao"],
            "impressao": base["impressao"],
            "etapa": etapa,
            "estado": "concluida",
            "contexto": contexto,
            "regras_negocio": {"contrato": contrato},
            "esclarecimentos": {},
            "contrato_versao": contrato,
            "resultado": resultado,
        },
    )


def _material(email, slug):
    _usuario, _franquia, escopo, perfil, pacote, _aprovada = _jornada(email, slug)
    documento = definir(perfil, pacote)
    salva = _gravar(escopo, pacote["id"], documento)
    calculo = _calcular(escopo, salva["id"])
    painel = montar_dashboard(escopo, {"analise_id": calculo["calculo"]["id"]})
    contexto = montar_contexto_definir(perfil, pacote)
    definicao, vinculo = _conjunto(
        documento["definicoes"],
        contexto,
        len(documento["definicoes"]),
        anterior=salva["id"],
        revisao=painel["dashboard"]["revisao"],
    )
    gravada = _persistir(
        escopo,
        salva,
        contrato=CONTRATO_DEFINIR_V2,
        resultado=definicao,
        contexto={
            "contrato": CONTRATO_CONTEXTO_DEFINICAO_V2,
            "vinculo_preparacao": perfil["vinculo_preparacao"],
            "vinculo": vinculo,
        },
    )
    return {
        "escopo": escopo,
        "perfil": perfil,
        "pacote": pacote,
        "documento": documento,
        "salva": salva,
        "painel": painel,
        "definicao": definicao,
        "vinculo": vinculo,
        "gravada": gravada,
        "contexto": contexto,
    }


def _plano_inserido(escopo, painel, texto, *, alterar=None, resultado=None):
    from app.services.agente_bi_contrato_v2_service import CONTRATO_PATCH_V2
    from app.services.agente_bi_interpretacao_v2_service import _carregar, _contexto_interno, _identidade

    carregado = _carregar(escopo, painel["dashboard"]["id"])
    identidade = _identidade(escopo, carregado, texto)
    interno = _contexto_interno(escopo, carregado, identidade)
    if alterar is not None:
        alterar(interno)
    perfil = carregado.get("perfil") if isinstance(carregado.get("perfil"), dict) else {}
    corpo = {
        "chave": interno["identidade"] if alterar is None else identidade,
        "rodada": 1,
        "dataset_id": int(carregado["dataset"]["id"]),
        "versao": int(carregado["dataset"]["versao"]),
        "impressao": carregado["dataset"]["impressao"],
        "etapa": "definicao",
        "estado": "concluida",
        "contexto": {
            "contrato": "contexto.plano.v2",
            "vinculo_preparacao": perfil.get("vinculo_preparacao"),
            "interno": interno,
        },
        "regras_negocio": {
            "contrato": CONTRATO_PATCH_V2,
            "vinculo": {"graficos": list(_contexto_interno(escopo, carregado, identidade)["graficos"]), "colunas": []},
            "parametros": [
                {"referencia": item["referencia"], "tipo": item["tipo"]}
                for item in _contexto_interno(escopo, carregado, identidade)["parametros"]
            ],
            "associacoes": [],
            "texto": texto,
        },
        "esclarecimentos": {},
        "contrato_versao": CONTRATO_PATCH_V2,
        "resultado": resultado
        if resultado is not None
        else {"contrato": CONTRATO_PATCH_V2, "versao": 2, "situacao": "proposto", "operacoes": []},
        "chave_recuperacao": interno["identidade"],
    }
    if alterar is not None:
        corpo["chave"] = interno["identidade"]
        corpo["chave_recuperacao"] = interno["identidade"]
    return criar_analise(escopo, corpo)


def test_leitura_v1_sem_regressao(ctx):
    material = _material("leitura.v1@test.com", "leitura-v1")
    escopo = material["escopo"]
    documento = material["documento"]
    antes = AgenteBiDashboard.query.count()
    calculos = AgenteBiAnalise.query.filter_by(contrato_versao="calculo.v1").count()
    leitura = ler_definicao(escopo, material["salva"]["id"])
    assert leitura["contrato"] == CONTRATO_DEFINIR
    assert leitura["analise_id"] == material["salva"]["id"]
    assert leitura["definicoes"] == documento["definicoes"]
    assert leitura["dataset"] == {
        "id": material["salva"]["dataset_id"],
        "versao": material["salva"]["versao"],
        "impressao": material["salva"]["impressao"],
    }
    assert leitura["dataset"]["id"] == documento["dataset_id"]
    assert leitura["vinculo_preparacao"] == documento["vinculo_preparacao"]
    assert validar_saida(material["contexto"], documento)["contrato"] == CONTRATO_DEFINIR
    assert reconhecer_historico({"contrato": CONTRATO_DEFINIR, "dataset_id": 1})["contrato"] == CONTRATO_DEFINIR
    assert AgenteBiDashboard.query.count() == antes
    assert AgenteBiAnalise.query.filter_by(contrato_versao="calculo.v1").count() == calculos

    adulterado = copy.deepcopy(documento)
    adulterado["dataset_id"] = int(documento["dataset_id"]) + 1
    copia = _persistir(
        escopo,
        material["salva"],
        contrato=CONTRATO_DEFINIR,
        resultado=adulterado,
        contexto={"contrato": "contexto.definir.v1", "definicao": True},
    )
    with pytest.raises(LeituraErro) as erro:
        ler_definicao(escopo, copia["id"])
    assert erro.value.codigo == "identidade_invalida"


def test_leitura_v2_autorizada_identidade_e_vinculo(ctx):
    material = _material("leitura.v2@test.com", "leitura-v2")
    escopo = material["escopo"]
    definicao = material["definicao"]
    assert frozenset(
        {
            "usuario_id",
            "conta_id",
            "franquia_id",
            "dashboard_id",
            "dataset_id",
            "dataset_versao",
            "dataset_impressao",
            "apto_anthropic",
            "contrato_saida_validado",
        }
    ).isdisjoint(definicao)
    assert "vinculo_preparacao" not in definicao
    antes = AgenteBiDashboard.query.count()
    calculos = AgenteBiAnalise.query.filter_by(contrato_versao="calculo.v1").count()
    tentativas = IaChamadaTentativa.query.count()

    leitura = ler_definicao(escopo, material["gravada"]["id"])
    assert leitura["contrato"] == CONTRATO_DEFINIR_V2
    assert [item["id"] for item in leitura["definicoes"]] == [item["id"] for item in definicao["definicoes"]]
    assert leitura["definicao_anterior_id"] == material["salva"]["id"]
    assert leitura["revisao_base"] == material["painel"]["dashboard"]["revisao"]
    assert leitura["origem_contrato"] == CONTRATO_DEFINIR
    assert leitura["dataset"] == {
        "id": material["gravada"]["dataset_id"],
        "versao": material["gravada"]["versao"],
        "impressao": material["gravada"]["impressao"],
    }
    assert leitura["vinculo_preparacao"] == material["perfil"]["vinculo_preparacao"]
    assert "dataset_id" not in leitura["definicoes"][0]
    assert AgenteBiDashboard.query.count() == antes
    assert AgenteBiAnalise.query.filter_by(contrato_versao="calculo.v1").count() == calculos
    assert IaChamadaTentativa.query.count() == tentativas

    substituida = copy.deepcopy(definicao)
    substituida["dataset_id"] = int(leitura["dataset"]["id"]) + 9
    copia_identidade = _persistir(
        escopo,
        material["salva"],
        contrato=CONTRATO_DEFINIR_V2,
        resultado=substituida,
        contexto={
            "contrato": CONTRATO_CONTEXTO_DEFINICAO_V2,
            "vinculo_preparacao": material["perfil"]["vinculo_preparacao"],
            "vinculo": material["vinculo"],
        },
    )
    with pytest.raises(LeituraErro) as identidade:
        ler_definicao(escopo, copia_identidade["id"])
    assert identidade.value.codigo == "autorizacao_indevida"

    vinculo_identidade = copy.deepcopy(material["vinculo"])
    vinculo_identidade["dataset_id"] = leitura["dataset"]["id"]
    copia_vinculo = _persistir(
        escopo,
        material["salva"],
        contrato=CONTRATO_DEFINIR_V2,
        resultado=definicao,
        contexto={
            "contrato": CONTRATO_CONTEXTO_DEFINICAO_V2,
            "vinculo_preparacao": material["perfil"]["vinculo_preparacao"],
            "vinculo": vinculo_identidade,
        },
    )
    with pytest.raises(LeituraErro) as vinculo_modelo:
        ler_definicao(escopo, copia_vinculo["id"])
    assert vinculo_modelo.value.codigo == "autorizacao_indevida"

    contexto_trocado = copy.deepcopy(material["contexto"])
    contexto_trocado["dataset"] = dict(contexto_trocado["dataset"])
    contexto_trocado["dataset"]["id"] = int(leitura["dataset"]["id"]) + 3
    vinculo_trocado = copy.deepcopy(material["vinculo"])
    vinculo_trocado["contexto"] = contexto_trocado
    copia_embutida = _persistir(
        escopo,
        material["salva"],
        contrato=CONTRATO_DEFINIR_V2,
        resultado=definicao,
        contexto={
            "contrato": CONTRATO_CONTEXTO_DEFINICAO_V2,
            "vinculo_preparacao": material["perfil"]["vinculo_preparacao"],
            "vinculo": vinculo_trocado,
        },
    )
    with pytest.raises(LeituraErro) as embutido:
        ler_definicao(escopo, copia_embutida["id"])
    assert embutido.value.codigo == "identidade_inconsistente"
    assert AgenteBiDashboard.query.count() == antes


def test_rejeicao_de_contrato_desconhecido_e_vinculo(ctx):
    material = _material("leitura.rejeita@test.com", "leitura-rejeita")
    escopo = material["escopo"]
    desconhecido = _persistir(
        escopo,
        material["salva"],
        contrato="definir.v9",
        resultado={"contrato": "definir.v9"},
        contexto={"contrato": "contexto.definir.v9"},
    )
    with pytest.raises(LeituraErro) as contrato:
        ler_definicao(escopo, desconhecido["id"])
    assert contrato.value.codigo == "contrato_saida_invalido"

    divergente = copy.deepcopy(material["definicao"])
    divergente["revisao_base"] = 9
    divergente["proveniencia"] = dict(divergente["proveniencia"])
    divergente["proveniencia"]["revisao_base"] = 9
    copia_proveniencia = _persistir(
        escopo,
        material["salva"],
        contrato=CONTRATO_DEFINIR_V2,
        resultado=divergente,
        contexto={
            "contrato": CONTRATO_CONTEXTO_DEFINICAO_V2,
            "vinculo_preparacao": material["perfil"]["vinculo_preparacao"],
            "vinculo": material["vinculo"],
        },
    )
    with pytest.raises(LeituraErro) as proveniencia:
        ler_definicao(escopo, copia_proveniencia["id"])
    assert proveniencia.value.codigo == "proveniencia_inconsistente"

    _outro, outro_escopo, outro_painel = _painel("leitura.outro@test.com", "leitura-outro")
    outro = obter_analise(outro_escopo, outro_painel["consulta"]["definicao_id"])
    documento = copy.deepcopy(material["definicao"])
    vinculo = copy.deepcopy(material["vinculo"])
    documento["definicao_anterior_id"] = outro["id"]
    documento["proveniencia"] = dict(documento["proveniencia"])
    documento["proveniencia"]["definicao_anterior_id"] = outro["id"]
    vinculo["definicao_anterior_id"] = outro["id"]
    copia_alheia = _persistir(
        escopo,
        material["salva"],
        contrato=CONTRATO_DEFINIR_V2,
        resultado=documento,
        contexto={
            "contrato": CONTRATO_CONTEXTO_DEFINICAO_V2,
            "vinculo_preparacao": material["perfil"]["vinculo_preparacao"],
            "vinculo": vinculo,
        },
    )
    with pytest.raises(LeituraErro) as alheio:
        ler_definicao(escopo, copia_alheia["id"])
    assert alheio.value.codigo == "proveniencia_inconsistente"

    documento = copy.deepcopy(material["definicao"])
    vinculo = copy.deepcopy(material["vinculo"])
    documento["revisao_base"] = int(outro_painel["dashboard"]["revisao"]) + 1
    documento["proveniencia"] = dict(documento["proveniencia"])
    documento["proveniencia"]["revisao_base"] = documento["revisao_base"]
    vinculo["revisao_base"] = documento["revisao_base"]
    copia_revisao = _persistir(
        escopo,
        material["salva"],
        contrato=CONTRATO_DEFINIR_V2,
        resultado=documento,
        contexto={
            "contrato": CONTRATO_CONTEXTO_DEFINICAO_V2,
            "vinculo_preparacao": material["perfil"]["vinculo_preparacao"],
            "vinculo": vinculo,
        },
    )
    with pytest.raises(LeituraErro) as revisao:
        ler_definicao(escopo, copia_revisao["id"])
    assert revisao.value.codigo == "proveniencia_inconsistente"


def test_contexto_recuperado_apos_nova_sessao(ctx):
    _franquia, escopo, painel = _creditar("leitura.sessao@test.com", "leitura-sessao")
    identificador = painel["dashboard"]["id"]
    definicao_id = painel["consulta"]["definicao_id"]
    vistos = {}

    def enviar(pedido):
        vistos["mensagem"] = _mensagem(pedido)
        return _resposta(_plano([_operacao()]))

    saida = interpretar_plano(escopo, identificador, {"texto": "consulte o gráfico existente"}, transporte=enviar)
    assert _CHAVES_INTERNAS.isdisjoint(_chaves(vistos["mensagem"]))
    assert "interno" not in vistos["mensagem"]
    analise = db.session.get(AgenteBiAnalise, saida["analise_id"])
    assert "consulte o gráfico existente" not in (analise.contexto_json or "")
    db.session.expire_all()
    novo = EscopoAgenteBi(conta_id=escopo.conta_id, franquia_id=escopo.franquia_id, usuario_id=escopo.usuario_id)
    recuperado = recuperar_contexto(novo, saida["analise_id"])
    assert novo is not escopo
    assert recuperado["dashboard_id"] == identificador
    assert recuperado["revisao_base"] == painel["dashboard"]["revisao"]
    assert recuperado["definicao_anterior_id"] == definicao_id
    assert recuperado["dataset"]["id"] == painel["consulta"]["dataset_id"]
    assert recuperado["dataset"]["versao"] == painel["consulta"]["versao"]
    assert recuperado["dataset"]["impressao"] == painel["consulta"]["impressao"]
    assert recuperado["graficos"]
    assert all("valor" not in item for item in recuperado["parametros"])
    linha = db.session.get(AgenteBiAnalise, saida["analise_id"])
    assert recuperado["identidade"] == linha.chave_recuperacao
    assert recuperado["identidade"].startswith("plano.")


def test_rejeicao_de_escopo_e_identidade_canonica(ctx):
    _franquia, escopo, painel = _creditar("leitura.escopo@test.com", "leitura-escopo")
    _outra, _escopo_alheio, painel_alheio = _creditar("leitura.escopo2@test.com", "leitura-escopo-2")
    evolvido = _evoluir(_escopo_alheio, painel_alheio, "filtre beta igual a sul")

    def enviar(_pedido):
        return _resposta(_plano([_operacao()]))

    saida = interpretar_plano(escopo, painel["dashboard"]["id"], {"texto": "consulte o gráfico sem trocar o painel"}, transporte=enviar)
    analise = db.session.get(AgenteBiAnalise, saida["analise_id"])
    canonica = analise.chave_recuperacao
    recuperado = recuperar_contexto(escopo, analise.id)
    assert recuperado["identidade"] == canonica
    resultado = json.loads(analise.resultado_json)
    assert resultado.get("identidade") is None

    def apontar_outro(interno):
        interno["dashboard_id"] = evolvido["dashboard"]["id"]

    alheio = _plano_inserido(escopo, painel, "consulta de outro dashboard", alterar=apontar_outro)
    with pytest.raises(InterpretacaoErro) as dashboard:
        recuperar_contexto(escopo, alheio["id"])
    assert dashboard.value.codigo == "identidade_inconsistente"

    def apontar_revisao(interno):
        interno["revisao_base"] = evolvido["dashboard"]["revisao"]

    outra_revisao = _plano_inserido(escopo, painel, "consulta de outra revisao", alterar=apontar_revisao)
    with pytest.raises(InterpretacaoErro) as revisao:
        recuperar_contexto(escopo, outra_revisao["id"])
    assert revisao.value.codigo == "identidade_inconsistente"

    falsa = "plano." + ("ab" * 32)

    def trocar_identidade(interno):
        interno["identidade"] = falsa

    substituida = _plano_inserido(
        escopo,
        painel,
        "consulta com identidade substituida",
        alterar=trocar_identidade,
        resultado={"contrato": "patch.definir.v2", "versao": 2, "identidade": falsa, "operacoes": []},
    )
    with pytest.raises(InterpretacaoErro) as trocada:
        recuperar_contexto(escopo, substituida["id"])
    assert trocada.value.codigo == "identidade_inconsistente"
    assert db.session.get(AgenteBiAnalise, analise.id).chave_recuperacao == canonica
    assert db.session.get(AgenteBiAnalise, substituida["id"]).chave_recuperacao == falsa
    assert falsa != canonica

    def manter(interno):
        return interno

    preservada = _plano_inserido(
        escopo,
        painel,
        "consulta cuja identidade permanece a do servidor",
        resultado={"contrato": "patch.definir.v2", "versao": 2, "identidade": falsa, "operacoes": []},
    )
    lida = recuperar_contexto(escopo, preservada["id"])
    assert lida["identidade"] != falsa
    assert lida["identidade"] == db.session.get(AgenteBiAnalise, preservada["id"]).chave_recuperacao


def test_parametro_opaco_nao_e_inventado_e_rejeita_incompativel(ctx):
    assert _vinculos_parametro(
        [
            {
                "id": "g1",
                "parametros": [
                    {"nome": "limite", "tipo": "numero", "obrigatorio": True},
                    {"nome": "sem_tipo"},
                    {"tipo": "texto"},
                ],
            }
        ]
    ) == [{"referencia": "p1", "grafico": "g1", "nome": "limite", "tipo": "numero"}]
    _franquia, escopo, painel = _creditar("leitura.param@test.com", "leitura-param")
    vistos = {}

    def enviar(pedido):
        vistos["mensagem"] = _mensagem(pedido)
        return _resposta(_plano([_operacao()]))

    sem_parametro = interpretar_plano(
        escopo,
        painel["dashboard"]["id"],
        {"texto": "use o parametro secreto 42 e o valor sul"},
        transporte=enviar,
    )
    assert vistos["mensagem"]["parametros_opacos"] == []
    assert recuperar_contexto(escopo, sem_parametro["analise_id"])["parametros"] == []
    assert "42" not in json.dumps(recuperar_contexto(escopo, sem_parametro["analise_id"])["parametros"])

    filtrado = _evoluir(escopo, painel, "filtre beta igual a sul")
    definicao = obter_analise(escopo, filtrado["consulta"]["definicao_id"])
    declarados = [
        item
        for grafico in definicao["resultado"]["definicoes"]
        if isinstance(grafico, dict)
        for item in grafico.get("parametros") or []
        if isinstance(item, dict)
    ]
    assert declarados
    vistos.clear()

    def enviar_filtrado(pedido):
        vistos["mensagem"] = _mensagem(pedido)
        return _resposta(_plano([_operacao()]))

    com_parametro = interpretar_plano(
        escopo,
        filtrado["dashboard"]["id"],
        {"texto": "consulte o gráfico já filtrado"},
        transporte=enviar_filtrado,
    )
    recuperado = recuperar_contexto(escopo, com_parametro["analise_id"])
    assert recuperado["parametros"]
    assert {item["nome"] for item in recuperado["parametros"]} == {item["nome"] for item in declarados}
    assert {item["tipo"] for item in recuperado["parametros"]} == {item["tipo"] for item in declarados}
    assert all(set(item) == {"referencia", "grafico", "nome", "tipo"} for item in recuperado["parametros"])
    assert all(set(item) == {"referencia", "tipo"} for item in vistos["mensagem"]["parametros_opacos"])
    assert "sul" not in json.dumps(recuperado["parametros"], ensure_ascii=False)
    assert _CHAVES_INTERNAS.isdisjoint(_chaves(vistos["mensagem"]))

    def referencia_inexistente(interno):
        interno["parametros"] = [dict(item) for item in interno["parametros"]]
        interno["parametros"][0]["referencia"] = "p9"

    inexistente_plano = _plano_inserido(
        escopo,
        filtrado,
        "consulta com referencia opaca inexistente",
        alterar=referencia_inexistente,
    )
    with pytest.raises(InterpretacaoErro) as inexistente:
        recuperar_contexto(escopo, inexistente_plano["id"])
    assert inexistente.value.codigo == "referencia_inexistente"

    atual = next(item["tipo"] for item in declarados)

    def tipo_incompativel(interno):
        interno["parametros"] = [dict(item) for item in interno["parametros"]]
        interno["parametros"][0]["tipo"] = "data" if atual != "data" else "numero"

    incompativel_plano = _plano_inserido(
        escopo,
        filtrado,
        "consulta com tipo de parametro incompativel",
        alterar=tipo_incompativel,
    )
    with pytest.raises(InterpretacaoErro) as incompativel:
        recuperar_contexto(escopo, incompativel_plano["id"])
    assert incompativel.value.codigo == "parametro_inconsistente"


def test_preparacao_nao_publica_nem_calcula(ctx, monkeypatch):
    material = _material("leitura.prep@test.com", "leitura-prep")
    escopo = material["escopo"]
    dashboards = AgenteBiDashboard.query.count()
    calculos = AgenteBiAnalise.query.filter_by(contrato_versao="calculo.v1").count()
    tentativas = IaChamadaTentativa.query.count()

    def calculo_proibido(*_args, **_kwargs):
        raise AssertionError("calculo")

    def painel_proibido(*_args, **_kwargs):
        raise AssertionError("publicacao")

    monkeypatch.setattr("app.services.agente_bi_calculo_service._calcular_um", calculo_proibido)
    monkeypatch.setattr("app.services.agente_bi_dashboard_service.montar_dashboard", painel_proibido)
    monkeypatch.setattr("app.services.agente_bi_dashboard_service.criar_dashboard", painel_proibido)
    leitura = ler_definicao(escopo, material["gravada"]["id"])
    assert leitura["contrato"] == CONTRATO_DEFINIR_V2
    with pytest.raises(CalculoErro) as parado:
        calcular_definicao(escopo, {"analise_id": material["gravada"]["id"]})
    assert parado.value.codigo == "analise_indisponivel"
    assert AgenteBiDashboard.query.count() == dashboards
    assert AgenteBiAnalise.query.filter_by(contrato_versao="calculo.v1").count() == calculos
    assert IaChamadaTentativa.query.count() == tentativas
    assert "calcular_definicao" not in FONTE_LEITURA
    assert "montar_dashboard" not in FONTE_LEITURA
    assert "chamar_anthropic" not in FONTE_LEITURA
    assert "import re" not in FONTE_LEITURA
    assert "import re" not in FONTE_INTERPRETACAO
    assert "_calcular_um" not in FONTE_INTERPRETACAO


def _apontar(material, *, anterior, revisao=None, origem=None):
    definicao = copy.deepcopy(material["definicao"])
    vinculo = copy.deepcopy(material["vinculo"])
    definicao["definicao_anterior_id"] = anterior
    definicao["proveniencia"]["definicao_anterior_id"] = anterior
    vinculo["definicao_anterior_id"] = anterior
    if revisao is not None:
        definicao["revisao_base"] = revisao
        definicao["proveniencia"]["revisao_base"] = revisao
        vinculo["revisao_base"] = revisao
    if origem is not None:
        definicao["origem_contrato"] = origem
        definicao["proveniencia"]["origem_contrato"] = origem
        vinculo["origem_contrato"] = origem
    return definicao, vinculo


def _gravar_v2(material, definicao, vinculo):
    return _persistir(
        material["escopo"],
        material["salva"],
        contrato=CONTRATO_DEFINIR_V2,
        resultado=definicao,
        contexto={
            "contrato": CONTRATO_CONTEXTO_DEFINICAO_V2,
            "vinculo_preparacao": material["perfil"]["vinculo_preparacao"],
            "vinculo": vinculo,
        },
    )


def _trocar_indice(valor, antigo, novo):
    if isinstance(valor, dict):
        if {"arquivo", "aba", "regiao", "indice"} <= set(valor) and valor.get("indice") == antigo:
            valor["indice"] = novo
        for item in valor.values():
            _trocar_indice(item, antigo, novo)
    elif isinstance(valor, list):
        for item in valor:
            _trocar_indice(item, antigo, novo)


def _calculo_de(material, definicao_id, chave):
    base = material["salva"]
    return criar_analise(
        material["escopo"],
        {
            "chave": chave,
            "rodada": 1,
            "dataset_id": base["dataset_id"],
            "versao": base["versao"],
            "impressao": base["impressao"],
            "etapa": "execucao",
            "estado": "concluida",
            "contexto": {"contrato": "contexto.calculo.v1"},
            "regras_negocio": {"executor": "executor.deterministico.v1"},
            "esclarecimentos": {},
            "contrato_versao": "calculo.v1",
            "resultado": {
                "contrato": "calculo.v1",
                "origem_definicao_id": definicao_id,
                "origem_contrato": CONTRATO_DEFINIR,
                "dataset_id": base["dataset_id"],
                "dataset_versao": base["versao"],
                "dataset_impressao": base["impressao"],
            },
        },
    )


def _dashboard_de(material, calculo, chave):
    origem = material["painel"]["dashboard"]
    return criar_dashboard(
        material["escopo"],
        {
            "chave": chave,
            "revisao": 1,
            "estado_publicacao": origem["estado_publicacao"],
            "contrato_versao": origem["contrato_versao"],
            "executor_versao": origem["executor_versao"],
            "definicoes_graficos": origem["definicoes_graficos"],
            "datasets": origem["datasets"],
            "analise_id": calculo["id"],
        },
    )


def test_referencias_tecnicas_do_perfil_real(ctx):
    material = _material("leitura.perfil@test.com", "leitura-perfil")
    escopo = material["escopo"]
    aceita = ler_definicao(escopo, material["gravada"]["id"])
    assert aceita["contrato"] == CONTRATO_DEFINIR_V2
    assert aceita["definicao_anterior_id"] == material["salva"]["id"]

    definicao = copy.deepcopy(material["definicao"])
    vinculo = copy.deepcopy(material["vinculo"])
    antigo = vinculo["contexto"]["colunas"][0]["indice"]
    _trocar_indice(definicao, antigo, antigo + 1000)
    _trocar_indice(vinculo, antigo, antigo + 1000)
    inexistente = _gravar_v2(material, definicao, vinculo)
    with pytest.raises(LeituraErro) as indice:
        ler_definicao(escopo, inexistente["id"])
    assert indice.value.codigo == "identidade_inconsistente"

    definicao_tipo = copy.deepcopy(material["definicao"])
    vinculo_tipo = copy.deepcopy(material["vinculo"])
    coluna = vinculo_tipo["contexto"]["colunas"][0]
    coluna["tipos"] = list(coluna.get("tipos") or []) + ["tipo_inexistente"]
    adulterado = _gravar_v2(material, definicao_tipo, vinculo_tipo)
    with pytest.raises(LeituraErro) as tipo:
        ler_definicao(escopo, adulterado["id"])
    assert tipo.value.codigo == "identidade_inconsistente"


def test_proveniencia_exige_definicao_anterior_da_revisao(ctx):
    material = _material("leitura.origem@test.com", "leitura-origem")
    escopo = material["escopo"]
    leitura_v1 = ler_definicao(escopo, material["salva"]["id"])
    assert leitura_v1["contrato"] == CONTRATO_DEFINIR
    assert leitura_v1["definicoes"] == material["documento"]["definicoes"]

    vazia_doc = copy.deepcopy(material["documento"])
    vazia_doc["definicoes"] = []
    vazia = _persistir(
        escopo,
        material["salva"],
        contrato=CONTRATO_DEFINIR,
        resultado=vazia_doc,
        contexto={"contrato": "contexto.definir.v1"},
    )
    calculo_vazio = _calculo_de(material, vazia["id"], "cadeia-vazia")
    painel_vazio = _dashboard_de(material, calculo_vazio, "cadeia-vazia")
    definicao_vazia, vinculo_vazio = _apontar(material, anterior=vazia["id"], revisao=painel_vazio["revisao"])
    gravada_vazia = _gravar_v2(material, definicao_vazia, vinculo_vazio)
    with pytest.raises(LeituraErro) as vazia_erro:
        ler_definicao(escopo, gravada_vazia["id"])
    assert vazia_erro.value.codigo == "proveniencia_inconsistente"

    outra = _persistir(
        escopo,
        material["salva"],
        contrato=CONTRATO_DEFINIR,
        resultado=material["documento"],
        contexto={"contrato": "contexto.definir.v1"},
    )
    assert ler_definicao(escopo, outra["id"])["contrato"] == CONTRATO_DEFINIR
    definicao_outra, vinculo_outro = _apontar(material, anterior=outra["id"])
    gravada_outra = _gravar_v2(material, definicao_outra, vinculo_outro)
    with pytest.raises(LeituraErro) as outra_cadeia:
        ler_definicao(escopo, gravada_outra["id"])
    assert outra_cadeia.value.codigo == "proveniencia_inconsistente"

    definicao_grafico, vinculo_grafico = _apontar(material, anterior=material["salva"]["id"])
    origem_atual = definicao_grafico["definicoes"][0]["origem"]
    origem_trocada = "referencia" if origem_atual != "referencia" else "dados"
    definicao_grafico["definicoes"][0]["origem"] = origem_trocada
    vinculo_grafico["definicoes"][0]["origem"] = origem_trocada
    gravada_grafico = _gravar_v2(material, definicao_grafico, vinculo_grafico)
    with pytest.raises(LeituraErro) as graficos:
        ler_definicao(escopo, gravada_grafico["id"])
    assert graficos.value.codigo == "proveniencia_inconsistente"

    calculo_v2 = _calculo_de(material, material["gravada"]["id"], "cadeia-v2")
    painel_v2 = _dashboard_de(material, calculo_v2, "cadeia-v2")
    definicao_v2, vinculo_v2 = _apontar(
        material,
        anterior=material["gravada"]["id"],
        revisao=painel_v2["revisao"],
        origem=CONTRATO_DEFINIR_V2,
    )
    gravada_v2 = _gravar_v2(material, definicao_v2, vinculo_v2)
    leitura_v2 = ler_definicao(escopo, gravada_v2["id"])
    assert leitura_v2["origem_contrato"] == CONTRATO_DEFINIR_V2
    assert leitura_v2["definicao_anterior_id"] == material["gravada"]["id"]


def _gravar_v1(material, documento):
    return _persistir(
        material["escopo"],
        material["salva"],
        contrato=CONTRATO_DEFINIR,
        resultado=documento,
        contexto={"contrato": "contexto.definir.v1"},
    )


def _sucessoes(material, quantidade):
    anterior_id = material["salva"]["id"]
    origem = CONTRATO_DEFINIR
    ultima = None
    for indice in range(1, quantidade + 1):
        chave = f"evo-{quantidade}-{indice}"
        calculo = _calculo_de(material, anterior_id, f"calc-{chave}")
        painel = _dashboard_de(material, calculo, chave)
        definicao, vinculo = _apontar(material, anterior=anterior_id, revisao=painel["revisao"], origem=origem)
        ultima = _gravar_v2(material, definicao, vinculo)
        anterior_id = ultima["id"]
        origem = CONTRATO_DEFINIR_V2
    return ultima


def _ids_previstos(quantidade):
    atual = db.session.query(func.max(AgenteBiAnalise.id)).scalar() or 0
    return [int(atual) + deslocamento for deslocamento in range(1, quantidade + 1)]


def _elo(material, anterior_id):
    return _persistir(
        material["escopo"],
        material["salva"],
        contrato=CONTRATO_DEFINIR_V2,
        resultado={"contrato": CONTRATO_DEFINIR_V2, "definicao_anterior_id": anterior_id},
        contexto={
            "contrato": CONTRATO_CONTEXTO_DEFINICAO_V2,
            "vinculo": {"definicao_anterior_id": anterior_id},
        },
    )


def test_cadeia_valida_de_oito_e_nove_evolucoes(ctx):
    material = _material("leitura.cadeia@test.com", "leitura-cadeia")
    escopo = material["escopo"]
    oito = _sucessoes(material, 8)
    leitura_oito = ler_definicao(escopo, oito["id"])
    assert leitura_oito["contrato"] == CONTRATO_DEFINIR_V2
    assert leitura_oito["definicao_anterior_id"] != material["salva"]["id"]

    nove = _sucessoes(material, 9)
    leitura_nove = ler_definicao(escopo, nove["id"])
    assert leitura_nove["contrato"] == CONTRATO_DEFINIR_V2
    assert leitura_nove["origem_contrato"] == CONTRATO_DEFINIR_V2


def test_ciclo_e_autorreferencia_sao_recusados(ctx):
    material = _material("leitura.ciclo@test.com", "leitura-ciclo")
    escopo = material["escopo"]
    id_a, id_b = _ids_previstos(2)
    elo_a = _elo(material, id_b)
    assert elo_a["id"] == id_a
    elo_b = _elo(material, id_a)
    assert elo_b["id"] == id_b
    with pytest.raises(LeituraErro) as ciclo:
        ler_definicao(escopo, elo_a["id"])
    assert ciclo.value.codigo == "proveniencia_inconsistente"

    id_proprio = _ids_previstos(1)[0]
    proprio = _elo(material, id_proprio)
    assert proprio["id"] == id_proprio
    with pytest.raises(LeituraErro) as autorreferencia:
        ler_definicao(escopo, proprio["id"])
    assert autorreferencia.value.codigo == "proveniencia_inconsistente"


def test_ancestral_invalido_recusa_a_cadeia(ctx):
    material = _material("leitura.meio@test.com", "leitura-meio")
    escopo = material["escopo"]
    definicao, vinculo = _apontar(material, anterior=material["salva"]["id"])
    vinculo["contexto"]["colunas"][0]["tipos"] = list(vinculo["contexto"]["colunas"][0].get("tipos") or []) + [
        "tipo_inexistente"
    ]
    invalido = _gravar_v2(material, definicao, vinculo)
    calculo = _calculo_de(material, invalido["id"], "calc-meio-invalido")
    painel = _dashboard_de(material, calculo, "meio-invalido")
    seguinte, vinculo_seguinte = _apontar(
        material,
        anterior=invalido["id"],
        revisao=painel["revisao"],
        origem=CONTRATO_DEFINIR_V2,
    )
    gravada = _gravar_v2(material, seguinte, vinculo_seguinte)
    with pytest.raises(LeituraErro) as erro:
        ler_definicao(escopo, gravada["id"])
    assert erro.value.codigo == "proveniencia_inconsistente"


def test_v1_ancestral_sem_estrutura_valida_e_recusada(ctx):
    material = _material("leitura.raiz@test.com", "leitura-raiz")
    escopo = material["escopo"]
    documento = copy.deepcopy(material["documento"])
    documento["definicoes"][0]["metrica"]["operacao"] = "operacao_inexistente"
    raiz = _gravar_v1(material, documento)
    with pytest.raises(LeituraErro) as direta:
        ler_definicao(escopo, raiz["id"])
    assert direta.value.codigo == "contrato_saida_invalido"
    calculo = _calculo_de(material, raiz["id"], "calc-raiz-operacao")
    painel = _dashboard_de(material, calculo, "raiz-operacao")
    definicao, vinculo = _apontar(material, anterior=raiz["id"], revisao=painel["revisao"])
    gravada = _gravar_v2(material, definicao, vinculo)
    with pytest.raises(LeituraErro) as cadeia:
        ler_definicao(escopo, gravada["id"])
    assert cadeia.value.codigo == "proveniencia_inconsistente"

    inexistente = copy.deepcopy(material["documento"])
    inexistente["origem_selecao_id"] = 999_999
    sem_selecao = _gravar_v1(material, inexistente)
    with pytest.raises(LeituraErro) as selecao:
        ler_definicao(escopo, sem_selecao["id"])
    assert selecao.value.codigo == "proveniencia_inconsistente"

    selecao_real = obter_analise(escopo, material["documento"]["origem_selecao_id"])
    resultado = copy.deepcopy(selecao_real["resultado"])
    resultado["dataset_id"] = int(resultado["dataset_id"]) + 1
    outra_selecao = _persistir(
        escopo,
        material["salva"],
        contrato="selecao.descoberta.v1",
        resultado=resultado,
        contexto={"contrato": "contexto.selecao.v1"},
        etapa="selecao",
    )
    outro_dataset = copy.deepcopy(material["documento"])
    outro_dataset["origem_selecao_id"] = outra_selecao["id"]
    fora = _gravar_v1(material, outro_dataset)
    with pytest.raises(LeituraErro) as dataset:
        ler_definicao(escopo, fora["id"])
    assert dataset.value.codigo == "proveniencia_inconsistente"


def test_perfil_e_calculado_uma_vez_na_cadeia(ctx, monkeypatch):
    material = _material("leitura.perfil.unico@test.com", "leitura-perfil-unico")
    ultima = _sucessoes(material, 3)
    from app.services.agente_bi_leitura_service import perfilar_dataset as perfil_original

    chamadas = []

    def contar(escopo, dataset_id):
        chamadas.append(int(dataset_id))
        return perfil_original(escopo, dataset_id)

    monkeypatch.setattr("app.services.agente_bi_leitura_service.perfilar_dataset", contar)
    leitura = ler_definicao(material["escopo"], ultima["id"])
    assert leitura["contrato"] == CONTRATO_DEFINIR_V2
    assert chamadas == [material["salva"]["dataset_id"]]
