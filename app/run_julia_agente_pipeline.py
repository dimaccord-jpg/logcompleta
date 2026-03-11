"""
Júlia - Agente Pipeline: orquestra pauta → redação → imagem → qualidade → publicação.
Entrada: payload do Cleiton (mission_id, tipo_missao, tema, ...).
Saída: True apenas quando publicação concluída no formato correto; falhas auditáveis.
"""
import logging
import os
import re
from typing import Any
from app.extensions import db
from app.models import Pauta, NoticiaPortal, SerieItemEditorial
from app.run_julia_agente_redacao import gerar_conteudo
from app.run_julia_agente_imagem import gerar_url_imagem, classificar_origem_url_imagem
from app.run_julia_agente_qualidade import validar_conteudo
from app.run_julia_agente_publicacao import publicar
from app.run_julia_agente_designer import gerar_assets_por_canal, normalizar_assets_json
from app.run_julia_agente_publisher import publicar_multicanal, RESULTADO_FALHA_TOTAL
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar
from app.run_julia_regras import status_verificacao_permitidos

logger = logging.getLogger(__name__)


def _limpar_texto_prompt(v: str | None, limite: int) -> str:
    txt = (v or "").strip()
    txt = re.sub(r"\s+", " ", txt)
    return txt[:limite]


def _montar_prompt_imagem_contextual(conteudo: dict[str, Any], pauta: Pauta, tipo_missao: str) -> str:
    """
    Monta prompt semântico para imagem com base no texto gerado e na pauta aprovada.
    Mantém isolamento: apenas compõe metadados para o agente de imagem.
    """
    prompt_base = _limpar_texto_prompt(conteudo.get("prompt_imagem"), 320)
    titulo = _limpar_texto_prompt(conteudo.get("titulo_julia") or pauta.titulo_original, 140)
    subtitulo = _limpar_texto_prompt(conteudo.get("subtitulo"), 160)
    resumo = _limpar_texto_prompt(conteudo.get("resumo_julia"), 220)
    fonte = _limpar_texto_prompt(pauta.fonte, 80)

    if prompt_base and len(prompt_base) >= 30:
        return prompt_base

    contexto = " | ".join([x for x in [titulo, subtitulo, resumo] if x])
    tipo_desc = "strategic long-form article" if (tipo_missao or "noticia") == "artigo" else "fast logistics insight"
    prompt_final = (
        "Create a professional editorial cover image, realistic photography style, "
        "no text overlay, no watermark, logistics and supply chain theme. "
        f"Content type: {tipo_desc}. Source: {fonte or 'logistics portal'}. "
        f"Context: {contexto or 'global supply chain operations'}"
    )
    return prompt_final[:500]


def _status_verificacao_permitidos() -> list[str]:
    """
    Backward-compat wrapper: delega para app.run_julia_regras.status_verificacao_permitidos().
    Mantém a assinatura usada por outros módulos sem acoplá-los à pipeline.
    """
    return status_verificacao_permitidos()


def obter_pauta_validada(tipo_missao: str, mission_id: str | None) -> Pauta | None:
    """
    Retorna uma pauta pendente elegível pelo Verificador para o tipo (noticia | artigo).
    Por padrão só 'aprovado'; pode incluir 'revisar' via env em homolog.
    Marca como em_processamento e opcionalmente associa mission_id.
    """
    tipo = (tipo_missao or "noticia").lower()
    status_permitidos = _status_verificacao_permitidos()
    try:
        pauta = (
            Pauta.query.filter(
                Pauta.tipo == tipo,
                Pauta.status == "pendente",
                Pauta.status_verificacao.in_(status_permitidos),
            )
            .order_by(Pauta.created_at.asc())
            .first()
        )
        if not pauta:
            return None
        pauta.status = "em_processamento"
        if mission_id:
            pauta.mission_id = mission_id
        db.session.commit()
        return pauta
    except Exception as e:
        logger.exception("Falha ao obter pauta: %s", e)
        db.session.rollback()
        return None


def marcar_pauta_publicada(pauta_id: int) -> None:
    try:
        p = db.session.get(Pauta, pauta_id)
        if p:
            p.status = "publicada"
            db.session.commit()
    except Exception:
        db.session.rollback()


def marcar_pauta_falha(pauta_id: int) -> None:
    try:
        p = db.session.get(Pauta, pauta_id)
        if p:
            p.status = "falha"
            db.session.commit()
    except Exception:
        db.session.rollback()


def _atualizar_item_serie_por_pauta(pauta_id: int, status: str, noticia_id: int | None = None) -> None:
    """
    Atualiza (se existir) o item de série associado a uma pauta,
    delegando para o serviço de série (mantendo auditoria e ciclo de vida centralizados).
    """
    try:
        item = SerieItemEditorial.query.filter_by(pauta_id=pauta_id).first()
        if not item:
            return
        from app.run_cleiton_agente_serie import atualizar_status_item
        atualizar_status_item(item.id, status, noticia_id=noticia_id)
    except Exception:
        db.session.rollback()


def executar_pipeline(payload: dict[str, Any], app_flask) -> bool:
    """
    Pipeline: obter pauta → redigir → gerar imagem → validar → publicar.
    Retorna True apenas quando publicação for concluída no formato correto.
    """
    mission_id = payload.get("mission_id", "")
    tipo_missao = (payload.get("tipo_missao") or "noticia").lower()
    logger.info("Júlia pipeline: mission_id=%s tipo=%s", mission_id, tipo_missao)

    with app_flask.app_context():
        pauta = obter_pauta_validada(tipo_missao, mission_id)
        if not pauta:
            logger.warning("Júlia pipeline: nenhuma pauta pendente/ elegível para tipo=%s", tipo_missao)
            auditoria_registrar(
                tipo_decisao="julia",
                decisao="Nenhuma pauta elegível para processamento",
                contexto={
                    "mission_id": mission_id,
                    "tipo_missao": tipo_missao,
                    "status_verificacao_permitidos": _status_verificacao_permitidos(),
                },
                resultado="ignorado",
                detalhe="Pipeline não encontrou pauta com status_verificacao permitido.",
            )
            return False

        auditoria_registrar(
            tipo_decisao="julia",
            decisao="Pauta selecionada para pipeline",
            contexto={
                "mission_id": mission_id,
                "tipo_missao": tipo_missao,
                "pauta_id": pauta.id,
                "link": pauta.link,
            },
            resultado="sucesso",
        )

        # 1. Redação
        conteudo = gerar_conteudo(pauta.titulo_original, pauta.fonte or "", pauta.link, tipo_missao)
        if not conteudo:
            logger.error("Júlia pipeline: falha na redação")
            marcar_pauta_falha(pauta.id)
            _atualizar_item_serie_por_pauta(pauta.id, "falha")
            auditoria_registrar(
                tipo_decisao="julia",
                decisao="Falha na redação",
                contexto={
                    "mission_id": mission_id,
                    "tipo_missao": tipo_missao,
                    "pauta_id": pauta.id,
                },
                resultado="falha",
                detalhe="Agente de redação retornou conteúdo vazio ou inválido.",
            )
            return False

        auditoria_registrar(
            tipo_decisao="julia",
            decisao="Redação concluída",
            contexto={
                "mission_id": mission_id,
                "tipo_missao": tipo_missao,
                "pauta_id": pauta.id,
                "tem_cta": bool(conteudo.get("cta")),
                "tem_objetivo_lead": bool(conteudo.get("objetivo_lead")),
            },
            resultado="sucesso",
        )

        # 2. Imagem
        prompt_imagem = _montar_prompt_imagem_contextual(conteudo, pauta, tipo_missao)
        fallback_usado = False
        try:
            url_imagem = gerar_url_imagem(prompt_imagem)
        except Exception as e:
            logger.exception("Júlia pipeline: falha inesperada na geração de imagem, usando fallback: %s", e)
            url_imagem = gerar_url_imagem(prompt_imagem)
            fallback_usado = True
        origem_imagem = classificar_origem_url_imagem(url_imagem)
        if origem_imagem in ("contingencia_fixa", "placeholder_remoto"):
            fallback_usado = True
        conteudo["url_imagem"] = url_imagem
        conteudo["prompt_imagem"] = prompt_imagem
        conteudo["link"] = pauta.link
        conteudo["fonte_link"] = pauta.link
        conteudo["titulo_original"] = pauta.titulo_original
        conteudo["fonte"] = pauta.fonte or ""

        auditoria_registrar(
            tipo_decisao="julia",
            decisao="Imagem gerada para conteúdo",
            contexto={
                "mission_id": mission_id,
                "tipo_missao": tipo_missao,
                "pauta_id": pauta.id,
                "prompt_imagem_vazio": not bool(_limpar_texto_prompt(conteudo.get("prompt_imagem"), 500)),
                "prompt_imagem_len": len(prompt_imagem or ""),
                "origem_imagem": origem_imagem,
                "fallback_usado": fallback_usado,
            },
            resultado="sucesso",
        )

        # 3. Qualidade
        ok, erros = validar_conteudo(conteudo, tipo_missao)
        if not ok:
            logger.error("Júlia pipeline: validação falhou: %s", erros)
            marcar_pauta_falha(pauta.id)
            _atualizar_item_serie_por_pauta(pauta.id, "falha")
            auditoria_registrar(
                tipo_decisao="julia",
                decisao="Validação de qualidade reprovada",
                contexto={
                    "mission_id": mission_id,
                    "tipo_missao": tipo_missao,
                    "pauta_id": pauta.id,
                    "erros": erros,
                },
                resultado="falha",
            )
            return False

        auditoria_registrar(
            tipo_decisao="julia",
            decisao="Validação de qualidade aprovada",
            contexto={
                "mission_id": mission_id,
                "tipo_missao": tipo_missao,
                "pauta_id": pauta.id,
            },
            resultado="sucesso",
        )

        # 4. Designer (assets por canal)
        design = gerar_assets_por_canal(
            conteudo.get("url_imagem"),
            conteudo.get("prompt_imagem"),
            tipo_conteudo=tipo_missao,
        )
        url_master = design.get("url_imagem_master") or conteudo.get("url_imagem")
        assets_por_canal = design.get("assets_por_canal") or {}
        assets_json = normalizar_assets_json(assets_por_canal)
        auditoria_registrar(
            tipo_decisao="designer",
            decisao=f"Designer: assets para {len(assets_por_canal)} canais | provider={design.get('provider_utilizado')}",
            contexto={"canais": list(assets_por_canal.keys()), "url_master": url_master[:80] if url_master else None},
            resultado="sucesso",
        )

        # 5. Publicação (cria NoticiaPortal com status_publicacao=pendente)
        n = publicar(
            tipo=tipo_missao,
            titulo_julia=conteudo.get("titulo_julia", ""),
            link=pauta.link,
            fonte=pauta.fonte or "",
            resumo_julia=conteudo.get("resumo_julia"),
            subtitulo=conteudo.get("subtitulo"),
            conteudo_completo=conteudo.get("conteudo_completo"),
            url_imagem=url_master,
            referencias=conteudo.get("referencias"),
            titulo_original=pauta.titulo_original,
            cta=conteudo.get("cta"),
            objetivo_lead=conteudo.get("objetivo_lead"),
            origem_pauta="pipeline",
            url_imagem_master=url_master,
            assets_canais_json=assets_json,
            status_publicacao="pendente",
        )
        if not n:
            marcar_pauta_falha(pauta.id)
            auditoria_registrar(
                tipo_decisao="julia",
                decisao="Falha ao criar registro de publicação interna",
                contexto={
                    "mission_id": mission_id,
                    "tipo_missao": tipo_missao,
                    "pauta_id": pauta.id,
                },
                resultado="falha",
            )
            return False

        auditoria_registrar(
            tipo_decisao="julia",
            decisao="Publicação interna criada",
            contexto={
                "mission_id": mission_id,
                "tipo_missao": tipo_missao,
                "pauta_id": pauta.id,
                "noticia_id": n.id,
            },
            resultado="sucesso",
        )

        # 6. Publisher (portal + canais; atualiza status_publicacao e PublicacaoCanal)
        pub_result = publicar_multicanal(n, mission_id, assets_por_canal=assets_por_canal)
        if pub_result.get("resultado") == RESULTADO_FALHA_TOTAL:
            logger.error("Júlia pipeline: Publisher falha total (portal não publicado)")
            marcar_pauta_falha(pauta.id)
            _atualizar_item_serie_por_pauta(pauta.id, "falha")
            return False
        marcar_pauta_publicada(pauta.id)
        _atualizar_item_serie_por_pauta(pauta.id, "publicado", noticia_id=n.id)
        return True
