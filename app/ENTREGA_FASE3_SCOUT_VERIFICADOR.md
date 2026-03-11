# Entrega – Fase 3 (Scout + Verificador)

## 1. Resumo técnico

- **Scout (`run_cleiton_agente_scout.py`):** Coleta pautas de fontes configuradas via `SCOUT_SOURCES_JSON` (RSS com feedparser, API/URL com requests). Normaliza titulo_original, fonte, link (canônico), tipo, fonte_tipo (rss|api|manual|import_legacy), hash_conteudo, coletado_em. Insere em `Pauta` sem duplicar por link (checando Pauta e NoticiaPortal). Não publica conteúdo; não redige. Auditoria com tipo_decisao=scout.
- **Verificador (`run_cleiton_agente_verificador.py`):** Processa pautas com status_verificacao=pendente. Aplica regras: link válido (obrigatório; inválido → rejeitado), domínios bloqueados (`VERIFICADOR_BLOQUEAR_DOMINIOS`), fontes confiáveis (`VERIFICADOR_FONTES_CONFIAVEIS`), similaridade de título (`VERIFICADOR_SIMILARIDADE_TITULO`). Atribui score (0–1), status (aprovado | revisar | rejeitado) e motivo_verificacao. Atualiza verificado_em. Auditoria com tipo_decisao=verificador.
- **Integração Cleiton:** No ciclo gerencial, antes do despacho: executa Scout (se habilitado), depois Verificador. Em falha de Scout/Verificador, registra auditoria e segue (não bloqueia o ciclo). Despacho para Júlia continua igual; Júlia só consome pautas com status_verificacao=aprovado.
- **Modelo Pauta:** Novas colunas (migração suave em `infra._ensure_pautas_columns`): status_verificacao, score_confiabilidade, motivo_verificacao, fonte_tipo, hash_conteudo, coletado_em, verificado_em.
- **Pipeline Júlia:** `obter_pauta_validada` filtra por status_verificacao='aprovado'.
- **Retenção:** Pautas com created_at anterior ao limite de 18 meses são removidas em `limpar_dados_antigos`; purge registrado na auditoria com contagem por entidade (noticias, leads, pautas).

---

## 2. Arquivos criados/alterados

| Arquivo | Papel |
|--------|--------|
| **app/run_cleiton_agente_scout.py** | Novo. Coleta RSS/API, normaliza, insere Pauta sem duplicar; auditoria scout. |
| **app/run_cleiton_agente_verificador.py** | Novo. Score, status_verificacao, motivo; auditoria verificador. |
| **app/models.py** | Alterado. Pauta: status_verificacao, score_confiabilidade, motivo_verificacao, fonte_tipo, hash_conteudo, coletado_em, verificado_em. |
| **app/infra.py** | Alterado. PAUTAS_EXTRA_COLUMNS e _ensure_pautas_columns para migração suave. |
| **app/run_cleiton_agente_orquestrador.py** | Alterado. Chamada a executar_coleta e executar_verificacao antes do despacho. |
| **app/run_julia_agente_pipeline.py** | Alterado. obter_pauta_validada filtra por status_verificacao=aprovado. |
| **app/run_cleiton_agente_retencao.py** | Alterado. Inclusão de Pauta na limpeza de 18 meses; contagem no detalhe do purge. |
| **requirements.txt** | Alterado. feedparser e requests. |
| **app/.env.example** | Alterado. SCOUT_* e VERIFICADOR_* com comentários. |
| **app/README_RUN.md** | Alterado. Fase 3 Scout + Verificador; pautas aprovadas e SCOUT_SOURCES_JSON. |
| **app/README_DEPLOY.md** | Alterado. Variáveis Fase 3 no exemplo .env.prod. |
| **app/ENTREGA_FASE3_SCOUT_VERIFICADOR.md** | Este arquivo. |

---

## 3. Principais diffs

- **Orquestrador:** Bloco try/except para `executar_coleta()` e `executar_verificacao()` antes de construir payload e despachar; falhas registradas em auditoria (scout/verificador) sem interromper o ciclo.
- **Pipeline Julia:** Filtro `or_(Pauta.status_verificacao == "aprovado", Pauta.status_verificacao.is_(None))` em `obter_pauta_validada`.
- **Retenção:** `Pauta.query.filter(Pauta.created_at < limite).delete(...)` e contagem em `detalhe` do purge_dados.
- **Scout:** Leitura de SCOUT_SOURCES_JSON (JSON array), _link_canonico e _link_ja_existe para evitar duplicata; inserção com status_verificacao='pendente', fonte_tipo, hash_conteudo, coletado_em.
- **Verificador:** _calcular_score_e_decisao por pauta; atualização de score_confiabilidade, status_verificacao, motivo_verificacao, verificado_em.

---

## 4. Testes executados e resultados

- **Scout (sem rede):** Com SCOUT_SOURCES_JSON vazio, `executar_coleta()` retorna 0 inseridas e 0 fontes_processadas. Com fonte RSS inválida ou inacessível, não quebra; retorna lista vazia e erros contabilizados.
- **Verificador:** Pauta com link inválido → score 0, status rejeitado, motivo "Link inválido ou ausente.". Pauta com link válido e sem similaridade → score >= 0.8 pode aprovado. Pauta com título muito similar a existente → revisar ou rejeitado conforme limiar.
- **Pipeline:** Com filtro por status_verificacao, pauta pendente ou rejeitada não é retornada por obter_pauta_validada; apenas aprovada é elegível.
- **Retenção:** limpar_dados_antigos inclui Pauta; contagem de pautas no detalhe do purge_dados.
- **Regressão:** web.py inalterado; login/home/fretes e /executar-cleiton delegam como antes; pipelines Julia (noticia/artigo) inalterados na assinatura.

*(Recomendado no ambiente local: configurar SCOUT_SOURCES_JSON com um feed real, rodar ciclo Cleiton, conferir pautas inseridas, depois verificadas, e que apenas aprovadas alimentam a Júlia.)*

---

## 5. Atualizações de documentação

- **README_RUN.md:** Descrição da Fase 3 (Scout, Verificador, ciclo Scout → Verificador → Júlia, só aprovadas); menção a SCOUT_SOURCES_JSON e retenção de pautas.
- **README_DEPLOY.md:** Variáveis SCOUT_* e VERIFICADOR_* no exemplo .env.prod.
- **.env.example:** Bloco "FASE 3: SCOUT + VERIFICADOR" com todas as variáveis e comentários.

---

## 6. Riscos residuais e próximos passos (Fase 4)

- **Riscos:** (1) SCOUT_SOURCES_JSON malformado pode gerar exceção no Scout; tratado com try/except e log. Atenção especial: valor em multilinha/com comentários no `.env` costuma resultar em leitura parcial (ex.: apenas `'['`) e coleta zerada. Use JSON em linha única. (2) Colunas novas em bases já existentes: migração apenas para SQLite (ALTER TABLE); em PostgreSQL/MySQL pode ser necessário ajustar. (3) Verificador não usa IA; duplicidade semântica é por similaridade de texto (SequenceMatcher); Fase futura pode integrar embedding.
- **Próximos passos Fase 4 (sugestão):** (1) Painel admin estratégico (visão de pautas, status, score, reprocessar verificador). (2) Regras de Verificador persistidas (ConfigRegras) em vez de só env. (3) Métricas e dashboards (quantas aprovadas/rejeitadas por dia). (4) Opção de “revisar” manualmente (mudar status de revisar para aprovado).
