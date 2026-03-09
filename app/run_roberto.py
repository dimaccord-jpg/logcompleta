import os
import json
from google import genai
import logging
from app.env_loader import load_app_env

# Carrega .env por caminho absoluto (independente do CWD)
load_app_env()

logger = logging.getLogger(__name__)

class AgenteRoberto:
    def __init__(self):
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
        self.model_candidates = self._get_model_candidates()

    def _get_model_candidates(self):
        """Modelos em ordem de fallback para evitar indisponibilidade por conta/projeto."""
        candidates = [
            os.getenv("GEMINI_MODEL_FRETE", "").strip(),
            os.getenv("GEMINI_MODEL_TEXT", "").strip(),
            "gemini-2.5-flash",
            "gemini-1.5-flash",
        ]
        seen = set()
        ordered = []
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                ordered.append(c)
        return ordered

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

        last_error = None
        for model_name in self.model_candidates:
            try:
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=full_prompt
                )

                content = (response.text or "").replace('```json', '').replace('```', '').strip()
                inicio = content.find("{")
                fim = content.rfind("}") + 1
                if inicio < 0 or fim <= inicio:
                    raise ValueError("Resposta sem JSON válido")

                resultado = json.loads(content[inicio:fim])
                logger.info("✅ Roberto analisou %s meses de mercado para a rota %s (modelo=%s).", len(trilha_mercado), rota, model_name)
                return resultado
            except Exception as e:
                last_error = e
                logger.warning("Modelo indisponível/falhou para Roberto (%s): %s", model_name, e)

        if last_error:
            return {
                "previsao_texto": "Ih chefe, deu um curto aqui no modelo e não consegui processar a análise.",
                "tendencia_macro": "Indisponível",
                "acuracia_percentual": "0%",
                "recado_do_roberto": f"Aperta o F5 aí, deu ruim: {str(last_error)}"
            }
# Instância única para ser importada pelo brain.py
roberto = AgenteRoberto()