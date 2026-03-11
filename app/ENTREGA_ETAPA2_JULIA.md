# Entrega – Etapa 2 (Júlia operacional + conteúdo com imagem IA + leads)

## 1. Resumo técnico

- **Pipeline operacional:** Entrada pelo payload do Cleiton; etapas: obter pauta validada (tabela `Pauta`) → redação (Gemini, `GEMINI_MODEL_TEXT`) → composição de prompt contextual de imagem (pauta + título/subtítulo/resumo) → imagem IA (com retries e fallback em camadas) → validação por tipo (notícia curta / artigo completo) → publicação em `NoticiaPortal`. Sucesso só quando a publicação é concluída no formato correto; falhas marcam missão como falha e pauta como falha.
- **Remoção de processadas.json:** Pautas vêm exclusivamente da tabela `Pauta`. Função `popular_pautas_de_arquivo_json()` em `news_ai` permite importar do formato legado para semear/migrar.
- **Notícia curta:** titulo_julia, url_imagem (ou fallback estático local), resumo_julia (insight 3–5 linhas), link (fonte). Não publica sem link.
- **Artigo:** titulo_julia, url_imagem, subtitulo, resumo_julia, conteudo_completo (HTML seguro), link, cta, objetivo_lead. CTA e objetivo_lead obrigatórios para aprovação na qualidade.
- **Imagem:** `run_julia_agente_imagem` gera URL a partir de prompt contextual (Gemini Imagen se configurado), com retries antes de fallback. Ordem de fallback: (1) stock contextual salvo localmente `app/static/generated/julia_stock_<hash>.jpg` → (2) fallback estático fixo/versionado `app/static/img/fallback-capa-v1.svg` → (3) remoto opcional somente se habilitado. Nunca persiste list/dict em coluna.
- **Modelos:** `NoticiaPortal` ganha colunas `cta`, `objetivo_lead`, `status_qualidade`, `origem_pauta`. Nova tabela `Pauta` (bind noticias). Migração suave em `infra._ensure_noticias_portal_columns`.
- **Dispatcher:** Continua marcando sucesso apenas quando `processar_insight_do_momento(payload_cleiton=payload)` retorna True (publicação concluída).
- **Retenção:** Mantida (18 meses dados, 2 meses imagens); compatível com `run_cleiton_agente_retencao`.

---

## 2. Arquivos criados/alterados

| Arquivo | Papel |
|--------|--------|
| **app/run_julia_agente_redacao.py** | Novo. Geração de texto notícia/artigo via Gemini; modelo por `GEMINI_MODEL_TEXT`. |
| **app/run_julia_agente_imagem.py** | Novo. Geração de url_imagem (Imagen ou fallback); normalização para não quebrar templates. |
| **app/run_julia_agente_qualidade.py** | Novo. Validação por tipo (campos mínimos, tamanhos, link obrigatório). |
| **app/run_julia_agente_publicacao.py** | Novo. Persistência em NoticiaPortal com tipos normalizados. |
| **app/run_julia_agente_pipeline.py** | Novo. Orquestra pauta → redação → imagem → qualidade → publicação. |
| **app/run_julia.py** | Refatorado. Fachada; delega ao pipeline; aceita `payload_cleiton` opcional. |
| **app/run_cleiton_agente_dispatcher.py** | Alterado. Passa `payload` completo para Júlia; sucesso só com publicação. |
| **app/models.py** | Alterado. Modelo `Pauta`; em `NoticiaPortal` colunas cta, objetivo_lead, status_qualidade, origem_pauta. |
| **app/infra.py** | Alterado. `_ensure_noticias_portal_columns` para migração suave. |
| **app/news_ai.py** | Alterado. `popular_pautas_de_arquivo_json`; import de `Pauta`. |
| **app/templates/noticia_interna.html** | Alterado. Bloco CTA + objetivo_lead para artigos. |
| **app/.env.example** | Alterado. GEMINI_MODEL_TEXT, IMAGE_PROVIDER, GEMINI_MODEL_IMAGE, IMAGEM_FALLBACK_URL. |
| **app/README_RUN.md** | Alterado. Seção Júlia Etapa 2, pautas, pipeline. |
| **app/README_DEPLOY.md** | Alterado. Variáveis de imagem e modelo textual. |
| **app/ENTREGA_ETAPA2_JULIA.md** | Este arquivo. |

---

## 3. Principais diffs

- **run_julia.py:** Removida lógica de leitura de processadas.json e gravação direta; apenas monta payload e chama `executar_pipeline(payload, app)`; aceita `payload_cleiton`.
- **Dispatcher:** `processar_insight_do_momento(payload_cleiton=payload)` para preservar mission_id e marcar sucesso somente com publicação.
- **models.py:** Inclusão de `Pauta`; em `NoticiaPortal` quatro colunas novas (todas opcionais para registros antigos).
- **infra:** Loop ALTER TABLE para cta, objetivo_lead, status_qualidade, origem_pauta quando ausentes.
- **noticia_interna.html:** Bloco condicional `{% if noticia.tipo == 'artigo' and noticia.cta %}` com CTA e badge objetivo_lead.

---

## 4. Evidências de testes

- **Imports e pipeline (sem Flask completo):**  
  - `run_julia_agente_redacao.gerar_conteudo`, `run_julia_agente_qualidade.validar_noticia_curta`/`validar_artigo`, `run_julia_agente_imagem.gerar_url_imagem` (retorno fallback), `run_julia_agente_publicacao.publicar` (normalização) executados em ambiente com app_context.
- **Validação:** Notícia sem link → erros; artigo sem cta/objetivo_lead → erros.
- **Pauta:** `obter_pauta_validada("noticia")` retorna primeira pendente e marca em_processamento; sem pauta retorna None e pipeline retorna False.
- **Compatibilidade:** index (noticias/artigos) e detalhe_noticia usam campos existentes; artigos com cta exibem bloco CTA no detalhe.

*(Recomendado rodar em ambiente completo: popular pautas, POST /executar-cleiton para noticia e artigo, conferir registros em noticias_portal e auditoria_gerencial.)*

---

## 5. Atualizações dos arquivos-guia

- **README_RUN.md:** Descrição da camada Júlia (pipeline, notícia curta, artigo, imagem, pautas); seção 5 “Pautas para a Júlia” com `popular_pautas_de_arquivo_json`.
- **README_DEPLOY.md:** Variáveis GEMINI_MODEL_TEXT, IMAGE_PROVIDER, GEMINI_MODEL_IMAGE, IMAGEM_FALLBACK_URL no exemplo .env.prod.
- **.env.example:** Comentários e variáveis para modelo textual e imagem IA.

---

## 6. Riscos residuais e próximos passos

- **Riscos:** (1) Gemini Imagen pode exigir projeto/API diferente; com fallback local estático fixo/versionado a publicação continua sem volatilidade visual e sem geração de arquivos de contingência por notícia. (2) Tabela `Pauta` vazia: sem coleta ou import, nenhuma missão Júlia publica; documentado em README. (3) Colunas novas em bases já existentes: migração via `_ensure_noticias_portal_columns`; em outros SGBDs (ex.: PostgreSQL) pode ser necessário ajustar sintaxe ALTER.
- **Próximos passos:** (1) Implementar coleta (RSS/API) em `processar_ciclo_noticias` gravando em `Pauta`. (2) Opcional: rota ou script para import em massa de pautas. (3) Revisar segurança do HTML em `conteudo_completo` (bleach/Markup) se conteúdo for editável por usuários.
