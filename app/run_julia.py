import json
import logging
import os
from google import genai
from datetime import datetime
from dotenv import load_dotenv

# Integração com o ecossistema do projeto
from web import app
from extensions import db
from models import NoticiaPortal

# Carrega variáveis do arquivo .env
# Baseado na variável de sistema APP_ENV
env_name = os.getenv('APP_ENV', 'dev')
load_dotenv(f'.env.{env_name}')

# Configuração de Log profissional
logger = logging.getLogger(__name__)

# Configuração da API do Gemini
client_noticias = genai.Client(api_key=os.getenv("GEMINI_API_KEY_1"))
client_artigos = genai.Client(api_key=os.getenv("GEMINI_API_KEY_2"))

# Definição de caminhos
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.normpath(os.path.join(BASE_DIR, 'processadas.json'))

def gerar_conteudo_ia(titulo_original, fonte, tipo='noticia'):
    """Solicita ao Gemini o conteúdo com a Persona da Júlia, alternando entre notícia e artigo."""
    
    persona_base = """
    Você é Júlia, Editora-Chefe de 32 anos, executiva premium e assertiva. 
    Seu tom é elegante, profissional e estratégico. Você foca em eficiência real e resultados.
    Use termos técnicos de Logística 4.0 corretamente. Evite termos "woke", militantes ou "greenwashing".
    Seja realista e eficiente. Nunca use linguagem infantil ou agressiva.
    """

    if tipo == 'artigo':
        instrucao = f"""
        TAREFA: Escreva um ARTIGO ESTRATÉGICO PREMIUM baseado na pauta: "{titulo_original}" (Fonte: {fonte}).
        
        REQUISITOS DO ARTIGO:
        1. 'titulo_julia': Premium, forte e voltado para alta gestão.
        2. 'subtitulo': Uma frase de impacto que resuma a oportunidade estratégica.
        3. 'resumo_julia': Insight de 3 linhas para o card de chamada.
        4. 'conteudo_completo': Texto técnico e fluido (mínimo 4 parágrafos) em HTML (use apenas <p>, <strong>, <ul>, <li>).
        5. 'prompt_imagem': Comando para IA gerar imagem realista de logística high-tech.
        6. 'referencias': Links e fontes de insights.

        FORMATO JSON:
        {{
            "titulo_julia": "...", "subtitulo": "...", "resumo_julia": "...",
            "conteudo_completo": "...", "prompt_imagem": "...", "referencias": "..."
        }}
        """
    else:
        instrucao = f"""
        TAREFA: Resuma esta notícia para o feed rápido: "{titulo_original}" (Fonte: {fonte}).
        
        REQUISITOS DA NOTÍCIA:
        1. 'titulo_julia': Premium, forte e executivo.
        2. 'resumo_julia': Insight de 4 linhas focado em solução de dores ou oportunidades.
        
        FORMATO JSON:
        {{
            "titulo_julia": "...",
            "resumo_julia": "..."
        }}
        """

    prompt = f"{persona_base}\n{instrucao}\nRetorne APENAS o JSON puro."

    try:
        # Escolha dinâmica do motor baseado no tipo de conteúdo
        selecao_client = client_artigos if tipo == 'artigo' else client_noticias
        
        response = selecao_client.models.generate_content(
            model='gemini-2.0-flash', 
            contents=prompt
        )
        txt = response.text
        # Limpeza de segurança para garantir o parsing do JSON
        inicio = txt.find("{")
        fim = txt.rfind("}") + 1
        return json.loads(txt[inicio:fim])
    except Exception as e:
        logger.error(f"Falha na Editora Júlia ({tipo}): {e}")
        return None

def processar_insight_do_momento(tipo_desejado='noticia'):
    """Lógica de curadoria usando SQLAlchemy e contexto do Flask."""
    logger.info(f"Júlia está revisando as pautas para gerar: {tipo_desejado}...")

    with app.app_context():
        if not os.path.exists(JSON_PATH):
            logger.warning("Arquivo processadas.json não encontrado.")
            return

        with open(JSON_PATH, 'r', encoding='utf-8') as f:
            noticias_brutas = json.load(f)

        for link, info in noticias_brutas.items():
            # Verificação se o link já existe no banco de dados [cite: 37]
            if not NoticiaPortal.query.filter_by(link=link).first():
                logger.info(f"Julia selecionou a pauta: {link}")
                
                conteudo = gerar_conteudo_ia(info.get('titulo_original'), info.get('fonte'), tipo=tipo_desejado)

                if conteudo:
                    try:
                        nova_entrada = NoticiaPortal(
                            tipo=tipo_desejado,
                            titulo_julia=conteudo['titulo_julia'],
                            titulo_original=info['titulo_original'],
                            link=link,
                            fonte=info['fonte'],
                            resumo_julia=conteudo['resumo_julia'],
                            subtitulo=conteudo.get('subtitulo'),
                            conteudo_completo=conteudo.get('conteudo_completo'),
                            url_imagem=conteudo.get('url_imagem'), # URL ou prompt para posterior geração
                            referencias=conteudo.get('referencias')
                        )
                        db.session.add(nova_entrada)
                        db.session.commit()
                        logger.info(f"✅ Publicação finalizada: {conteudo['titulo_julia']}")
                        return 
                    except Exception as e:
                        db.session.rollback()
                        logger.error(f"Erro técnico ao publicar: {e}")
        
    logger.info("Nenhuma pauta nova pendente para o perfil selecionado.")

if __name__ == "__main__":
    # Por padrão, busca uma notícia comum.
    processar_insight_do_momento(tipo_desejado='noticia')