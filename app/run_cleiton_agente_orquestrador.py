"""
Cleiton - Agente Orquestrador: cérebro gerencial.
Lê plano estratégico ativo, aplica regras (frequência, prioridade, janela, retries),
registra auditoria e dispara jobs para agentes especializados. Nunca escreve conteúdo final.
"""
import logging
import json
from datetime import datetime, timezone
from typing import Any
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
)
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar
from app.run_julia_regras import status_verificacao_permitidos
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


def _contexto_indica_bypass_frequencia(contexto_json: str | None) -> bool:
    """Retorna True quando o contexto da auditoria indica bypass manual da frequência."""
    if not contexto_json:
        return False
    try:
        data = json.loads(contexto_json)
        return isinstance(data, dict) and bool(data.get("bypass_frequencia"))
    except Exception:
        return False


def _contexto_orquestracao(base: dict | None, bypass_frequencia: bool) -> dict:
    """Garante metadado de bypass no contexto para preservar rastreabilidade."""
    contexto = dict(base or {})
    if bypass_frequencia:
        contexto["bypass_frequencia"] = True
    return contexto


def obter_plano_ativo() -> PlanoEstrategico | None:
    """Retorna o plano estratégico ativo (tema, objetivo, estágio)."""
    try:
        return PlanoEstrategico.query.filter_by(ativo=True).order_by(PlanoEstrategico.updated_at.desc()).first()
    except Exception as e:
        logger.warning("Falha ao obter plano ativo: %s", e)
        return None


def ultima_auditoria_orquestracao() -> datetime | None:
    """Data/hora da última decisão de tipo orquestracao (para frequência)."""
    try:
        registros = (
            AuditoriaGerencial.query.filter_by(tipo_decisao="orquestracao")
            .order_by(AuditoriaGerencial.created_at.desc())
            .limit(200)
            .all()
        )
        for r in registros:
            if not _contexto_indica_bypass_frequencia(r.contexto_json):
                return r.created_at
        return None
    except Exception:
        return None


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


def _tentativas_artigo_hoje() -> int:
    """
    Conta quantas missões de artigo já foram disparadas hoje.
    Usa MissaoAgente como fonte de verdade (tipo_missao='artigo').
    """
    hoje_inicio = _utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return (
            MissaoAgente.query.filter(
                MissaoAgente.tipo_missao == "artigo",
                MissaoAgente.created_at >= hoje_inicio,
            ).count()
        )
    except Exception:
        return 0


def _buscar_pauta_manual_artigo() -> Pauta | None:
    """Busca a pauta manual de artigo mais antiga e elegível para execução."""
    try:
        status_permitidos = status_verificacao_permitidos()
        return (
            Pauta.query.filter(
                Pauta.tipo == "artigo",
                Pauta.status == "pendente",
                Pauta.status_verificacao.in_(status_permitidos),
                Pauta.arquivada.isnot(True),
            )
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
    """
    tem_artigo_hoje = _artigo_publicado_hoje()

    status_permitidos = status_verificacao_permitidos()

    tem_artigo_backlog = Pauta.query.filter(
        Pauta.tipo == "artigo",
        Pauta.status == "pendente",
        Pauta.status_verificacao.in_(status_permitidos),
        Pauta.arquivada.isnot(True),
    ).first()

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


def executar_ciclo_gerencial(
    app_flask,
    bypass_frequencia: bool = False,
    tipo_missao_forcado: str | None = None,
    ignorar_trava_artigo_hoje: bool = False,
    ignorar_janela_publicacao: bool = False,
) -> dict[str, Any]:
    """
    Ciclo principal do Cleiton (gerencial):
    1. Garante bootstrap de regras e plano
    2. Lê plano ativo
    3. Verifica frequência e janela
    4. Decide tipo de missão (artigo/noticia)
    5. Registra auditoria
    6. Constrói payload e despacha para agente operacional
    7. Executa retenção (purge auditável)
    Nenhuma geração de conteúdo final aqui.
    """
    logger.info("Cleiton orquestrador: iniciando ciclo gerencial.")
    with app_flask.app_context():
        bootstrap_regras()
        bootstrap_plano_se_necessario()
        plano = obter_plano_ativo()
        tema_serie = (plano.tema_serie if plano else "") or "portal"
        objetivo = (plano.objetivo if plano else "") or "conteúdo editorial"

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
            return resultado

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
            return resultado

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
                return resultado

        # Se a missão for artigo, tenta selecionar item de série editorial elegível e preparar pauta.
        if tipo_missao == "artigo":
            fonte_artigo_resolvida = False
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
                    fonte_artigo_resolvida = True
                else:
                    auditoria_registrar(
                        tipo_decisao="orquestracao",
                        decisao="Falha ao preparar pauta de item de série; tentando fallback manual",
                        contexto={"serie_item_id": item_serie.id, "serie_id": item_serie.serie_id},
                        resultado="falha",
                    )

            if not fonte_artigo_resolvida:
                # Fallback explícito: tentar pauta manual de artigo.
                pauta_manual = _buscar_pauta_manual_artigo()
                if pauta_manual:
                    resultado["caminho_usado"] = "pauta_manual"
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
                return resultado
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
        payload = construir_payload(
            tipo_missao=tipo_missao,
            tema=tema_efetivo,
            prioridade=prioridade_efetiva,
            janela_publicacao_inicio=janela_inicio,
            janela_publicacao_fim=janela_fim,
            tentativa_atual=1,
            metadados=metadados,
        )
        resultado["mission_id"] = payload.get("mission_id")
        registrar_missao(payload)
        auditoria_registrar(
            tipo_decisao="orquestracao",
            decisao=f"Missão criada tipo={tipo_missao} theme={tema_efetivo}",
            contexto=_contexto_orquestracao(
                {"mission_id": payload.get("mission_id"), "tipo_missao": tipo_missao},
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
            resultado["motivo"] = "Despacho para agente operacional falhou ou não houve publicação."
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
    logger.info("Cleiton orquestrador: ciclo gerencial encerrado.")
    return resultado
