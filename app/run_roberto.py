import os
import json
from google import genai
from dotenv import load_dotenv
import logging

# Carrega as chaves do arquivo .env
# Baseado na variável de sistema APP_ENV
env_name = os.getenv('APP_ENV', 'dev')
load_dotenv(f'.env.{env_name}')

logger = logging.getLogger(__name__)

class AgenteRoberto:
    def __init__(self):
        # 2.0 Flash conforme solicitado para melhor raciocínio
        self.api_key = os.getenv("GEMINI_API_KEY_ROBERTO")
        
        if not self.api_key:
            raise ValueError("⚠️ Roberto está sem chave! Configure GEMINI_API_KEY_ROBERTO no .env")
            
        self.client = genai.Client(api_key=self.api_key)
        
        # Configuração da Persona do Roberto
        self.persona = (
            "Você é Roberto Santos, 28 anos, carioca, negro, especialista em BI e logística. "
            "Sua vibe é profissional, assertiva, mas despojada (estilo blusa de linho fora da calça). "
            "Você é mestre em SQL, Python e mercado financeiro (Dólar, Petróleo, BDI). "
            "Sua missão: Prever tendências de frete para os próximos 6 meses com base no histórico real "
            "e índices de mercado. Se os dados forem insuficientes, a acurácia deve ser baixa. "
            "Se os dados de mercado (índices) estiverem ausentes, baseie sua acurácia apenas na quantidade de amostras reais. "
            "Muitas amostras reais (>100) = Acurácia Alta, mesmo sem índices."
            "Retorne SEMPRE um JSON puro, sem formatação Markdown extra."
        )

    def analisar_frete(self, historico, indices_completos, rota):
        """
        historico: lista de dicts vinda do brain.py (dados reais do banco)
        indices_completos: dict vindo do indices.json (agora com a chave 'historico')
        rota: string identificando origem -> destino
        """
        
        # Extraímos a lista histórica para facilitar a leitura da IA
        trilha_mercado = indices_completos.get('historico', [])
        ultima_atualizacao = indices_completos.get('ultima_atualizacao', 'Data desconhecida')

        prompt_usuario = f"""
        Fala chefe! Roberto na área. Analisando a rota: {rota}.
        
        CONTEXTO DE MERCADO (Últimos 18 meses):
        Abaixo segue a evolução dos indicadores (Dólar, Petróleo, BDI, FBX). 
        Analise a CURVA desses dados, não apenas o último valor:
        {json.dumps(trilha_mercado, indent=2)}
        
        HISTÓRICO REAL DE FRETE (Nossos registros em {rota}):
        {json.dumps(historico, indent=2)}
        
        MISSÃO TÉCNICA:
        1. Analise a correlação entre a subida/descida dos indicadores e nossos valores reais de frete.
        2. Projete a tendência para os PRÓXIMOS 6 MESES.
        3. Use classificações como: Tendência de Alta, Baixa, Muito Alta, Muito Baixa ou Estabilidade.
        4. Avalie a ACURÁCIA: Se houver poucos dados no histórico real ou nos índices, baixe o percentual.
        
        Data da última leitura de mercado: {ultima_atualizacao}

        Responda obrigatoriamente neste formato JSON puro:
        {{
            "previsao_texto": "Análise técnica detalhada da tendência e dos motivos (mencione os indicadores)",
            "tendencia_macro": "Sua Tendência aqui",
            "acuracia_percentual": "XX%",
            "recado_do_roberto": "Frase curta com meu estilo carioca e papo reto sobre o que fazer"
        }}
        """
        
        full_prompt = f"{self.persona}\n\n{prompt_usuario}"

        try:
            # Usando a força do Gemini 2.0 para analisar a série temporal
            response = self.client.models.generate_content(
                model="gemini-2.0-flash",
                contents=full_prompt
            )
            
            # Limpeza de possíveis formatações markdown
            content = response.text.replace('```json', '').replace('```', '').strip()
            
            # Conversão para dicionário Python
            resultado = json.loads(content)
            
            # Debug para o terminal do Cleiton
            logger.info(f"✅ Roberto analisou {len(trilha_mercado)} meses de mercado para a rota {rota}.")
            
            return resultado

        except Exception as e:
            return {
                "previsao_texto": "Ih chefe, deu um curto aqui no modelo e não consegui processar a análise.",
                "tendencia_macro": "Indisponível",
                "acuracia_percentual": "0%",
                "recado_do_roberto": f"Aperta o F5 aí, deu ruim: {str(e)}"
            }
# Instância única para ser importada pelo brain.py
roberto = AgenteRoberto()