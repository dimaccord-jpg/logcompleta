# Entrega – Fase 4 (Designer + Publisher)

## 1. Resumo técnico

- **Designer (`run_julia_agente_designer.py`):** Recebe conteúdo validado e gera/ajusta assets por canal. Saída: url_imagem_master, assets_por_canal (dict), prompt_final, provider_utilizado. Configurável por DESIGNER_ENABLED, DESIGNER_PROVIDER, DESIGNER_ASPECT_RATIO_PADRAO, DESIGNER_CANAIS_ATIVOS. Fallback seguro em camadas (contextual local quando disponível; contingência fixa `/static/img/fallback-capa-v1.svg` apenas no último nível); não publica e não altera texto.
- **Publisher (`run_julia_agente_publisher.py`):** Publica no portal (obrigatório) e nos canais ativos (portal, linkedin, instagram, email). Registra status por canal (pendente/publicado/falha/ignorado) em `PublicacaoCanal`. Bloqueia duplicidade por (noticia_id, canal). Aplica janela e intervalo entre posts para canais externos (`PUBLISHER_JANELA_*`, `PUBLISHER_INTERVALO_*`). Modo mock para canais externos; `PUBLISHER_MODO=real` retorna falha explícita enquanto APIs reais não estiverem implementadas. Resultado: sucesso_total, sucesso_parcial, falha_total; auditoria com tipo_decisao=publisher.
- **Integração no pipeline:** Após qualidade: Designer → criação de NoticiaPortal (com url_imagem_master, assets_canais_json, status_publicacao=pendente) → Publisher (atualiza status_publicacao/publicado_em e cria PublicacaoCanal por canal). Missão só é sucesso se portal for publicado (falha_total = portal não publicado).
- **Modelos:** NoticiaPortal: url_imagem_master, assets_canais_json, status_publicacao, publicado_em. Nova tabela PublicacaoCanal (gerencial): noticia_id, mission_id, canal, status, tentativa_atual, max_tentativas, payload_envio_json, resposta_canal_json, erro_detalhe, criado_em, atualizado_em. Migração suave em infra para colunas de NoticiaPortal.
- **Retenção:** PublicacaoCanal incluído na limpeza de 18 meses (criado_em); contagem no detalhe do purge.
- **Auditoria:** designer e publisher registrados em auditoria_gerencial (tipo_decisao designer e publisher).

---

## 2. Arquivos criados/alterados

| Arquivo | Papel |
|--------|--------|
| **app/run_julia_agente_designer.py** | Novo. Assets por canal; url_imagem_master, assets_por_canal, prompt_final, provider_utilizado. |
| **app/run_julia_agente_publisher.py** | Novo. Publicação multicanal; status por canal; PublicacaoCanal; bloqueio de duplicidade. |
| **app/models.py** | NoticiaPortal: url_imagem_master, assets_canais_json, status_publicacao, publicado_em. Nova classe PublicacaoCanal. |
| **app/infra.py** | NOTICIAS_PORTAL_EXTRA_COLUMNS_FASE4 e _ensure_noticias_portal_columns_fase4. |
| **app/run_julia_agente_publicacao.py** | publicar() aceita url_imagem_master, assets_canais_json, status_publicacao. |
| **app/run_julia_agente_pipeline.py** | Integra Designer e Publisher; auditoria designer; sucesso só se portal publicado. |
| **app/run_cleiton_agente_retencao.py** | Inclusão de PublicacaoCanal na retenção 18 meses. |
| **app/.env.example** | DESIGNER_* e PUBLISHER_*. |
| **app/README_RUN.md** | Fase 4 Designer + Publisher. |
| **app/README_DEPLOY.md** | Variáveis Fase 4 no exemplo .env.prod. |
| **app/ENTREGA_FASE4_DESIGNER_PUBLISHER.md** | Este arquivo. |

---

## 3. Principais diffs

- **Pipeline:** Ordem: redação → imagem → qualidade → **Designer** (gerar_assets_por_canal) → **publicar()** (com url_imagem_master, assets_canais_json, status_publicacao=pendente) → **publicar_multicanal()** (portal + canais; atualiza NoticiaPortal e cria PublicacaoCanal). Retorno True apenas se pub_resultado != RESULTADO_FALHA_TOTAL.
- **Publisher:** Portal: atualiza status_publicacao e publicado_em da notícia e cria PublicacaoCanal(portal). Canais externos: em mock cria PublicacaoCanal com status publicado ou ignorado (duplicidade). _ja_publicado_canal(noticia_id, canal) evita duplicata.
- **Designer:** gerar_assets_por_canal() sempre retorna dict com url_imagem_master, assets_por_canal, prompt_final, provider_utilizado; fallback por canal quando url vazia.
- **Retenção:** PublicacaoCanal.query.filter(criado_em < limite).delete(); count_pub no detalhe do purge_dados.

---

## 4. Testes executados e resultados

- **Designer:** gerar_assets_por_canal(None, "logística", "noticia") retorna url_imagem_master (fallback estático/local fixo versionado), assets_por_canal com canais de DESIGNER_CANAIS_ATIVOS, provider_utilizado.
- **Publisher:** Com noticia aprovada, publicar_multicanal() atualiza portal e cria PublicacaoCanal para cada canal; segunda chamada para mesmo noticia_id+canal gera ignorado por duplicidade.
- **Pipeline:** Com conteúdo validado, fluxo Designer → publicar → publicar_multicanal conclui; falha_total (ex.: portal não gravado) faz pipeline retornar False.
- **Regressão:** web.py inalterado; Cleiton e Júlia (noticia/artigo) seguem compatíveis; Fase 3 (só pauta aprovada) preservada.

---

## 5. Atualizações de documentação

- **README_RUN.md:** Fase 4 (Designer, Publisher, canais, status, PublicacaoCanal, retenção).
- **README_DEPLOY.md:** Variáveis Fase 4 no exemplo .env.prod.
- **.env.example:** Bloco "FASE 4: DESIGNER + PUBLISHER" com todas as variáveis.

---

## 6. Riscos residuais e próximos passos (Fase 5)

- **Riscos:** (1) Colunas Fase 4 em bases existentes dependem de migração SQLite (ALTER TABLE); outros SGBDs podem exigir ajuste. (2) Canais externos em modo real ainda não implementados (APIs LinkedIn/Instagram/email); agora retornam falha explícita para não mascarar resultado. (3) Janela/intervalo de publicação já aplicados no Publisher para canais externos; validação operacional fina por canal (calendário por rede) permanece para Fase 5.
- **Próximos passos Fase 5 (sugestão):** (1) Painel admin (listar publicações por canal, reprocessar canal). (2) Modo real para um ou mais canais (API LinkedIn, etc.). (3) Aplicar janela e intervalo entre posts no Publisher. (4) Métricas de publicação (sucesso/falha por canal).
