"""
Serviço de gestão e execução dos agentes de IA (Cleiton, Júlia, Roberto).
Centraliza leitura de configuração, KPIs do dashboard, execução síncrona e persistência de resultados.
A execução em background fica em app.tasks.agent_tasks.
"""
import os
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.extensions import db
from app.models import (
    ConfigRegras,
    Pauta,
    NoticiaPortal,
    RecomendacaoEstrategica,
    InsightCanal,
    AuditoriaGerencial,
)
from app.run_julia_regras import status_verificacao_permitidos
from app.run_cleiton_agente_regras import (
    bootstrap_regras,
    CHAVE_FREQUENCIA_HORAS,
    DEFAULTS,
    get_janela_publicacao,
)
from app.run_cleiton_agente_orquestrador import ultima_auditoria_orquestracao

logger = logging.getLogger(__name__)

# Nome do arquivo de última execução manual (sob data_dir)
LAST_ADMIN_RUN_FILENAME = "last_admin_run.json"
INDICES_MANUAL_LOG_FILENAME = "indices_manual_log.json"
MAX_ENTRIES_INDICES_LOG = 20


def get_data_dir(app=None):
    """Retorna o diretório de dados (para persistência de execuções manuais e logs)."""
    if app and getattr(app, "config", None) and app.config.get("DATA_DIR"):
        return app.config["DATA_DIR"]
    try:
        from app.settings import settings
        return settings.data_dir
    except Exception:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, "data")


def admin_exec_mode() -> str:
    """
    Define modo de execução do botão admin: homolog/prod = async, dev = sync.
    Pode ser sobrescrito por ADMIN_CLEITON_EXEC_MODE=sync|async.
    """
    forced = (os.getenv("ADMIN_CLEITON_EXEC_MODE", "") or "").strip().lower()
    if forced in ("sync", "async"):
        return forced
    app_env = (os.getenv("APP_ENV", "dev") or "dev").strip().lower()
    return "async" if app_env in ("homolog", "prod") else "sync"


def persistir_ultima_execucao_manual(
    resultado: dict, origem: str, app=None
) -> None:
    """Persiste o resultado da última execução manual para exibição no Admin."""
    try:
        data_dir = get_data_dir(app)
        path = os.path.join(data_dir, LAST_ADMIN_RUN_FILENAME)
        os.makedirs(data_dir, exist_ok=True)
        payload = {
            "status": resultado.get("status") or "falha",
            "motivo": resultado.get("motivo_final") or resultado.get("motivo") or "",
            "caminho_usado": resultado.get("caminho_usado") or "",
            "mission_id": resultado.get("mission_id"),
            "tipo_missao": resultado.get("tipo_missao"),
            "origem": origem,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=0)
    except Exception as e:
        logger.warning("Falha ao persistir última execução manual: %s", e)


def ler_ultima_execucao_manual(app=None) -> dict | None:
    """Lê o resultado da última execução manual (para exibir no Admin)."""
    try:
        data_dir = get_data_dir(app)
        path = os.path.join(data_dir, LAST_ADMIN_RUN_FILENAME)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def indices_admin_log_path(app=None) -> str:
    """Caminho do arquivo de log das execuções manuais de índices financeiros."""
    data_dir = get_data_dir(app)
    return os.path.join(data_dir, INDICES_MANUAL_LOG_FILENAME)


def persistir_execucao_indices_admin(resultado: dict, app=None) -> None:
    """Persiste histórico compacto de execuções manuais dos índices (últimas 20)."""
    try:
        path = indices_admin_log_path(app)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        execucoes = []
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                antigo = json.load(f)
                if isinstance(antigo, dict):
                    execucoes = antigo.get("execucoes") or []
                elif isinstance(antigo, list):
                    execucoes = antigo
        registro = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status_global": resultado.get("status_global") or "desconhecido",
            "mensagem": resultado.get("mensagem") or "",
            "indices": resultado.get("indices") or {},
            "arquivo_destino": resultado.get("arquivo_destino"),
            "data_referencia": resultado.get("data_referencia"),
        }
        execucoes.append(registro)
        execucoes = execucoes[-MAX_ENTRIES_INDICES_LOG:]
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"execucoes": execucoes}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Falha ao persistir log de índices admin: %s", e)


def ler_execucoes_indices_admin(app=None) -> list:
    """Retorna lista de execuções manuais de índices para exibição no painel."""
    try:
        path = indices_admin_log_path(app)
        if not os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            conteudo = json.load(f)
        if isinstance(conteudo, dict):
            return conteudo.get("execucoes") or []
        if isinstance(conteudo, list):
            return conteudo
        return []
    except Exception:
        return []


def obter_kpis_insight() -> dict:
    """Retorna dict com contagens para painel estratégico (recomendações e insights)."""
    try:
        pendentes = RecomendacaoEstrategica.query.filter_by(status="pendente").count()
        aplicadas = RecomendacaoEstrategica.query.filter_by(status="aplicada").count()
        descartadas = RecomendacaoEstrategica.query.filter_by(status="descartada").count()
        total_metricas = InsightCanal.query.count()
        total_auditorias_insight = AuditoriaGerencial.query.filter_by(
            tipo_decisao="insight"
        ).count()
        return {
            "recomendacoes_pendentes": pendentes,
            "recomendacoes_aplicadas": aplicadas,
            "recomendacoes_descartadas": descartadas,
            "total_metricas": total_metricas,
            "total_auditorias_insight": total_auditorias_insight,
        }
    except Exception:
        return {
            "recomendacoes_pendentes": 0,
            "recomendacoes_aplicadas": 0,
            "recomendacoes_descartadas": 0,
            "total_metricas": 0,
            "total_auditorias_insight": 0,
        }


def obter_recomendacoes_recentes(limite: int = 15) -> list:
    """Lista recomendações recentes (todas as status) para exibição no dashboard."""
    try:
        limite = max(1, min(50, limite))
        return (
            RecomendacaoEstrategica.query
            .order_by(RecomendacaoEstrategica.criado_em.desc())
            .limit(limite)
            .all()
        )
    except Exception:
        return []


def obter_frequencia_horas() -> int:
    """Retorna a frequência atual do ciclo (fallback seguro = 3h)."""
    try:
        bootstrap_regras()
        cfg = ConfigRegras.query.filter_by(chave=CHAVE_FREQUENCIA_HORAS).first()
        if cfg and cfg.valor_inteiro is not None:
            return max(1, int(cfg.valor_inteiro))
        return int(DEFAULTS.get(CHAVE_FREQUENCIA_HORAS, 3))
    except Exception:
        return 3


def obter_ultima_e_proxima_execucao(
    frequencia_horas: int,
) -> tuple[datetime | None, datetime | None]:
    """
    Retorna (última execução de orquestração, próxima prevista).
    Próxima = última + frequência em horas.
    """
    try:
        from datetime import timedelta
        ultima = ultima_auditoria_orquestracao()
        if ultima is None:
            return None, None
        proxima = ultima + timedelta(hours=max(1, frequencia_horas))
        return ultima, proxima
    except Exception:
        return None, None


def obter_janela_publicacao() -> tuple[int, int]:
    """Retorna (hora_inicio, hora_fim) da janela de publicação (0-23)."""
    try:
        return get_janela_publicacao()
    except Exception:
        return 6, 22


def obter_status_pautas_artigo() -> dict:
    """Resumo de backlog de pautas de artigo para o painel admin."""
    try:
        status_permitidos = status_verificacao_permitidos()
        total = Pauta.query.filter(
            Pauta.tipo == "artigo", Pauta.arquivada.isnot(True)
        ).count()
        pendentes = Pauta.query.filter(
            Pauta.tipo == "artigo",
            Pauta.status == "pendente",
            Pauta.arquivada.isnot(True),
        ).count()
        elegiveis = Pauta.query.filter(
            Pauta.tipo == "artigo",
            Pauta.status == "pendente",
            Pauta.status_verificacao.in_(status_permitidos),
            Pauta.arquivada.isnot(True),
        ).count()
        em_proc = Pauta.query.filter(
            Pauta.tipo == "artigo",
            Pauta.status == "em_processamento",
            Pauta.arquivada.isnot(True),
        ).count()
        falha = Pauta.query.filter(
            Pauta.tipo == "artigo",
            Pauta.status == "falha",
            Pauta.arquivada.isnot(True),
        ).count()
        return {
            "total": total,
            "pendentes": pendentes,
            "elegiveis": elegiveis,
            "em_processamento": em_proc,
            "falha": falha,
        }
    except Exception:
        return {
            "total": 0,
            "pendentes": 0,
            "elegiveis": 0,
            "em_processamento": 0,
            "falha": 0,
        }


def obter_ultima_publicacao_artigo() -> datetime | None:
    """Retorna a última data de publicação efetiva de artigo no portal (ou None)."""
    try:
        ultimo = (
            NoticiaPortal.query.filter(
                NoticiaPortal.tipo == "artigo",
                NoticiaPortal.status_publicacao.in_(["publicado", "parcial"]),
            )
            .order_by(
                NoticiaPortal.publicado_em.desc(),
                NoticiaPortal.data_publicacao.desc(),
            )
            .first()
        )
        if not ultimo:
            return None
        return ultimo.publicado_em or ultimo.data_publicacao
    except Exception:
        return None


def configurar_frequencia_horas(valor: int) -> None:
    """Persiste a frequência do ciclo em horas em ConfigRegras."""
    bootstrap_regras()
    cfg = ConfigRegras.query.filter_by(chave=CHAVE_FREQUENCIA_HORAS).first()
    if not cfg:
        cfg = ConfigRegras(
            chave=CHAVE_FREQUENCIA_HORAS,
            descricao="Intervalo de execução do ciclo em horas",
        )
        db.session.add(cfg)
    cfg.valor_inteiro = valor
    cfg.valor_texto = None
    cfg.valor_real = None
    db.session.commit()


def formatar_mensagem_resultado_cleiton(resultado: dict) -> str:
    """Monta mensagem única a partir do resultado de executar_orquestracao (Cleiton)."""
    motivo = resultado.get("motivo") or "Ciclo não informou motivo detalhado."
    partes = [motivo]
    if resultado.get("mission_id"):
        partes.append(f"mission_id={resultado.get('mission_id')}")
    if resultado.get("tipo_missao"):
        partes.append(f"tipo_missao={resultado.get('tipo_missao')}")
    scout = resultado.get("scout") or {}
    if scout:
        partes.append(
            f"Scout: inseridas={scout.get('inseridas', 0)}, "
            f"reativadas={scout.get('reativadas', 0)}, "
            f"ignoradas={scout.get('ignoradas_duplicata', 0)}, "
            f"erros={scout.get('erros', 0)}"
        )
        if "fontes_processadas" in scout:
            partes.append(
                f"Fontes Scout: processadas={scout.get('fontes_processadas', 0)}, "
                f"com_erro={scout.get('fontes_com_erro', 0)}, "
                f"sem_itens={scout.get('fontes_sem_itens', 0)}"
            )
    verif = resultado.get("verificador") or {}
    if verif:
        partes.append(
            f"Verificador: aprovadas={verif.get('aprovadas', 0)}, "
            f"revisar={verif.get('revisar', 0)}, "
            f"rejeitadas={verif.get('rejeitadas', 0)}"
        )
    return " | ".join(partes)


def executar_cleiton_sincrono(app, bypass_frequencia: bool) -> dict:
    """Executa orquestração Cleiton de forma síncrona. Retorna resultado para a rota tratar."""
    from app.run_cleiton import executar_orquestracao
    return executar_orquestracao(
        app,
        bypass_frequencia=bypass_frequencia,
        ignorar_janela_publicacao=bypass_frequencia,
    ) or {}


def executar_artigo_manual_sincrono(app) -> dict:
    """Executa missão manual de artigo de forma síncrona. Retorna resultado."""
    from app.run_cleiton import executar_orquestracao
    return executar_orquestracao(
        app,
        bypass_frequencia=True,
        tipo_missao_forcado="artigo",
        ignorar_trava_artigo_hoje=True,
        ignorar_janela_publicacao=True,
    ) or {}


def executar_coleta_noticias() -> tuple[dict, dict]:
    """Executa apenas Scout + Verificador para notícias. Retorna (resultado_scout, resultado_verif)."""
    from app.run_cleiton_agente_scout import executar_coleta
    from app.run_cleiton_agente_verificador import executar_verificacao
    return executar_coleta(), executar_verificacao()


def atualizar_status_recomendacao(
    recomendacao_id: int, novo_status: str, app, detalhe: str | None = None
) -> bool:
    """Delega para o agente de customer insight. Retorna True se atualizado."""
    from app.run_cleiton_agente_customer_insight import atualizar_status_recomendacao as _atualizar
    return _atualizar(recomendacao_id, novo_status, app, detalhe=detalhe)
