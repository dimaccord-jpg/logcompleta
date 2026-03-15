"""
Serviço de CRUD de séries editoriais e itens.
Inclui reabrir/pular itens, vincular e desvincular pauta, com auditoria.
"""
import logging
from datetime import datetime

from app.extensions import db
from app.models import SerieEditorial, SerieItemEditorial, Pauta
from app.run_cleiton_agente_serie import atualizar_status_item
from app.services.auditoria_service import registrar_auditoria_admin

logger = logging.getLogger(__name__)


def listar_series() -> list:
    """Lista séries ordenadas por ativo e created_at."""
    return (
        SerieEditorial.query.order_by(
            SerieEditorial.ativo.desc(), SerieEditorial.created_at.desc()
        )
        .all()
    )


def obter_serie_por_id(serie_id: int) -> SerieEditorial | None:
    """Retorna série por id ou None."""
    return SerieEditorial.query.filter_by(id=serie_id).first()


def salvar_serie(
    serie_id: int | None,
    nome: str,
    tema: str,
    objetivo_lead: str,
    cta_base: str,
    descricao: str,
    cadencia_dias: int,
    ativo: bool,
) -> tuple[SerieEditorial | None, str | None]:
    """Cria ou atualiza série. Retorna (serie, None) ou (None, mensagem_erro)."""
    if not nome or not tema:
        return None, "Nome e tema são obrigatórios."
    try:
        if serie_id:
            serie = SerieEditorial.query.filter_by(id=serie_id).first()
            if not serie:
                return None, "Série não encontrada."
        else:
            serie = SerieEditorial()
            db.session.add(serie)
        serie.nome = nome
        serie.tema = tema
        serie.objetivo_lead = objetivo_lead or None
        serie.cta_base = cta_base or None
        serie.descricao = descricao or None
        serie.cadencia_dias = max(1, cadencia_dias)
        serie.ativo = ativo
        db.session.commit()
        return serie, None
    except Exception as e:
        db.session.rollback()
        return None, str(e)


def toggle_serie_ativo(serie_id: int) -> tuple[bool, str | None]:
    """Alterna ativo da série. Retorna (True, None) ou (False, mensagem_erro)."""
    try:
        serie = SerieEditorial.query.filter_by(id=serie_id).first()
        if not serie:
            return False, "Série não encontrada."
        serie.ativo = not bool(serie.ativo)
        db.session.commit()
        return True, None
    except Exception as e:
        db.session.rollback()
        return False, str(e)


def listar_itens_serie(serie_id: int) -> list[SerieItemEditorial]:
    """Lista itens da série ordenados por data_planejada, ordem e id."""
    return (
        SerieItemEditorial.query.filter_by(serie_id=serie_id)
        .order_by(
            SerieItemEditorial.data_planejada.asc(),
            SerieItemEditorial.ordem.asc(),
            SerieItemEditorial.id.asc(),
        )
        .all()
    )


def obter_item_serie(serie_id: int, item_id: int) -> SerieItemEditorial | None:
    """Retorna item da série ou None."""
    return SerieItemEditorial.query.filter_by(
        id=item_id, serie_id=serie_id
    ).first()


def salvar_item_serie(
    serie_id: int,
    item_id: int | None,
    ordem: int,
    titulo_planejado: str,
    subtitulo_planejado: str,
    data_planejada: datetime | None,
    status: str,
) -> tuple[SerieItemEditorial | None, str | None]:
    """Cria ou atualiza item da série. status é normalizado. Retorna (item, None) ou (None, erro)."""
    from app.utils.validators import status_item_serie_valido
    status = status if status_item_serie_valido(status) else "planejado"
    try:
        serie = SerieEditorial.query.filter_by(id=serie_id).first()
        if not serie:
            return None, "Série não encontrada."
        if item_id:
            item = SerieItemEditorial.query.filter_by(
                id=item_id, serie_id=serie_id
            ).first()
            if not item:
                return None, "Item não encontrado."
        else:
            item = SerieItemEditorial(serie_id=serie_id)
            db.session.add(item)
        item.ordem = max(1, ordem)
        item.titulo_planejado = titulo_planejado or None
        item.subtitulo_planejado = subtitulo_planejado or None
        item.data_planejada = data_planejada
        item.status = status
        db.session.commit()
        return item, None
    except Exception as e:
        db.session.rollback()
        return None, str(e)


def reabrir_item(
    actor_email: str | None,
    serie_id: int,
    item_id: int,
    motivo: str,
) -> tuple[bool, str | None]:
    """Reabre item para planejado via máquina de estados. Retorna (ok, mensagem_erro)."""
    item = obter_item_serie(serie_id, item_id)
    if not item:
        return False, "Item não encontrado."
    estado_antes = {
        "status": item.status,
        "data_planejada": item.data_planejada.isoformat() if item.data_planejada else None,
    }
    ok = atualizar_status_item(item.id, "planejado", motivo=motivo)
    if ok:
        db.session.refresh(item)
        estado_depois = {
            "status": item.status,
            "data_planejada": item.data_planejada.isoformat() if item.data_planejada else None,
        }
        registrar_auditoria_admin(
            actor_email,
            "admin_operacao",
            "Reabrir item de série para planejado",
            "serie_item",
            item.id,
            estado_antes,
            estado_depois,
            motivo,
            "sucesso",
        )
        return True, None
    registrar_auditoria_admin(
        actor_email,
        "admin_operacao",
        "Reabertura de item de série bloqueada pela máquina de estados",
        "serie_item",
        item.id,
        estado_antes,
        None,
        motivo,
        "ignorado",
    )
    return False, "Transição de status inválida para o item selecionado."


def pular_item(
    actor_email: str | None,
    serie_id: int,
    item_id: int,
    motivo: str,
) -> tuple[bool, str | None]:
    """Marca item como pulado. Retorna (ok, mensagem_erro)."""
    item = obter_item_serie(serie_id, item_id)
    if not item:
        return False, "Item não encontrado."
    estado_antes = {
        "status": item.status,
        "data_planejada": item.data_planejada.isoformat() if item.data_planejada else None,
    }
    ok = atualizar_status_item(item.id, "pulado", motivo=motivo)
    if ok:
        db.session.refresh(item)
        estado_depois = {
            "status": item.status,
            "data_planejada": item.data_planejada.isoformat() if item.data_planejada else None,
        }
        registrar_auditoria_admin(
            actor_email,
            "admin_operacao",
            "Pular item de série",
            "serie_item",
            item.id,
            estado_antes,
            estado_depois,
            motivo,
            "sucesso",
        )
        return True, None
    registrar_auditoria_admin(
        actor_email,
        "admin_operacao",
        "Marcação de item como pulado bloqueada pela máquina de estados",
        "serie_item",
        item.id,
        estado_antes,
        None,
        motivo,
        "ignorado",
    )
    return False, "Transição de status inválida para o item selecionado."


def vincular_pauta_item(
    actor_email: str | None,
    serie_id: int,
    item_id: int,
    pauta_id: int,
    motivo: str,
) -> tuple[bool, str | None]:
    """
    Vincula pauta ao item. Valida tipo artigo, não arquivada, item não publicado.
    Retorna (True, None) ou (False, mensagem_erro).
    """
    item = obter_item_serie(serie_id, item_id)
    if not item:
        return False, "Item não encontrado."
    pauta = Pauta.query.filter_by(id=pauta_id).first()
    if not pauta:
        return False, "Pauta não encontrada."
    if pauta.tipo != "artigo":
        return False, "Somente pautas do tipo artigo podem ser vinculadas a itens de série."
    if getattr(pauta, "arquivada", False):
        return False, "Pauta arquivada não pode ser vinculada."
    if item.status == "publicado":
        return False, "Item em estado publicado não pode receber novo vínculo de pauta."
    existente = SerieItemEditorial.query.filter(
        SerieItemEditorial.pauta_id == pauta.id,
        SerieItemEditorial.id != item.id,
    ).first()
    if existente:
        registrar_auditoria_admin(
            actor_email,
            "admin_vinculo",
            "Tentativa de vínculo de pauta já vinculada a outro item",
            "serie_item",
            item.id,
            {"pauta_id": item.pauta_id},
            None,
            motivo,
            "ignorado",
        )
        return False, "Pauta já está vinculada a outro item de série."
    estado_antes = {"pauta_id": item.pauta_id}
    if item.pauta_id == pauta.id:
        registrar_auditoria_admin(
            actor_email,
            "admin_vinculo",
            "Vínculo de pauta já existente (idempotente)",
            "serie_item",
            item.id,
            estado_antes,
            estado_antes,
            motivo,
            "sucesso",
        )
        return True, "idempotente"
    item.pauta_id = pauta.id
    db.session.commit()
    estado_depois = {"pauta_id": item.pauta_id}
    registrar_auditoria_admin(
        actor_email,
        "admin_vinculo",
        "Vincular pauta a item de série",
        "serie_item",
        item.id,
        estado_antes,
        estado_depois,
        motivo,
        "sucesso",
    )
    return True, None


def desvincular_pauta_item(
    actor_email: str | None, serie_id: int, item_id: int, motivo: str
) -> tuple[bool, str | None]:
    """Desvincula pauta do item. Item publicado não pode ser desvinculado."""
    item = obter_item_serie(serie_id, item_id)
    if not item:
        return False, "Item não encontrado."
    if not item.pauta_id:
        return True, "idempotente"
    if item.status == "publicado":
        return False, "Item publicado não pode ter vínculo de pauta removido."
    estado_antes = {"pauta_id": item.pauta_id}
    item.pauta_id = None
    db.session.commit()
    estado_depois = {"pauta_id": item.pauta_id}
    registrar_auditoria_admin(
        actor_email,
        "admin_vinculo",
        "Desvincular pauta de item de série",
        "serie_item",
        item.id,
        estado_antes,
        estado_depois,
        motivo,
        "sucesso",
    )
    return True, None
