"""
Júlia - Agente Publisher: publicação multicanal com status por canal e regras (janela, duplicidade).
Portal obrigatório; canais externos em modo mock/real. Auditoria detalhada.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from app.extensions import db
from app.models import NoticiaPortal, PublicacaoCanal
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

STATUS_PENDENTE = "pendente"
STATUS_PUBLICADO = "publicado"
STATUS_FALHA = "falha"
STATUS_IGNORADO = "ignorado"
RESULTADO_SUCESSO_TOTAL = "sucesso_total"
RESULTADO_SUCESSO_PARCIAL = "sucesso_parcial"
RESULTADO_FALHA_TOTAL = "falha_total"


def _publisher_enabled() -> bool:
    return os.getenv("PUBLISHER_ENABLED", "true").strip().lower() in ("true", "1", "t", "yes")


def _canais_ativos() -> list[str]:
    raw = os.getenv("PUBLISHER_CANAIS_ATIVOS", "portal").strip()
    if not raw:
        return ["portal"]
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _modo_mock() -> bool:
    return (os.getenv("PUBLISHER_MODO", "mock").strip() or "mock").lower() == "mock"


def _max_tentativas() -> int:
    try:
        return max(1, min(10, int(os.getenv("PUBLISHER_MAX_TENTATIVAS", "3").strip())))
    except ValueError:
        return 3


def _janela_publicacao() -> tuple[int, int] | None:
    """Retorna janela (inicio, fim) em hora local, ou None quando não configurada."""
    raw_i = (os.getenv("PUBLISHER_JANELA_PUBLICACAO_INICIO", "").strip())
    raw_f = (os.getenv("PUBLISHER_JANELA_PUBLICACAO_FIM", "").strip())
    if not raw_i or not raw_f:
        return None
    try:
        i = max(0, min(23, int(raw_i)))
        f = max(0, min(23, int(raw_f)))
        return (i, f)
    except ValueError:
        return None


def _dentro_janela_publicacao(agora: datetime | None = None) -> bool:
    """True se está dentro da janela configurada; sem configuração => True."""
    janela = _janela_publicacao()
    if not janela:
        return True
    inicio, fim = janela
    t = agora or datetime.now()
    hora = t.hour
    if inicio <= fim:
        return inicio <= hora < fim
    return hora >= inicio or hora < fim


def _intervalo_minutos_entre_posts() -> int:
    """Retorna intervalo mínimo entre posts por canal. 0 desativa."""
    raw = (os.getenv("PUBLISHER_INTERVALO_MINUTOS_ENTRE_POSTS", "").strip() or "0")
    try:
        return max(0, min(1440, int(raw)))
    except ValueError:
        return 0


def _respeita_intervalo(canal: str, agora: datetime | None = None) -> bool:
    """True se o último publicado do canal está fora da janela de intervalo mínimo."""
    intervalo = _intervalo_minutos_entre_posts()
    if intervalo <= 0:
        return True
    t = agora or datetime.now()
    ultimo = (
        PublicacaoCanal.query.filter_by(canal=canal, status=STATUS_PUBLICADO)
        .order_by(PublicacaoCanal.criado_em.desc())
        .first()
    )
    if not ultimo or not ultimo.criado_em:
        return True
    delta_min = (t - ultimo.criado_em).total_seconds() / 60.0
    return delta_min >= intervalo


def _ja_publicado_canal(noticia_id: int, canal: str) -> bool:
    """True se já existe publicação ativa para noticia_id + canal (evita duplicidade por canal)."""
    return bool(
        PublicacaoCanal.query.filter_by(
            noticia_id=noticia_id,
            canal=canal,
        ).filter(PublicacaoCanal.status != STATUS_FALHA).first()
    )


def _publicar_portal(noticia: NoticiaPortal) -> bool:
    """Marca publicação no portal (atualiza status_publicacao e publicado_em)."""
    try:
        noticia.status_publicacao = STATUS_PUBLICADO
        noticia.publicado_em = _utcnow_naive()
        db.session.commit()
        return True
    except Exception as e:
        logger.exception("Falha ao marcar portal publicado: %s", e)
        db.session.rollback()
        return False


def _registrar_publicacao_canal(
    noticia_id: int,
    mission_id: str | None,
    canal: str,
    status: str,
    payload_envio: dict | None = None,
    resposta: dict | None = None,
    erro_detalhe: str | None = None,
) -> PublicacaoCanal | None:
    try:
        pc = PublicacaoCanal(
            noticia_id=noticia_id,
            mission_id=mission_id,
            canal=canal,
            status=status,
            tentativa_atual=1,
            max_tentativas=_max_tentativas(),
            payload_envio_json=json.dumps(payload_envio, ensure_ascii=False) if payload_envio else None,
            resposta_canal_json=json.dumps(resposta, ensure_ascii=False) if resposta else None,
            erro_detalhe=erro_detalhe,
        )
        db.session.add(pc)
        db.session.commit()
        return pc
    except Exception as e:
        logger.exception("Falha ao registrar PublicacaoCanal: %s", e)
        db.session.rollback()
        return None


def _publicar_canal_externo_mock(canal: str, noticia_id: int, mission_id: str | None, titulo: str) -> tuple[str, str | None]:
    """Simula publicação em canal externo. Retorna (status, erro_detalhe)."""
    if _ja_publicado_canal(noticia_id, canal):
        return STATUS_IGNORADO, "Duplicidade: já publicado neste canal."
    # Mock: marca como publicado com resposta simulada
    return STATUS_PUBLICADO, None


def publicar_multicanal(
    noticia: NoticiaPortal,
    mission_id: str | None,
    assets_por_canal: dict | None = None,
) -> dict[str, Any]:
    """
    Publica conteúdo no portal (obrigatório) e nos canais ativos.
    Bloqueia duplicidade por canal. Retorna resultado estruturado para auditoria.
    """
    if not _publisher_enabled():
        if noticia:
            _publicar_portal(noticia)
        return {"resultado": RESULTADO_SUCESSO_TOTAL, "portal": STATUS_PUBLICADO, "canais": {}}
    if not noticia or noticia.status_qualidade != "aprovado":
        return {"resultado": RESULTADO_FALHA_TOTAL, "erro": "Conteúdo não aprovado para publicação."}
    canais = _canais_ativos()
    assets_por_canal = assets_por_canal or {}
    resultado_canais = {}
    portal_ok = False
    for canal in canais:
        if canal == "portal":
            if _ja_publicado_canal(noticia.id, "portal"):
                resultado_canais["portal"] = STATUS_IGNORADO
                portal_ok = True
                continue
            if _publicar_portal(noticia):
                _registrar_publicacao_canal(
                    noticia.id, mission_id, "portal", STATUS_PUBLICADO,
                    payload_envio={"noticia_id": noticia.id, "titulo": noticia.titulo_julia},
                )
                resultado_canais["portal"] = STATUS_PUBLICADO
                portal_ok = True
            else:
                resultado_canais["portal"] = STATUS_FALHA
                _registrar_publicacao_canal(
                    noticia.id, mission_id, "portal", STATUS_FALHA,
                    erro_detalhe="Falha ao atualizar status_publicacao.",
                )
            continue
        # Canal externo
        if not _dentro_janela_publicacao():
            resultado_canais[canal] = STATUS_IGNORADO
            _registrar_publicacao_canal(
                noticia.id,
                mission_id,
                canal,
                STATUS_IGNORADO,
                erro_detalhe="Fora da janela de publicação configurada.",
            )
            continue
        if not _respeita_intervalo(canal):
            resultado_canais[canal] = STATUS_IGNORADO
            _registrar_publicacao_canal(
                noticia.id,
                mission_id,
                canal,
                STATUS_IGNORADO,
                erro_detalhe="Intervalo mínimo entre posts ainda não atendido.",
            )
            continue
        if _ja_publicado_canal(noticia.id, canal):
            resultado_canais[canal] = STATUS_IGNORADO
            continue
        if _modo_mock():
            st, err = _publicar_canal_externo_mock(canal, noticia.id, mission_id, noticia.titulo_julia or "")
            resultado_canais[canal] = st
            _registrar_publicacao_canal(
                noticia.id, mission_id, canal, st,
                payload_envio={"titulo": noticia.titulo_julia, "canal": canal},
                resposta={"mock": True, "canal": canal} if st == STATUS_PUBLICADO else None,
                erro_detalhe=err,
            )
        else:
            # Modo real: placeholder para futura integração com APIs
            resultado_canais[canal] = STATUS_FALHA
            _registrar_publicacao_canal(
                noticia.id, mission_id, canal, STATUS_FALHA,
                erro_detalhe="Modo real não implementado para este canal.",
            )
    # Determina resultado global
    publicados = sum(1 for s in resultado_canais.values() if s == STATUS_PUBLICADO)
    falhas = sum(1 for s in resultado_canais.values() if s == STATUS_FALHA)
    if not portal_ok:
        resultado_final = RESULTADO_FALHA_TOTAL
    elif falhas > 0:
        resultado_final = RESULTADO_SUCESSO_PARCIAL
    else:
        resultado_final = RESULTADO_SUCESSO_TOTAL
    # Atualiza status_publicacao na notícia se parcial
    if resultado_final == RESULTADO_SUCESSO_PARCIAL and portal_ok:
        try:
            noticia.status_publicacao = "parcial"
            db.session.commit()
        except Exception:
            db.session.rollback()
    out = {
        "resultado": resultado_final,
        "portal": resultado_canais.get("portal", STATUS_PENDENTE),
        "canais": resultado_canais,
        "publicados": publicados,
        "falhas": falhas,
    }
    auditoria_registrar(
        tipo_decisao="publisher",
        decisao=f"Publisher: {resultado_final} | noticia_id={noticia.id}",
        contexto=out,
        resultado="sucesso" if resultado_final != RESULTADO_FALHA_TOTAL else "falha",
        detalhe=json.dumps(resultado_canais, ensure_ascii=False),
    )
    logger.info("Publisher: noticia_id=%s resultado=%s canais=%s", noticia.id, resultado_final, resultado_canais)
    return out
