"""
Serviço de CRUD e fluxo de pautas.
Listagem com filtros, criação/edição, arquivar, reprocessar e marcar para revisão.
"""
import logging
from datetime import datetime

from app.extensions import db
from app.models import Pauta, SerieItemEditorial
from app.run_julia_regras import status_verificacao_permitidos
from app.services.auditoria_service import registrar_auditoria_admin

logger = logging.getLogger(__name__)

TIPOS_VALIDOS = {"noticia", "artigo"}
STATUS_VALIDOS = {"pendente", "em_processamento", "publicada", "falha"}


def listar_pautas(
    tipo: str | None = None,
    status: str | None = None,
    status_verificacao: str | None = None,
    data_ini: str | None = None,
    data_fim: str | None = None,
    serie_id: int | None = None,
    limite: int = 200,
) -> tuple[list[tuple[Pauta, SerieItemEditorial | None]], list[str]]:
    """
    Lista pautas com filtros opcionais.
    Retorna (lista de (pauta, item_serie ou None), status_verificacao_permitidos para template).
    """
    query = Pauta.query
    if tipo:
        query = query.filter(Pauta.tipo == tipo)
    if status:
        query = query.filter(Pauta.status == status)
    if status_verificacao:
        query = query.filter(Pauta.status_verificacao == status_verificacao)
    if data_ini:
        try:
            dt_ini = datetime.strptime(data_ini, "%Y-%m-%d")
            query = query.filter(Pauta.created_at >= dt_ini)
        except ValueError:
            pass
    if data_fim:
        try:
            dt_fim = datetime.strptime(data_fim, "%Y-%m-%d")
            dt_fim = dt_fim.replace(hour=23, minute=59, second=59, microsecond=999999)
            query = query.filter(Pauta.created_at <= dt_fim)
        except ValueError:
            pass
    if serie_id:
        query = query.join(
            SerieItemEditorial, SerieItemEditorial.pauta_id == Pauta.id
        ).filter(SerieItemEditorial.serie_id == serie_id)

    pautas_raw = query.order_by(Pauta.created_at.desc()).limit(limite).all()
    ids_pauta = [p.id for p in pautas_raw]
    vinculos = {}
    if ids_pauta:
        itens = SerieItemEditorial.query.filter(
            SerieItemEditorial.pauta_id.in_(ids_pauta)
        ).all()
        for it in itens:
            vinculos[it.pauta_id] = it
    pautas = [(p, vinculos.get(p.id)) for p in pautas_raw]
    permitidos = status_verificacao_permitidos()
    return pautas, permitidos


def salvar_pauta(
    actor_email: str | None,
    pauta_id: int | None,
    titulo_original: str,
    link: str,
    fonte: str,
    tipo: str,
    status: str,
    status_verificacao: str,
    motivo_admin: str | None,
) -> tuple[Pauta | None, str | None]:
    """
    Cria ou atualiza pauta. tipo/status/status_verificacao são normalizados.
    Retorna (pauta, None) em sucesso ou (None, mensagem_erro).
    """
    if not titulo_original or not link:
        return None, "Título e link são obrigatórios para a pauta."
    tipo = tipo if tipo in TIPOS_VALIDOS else "artigo"
    status = status if status in STATUS_VALIDOS else "pendente"
    permitidos = set(status_verificacao_permitidos() + ["revisar", "rejeitado"])
    status_verificacao = (
        status_verificacao if status_verificacao in permitidos else status_verificacao_permitidos()[0]
    )
    try:
        if pauta_id:
            pauta = Pauta.query.filter_by(id=pauta_id).first()
            if not pauta:
                return None, "Pauta não encontrada."
        else:
            pauta = Pauta()
            db.session.add(pauta)

        estado_antes = None
        if pauta.id:
            estado_antes = {
                "titulo_original": pauta.titulo_original,
                "link": pauta.link,
                "tipo": pauta.tipo,
                "status": pauta.status,
                "status_verificacao": pauta.status_verificacao,
                "fonte": pauta.fonte,
                "arquivada": bool(pauta.arquivada),
            }
        pauta.titulo_original = titulo_original[:500]
        pauta.link = link[:500]
        pauta.fonte = fonte[:200] or None
        pauta.tipo = tipo
        pauta.status = status
        pauta.status_verificacao = status_verificacao
        pauta.fonte_tipo = "manual"
        db.session.commit()
        estado_depois = {
            "titulo_original": pauta.titulo_original,
            "link": pauta.link,
            "tipo": pauta.tipo,
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
            "fonte": pauta.fonte,
            "arquivada": bool(pauta.arquivada),
        }
        registrar_auditoria_admin(
            actor_email,
            "admin_operacao",
            "Criar/editar pauta via admin",
            "pauta",
            pauta.id,
            estado_antes,
            estado_depois,
            motivo_admin,
            "sucesso",
        )
        return pauta, None
    except Exception as e:
        db.session.rollback()
        registrar_auditoria_admin(
            actor_email,
            "admin_operacao",
            "Erro ao salvar pauta via admin",
            "pauta",
            pauta_id,
            None,
            None,
            motivo_admin,
            "falha",
            detalhe=str(e),
        )
        return None, str(e)


def arquivar_pauta(
    actor_email: str | None, pauta_id: int, motivo: str | None
) -> tuple[bool, str | None]:
    """Arquivar pauta. Retorna (True, None) ou (False, mensagem_erro)."""
    try:
        pauta = Pauta.query.filter_by(id=pauta_id).first()
        if not pauta:
            return False, "Pauta não encontrada."
        estado_antes = {
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
            "arquivada": bool(pauta.arquivada),
        }
        pauta.arquivada = True
        db.session.commit()
        estado_depois = {
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
            "arquivada": bool(pauta.arquivada),
        }
        registrar_auditoria_admin(
            actor_email,
            "admin_operacao",
            "Arquivar pauta manualmente",
            "pauta",
            pauta.id,
            estado_antes,
            estado_depois,
            motivo,
            "sucesso",
        )
        return True, None
    except Exception as e:
        db.session.rollback()
        registrar_auditoria_admin(
            actor_email,
            "admin_operacao",
            "Erro ao arquivar pauta",
            "pauta",
            pauta_id,
            None,
            None,
            motivo,
            "falha",
            detalhe=str(e),
        )
        return False, str(e)


def reprocessar_pauta(
    actor_email: str | None, pauta_id: int, motivo: str | None
) -> tuple[bool, str | None]:
    """Marca pauta para reprocessamento (status pendente)."""
    try:
        pauta = Pauta.query.filter_by(id=pauta_id).first()
        if not pauta:
            return False, "Pauta não encontrada."
        estado_antes = {
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
        }
        pauta.status = "pendente"
        db.session.commit()
        estado_depois = {
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
        }
        registrar_auditoria_admin(
            actor_email,
            "admin_reprocessamento",
            "Reprocessar pauta manualmente",
            "pauta",
            pauta.id,
            estado_antes,
            estado_depois,
            motivo,
            "sucesso",
        )
        return True, None
    except Exception as e:
        db.session.rollback()
        registrar_auditoria_admin(
            actor_email,
            "admin_reprocessamento",
            "Erro ao reprocessar pauta",
            "pauta",
            pauta_id,
            None,
            None,
            motivo,
            "falha",
            detalhe=str(e),
        )
        return False, str(e)


def marcar_revisao_pauta(
    actor_email: str | None, pauta_id: int, motivo: str | None
) -> tuple[bool, str | None]:
    """Marca pauta para revisão manual (status_verificacao = revisar)."""
    try:
        pauta = Pauta.query.filter_by(id=pauta_id).first()
        if not pauta:
            return False, "Pauta não encontrada."
        estado_antes = {
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
        }
        pauta.status_verificacao = "revisar"
        db.session.commit()
        estado_depois = {
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
        }
        registrar_auditoria_admin(
            actor_email,
            "admin_operacao",
            "Marcar pauta para revisão manual",
            "pauta",
            pauta.id,
            estado_antes,
            estado_depois,
            motivo,
            "sucesso",
        )
        return True, None
    except Exception as e:
        db.session.rollback()
        registrar_auditoria_admin(
            actor_email,
            "admin_operacao",
            "Erro ao marcar pauta para revisão",
            "pauta",
            pauta_id,
            None,
            None,
            motivo,
            "falha",
            detalhe=str(e),
        )
        return False, str(e)
