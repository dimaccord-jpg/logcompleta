"""
Cleiton - Agente Orquestrador: cérebro gerencial.
Lê plano estratégico ativo, aplica regras (frequência, prioridade, janela, retries),
registra auditoria e dispara jobs para agentes especializados. Nunca escreve conteúdo final.
"""
import logging
import json
from datetime import datetime, timezone
from typing import Any, Optional
from app.extensions import db
from app.models import (
    PlanoEstrategico,
    NoticiaPortal,
    AuditoriaGerencial,
    Pauta,
    MissaoAgente,
)
from app.run_cleiton_agente_regras import (
    get_prioridade_padrao,
    get_janela_publicacao,
    dentro_janela_publicacao,
    get_max_retries,
    pode_executar_por_frequencia,
    bootstrap_regras,
    get_max_tentativas_artigo_dia,
    get_evergreen_automatico_habilitado,
    get_evergreen_frequencia_minutos,
)
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar
from app.services.pauta_service import (
    arquivar_pautas_automaticas_vencidas,
    obter_pauta_artigo_elegivel_por_id,
    query_pautas_artigo_elegiveis_legado_automatico,
    selecionar_pauta_evergreen_automatico,
)
from app.run_cleiton_agente_serie import (
    selecionar_item_para_missao,
    preparar_pauta_para_item,
)
from app.run_cleiton_agente_dispatcher import (
    construir_payload,
    registrar_missao,
    despachar,
)

logger = logging.getLogger(__name__)

# Origem/cadência identificável na auditoria (SCRUM-215B). Não mistura com ciclo legado.
ORIGEM_EVERGREEN_AUTOMATICO = "evergreen_automatico"
TIPO_DECISAO_EVERGREEN_AUTOMATICO = "evergreen_automatico"


def _detalhe_falha_dispatch_julia(mission_id: str | None) -> str | None:
    if not mission_id:
        return None
    try:
        rows = (
            AuditoriaGerencial.query.filter(
                AuditoriaGerencial.tipo_decisao == "julia",
                AuditoriaGerencial.contexto_json.like(f'%"{mission_id}"%'),
            )
            .order_by(AuditoriaGerencial.id.desc())
            .limit(10)
            .all()
        )
        for row in rows:
            contexto = {}
            if row.contexto_json:
                try:
                    contexto = json.loads(row.contexto_json)
                except Exception:
                    contexto = {}
            if row.decisao == "Fallback de redação bloqueado antes da publicação":
                motivo = (contexto.get("redacao_motivo") or "unknown").strip() or "unknown"
                return f"Falha de redação Júlia: {motivo}."
            if row.decisao == "Falha na redação":
                tipo_retorno = (contexto.get("tipo_retorno") or "").strip()
                if tipo_retorno:
                    return f"Falha de redação Júlia: retorno inválido ({tipo_retorno})."
                return "Falha de redação Júlia: conteúdo vazio ou inválido."
            if row.decisao == "Erro inesperado no pipeline":
                detalhe = (row.detalhe or "").strip()
                if detalhe:
                    return f"Erro inesperado no pipeline da Júlia: {detalhe}"
                return "Erro inesperado no pipeline da Júlia."
    except Exception:
        logger.exception("Falha ao enriquecer motivo da missão %s", mission_id)
    return None


def _contexto_indica_bypass_frequencia(contexto_json: str | None) -> bool:
    """Retorna True quando o contexto da auditoria indica bypass manual da frequência."""
    if not contexto_json:
        return False
    try:
        data = json.loads(contexto_json)
        return isinstance(data, dict) and bool(data.get("bypass_frequencia"))
    except Exception:
        return False


def _contexto_indica_evergreen_automatico(contexto_json: str | None) -> bool:
    """True quando o registro pertence à cadência evergreen automática."""
    if not contexto_json:
        return False
    try:
        data = json.loads(contexto_json)
        if not isinstance(data, dict):
            return False
        origem = str(data.get("origem") or data.get("cadencia") or "").strip().lower()
        return origem == ORIGEM_EVERGREEN_AUTOMATICO
    except Exception:
        return False


def _contexto_orquestracao(base: dict | None, bypass_frequencia: bool) -> dict:
    """Garante metadado de bypass no contexto para preservar rastreabilidade."""
    contexto = dict(base or {})
    if bypass_frequencia:
        contexto["bypass_frequencia"] = True
    return contexto


def _contexto_evergreen_automatico(base: dict | None = None) -> dict:
    contexto = dict(base or {})
    contexto["origem"] = ORIGEM_EVERGREEN_AUTOMATICO
    contexto["cadencia"] = ORIGEM_EVERGREEN_AUTOMATICO
    contexto["intencao_editorial"] = "evergreen"
    return contexto


def obter_plano_ativo() -> PlanoEstrategico | None:
    """Retorna o plano estratégico ativo (tema, objetivo, estágio)."""
    try:
        return PlanoEstrategico.query.filter_by(ativo=True).order_by(PlanoEstrategico.updated_at.desc()).first()
    except Exception as e:
        logger.warning("Falha ao obter plano ativo: %s", e)
        return None


def ultima_auditoria_orquestracao() -> datetime | None:
    """Data/hora da última execução efetiva de orquestração válida para frequência."""
    try:
        registros = (
            AuditoriaGerencial.query.filter_by(tipo_decisao="orquestracao")
            .order_by(AuditoriaGerencial.created_at.desc())
            .limit(200)
            .all()
        )
        for r in registros:
            if (r.resultado or "").strip().lower() == "ignorado":
                continue
            if _contexto_indica_bypass_frequencia(r.contexto_json):
                continue
            if _contexto_indica_evergreen_automatico(r.contexto_json):
                continue
            return r.created_at
        return None
    except Exception:
        return None


def ultima_auditoria_evergreen_automatico() -> datetime | None:
    """
    Última execução automática evergreen bem-sucedida.
    Independente do ciclo legado; ignora manual, news e analysis.
    """
    try:
        registros = (
            AuditoriaGerencial.query.filter_by(
                tipo_decisao=TIPO_DECISAO_EVERGREEN_AUTOMATICO
            )
            .order_by(AuditoriaGerencial.created_at.desc())
            .limit(200)
            .all()
        )
        for r in registros:
            if (r.resultado or "").strip().lower() != "sucesso":
                continue
            if not _contexto_indica_evergreen_automatico(r.contexto_json):
                continue
            return r.created_at
        return None
    except Exception:
        return None


def pode_executar_evergreen_por_frequencia(
    ultima_execucao: datetime | None, agora: datetime | None = None
) -> bool:
    """True se o intervalo evergreen_frequencia_minutos já venceu (ou nunca executou)."""
    if ultima_execucao is None:
        return True
    t = agora or _utcnow_naive()
    delta = t - ultima_execucao
    return delta.total_seconds() >= get_evergreen_frequencia_minutos() * 60


def _utcnow_naive() -> datetime:
    """Retorna datetime UTC naive para comparações de data."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _artigo_publicado_hoje() -> bool:
    """
    Verdade única para 'artigo publicado hoje'.
    Considera apenas status_publicacao publicado/parcial e publicado_em no dia atual.
    """
    hoje_inicio = _utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    return bool(
        NoticiaPortal.query.filter(
            NoticiaPortal.tipo == "artigo",
            NoticiaPortal.status_publicacao.in_(["publicado", "parcial"]),
            NoticiaPortal.publicado_em >= hoje_inicio,
        ).first()
    )


def _intencao_editorial_da_missao(missao: MissaoAgente) -> str | None:
    """Extrai intencao_editorial do payload persistido da missão, se houver."""
    bruto = missao.payload_metadados
    if not bruto:
        return None
    try:
        payload = json.loads(bruto)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    valor = payload.get("intencao_editorial")
    if not valor and isinstance(payload.get("metadados"), dict):
        valor = payload["metadados"].get("intencao_editorial")
    if isinstance(valor, str) and valor.strip():
        return valor.strip().lower()
    return None


def _tentativas_artigo_hoje() -> int:
    """
    Conta missões de artigo do fluxo legado disparadas hoje.
    Exclui evergreen (automático ou manual) para não bloquear analysis.
    """
    hoje_inicio = _utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        missoes = (
            MissaoAgente.query.filter(
                MissaoAgente.tipo_missao == "artigo",
                MissaoAgente.created_at >= hoje_inicio,
            ).all()
        )
        total = 0
        for missao in missoes:
            if _intencao_editorial_da_missao(missao) == "evergreen":
                continue
            total += 1
        return total
    except Exception:
        return 0


def _buscar_pauta_manual_artigo() -> Pauta | None:
    """Busca a pauta manual de artigo mais antiga elegível para o ciclo legado."""
    try:
        return (
            query_pautas_artigo_elegiveis_legado_automatico()
            .order_by(Pauta.created_at.asc())
            .first()
        )
    except Exception:
        return None


def decidir_tipo_missao() -> str:
    """
    Decide se a missão será artigo ou notícia.
    Regra atual:
    - Se ainda não houve artigo hoje e existe pauta de artigo elegível (pendente e aprovada pelo Verificador),
      prioriza artigo.
    - Caso contrário, prioriza notícia automática.
    Deve ser chamada dentro de app_context. Não gera conteúdo; apenas decide o tipo.
    Não inclui evergreen em decidir_tipo_missao (cadência independente em 215B).
    """
    tem_artigo_hoje = _artigo_publicado_hoje()
    arquivar_pautas_automaticas_vencidas()

    tem_artigo_backlog = (
        query_pautas_artigo_elegiveis_legado_automatico().first()
    )

    # Série editorial ativa também conta como fonte elegível para artigo do dia.
    item_serie, _motivo = selecionar_item_para_missao()
    tem_item_serie = bool(item_serie)

    if not tem_artigo_hoje and (tem_artigo_backlog or tem_item_serie):
        return "artigo"
    return "noticia"


def executar_retencao(app_flask) -> None:
    """Executa política de retenção (18 meses dados, 2 meses imagens) e registra purge na auditoria."""
    try:
        from app.run_cleiton_agente_retencao import executar_limpeza_retencao
        executar_limpeza_retencao(app_flask)
    except ImportError:
        logger.debug("Módulo run_cleiton_agente_retencao não disponível; retenção ignorada neste ciclo.")
    except Exception as e:
        logger.exception("Erro na execução da retenção: %s", e)
        auditoria_registrar(
            tipo_decisao="orquestracao",
            decisao="Retenção falhou",
            contexto={},
            resultado="falha",
            detalhe=str(e),
        )


def bootstrap_plano_se_necessario() -> None:
    """Cria um plano ativo padrão se não existir nenhum (idempotente)."""
    try:
        if PlanoEstrategico.query.filter_by(ativo=True).first():
            return
        p = PlanoEstrategico(
            tema_serie="portal logística",
            objetivo="Conteúdo editorial de qualidade para o portal",
            estagio_atual="operacao",
            ativo=True,
        )
        db.session.add(p)
        db.session.commit()
        logger.info("Plano estratégico padrão criado.")
    except Exception as e:
        logger.debug("Bootstrap plano: %s", e)
        try:
            db.session.rollback()
        except Exception:
            pass


def _avaliar_e_executar_evergreen_automatico(
    app_flask,
    *,
    ignorar_janela_publicacao: bool = False,
    excluir_pauta_ids: set[int] | frozenset[int] | None = None,
    tema_serie: str = "portal",
    objetivo: str = "conteúdo editorial",
    plano: PlanoEstrategico | None = None,
) -> dict[str, Any]:
    """
    Cadência evergreen independente do ciclo legado (SCRUM-215B).
    Não altera decidir_tipo_missao nem frequencia_minutos legado.
    """
    resultado: dict[str, Any] = {
        "status": "ignorado",
        "motivo": "Evergreen automático não executado.",
        "tipo_missao": None,
        "intencao_editorial": "evergreen",
        "pauta_id": None,
        "mission_id": None,
        "dispatch_ok": None,
        "caminho_usado": "evergreen_automatico",
    }

    if not get_evergreen_automatico_habilitado():
        msg = "Evergreen automático desabilitado."
        auditoria_registrar(
            tipo_decisao=TIPO_DECISAO_EVERGREEN_AUTOMATICO,
            decisao="Evergreen automático desabilitado",
            contexto=_contexto_evergreen_automatico({}),
            resultado="ignorado",
            detalhe=msg,
        )
        resultado["motivo"] = msg
        resultado["caminho_usado"] = "evergreen_desabilitado"
        return resultado

    ultima_ev = ultima_auditoria_evergreen_automatico()
    if not pode_executar_evergreen_por_frequencia(ultima_ev):
        msg = "Evergreen automático não elegível por intervalo."
        auditoria_registrar(
            tipo_decisao=TIPO_DECISAO_EVERGREEN_AUTOMATICO,
            decisao="Evergreen automático não elegível por intervalo",
            contexto=_contexto_evergreen_automatico(
                {
                    "ultima_execucao_automatica": (
                        ultima_ev.isoformat() if ultima_ev else None
                    ),
                    "frequencia_minutos": get_evergreen_frequencia_minutos(),
                }
            ),
            resultado="ignorado",
            detalhe=msg,
        )
        resultado["motivo"] = msg
        resultado["caminho_usado"] = "evergreen_intervalo"
        return resultado

    if not ignorar_janela_publicacao and not dentro_janela_publicacao():
        msg = "Evergreen automático fora da janela de publicação."
        auditoria_registrar(
            tipo_decisao=TIPO_DECISAO_EVERGREEN_AUTOMATICO,
            decisao="Evergreen automático fora da janela de publicação",
            contexto=_contexto_evergreen_automatico(
                {"janela": list(get_janela_publicacao())}
            ),
            resultado="ignorado",
            detalhe=msg,
        )
        resultado["motivo"] = msg
        resultado["fora_janela"] = True
        resultado["caminho_usado"] = "evergreen_fora_janela"
        return resultado

    pauta_escolhida_id: int | None = None
    pauta = selecionar_pauta_evergreen_automatico(excluir_ids=excluir_pauta_ids)
    if not pauta:
        msg = "Evergreen automático sem pauta elegível."
        auditoria_registrar(
            tipo_decisao=TIPO_DECISAO_EVERGREEN_AUTOMATICO,
            decisao="Evergreen automático sem pauta elegível",
            contexto=_contexto_evergreen_automatico({}),
            resultado="ignorado",
            detalhe=msg,
        )
        resultado["motivo"] = msg
        resultado["caminho_usado"] = "evergreen_sem_pauta"
        return resultado

    pauta_escolhida_id = int(pauta.id)
    # Revalida imediatamente antes do dispatch (sem fallback para outra).
    pauta_ainda = obter_pauta_artigo_elegivel_por_id(pauta_escolhida_id)
    if not pauta_ainda:
        msg = (
            f"Pauta {pauta_escolhida_id} perdeu elegibilidade antes da execução "
            "evergreen; sem fallback."
        )
        auditoria_registrar(
            tipo_decisao=TIPO_DECISAO_EVERGREEN_AUTOMATICO,
            decisao="Evergreen automático falhou",
            contexto=_contexto_evergreen_automatico(
                {"pauta_id": pauta_escolhida_id, "motivo": "pauta_inelegivel"}
            ),
            resultado="falha",
            detalhe=msg,
        )
        resultado["status"] = "falha"
        resultado["motivo"] = msg
        resultado["pauta_id"] = pauta_escolhida_id
        resultado["caminho_usado"] = "evergreen_pauta_inelegivel"
        return resultado

    resultado["pauta_id"] = pauta_escolhida_id
    resultado["tipo_missao"] = "artigo"

    inicio_janela, fim_janela = get_janela_publicacao()
    agora = datetime.now()
    janela_inicio = agora.replace(hour=inicio_janela, minute=0, second=0, microsecond=0)
    janela_fim = agora.replace(hour=fim_janela, minute=0, second=0, microsecond=0)
    from datetime import timedelta

    if janela_fim <= janela_inicio:
        janela_fim = janela_fim + timedelta(days=1)

    metadados = {
        "objetivo": objetivo,
        "estagio": plano.estagio_atual if plano else None,
        "pauta_id": pauta_escolhida_id,
        "intencao_editorial": "evergreen",
        "origem": ORIGEM_EVERGREEN_AUTOMATICO,
        "cadencia": ORIGEM_EVERGREEN_AUTOMATICO,
    }
    payload = construir_payload(
        tipo_missao="artigo",
        tema=tema_serie,
        prioridade=get_prioridade_padrao(),
        janela_publicacao_inicio=janela_inicio,
        janela_publicacao_fim=janela_fim,
        tentativa_atual=1,
        metadados=metadados,
    )
    payload["pauta_id"] = pauta_escolhida_id
    payload["intencao_editorial"] = "evergreen"
    resultado["mission_id"] = payload.get("mission_id")
    registrar_missao(payload)

    ok = despachar(payload, app_flask)
    resultado["dispatch_ok"] = bool(ok)
    if not ok:
        msg = (
            _detalhe_falha_dispatch_julia(payload.get("mission_id"))
            or "Despacho evergreen automático falhou."
        )
        auditoria_registrar(
            tipo_decisao=TIPO_DECISAO_EVERGREEN_AUTOMATICO,
            decisao="Evergreen automático falhou",
            contexto=_contexto_evergreen_automatico(
                {
                    "mission_id": payload.get("mission_id"),
                    "pauta_id": pauta_escolhida_id,
                }
            ),
            resultado="falha",
            detalhe=msg,
        )
        resultado["status"] = "falha"
        resultado["motivo"] = msg
        return resultado

    auditoria_registrar(
        tipo_decisao=TIPO_DECISAO_EVERGREEN_AUTOMATICO,
        decisao="Evergreen automático executado",
        contexto=_contexto_evergreen_automatico(
            {
                "mission_id": payload.get("mission_id"),
                "pauta_id": pauta_escolhida_id,
                "tipo_missao": "artigo",
            }
        ),
        resultado="sucesso",
    )
    resultado["status"] = "sucesso"
    resultado["motivo"] = "Evergreen automático despachado com sucesso."
    return resultado


def executar_ciclo_gerencial(
    app_flask,
    bypass_frequencia: bool = False,
    tipo_missao_forcado: str | None = None,
    ignorar_trava_artigo_hoje: bool = False,
    ignorar_janela_publicacao: bool = False,
    consumo_identidade: Optional[dict] = None,
    pauta_id: int | None = None,
    intencao_editorial: str | None = None,
) -> dict[str, Any]:
    """
    Ciclo principal do Cleiton (gerencial):
    1. Garante bootstrap de regras e plano
    2. Lê plano ativo
    3. Verifica frequência e janela
    4. Decide tipo de missão (artigo/noticia)
    5. Registra auditoria
    6. Constrói payload e despacha para agente operacional
    7. Avalia evergreen automático de forma independente (SCRUM-215B)
    8. Executa retenção (purge auditável)
    Nenhuma geração de conteúdo final aqui.

    pauta_id / intencao_editorial: opcionais para execução manual explícita (SCRUM-215A).
    Automação evergreen é avaliação separada no mesmo acionamento
    (não disputa decidir_tipo_missao).
    """
    logger.info("Cleiton orquestrador: iniciando ciclo gerencial.")
    with app_flask.app_context():
        from app.consumo_identidade import ensure_consumo_identidade_no_app_context

        ensure_consumo_identidade_no_app_context(explicit_override=consumo_identidade)
        bootstrap_regras()
        bootstrap_plano_se_necessario()
        plano = obter_plano_ativo()
        tema_serie = (plano.tema_serie if plano else "") or "portal"
        objetivo = (plano.objetivo if plano else "") or "conteúdo editorial"

        # Evergreen automático só no acionamento sem missão/pauta forçadas.
        avaliar_evergreen_auto = tipo_missao_forcado is None and pauta_id is None
        excluir_pauta_ids: set[int] = set()

        resultado: dict[str, Any] = {
            "status": "falha",
            "motivo": "Ciclo não concluído.",
            "bypass_frequencia": bool(bypass_frequencia),
            "fora_janela": False,
            "ignorado_frequencia": False,
            "tipo_missao": None,
            "mission_id": None,
            "dispatch_ok": None,
            "scout": None,
            "verificador": None,
            # Sprint 4: meta diária de artigo
            "artigo_publicado_hoje": _artigo_publicado_hoje(),
            "tentativa_realizada": False,
            "caminho_usado": "erro_ciclo",
            "motivo_final": "Ciclo não concluído.",
        }

        def _finalize_com_evergreen(res: dict[str, Any]) -> dict[str, Any]:
            if not avaliar_evergreen_auto:
                return res
            try:
                res["evergreen_automatico"] = _avaliar_e_executar_evergreen_automatico(
                    app_flask,
                    ignorar_janela_publicacao=ignorar_janela_publicacao,
                    excluir_pauta_ids=excluir_pauta_ids,
                    tema_serie=tema_serie,
                    objetivo=objetivo,
                    plano=plano,
                )
            except Exception as e:
                logger.exception("Evergreen automático falhou de forma inesperada: %s", e)
                auditoria_registrar(
                    tipo_decisao=TIPO_DECISAO_EVERGREEN_AUTOMATICO,
                    decisao="Evergreen automático falhou",
                    contexto=_contexto_evergreen_automatico({}),
                    resultado="falha",
                    detalhe=str(e),
                )
                res["evergreen_automatico"] = {
                    "status": "falha",
                    "motivo": str(e),
                    "caminho_usado": "evergreen_erro_inesperado",
                }
            return res

        ultima = ultima_auditoria_orquestracao()
        if not bypass_frequencia and not pode_executar_por_frequencia(ultima):
            logger.info("Cleiton: ciclo ignorado por frequência (última execução recente).")
            auditoria_registrar(
                tipo_decisao="orquestracao",
                decisao="Ciclo ignorado por frequência",
                contexto=_contexto_orquestracao(
                    {"ultima_execucao": ultima.isoformat() if ultima else None},
                    bypass_frequencia,
                ),
                resultado="ignorado",
            )
            resultado["status"] = "ignorado"
            resultado["motivo"] = "Ciclo ignorado por frequência (última execução recente)."
            resultado["ignorado_frequencia"] = True
            resultado["motivo_final"] = resultado["motivo"]
            resultado["caminho_usado"] = "ignorado_frequencia"
            return _finalize_com_evergreen(resultado)

        if bypass_frequencia:
            auditoria_registrar(
                tipo_decisao="orquestracao",
                decisao="Bypass manual de frequência aplicado",
                contexto=_contexto_orquestracao(
                    {"ultima_execucao": ultima.isoformat() if ultima else None},
                    True,
                ),
                resultado="sucesso",
            )

        if not ignorar_janela_publicacao and not dentro_janela_publicacao():
            logger.info("Cleiton: fora da janela de publicação; ciclo adiado.")
            auditoria_registrar(
                tipo_decisao="orquestracao",
                decisao="Fora da janela de publicação",
                contexto=_contexto_orquestracao(
                    {"janela": list(get_janela_publicacao())},
                    bypass_frequencia,
                ),
                resultado="ignorado",
            )
            resultado["status"] = "ignorado"
            resultado["motivo"] = "Fora da janela de publicação; ciclo não executado."
            resultado["fora_janela"] = True
            resultado["motivo_final"] = resultado["motivo"]
            resultado["caminho_usado"] = "fora_janela_publicacao"
            return _finalize_com_evergreen(resultado)

        # Define tipo de missão base (artigo x notícia) antes de aplicar recomendações.
        if tipo_missao_forcado:
            tipo_missao = str(tipo_missao_forcado).lower()
        else:
            tipo_missao = decidir_tipo_missao()
        resultado["tipo_missao"] = tipo_missao
        tema_efetivo = tema_serie
        prioridade_efetiva = get_prioridade_padrao()
        recomendacao_em_uso = None  # Fase 6: feedback loop estratégico
        item_serie_usado = None
        pauta_id_explicito: int | None = None
        if pauta_id is not None:
            try:
                pauta_id_explicito = int(pauta_id)
            except (TypeError, ValueError):
                resultado["status"] = "falha"
                resultado["motivo"] = "pauta_id inválido."
                resultado["motivo_final"] = resultado["motivo"]
                resultado["caminho_usado"] = "pauta_explicita_invalida"
                return _finalize_com_evergreen(resultado)

        intencao_explicita = None
        if intencao_editorial is not None and str(intencao_editorial).strip():
            intencao_explicita = str(intencao_editorial).strip().lower()

        try:
            from app.run_cleiton_agente_customer_insight import (
                selecionar_recomendacao_prioritaria,
                parse_recomendacao_json,
            )
            rec = selecionar_recomendacao_prioritaria()
            if rec:
                recomendacao_em_uso = rec
                parsed = parse_recomendacao_json(rec.recomendacao)
                if parsed.get("tema_sugerido"):
                    tema_efetivo = str(parsed["tema_sugerido"])[:255]
                # Não sobrescrever tipo_missao quando foi forçado (ex.: botão "Executar artigo agora").
                if not tipo_missao_forcado and parsed.get("tipo") and str(parsed["tipo"]).lower() in ("noticia", "artigo"):
                    tipo_missao = str(parsed["tipo"]).lower()
                    resultado["tipo_missao"] = tipo_missao
                if isinstance(parsed.get("prioridade"), (int, float)):
                    prioridade_efetiva = max(1, min(10, int(parsed["prioridade"])))
                elif rec.prioridade is not None:
                    prioridade_efetiva = max(1, min(10, int(rec.prioridade)))
                logger.info(
                    "Cleiton: recomendação id=%s aplicada ao planejamento (tema=%s tipo=%s prioridade=%s)",
                    rec.id, tema_efetivo, tipo_missao, prioridade_efetiva,
                )
                auditoria_registrar(
                    tipo_decisao="insight",
                    decisao="Recomendação utilizada no planejamento",
                    contexto={"recomendacao_id": rec.id, "tema": tema_efetivo, "tipo_missao": tipo_missao, "prioridade": prioridade_efetiva},
                    resultado="sucesso",
                )
        except Exception as e:
            logger.warning("Falha ao obter/aplicar recomendação pendente (continuando): %s", e)
            auditoria_registrar(
                tipo_decisao="insight",
                decisao="Falha ao aplicar recomendação no planejamento",
                contexto={},
                resultado="falha",
                detalhe=str(e),
            )

        # Limite de tentativas de artigo no dia (evita loop infinito de missão de artigo).
        # Evergreen não entra neste limite (_tentativas_artigo_hoje exclui intencao evergreen).
        if tipo_missao == "artigo" and not ignorar_trava_artigo_hoje:
            tentativas_hoje = _tentativas_artigo_hoje()
            max_tentativas = get_max_tentativas_artigo_dia()
            if tentativas_hoje >= max_tentativas:
                msg = (
                    f"Limite diário de tentativas de artigo atingido "
                    f"({tentativas_hoje}/{max_tentativas}); ciclo não criará nova missão de artigo."
                )
                logger.info("Cleiton: %s", msg)
                auditoria_registrar(
                    tipo_decisao="orquestracao",
                    decisao="Limite diário de tentativas de artigo atingido",
                    contexto={"tentativas_hoje": tentativas_hoje, "max_tentativas": max_tentativas},
                    resultado="ignorado",
                )
                resultado["status"] = "ignorado"
                resultado["motivo"] = msg
                resultado["motivo_final"] = msg
                resultado["caminho_usado"] = "limite_artigo_dia"
                return _finalize_com_evergreen(resultado)

        # Se a missão for artigo, tenta selecionar item de série editorial elegível e preparar pauta.
        if tipo_missao == "artigo":
            fonte_artigo_resolvida = False
            if pauta_id_explicito is not None:
                pauta_explicita = obter_pauta_artigo_elegivel_por_id(pauta_id_explicito)
                if not pauta_explicita:
                    msg = (
                        f"Pauta {pauta_id_explicito} inexistente ou inelegível "
                        "para artigo manual; sem fallback."
                    )
                    logger.warning("Cleiton: %s", msg)
                    auditoria_registrar(
                        tipo_decisao="orquestracao",
                        decisao="Pauta explícita inválida ou inelegível",
                        contexto={"pauta_id": pauta_id_explicito},
                        resultado="falha",
                        detalhe=msg,
                    )
                    resultado["status"] = "falha"
                    resultado["motivo"] = msg
                    resultado["motivo_final"] = msg
                    resultado["caminho_usado"] = "pauta_explicita_invalida"
                    return _finalize_com_evergreen(resultado)
                resultado["caminho_usado"] = "pauta_explicita"
                resultado["pauta_id"] = pauta_explicita.id
                excluir_pauta_ids.add(int(pauta_explicita.id))
                fonte_artigo_resolvida = True
                auditoria_registrar(
                    tipo_decisao="orquestracao",
                    decisao="Pauta explícita selecionada para artigo manual",
                    contexto={"pauta_id": pauta_explicita.id},
                    resultado="sucesso",
                )
            else:
                item_serie, motivo_selecao = selecionar_item_para_missao()
                if item_serie and motivo_selecao:
                    pauta = preparar_pauta_para_item(item_serie)
                    if pauta:
                        item_serie_usado = item_serie
                        resultado["serie_id"] = item_serie.serie_id
                        resultado["serie_item_id"] = item_serie.id
                        resultado["serie_motivo_selecao"] = motivo_selecao
                        # Caminho explícito de artigo via série: série do dia ou atrasada.
                        resultado["caminho_usado"] = motivo_selecao
                        resultado["pauta_id"] = pauta.id
                        excluir_pauta_ids.add(int(pauta.id))
                        fonte_artigo_resolvida = True
                    else:
                        auditoria_registrar(
                            tipo_decisao="orquestracao",
                            decisao="Falha ao preparar pauta de item de série; tentando fallback manual",
                            contexto={"serie_item_id": item_serie.id, "serie_id": item_serie.serie_id},
                            resultado="falha",
                        )

                if not fonte_artigo_resolvida:
                    # Fallback explícito: tentar pauta manual de artigo (sem [evergreen]).
                    pauta_manual = _buscar_pauta_manual_artigo()
                    if pauta_manual:
                        resultado["caminho_usado"] = "pauta_manual"
                        resultado["pauta_id"] = pauta_manual.id
                        excluir_pauta_ids.add(int(pauta_manual.id))
                        fonte_artigo_resolvida = True
                        auditoria_registrar(
                            tipo_decisao="orquestracao",
                            decisao="Fallback para pauta manual de artigo",
                            contexto={
                                "pauta_id": pauta_manual.id,
                            },
                            resultado="sucesso",
                        )
            if not fonte_artigo_resolvida:
                resultado["status"] = "ignorado"
                resultado["motivo"] = "Nenhum item de série ou pauta manual elegível para artigo."
                resultado["motivo_final"] = resultado["motivo"]
                resultado["caminho_usado"] = "sem_fonte_artigo"
                auditoria_registrar(
                    tipo_decisao="orquestracao",
                    decisao="Nenhum item de série ou pauta manual elegível para artigo",
                    contexto={},
                    resultado="ignorado",
                )
                return _finalize_com_evergreen(resultado)
        else:
            # Missão de notícia rápida mantém fluxo legado de notícias automáticas.
            resultado["caminho_usado"] = "noticia_rapida"

        logger.info("Cleiton: missão definida tipo=%s | tema=%s", tipo_missao, tema_efetivo)

        resultado_scout: dict[str, Any] | None = None
        resultado_verificador: dict[str, Any] | None = None

        # Fase 3: Scout (coleta) -> Verificador (só aprovadas vão para Julia)
        try:
            from app.run_cleiton_agente_scout import executar_coleta
            resultado_scout = executar_coleta()
            resultado["scout"] = resultado_scout
        except Exception as e:
            logger.warning("Scout falhou (continuando ciclo): %s", e)
            auditoria_registrar(
                tipo_decisao="scout",
                decisao="Scout falhou",
                contexto={},
                resultado="falha",
                detalhe=str(e),
            )
        try:
            from app.run_cleiton_agente_verificador import executar_verificacao
            resultado_verificador = executar_verificacao()
            resultado["verificador"] = resultado_verificador
        except Exception as e:
            logger.warning("Verificador falhou (continuando ciclo): %s", e)
            auditoria_registrar(
                tipo_decisao="verificador",
                decisao="Verificador falhou",
                contexto={},
                resultado="falha",
                detalhe=str(e),
            )

        inicio_janela, fim_janela = get_janela_publicacao()
        agora = datetime.now()
        janela_inicio = agora.replace(hour=inicio_janela, minute=0, second=0, microsecond=0)
        janela_fim = agora.replace(hour=fim_janela, minute=0, second=0, microsecond=0)
        from datetime import timedelta
        if janela_fim <= janela_inicio:
            janela_fim = janela_fim + timedelta(days=1)

        metadados = {"objetivo": objetivo, "estagio": plano.estagio_atual if plano else None}
        if item_serie_usado:
            metadados["serie_id"] = item_serie_usado.serie_id
            metadados["serie_item_id"] = item_serie_usado.id
        if recomendacao_em_uso:
            metadados["recomendacao_id"] = recomendacao_em_uso.id
            metadados["insight_recomendacao"] = True
        if pauta_id_explicito is not None:
            metadados["pauta_id"] = pauta_id_explicito
        elif resultado.get("pauta_id") is not None:
            metadados["pauta_id"] = resultado["pauta_id"]
        if intencao_explicita:
            metadados["intencao_editorial"] = intencao_explicita
        payload = construir_payload(
            tipo_missao=tipo_missao,
            tema=tema_efetivo,
            prioridade=prioridade_efetiva,
            janela_publicacao_inicio=janela_inicio,
            janela_publicacao_fim=janela_fim,
            tentativa_atual=1,
            metadados=metadados,
        )
        if pauta_id_explicito is not None:
            payload["pauta_id"] = pauta_id_explicito
        elif resultado.get("pauta_id") is not None:
            payload["pauta_id"] = resultado["pauta_id"]
        if intencao_explicita:
            payload["intencao_editorial"] = intencao_explicita
        resultado["mission_id"] = payload.get("mission_id")
        registrar_missao(payload)
        auditoria_registrar(
            tipo_decisao="orquestracao",
            decisao=f"Missão criada tipo={tipo_missao} theme={tema_efetivo}",
            contexto=_contexto_orquestracao(
                {
                    "mission_id": payload.get("mission_id"),
                    "tipo_missao": tipo_missao,
                    "pauta_id": payload.get("pauta_id"),
                    "intencao_editorial": intencao_explicita,
                },
                bypass_frequencia,
            ),
            resultado="sucesso",
        )

        ok = despachar(payload, app_flask)
        resultado["dispatch_ok"] = bool(ok)
        resultado["tentativa_realizada"] = True
        if not ok:
            auditoria_registrar(
                tipo_decisao="orquestracao",
                decisao="Despacho falhou",
                contexto=_contexto_orquestracao(payload, bypass_frequencia),
                resultado="falha",
            )
            resultado["status"] = "falha"
            resultado["motivo"] = (
                _detalhe_falha_dispatch_julia(payload.get("mission_id"))
                or "Despacho para agente operacional falhou ou não houve publicação."
            )
        else:
            resultado["status"] = "sucesso"
            resultado["motivo"] = "Missão despachada com sucesso e agente operacional publicou conteúdo."
            if tipo_missao == "artigo":
                resultado["caminho_usado"] = "artigo"

        # Atualiza campos Sprint 4
        resultado["artigo_publicado_hoje"] = _artigo_publicado_hoje()
        resultado["motivo_final"] = resultado["motivo"]
        # Fase 6: se missão sucesso e havia recomendação em uso, marcar como aplicada
        if ok and recomendacao_em_uso:
            try:
                from app.run_cleiton_agente_customer_insight import atualizar_status_recomendacao
                atualizar_status_recomendacao(
                    recomendacao_em_uso.id,
                    "aplicada",
                    app_flask,
                    detalhe="Aplicada no dispatch com sucesso",
                )
            except Exception as e:
                logger.warning("Falha ao marcar recomendação como aplicada: %s", e)
                auditoria_registrar(
                    tipo_decisao="insight",
                    decisao="Falha ao marcar recomendação aplicada",
                    contexto={"recomendacao_id": recomendacao_em_uso.id},
                    resultado="falha",
                    detalhe=str(e),
                )
        # Se missão falhou: recomendação permanece pendente (regra explícita documentada)

        executar_retencao(app_flask)

        # Fase 5: Customer Insight (ao final do ciclo; falha não quebra o ciclo)
        try:
            from app.run_cleiton_agente_customer_insight import executar_insight
            executar_insight(app_flask)
        except Exception as e:
            logger.warning("Customer Insight falhou (continuando): %s", e)
            auditoria_registrar(
                tipo_decisao="insight",
                decisao="Insight falhou no ciclo",
                contexto={},
                resultado="falha",
                detalhe=str(e),
            )
        return _finalize_com_evergreen(resultado)
    logger.info("Cleiton orquestrador: ciclo gerencial encerrado.")
    return resultado
