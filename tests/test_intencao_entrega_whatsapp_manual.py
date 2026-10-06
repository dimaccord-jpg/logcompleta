"""Validação semântica manual com Gemini real, em conversas completas.

Não faz parte da suíte obrigatória. Não valida o texto da resposta.
Verifica a ferramenta escolhida e os argumentos estruturados.

    set RUN_GEMINI_ORQUESTRACAO_WHATSAPP=1
    pytest tests/test_intencao_entrega_whatsapp_manual.py -q
"""
from __future__ import annotations

import os

import pytest

from app.services.canal_orquestracao_whatsapp_contextual_service import (
    NOME_ATUALIZAR_TERCEIRO,
    NOME_CANCELAR_TERCEIRO,
    NOME_CONFIRMAR_TERCEIRO,
    NOME_CONTINUAR,
    NOME_ENVIAR_PARA_MEU_WHATSAPP,
    NOME_ESCLARECER,
    NOME_PREPARAR_TERCEIRO,
    CatalogoDecisaoWhatsApp,
    decidir_acao_whatsapp_contextual,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_GEMINI_ORQUESTRACAO_WHATSAPP") != "1",
    reason="Teste manual. Defina RUN_GEMINI_ORQUESTRACAO_WHATSAPP=1 para chamar o Gemini.",
)

_CAPS = (
    NOME_ENVIAR_PARA_MEU_WHATSAPP,
    NOME_PREPARAR_TERCEIRO,
    NOME_ATUALIZAR_TERCEIRO,
    NOME_CONFIRMAR_TERCEIRO,
    NOME_CANCELAR_TERCEIRO,
    NOME_ESCLARECER,
    NOME_CONTINUAR,
)


def _catalogo(**alteracoes) -> CatalogoDecisaoWhatsApp:
    base = dict(
        mensagem="",
        historico=[
            {"role": "user", "content": "Quais são os três riscos desse frete?"},
            {
                "role": "model",
                "content": "Os três riscos são atraso, avaria e custo acima da tabela.",
                "conteudo_alias": "[CONTEUDO_1]",
            },
        ],
        superficie="julia",
        whatsapp_proprio_valido=True,
        conteudos={"[CONTEUDO_1]": "rc" + "a" * 32},
        telefones={},
        rascunho_alias=None,
        rascunho_referencia=None,
        rascunho_estado=None,
        rascunho_versao=None,
        rascunho_nome=None,
        rascunho_tem_telefone=False,
        confirmacao_alias=None,
        confirmacao_referencia=None,
        capabilities=_CAPS,
    )
    base.update(alteracoes)
    return CatalogoDecisaoWhatsApp(**base)


def _decidir(client, modelo, catalogo):
    from app.run_julia_chat import _api_key_label_chat

    return decidir_acao_whatsapp_contextual(
        catalogo=catalogo,
        agent="julia",
        flow_type="julia_chat",
        client=client,
        model=modelo,
        api_key_label=_api_key_label_chat(),
    )


def test_conversas_completas_com_gemini_real():
    from google import genai
    from google.genai import types as genai_types

    from app.run_julia_chat import _get_chat_model_candidates

    chave = (os.getenv("GEMINI_API_KEY_1") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not chave:
        pytest.skip("GEMINI_API_KEY_1 ou GEMINI_API_KEY ausente.")
    client = genai.Client(api_key=chave, http_options=genai_types.HttpOptions(timeout=60_000))
    modelo = _get_chat_model_candidates()[0]
    divergencias = []

    def _esperar(rotulo, catalogo, tipo, **argumentos):
        obtido = _decidir(client, modelo, catalogo)
        if obtido.tipo != tipo:
            divergencias.append(f"{rotulo}: tipo esperado={tipo} obtido={obtido.tipo}")
            return obtido
        for chave_arg, valor in argumentos.items():
            if obtido.argumentos.get(chave_arg) != valor:
                divergencias.append(
                    f"{rotulo}: {chave_arg} esperado={valor!r} obtido={obtido.argumentos.get(chave_arg)!r}"
                )
        return obtido

    _esperar(
        "manda isso pra mim",
        _catalogo(mensagem="manda isso pra mim"),
        NOME_ENVIAR_PARA_MEU_WHATSAPP,
        conteudo_ref="[CONTEUDO_1]",
    )
    _esperar(
        "manda no meu celular",
        _catalogo(mensagem="manda no meu celular"),
        NOME_ENVIAR_PARA_MEU_WHATSAPP,
        conteudo_ref="[CONTEUDO_1]",
    )
    _esperar(
        "nem por WhatsApp",
        _catalogo(
            mensagem="nem por WhatsApp?",
            historico=[
                {
                    "role": "model",
                    "content": "Os três riscos são atraso, avaria e custo acima da tabela.",
                    "conteudo_alias": "[CONTEUDO_1]",
                },
                {"role": "user", "content": "manda no celular"},
                {"role": "model", "content": "Posso enviar a análise anterior para o seu WhatsApp."},
            ],
        ),
        NOME_ENVIAR_PARA_MEU_WHATSAPP,
        conteudo_ref="[CONTEUDO_1]",
    )
    _esperar(
        "manda isso pro João",
        _catalogo(mensagem="manda isso pro João"),
        NOME_PREPARAR_TERCEIRO,
        conteudo_ref="[CONTEUDO_1]",
        nome_destinatario="João",
    )
    _esperar(
        "telefone no turno seguinte",
        _catalogo(
            mensagem="o número é [TEL_1]",
            telefones={"[TEL_1]": "+5519999999999"},
            rascunho_alias="[RASCUNHO_1]",
            rascunho_referencia="rw" + "b" * 32,
            rascunho_estado="aguardando_telefone",
            rascunho_versao=1,
            rascunho_nome="João",
            rascunho_tem_telefone=False,
        ),
        NOME_ATUALIZAR_TERCEIRO,
        rascunho_ref="[RASCUNHO_1]",
        versao=1,
        telefone_declarado_ref="[TEL_1]",
    )
    _esperar(
        "cancelamento",
        _catalogo(
            mensagem="cancela",
            rascunho_alias="[RASCUNHO_1]",
            rascunho_referencia="rw" + "c" * 32,
            rascunho_estado="aguardando_confirmacao",
            rascunho_versao=2,
            rascunho_nome="João",
            rascunho_tem_telefone=True,
            confirmacao_alias="[CONFIRMACAO_1]",
            confirmacao_referencia="cf" + "d" * 32,
        ),
        NOME_CANCELAR_TERCEIRO,
        rascunho_ref="[RASCUNHO_1]",
        versao=2,
    )
    _esperar(
        "correcao de telefone",
        _catalogo(
            mensagem="troca para [TEL_1]",
            telefones={"[TEL_1]": "+14155552671"},
            rascunho_alias="[RASCUNHO_1]",
            rascunho_referencia="rw" + "e" * 32,
            rascunho_estado="aguardando_confirmacao",
            rascunho_versao=2,
            rascunho_nome="João",
            rascunho_tem_telefone=True,
            confirmacao_alias="[CONFIRMACAO_1]",
            confirmacao_referencia="cf" + "f" * 32,
        ),
        NOME_ATUALIZAR_TERCEIRO,
        rascunho_ref="[RASCUNHO_1]",
        versao=2,
        telefone_declarado_ref="[TEL_1]",
    )
    _esperar(
        "manda para ele também com antecedente",
        _catalogo(
            mensagem="manda para ele também",
            historico=[
                {
                    "role": "model",
                    "content": "Os três riscos são atraso, avaria e custo acima da tabela.",
                    "conteudo_alias": "[CONTEUDO_1]",
                },
                {"role": "user", "content": "envia essa análise no meu WhatsApp"},
                {"role": "model", "content": "Enviei para o seu WhatsApp."},
                {"role": "user", "content": "o João também precisa ver"},
            ],
        ),
        NOME_PREPARAR_TERCEIRO,
        conteudo_ref="[CONTEUDO_1]",
    )
    ambiguo = _decidir(
        client,
        modelo,
        _catalogo(
            mensagem="manda para ele também",
            historico=[
                {"role": "model", "content": "Primeira análise de atraso.", "conteudo_alias": "[CONTEUDO_2]"},
                {"role": "model", "content": "Segunda análise de custo.", "conteudo_alias": "[CONTEUDO_1]"},
                {"role": "user", "content": "tanto o João quanto a Maria comentaram"},
            ],
            conteudos={
                "[CONTEUDO_1]": "rc" + "1" * 32,
                "[CONTEUDO_2]": "rc" + "2" * 32,
            },
        ),
    )
    if ambiguo.tipo not in {NOME_ESCLARECER, NOME_PREPARAR_TERCEIRO}:
        divergencias.append(f"ambíguo: tipo inesperado {ambiguo.tipo}")
    assert divergencias == []
