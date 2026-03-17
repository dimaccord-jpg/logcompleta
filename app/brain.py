import json
import logging

logger = logging.getLogger(__name__)

def gerar_chave_busca(cidade, uf):
    """
    REGRA DE OURO: Apenas concatena e coloca em minúsculo.
    Mantém acentos, cedilhas, traços e apóstrofos.
    """
    if not cidade or not uf:
        return ""
    
    # Remove espaços extras e monta a chave literal
    c_ajustada = str(cidade).strip()
    u_ajustada = str(uf).strip()
    
    return f"{c_ajustada}-{u_ajustada}".lower()

def processar_inteligencia_frete(origem_raw, destino_raw, uf_origem, uf_destino, models):
    # Movemos o import para dentro da função que o utiliza
    from app.run_cleiton import coordenar_analise_frete
    from app.infra import get_id_localidade_por_chave
    from app.extensions import db
    from sqlalchemy import text

    FreteReal = models
    
    # 1. Geração das Chaves Estritas
    chave_o = gerar_chave_busca(origem_raw, uf_origem)
    chave_d = gerar_chave_busca(destino_raw, uf_destino)
    
    # 2. Busca IDs nas localidades oficiais (base_localidades) via chave_busca
    id_origem = get_id_localidade_por_chave(chave_o)
    id_destino = get_id_localidade_por_chave(chave_d)

    if id_origem is None or id_destino is None:
        err = f"Localidade não encontrada: {'Origem' if id_origem is None else 'Destino'}"
        return None, err

    # 2.1. Recupera nomes de cidade/UF a partir de base_localidades para compor a rota
    cidade_o = cidade_d = uf_o_nome = uf_d_nome = ""
    try:
        engine_loc = db.engines.get("localidades")
        if engine_loc is not None:
            with engine_loc.connect() as conn:
                row_o = conn.execute(
                    text("SELECT cidade_nome, uf_nome FROM base_localidades WHERE id_cidade = :id"),
                    {"id": id_origem},
                ).fetchone()
                row_d = conn.execute(
                    text("SELECT cidade_nome, uf_nome FROM base_localidades WHERE id_cidade = :id"),
                    {"id": id_destino},
                ).fetchone()
                if row_o:
                    cidade_o, uf_o_nome = (row_o[0] or ""), (row_o[1] or "")
                if row_d:
                    cidade_d, uf_d_nome = (row_d[0] or ""), (row_d[1] or "")
    except Exception as e:
        logger.debug("Falha ao resolver nomes de localidades para a rota: %s", e)

    # 3. Busca Histórico
    fretes = FreteReal.query.filter(
        FreteReal.id_cidade_origem == id_origem,
        FreteReal.id_cidade_destino == id_destino,
    ).all()
    
    # --- LOGGING DE OPERAÇÃO ---
    logger.info(
        "Calculando rota: %s -> %s | Histórico: %s registros.",
        cidade_o or origem_raw,
        cidade_d or destino_raw,
        len(fretes),
    )

    soma_v = 0.0
    soma_p = 0.0
    
    # Preparando dados para o Roberto (mantendo isolamento)
    historico_ia = []
    
    for f in fretes:
        v = float(f.valor_frete_total or 0)
        p = float(f.peso_real or 0)
        soma_v += v
        soma_p += p
        historico_ia.append({
            'valor': v,
            'peso': p,
            'modal': (f.modal or ''),
            'data_emissao': f.data_emissao,
        })
    
    logger.debug(f"Totais Rota: Valor R$ {soma_v:.2f} | Peso {soma_p:.2f} KG")

    if soma_p > 0:
        media_bruta = soma_v / soma_p
        
        # 1. Definimos a string da rota com nomes oficiais quando disponíveis
        rota_str = f"{(cidade_o or origem_raw)} ({(uf_o_nome or uf_origem)}) -> {(cidade_d or destino_raw)} ({(uf_d_nome or uf_destino)})"
        
        # 2. DELEGAÇÃO: Chamamos o Cleiton (Gestor) para orquestrar a IA
        # Ele vai ler o indices.json e pedir a análise para o Roberto
        insight = coordenar_analise_frete(historico_ia, rota_str)
        
        # 3. Retorno completo para o fretes.html (previsão do modelo + explicação do LLM)
        acuracia_str = insight.get('acuracia_percentual', '0').replace('%', '').strip()
        try:
            assertividade = float(acuracia_str) / 100
        except (ValueError, TypeError):
            assertividade = 0.0

        return {
            'rota': rota_str,
            'media_bruta': media_bruta,
            'previsao_roberto': insight.get('tendencia_macro', 'Estabilidade'),
            'assertividade': assertividade,
            'amostras': len(fretes),
            'id_cidade': id_destino,
            # Previsão numérica e métricas (modelo estatístico)
            'previsao_numerica': insight.get('previsao_numerica'),
            'intervalo_confianca': insight.get('intervalo_confianca'),
            'metrica_erro': insight.get('metrica_erro') or {},
            # Explicação e insights (LLM)
            'explicacao_llm': insight.get('explicacao_llm'),
            'insights_adicionais': insight.get('insights_adicionais'),
            'previsao_frete': insight.get('explicacao_llm') or insight.get('previsao_texto'),
            'acuracia_ia': insight.get('acuracia_percentual'),
            'recado_roberto': insight.get('recado_do_roberto'),
        }, None
    
    return None, "Sem dados históricos para esta rota específica."