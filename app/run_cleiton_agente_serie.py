"""
Cleiton - Serviço de Série Editorial.

Responsável por:
- seleção determinística de itens de série;
- preparação de pauta a partir de item de série;
- atualizações de status do item com auditoria.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Literal, Tuple, Optional, Dict, Set

from app.extensions import db
from app.models import SerieEditorial, SerieItemEditorial, Pauta, NoticiaPortal
from app.run_julia_regras import status_verificacao_permitidos
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar

logger = logging.getLogger(__name__)

MotivoSelecao = Literal["serie_dia", "serie_atrasada"]

# Estados possíveis do item de série.
STATUS_PLANEJADO = "planejado"
STATUS_EM_ANDAMENTO = "em_andamento"
STATUS_PUBLICADO = "publicado"
STATUS_FALHA = "falha"
STATUS_PULADO = "pulado"

# Tabela central de transições permitidas.
ESTADOS_PERMITIDOS: Set[str] = {
    STATUS_PLANEJADO,
    STATUS_EM_ANDAMENTO,
    STATUS_PUBLICADO,
    STATUS_FALHA,
    STATUS_PULADO,
}

TRANSICOES_PERMITIDAS: Dict[str, Set[str]] = {
    STATUS_PLANEJADO: {STATUS_EM_ANDAMENTO, STATUS_PULADO},
    STATUS_EM_ANDAMENTO: {STATUS_PUBLICADO, STATUS_FALHA, STATUS_PLANEJADO},
    STATUS_FALHA: {STATUS_PLANEJADO, STATUS_PULADO},
    STATUS_PULADO: {STATUS_PLANEJADO},
    STATUS_PUBLICADO: set(),  # terminal
}


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pode_transicionar_status(atual: str | None, novo: str) -> bool:
    """
    Valida transição de estado de item de série conforme a máquina de estados.
    Estados não reconhecidos são tratados como inválidos para fins de transição.
    """
    if novo not in ESTADOS_PERMITIDOS:
        return False
    atual_normalizado = (atual or STATUS_PLANEJADO).strip() or STATUS_PLANEJADO
    if atual_normalizado not in ESTADOS_PERMITIDOS:
        return False
    # Transição "para si mesmo" é considerada neutra e permitida para idempotência.
    if atual_normalizado == novo:
        return True
    return novo in TRANSICOES_PERMITIDAS.get(atual_normalizado, set())


def selecionar_item_para_missao() -> Tuple[Optional[SerieItemEditorial], Optional[MotivoSelecao]]:
    """
    Seleciona item de série elegível para artigo diário.
    Regra:
      1) séries ativas;
      2) itens com status 'planejado' e data_planejada <= hoje;
      3) ordenação determinística: data_planejada ASC, ordem ASC, id ASC;
      4) motivo = 'serie_dia' quando data_planejada == hoje (dia UTC), senão 'serie_atrasada'.
    """
    hoje = _utcnow_naive().date()
    try:
        q = (
            SerieItemEditorial.query.join(
                SerieEditorial, SerieItemEditorial.serie_id == SerieEditorial.id
            )
            .filter(
                SerieEditorial.ativo.is_(True),
                SerieItemEditorial.status == "planejado",
                SerieItemEditorial.data_planejada.isnot(None),
                SerieItemEditorial.data_planejada <= _utcnow_naive(),
            )
            .order_by(
                SerieItemEditorial.data_planejada.asc(),
                SerieItemEditorial.ordem.asc(),
                SerieItemEditorial.id.asc(),
            )
        )
        item = q.first()
        if not item:
            return None, None
        data_item = (item.data_planejada or _utcnow_naive()).date()
        motivo: MotivoSelecao = "serie_dia" if data_item == hoje else "serie_atrasada"
        return item, motivo
    except Exception as e:
        logger.warning("Falha ao selecionar item de série: %s", e)
        return None, None


def preparar_pauta_para_item(item: SerieItemEditorial) -> Optional[Pauta]:
    """
    Garante existência de pauta de artigo elegível para Júlia a partir de um item de série.
    Regras:
      - tipo='artigo', status='pendente', status_verificacao em status_permitidos;
      - fonte_tipo='manual';
      - status do item -> 'em_andamento'.
    """
    if not item:
        return None
    status_permitidos = status_verificacao_permitidos()
    try:
        if not _pode_transicionar_status(item.status, STATUS_EM_ANDAMENTO):
            auditoria_registrar(
                tipo_decisao="orquestracao",
                decisao="Item de série inelegível para preparo de pauta",
                contexto={
                    "serie_id": item.serie_id,
                    "serie_item_id": item.id,
                    "status_atual": item.status,
                },
                resultado="ignorado",
                detalhe="Transição para em_andamento bloqueada pela máquina de estados.",
            )
            return None

        pauta = None
        if item.pauta_id:
            pauta = db.session.get(Pauta, item.pauta_id)
        if pauta is None:
            serie = db.session.get(SerieEditorial, item.serie_id) if item.serie_id else None
            titulo = (item.titulo_planejado or (serie.tema if serie else "") or "Artigo editorial").strip()
            fonte = (serie.nome if serie else "Série editorial").strip()
            # URL interna técnica para rastreio; não é regra de negócio de front.
            link = f"/serie/{item.serie_id or '0'}/item/{item.id}"
            pauta = Pauta(
                titulo_original=titulo[:500],
                fonte=fonte[:200],
                link=link,
                tipo="artigo",
                status="pendente",
                status_verificacao=status_permitidos[0],
                fonte_tipo="manual",
            )
            db.session.add(pauta)
            db.session.flush()
            item.pauta_id = pauta.id

        pauta.tipo = "artigo"
        pauta.status = "pendente"
        if pauta.status_verificacao not in status_permitidos:
            pauta.status_verificacao = status_permitidos[0]
        item.status = STATUS_EM_ANDAMENTO
        db.session.commit()

        auditoria_registrar(
            tipo_decisao="orquestracao",
            decisao="Pauta preparada a partir de item de série",
            contexto={
                "serie_id": item.serie_id,
                "serie_item_id": item.id,
                "pauta_id": pauta.id,
            },
            resultado="sucesso",
        )
        return pauta
    except Exception as e:
        logger.warning("Falha ao preparar pauta para item de série id=%s: %s", getattr(item, "id", None), e)
        try:
            db.session.rollback()
        except Exception:
            pass
        auditoria_registrar(
            tipo_decisao="orquestracao",
            decisao="Falha ao preparar pauta a partir de item de série",
            contexto={
                "serie_id": getattr(item, "serie_id", None),
                "serie_item_id": getattr(item, "id", None),
            },
            resultado="falha",
            detalhe=str(e),
        )
        return None


def atualizar_status_item(
    item_id: int,
    novo_status: str,
    noticia_id: int | None = None,
    motivo: str | None = None,
) -> bool:
    """
    Atualiza status do item de série com integridade transacional e auditoria.
    Ciclo de vida suportado (máquina de estados):
      - planejado -> em_andamento | pulado
      - em_andamento -> publicado | falha | planejado
      - falha -> planejado | pulado
      - pulado -> planejado
      - publicado -> (terminal)

    Retorna True quando a transição foi aplicada; False quando inválida ou em caso de erro.
    """
    novo_status = (novo_status or "").strip()
    if novo_status not in ESTADOS_PERMITIDOS:
        return False
    try:
        item = db.session.get(SerieItemEditorial, item_id)
        if not item:
            return False
        antigo = item.status or "desconhecido"
        if not _pode_transicionar_status(item.status, novo_status):
            # Transição inválida: registra auditoria, mas não altera estado.
            auditoria_registrar(
                tipo_decisao="orquestracao",
                decisao=f"Transição inválida item série {antigo}->{novo_status}",
                contexto={
                    "serie_id": item.serie_id,
                    "serie_item_id": item.id,
                    "noticia_id": item.noticia_id,
                },
                resultado="ignorado",
                detalhe=motivo or "Transição bloqueada pela máquina de estados.",
            )
            return False
        item.status = novo_status
        if noticia_id is not None:
            item.noticia_id = noticia_id
        db.session.commit()
        auditoria_registrar(
            tipo_decisao="orquestracao",
            decisao=f"Transição de status item série {antigo}->{novo_status}",
            contexto={
                "serie_id": item.serie_id,
                "serie_item_id": item.id,
                "noticia_id": item.noticia_id,
            },
            resultado="sucesso",
            detalhe=motivo,
        )
        return True
    except Exception as e:
        logger.warning("Falha ao atualizar status do item de série id=%s: %s", item_id, e)
        try:
            db.session.rollback()
        except Exception:
            pass
        auditoria_registrar(
            tipo_decisao="orquestracao",
            decisao="Falha ao atualizar status do item de série",
            contexto={"serie_item_id": item_id, "novo_status": novo_status},
            resultado="falha",
            detalhe=str(e),
        )
        return False


def reconciliar_itens_orfaos(agora: datetime | None = None) -> dict:
    """
    Rotina idempotente de reconciliação de itens de série "órfãos".

    Cenários tratados:
      - item em_andamento sem pauta_id e data_planejada já passada -> marca como falha.
      - item em_andamento com pauta inexistente -> marca como falha.
      - item em_andamento com pauta em status final:
          - pauta.publicada -> item.publicado (e tenta vincular noticia_id).
          - pauta.falha -> item.falha.
      - item publicado sem noticia_id -> marca como falha (inconsistência).
    Cada correção registra auditoria com antes/depois.
    """
    agora = agora or _utcnow_naive()
    hoje = (agora or _utcnow_naive()).date()
    stats = {
        "em_andamento_sem_pauta": 0,
        "em_andamento_pauta_inexistente": 0,
        "em_andamento_pauta_publicada": 0,
        "em_andamento_pauta_falha": 0,
        "publicado_sem_noticia": 0,
    }
    try:
        itens = SerieItemEditorial.query.all()
    except Exception as e:
        logger.warning("Falha ao listar itens de série para reconciliação: %s", e)
        return stats

    for item in itens:
        try:
            status_atual = item.status or STATUS_PLANEJADO
            # 1) em_andamento sem pauta_id e data_planejada passada
            if status_atual == STATUS_EM_ANDAMENTO and not item.pauta_id:
                if item.data_planejada and item.data_planejada.date() < hoje:
                    ok = atualizar_status_item(
                        item.id,
                        STATUS_FALHA,
                        motivo="Reconciliação: item em_andamento sem pauta e data_planejada passada.",
                    )
                    if ok:
                        stats["em_andamento_sem_pauta"] += 1
                continue

            # 2) em_andamento com pauta inexistente
            if status_atual == STATUS_EM_ANDAMENTO and item.pauta_id:
                pauta = db.session.get(Pauta, item.pauta_id)
                if pauta is None:
                    ok = atualizar_status_item(
                        item.id,
                        STATUS_FALHA,
                        motivo="Reconciliação: pauta inexistente para item em_andamento.",
                    )
                    if ok:
                        stats["em_andamento_pauta_inexistente"] += 1
                    continue

                # 3) em_andamento com pauta em status final incompatível
                if pauta.status == "publicada":
                    # Tenta vincular noticia_id se ainda não houver, usando link da pauta.
                    noticia_id = item.noticia_id
                    if noticia_id is None:
                        try:
                            n = NoticiaPortal.query.filter_by(link=pauta.link).first()
                            if n:
                                noticia_id = n.id
                        except Exception:
                            noticia_id = None
                    ok = atualizar_status_item(
                        item.id,
                        STATUS_PUBLICADO,
                        noticia_id=noticia_id,
                        motivo="Reconciliação: pauta publicada, item marcado como publicado.",
                    )
                    if ok:
                        stats["em_andamento_pauta_publicada"] += 1
                    continue

                if pauta.status == "falha":
                    ok = atualizar_status_item(
                        item.id,
                        STATUS_FALHA,
                        motivo="Reconciliação: pauta em falha, item marcado como falha.",
                    )
                    if ok:
                        stats["em_andamento_pauta_falha"] += 1
                    continue

            # 4) item publicado sem noticia_id
            if status_atual == STATUS_PUBLICADO and not item.noticia_id:
                # Exceção controlada de reconciliação: corrige inconsistência terminal legada.
                antigo = item.status
                item.status = STATUS_FALHA
                db.session.commit()
                auditoria_registrar(
                    tipo_decisao="orquestracao",
                    decisao=f"Reconciliação forçada item série {antigo}->{STATUS_FALHA}",
                    contexto={
                        "serie_id": item.serie_id,
                        "serie_item_id": item.id,
                        "noticia_id": item.noticia_id,
                    },
                    resultado="sucesso",
                    detalhe="Item publicado sem noticia_id; marcado como falha para correção operacional.",
                )
                stats["publicado_sem_noticia"] += 1
        except Exception as e:
            logger.warning("Falha ao reconciliar item de série id=%s: %s", getattr(item, "id", None), e)
            try:
                db.session.rollback()
            except Exception:
                pass
    return stats


def replanejar_itens_atrasados_e_falhos(agora: datetime | None = None) -> dict:
    """
    Política determinística de replanejamento:
      - Itens atrasados (status=planejado e data_planejada < hoje):
          -> ajusta data_planejada para hoje (mantém status).
      - Itens em falha (status=falha):
          -> reabertos para planejado e data_planejada = hoje + cadencia_dias da série.

    Retorna dict com contadores de replanejamentos aplicados.
    """
    agora = agora or _utcnow_naive()
    hoje = (agora or _utcnow_naive()).date()
    stats = {
        "planejados_replanejados": 0,
        "falha_reabertos_planejado": 0,
    }
    try:
        itens = SerieItemEditorial.query.all()
    except Exception as e:
        logger.warning("Falha ao listar itens de série para replanejamento: %s", e)
        return stats

    for item in itens:
        try:
            status_atual = item.status or STATUS_PLANEJADO
            # 1) planejado atrasado
            if (
                status_atual == STATUS_PLANEJADO
                and item.data_planejada is not None
                and item.data_planejada.date() < hoje
            ):
                antiga = item.data_planejada
                # Move data_planejada para o início do dia atual (determinístico).
                item.data_planejada = datetime(
                    hoje.year,
                    hoje.month,
                    hoje.day,
                    antiga.hour,
                    antiga.minute,
                    antiga.second,
                    antiga.microsecond,
                )
                db.session.commit()
                auditoria_registrar(
                    tipo_decisao="orquestracao",
                    decisao="Replanejamento item série (atrasado)",
                    contexto={
                        "serie_id": item.serie_id,
                        "serie_item_id": item.id,
                        "data_planejada_anterior": antiga.isoformat(),
                        "data_planejada_nova": item.data_planejada.isoformat(),
                    },
                    resultado="sucesso",
                )
                stats["planejados_replanejados"] += 1
                continue

            # 2) falha -> planejado + nova data com base na cadência da série
            if status_atual == STATUS_FALHA:
                serie = db.session.get(SerieEditorial, item.serie_id) if item.serie_id else None
                cadencia = max(1, int(getattr(serie, "cadencia_dias", 1) or 1))
                nova_data = datetime(hoje.year, hoje.month, hoje.day) + timedelta(days=cadencia)
                antiga_data = item.data_planejada
                item.data_planejada = nova_data
                db.session.commit()
                ok = atualizar_status_item(
                    item.id,
                    STATUS_PLANEJADO,
                    motivo="Replanejamento item série (falha -> planejado).",
                )
                if ok:
                    stats["falha_reabertos_planejado"] += 1
                    auditoria_registrar(
                        tipo_decisao="orquestracao",
                        decisao="Replanejamento item série (falha)",
                        contexto={
                            "serie_id": item.serie_id,
                            "serie_item_id": item.id,
                            "data_planejada_anterior": antiga_data.isoformat() if antiga_data else None,
                            "data_planejada_nova": nova_data.isoformat(),
                        },
                        resultado="sucesso",
                    )
        except Exception as e:
            logger.warning("Falha ao replanejar item de série id=%s: %s", getattr(item, "id", None), e)
            try:
                db.session.rollback()
            except Exception:
                pass
    return stats

