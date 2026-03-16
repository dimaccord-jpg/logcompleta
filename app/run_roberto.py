"""
Agente Roberto: previsão quantitativa via modelo estatístico + explicação via LLM (Gemini).
A previsão numérica é feita por roberto_modelo; o Gemini apenas explica e contextualiza.
"""
import os
import json
import logging
from google import genai

from app.settings import settings  # noqa: F401
from app.roberto_modelo import prever as modelo_prever

logger = logging.getLogger(__name__)


class AgenteRoberto:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY_ROBERTO")
        if not self.api_key:
            raise ValueError("⚠️ Roberto está sem chave! Configure GEMINI_API_KEY_ROBERTO no .env")
        self.client = genai.Client(api_key=self.api_key)

        self.persona = (
            "Você é Roberto Santos, 28 anos, carioca, negro, especialista em BI e logística. "
            "Sua vibe é profissional, assertiva, mas despojada (estilo blusa de linho fora da calça). "
            "Você é mestre em SQL, Python e mercado financeiro (Dólar, Petróleo, BDI). "
            "Sua missão AGORA: EXPLICAR e CONTEXTUALIZAR a previsão de frete que já foi calculada por um modelo estatístico. "
            "Você NÃO deve inventar ou alterar números de previsão. Use apenas os dados e resultados do modelo que forem fornecidos. "
            "Retorne SEMPRE um JSON puro, sem formatação Markdown extra."
        )
        self.model_candidates = self._get_model_candidates()

    def _get_model_candidates(self):
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
        Fluxo: (1) Modelo estatístico gera previsão numérica, IC e métricas.
               (2) Gemini gera apenas explicação textual e insights.
        """
        # 1. Previsão quantitativa pelo modelo estatístico
        resultado_modelo = modelo_prever(historico, indices_completos)
        previsao_numerica = resultado_modelo.get("previsao_numerica")
        intervalo_confianca = resultado_modelo.get("intervalo_confianca")
        metrica_erro = resultado_modelo.get("metrica_erro") or {}
        tendencia_macro = resultado_modelo.get("tendencia_macro", "Estabilidade")

        # 2. Acuracia percentual derivada do modelo (ex.: baseada em n_amostras e RMSE)
        n_amostras = metrica_erro.get("n_amostras") or 0
        n_meses = metrica_erro.get("n_meses") or 0
        rmse = metrica_erro.get("rmse")
        if rmse is not None and n_meses >= 2:
            # Quanto mais amostras/meses e menor RMSE relativo, maior confiança
            media_serie = sum(resultado_modelo.get("serie_historica_valores") or [0]) / max(1, n_meses)
            cv = (rmse / media_serie * 100) if media_serie else 100
            acuracia = max(0, min(100, 100 - min(cv, 50) + (min(n_amostras, 200) / 4)))
        else:
            acuracia = max(0, min(100, (n_amostras // 2) + (n_meses * 5)))
        acuracia_percentual = f"{int(round(acuracia))}%"

        # 3. Prompt para o Gemini: apenas explicar e contextualizar (não prever)
        trilha_mercado = indices_completos.get("historico", []) if indices_completos else []
        ultima_atualizacao = (indices_completos or {}).get("ultima_atualizacao", "Data desconhecida")

        prompt_usuario = f"""
Rota analisada: {rota}.

DADOS DE ENTRADA (histórico real de fretes nesta rota):
{json.dumps(historico[:50], indent=2, default=str)}
{f'... e mais {len(historico) - 50} registros.' if len(historico) > 50 else ''}

CONTEXTO DE MERCADO (índices macro – últimos períodos):
{json.dumps(trilha_mercado[-24:], indent=2, default=str)}
Última atualização dos índices: {ultima_atualizacao}.

RESULTADO DO MODELO ESTATÍSTICO (já calculado – NÃO altere estes números):
- Tendência macro: {tendencia_macro}
- Previsão numérica (próximos 6 meses, R$/kg): {json.dumps(previsao_numerica, default=str)}
- Intervalo de confiança (inferior/superior): {json.dumps(intervalo_confianca, default=str)}
- Métricas de erro: RMSE = {metrica_erro.get('rmse')}, MAE = {metrica_erro.get('mae')}, n_amostras = {n_amostras}, n_meses = {n_meses}

SUA MISSÃO (apenas texto):
1. Explique em linguagem clara o que a previsão e a tendência significam para o cliente.
2. Contextualize com os índices de mercado (dólar, petróleo, BDI) quando fizer sentido.
3. Dê insights práticos e um recado curto no seu estilo (carioca, direto).

NÃO invente números de previsão. Use apenas os resultados do modelo acima.

Responda obrigatoriamente neste formato JSON puro:
{{
    "explicacao_llm": "Texto explicativo da previsão e do que os números significam",
    "insights_adicionais": "Insights práticos e recomendações",
    "recado_do_roberto": "Frase curta no seu estilo sobre o que fazer"
}}
"""

        full_prompt = f"{self.persona}\n\n{prompt_usuario}"

        explicacao_llm = ""
        insights_adicionais = ""
        recado_do_roberto = ""

        last_error = None
        for model_name in self.model_candidates:
            try:
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=full_prompt,
                )
                content = (response.text or "").replace("```json", "").replace("```", "").strip()
                inicio = content.find("{")
                fim = content.rfind("}") + 1
                if inicio >= 0 and fim > inicio:
                    parsed = json.loads(content[inicio:fim])
                    explicacao_llm = parsed.get("explicacao_llm", "") or ""
                    insights_adicionais = parsed.get("insights_adicionais", "") or ""
                    recado_do_roberto = parsed.get("recado_do_roberto", "") or ""
                logger.info(
                    "✅ Roberto explicou a previsão da rota %s (modelo=%s).",
                    rota,
                    model_name,
                )
                break
            except Exception as e:
                last_error = e
                logger.warning("Modelo Gemini indisponível/falhou (%s): %s", model_name, e)

        if not explicacao_llm and last_error:
            explicacao_llm = "A previsão numérica acima foi gerada pelo nosso modelo estatístico. Não foi possível carregar a explicação em texto neste momento."
            recado_do_roberto = f"Deu um curto no explicador: {str(last_error)}. Confie nos números do modelo."

        # 4. Retorno unificado: modelo + LLM (e compatibilidade com interface antiga)
        return {
            "previsao_numerica": previsao_numerica,
            "intervalo_confianca": intervalo_confianca,
            "metrica_erro": metrica_erro,
            "explicacao_llm": explicacao_llm,
            "insights_adicionais": insights_adicionais,
            "recado_do_roberto": recado_do_roberto,
            "tendencia_macro": tendencia_macro,
            "acuracia_percentual": acuracia_percentual,
            "previsao_texto": explicacao_llm,
        }


roberto = AgenteRoberto()
