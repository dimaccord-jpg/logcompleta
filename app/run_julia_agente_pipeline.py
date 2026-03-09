"""
Júlia - Agente Pipeline: orquestra pauta → redação → imagem → qualidade → publicação.
Entrada: payload do Cleiton (mission_id, tipo_missao, tema, ...).
Saída: True apenas quando publicação concluída no formato correto; falhas auditáveis.
"""
import logging
import os
from typing import Any
from app.extensions import db
from app.models import Pauta, NoticiaPortal
from app.run_julia_agente_redacao import gerar_conteudo
from app.run_julia_agente_imagem import gerar_url_imagem
from app.run_julia_agente_qualidade import validar_conteudo
from app.run_julia_agente_publicacao import publicar
from app.run_julia_agente_designer import gerar_assets_por_canal, normalizar_assets_json
from app.run_julia_agente_publisher import publicar_multicanal, RESULTADO_FALHA_TOTAL
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar

logger = logging.getLogger(__name__)


def _status_verificacao_permitidos() -> list[str]:
    """
    Define quais status do Verificador podem alimentar a Júlia.
    Padrão seguro: apenas 'aprovado'.
    Exemplo em homolog: JULIA_STATUS_VERIFICACAO_PERMITIDOS=aprovado,revisar
    """
    raw = (os.getenv("JULIA_STATUS_VERIFICACAO_PERMITIDOS", "aprovado") or "aprovado").strip().lower()
    permitidos = [x.strip() for x in raw.split(",") if x.strip()]
    validos = [x for x in permitidos if x in ("aprovado", "revisar")]
    return validos or ["aprovado"]


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
            logger.warning("Júlia pipeline: nenhuma pauta pendente para tipo=%s", tipo_missao)
            return False

        # 1. Redação
        conteudo = gerar_conteudo(pauta.titulo_original, pauta.fonte or "", pauta.link, tipo_missao)
        if not conteudo:
            logger.error("Júlia pipeline: falha na redação")
            marcar_pauta_falha(pauta.id)
            return False

        # 2. Imagem
        prompt_imagem = (conteudo.get("prompt_imagem") or "").strip()
        url_imagem = gerar_url_imagem(prompt_imagem)
        conteudo["url_imagem"] = url_imagem
        conteudo["link"] = pauta.link
        conteudo["fonte_link"] = pauta.link
        conteudo["titulo_original"] = pauta.titulo_original
        conteudo["fonte"] = pauta.fonte or ""

        # 3. Qualidade
        ok, erros = validar_conteudo(conteudo, tipo_missao)
        if not ok:
            logger.error("Júlia pipeline: validação falhou: %s", erros)
            marcar_pauta_falha(pauta.id)
            return False

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
            return False

        # 6. Publisher (portal + canais; atualiza status_publicacao e PublicacaoCanal)
        pub_result = publicar_multicanal(n, mission_id, assets_por_canal=assets_por_canal)
        if pub_result.get("resultado") == RESULTADO_FALHA_TOTAL:
            logger.error("Júlia pipeline: Publisher falha total (portal não publicado)")
            marcar_pauta_falha(pauta.id)
            return False
        marcar_pauta_publicada(pauta.id)
        return True
