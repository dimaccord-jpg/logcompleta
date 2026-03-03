from sqlalchemy import func
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
    from run_cleiton import coordenar_analise_frete  
    DePara = models['DeParaLogistica']
    FreteReal = models['FreteReal']
    
    # 1. Geração das Chaves Estritas
    chave_o = gerar_chave_busca(origem_raw, uf_origem)
    chave_d = gerar_chave_busca(destino_raw, uf_destino)
    
    # 2. Busca IDs nas Localidades (Usando func.lower para match com o banco)
    depara_o = DePara.query.filter(func.lower(DePara.chave_busca) == chave_o).first()
    depara_d = DePara.query.filter(func.lower(DePara.chave_busca) == chave_d).first()

    if not depara_o or not depara_d:
        err = f"Localidade não encontrada: {'Origem' if not depara_o else 'Destino'}"
        return None, err

    # 3. Busca Histórico
    fretes = FreteReal.query.filter(
        FreteReal.id_cidade_origem == depara_o.id_cidade,
        FreteReal.id_cidade_destino == depara_d.id_cidade
    ).all()
    
    # --- LOGGING DE OPERAÇÃO ---
    logger.info(f"Calculando rota: {depara_o.cidade_nome} -> {depara_d.cidade_nome} | Histórico: {len(fretes)} registros.")

    soma_v = 0.0
    soma_p = 0.0
    
    # Preparando dados para o Roberto (mantendo isolamento)
    historico_ia = []
    
    for f in fretes:
        v = float(f.valor_frete_total or 0)
        p = float(f.peso_real or 0)
        soma_v += v
        soma_p += p
        historico_ia.append({'valor': v, 'peso': p, 'modal': f.modal})
    
    logger.debug(f"Totais Rota: Valor R$ {soma_v:.2f} | Peso {soma_p:.2f} KG")

    if soma_p > 0:
        media_bruta = soma_v / soma_p
        
        # 1. Definimos a string da rota (antes era solta, agora precisamos dela aqui)
        rota_str = f"{depara_o.cidade_nome} ({depara_o.uf_nome}) -> {depara_d.cidade_nome} ({depara_d.uf_nome})"
        
        # 2. DELEGAÇÃO: Chamamos o Cleiton (Gestor) para orquestrar a IA
        # Ele vai ler o indices.json e pedir a análise para o Roberto
        insight = coordenar_analise_frete(historico_ia, rota_str)
        
        # 3. Retorno completo para o fretes.html
        return {
            'rota': rota_str,
            'media_bruta': media_bruta,
            'previsao_roberto': insight.get('tendencia_macro', 'Estabilidade'),
            
            # CONVERSÃO IMPORTANTE: Transforma "85%" em 0.85 para o HTML multiplicar por 100
            'assertividade': float(insight.get('acuracia_percentual', '0').replace('%', '')) / 100,
            
            'amostras': len(fretes),
            'id_cidade': depara_d.id_cidade,
                        
            # Estrutura para os Agentes de IA/Cards extras
            'previsao_frete': insight.get('previsao_texto'),
            'acuracia_ia': insight.get('acuracia_percentual'),
            'recado_roberto': insight.get('recado_do_roberto')
        }, None
    
    return None, "Sem dados históricos para esta rota específica."