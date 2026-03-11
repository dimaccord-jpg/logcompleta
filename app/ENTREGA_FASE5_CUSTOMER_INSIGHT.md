# Entrega Fase 5 – Customer Insight

**Projeto:** Log Completa  
**Data:** 2026-03-08  
**Fase:** 5 – Customer Insight (métricas por conteúdo/canal e recomendações estratégicas)

---

## 1. Objetivo da Fase

- Medir desempenho por conteúdo e canal.
- Retroalimentar a estratégia do Cleiton com base em dados.
- Produzir recomendações objetivas (tema, tipo, canal, frequência, horário).

---

## 2. AS-IS (antes da Fase 5)

- Cleiton orquestrava ciclo: regras → Scout → Verificador → dispatch → Julia → retenção.
- Não havia métricas consolidadas por notícia/canal nem recomendações estratégicas persistidas.
- Auditoria não contemplava decisões do tipo *insight*.

---

## 3. TO-BE (após a Fase 5)

- **InsightCanal** (bind gerencial): métricas por `noticia_id`, `mission_id`, `canal` (impressões, cliques, CTR, leads, taxa_conversao, engajamento, score_performance, origem_dado, coletado_em, processado_em).
- **RecomendacaoEstrategica**: recomendações com contexto JSON, texto da recomendação, prioridade, status (pendente|aplicada|descartada), criado_em.
- Agente **Customer Insight** (Cleiton): ao final do ciclo, aciona coleta de métricas (Julia), classifica desempenho (manter/escalar/ajustar/pausar), gera e persiste recomendações, audita com `tipo_decisao=insight`. Em falha, registra auditoria e não quebra o ciclo.
- Agente **Métricas** (Julia): consolida métricas a partir de PublicacaoCanal e NoticiaPortal na janela configurada; persiste em InsightCanal; modo mock (e futuro real).
- Retenção 18 meses aplicada a InsightCanal e RecomendacaoEstrategica; purge registrado na auditoria.
- Configurações: INSIGHT_ENABLED, INSIGHT_COLETA_MODO, INSIGHT_JANELA_DIAS, INSIGHT_SCORE_ESCALAR, INSIGHT_SCORE_PAUSAR, INSIGHT_MIN_IMPRESSOES.
- Rota `POST /executar-insight` mantida por compatibilidade técnica, alinhada ao objetivo principal: aciona o mesmo ciclo completo do Cleiton que `/executar-cleiton` (o Insight roda ao final do ciclo, não existe atalho “somente Insight”).

---

## 4. Arquivos criados

| Arquivo | Papel |
|--------|--------|
| `app/run_cleiton_agente_customer_insight.py` | Motor de insight: classificação, geração de recomendações, persistência, auditoria. |
| `app/run_julia_agente_metricas.py` | Captura/consolidação de métricas por canal; persistência em InsightCanal (mock/real). |
| `app/ENTREGA_FASE5_CUSTOMER_INSIGHT.md` | Guia de entrega da Fase 5. |

---

## 5. Arquivos alterados

| Arquivo | Alteração |
|--------|-----------|
| `app/models.py` | Novos modelos `InsightCanal` e `RecomendacaoEstrategica` (bind gerencial). Comentário em AuditoriaGerencial sobre tipo_decisao `insight`. |
| `app/run_cleiton_agente_orquestrador.py` | Chamada a `executar_insight(app_flask)` ao final do ciclo; try/except com auditoria em falha. |
| `app/run_cleiton_agente_retencao.py` | Purge de InsightCanal e RecomendacaoEstrategica (18 meses); contagem incluída no detalhe do purge_dados. |
| `app/web.py` | Rota `POST /executar-insight` mantida por compatibilidade, alinhada ao mesmo ciclo completo do Cleiton que `/executar-cleiton` (Insight ao final, sem ciclo separado). |
| `app/.env.example` | Bloco Fase 5: INSIGHT_* . |
| `app/README_RUN.md` | Seção Fase 5 – Customer Insight e variáveis. |
| `app/README_DEPLOY.md` | Exemplo de variáveis Fase 5 em .env.prod. |

---

## 6. Critérios de aceite (DONE)

- [x] Customer Insight implementado com persistência (InsightCanal, RecomendacaoEstrategica).
- [x] Recomendações estratégicas geradas e auditadas (tipo_decisao=insight).
- [x] Integração com ciclo gerencial sem quebrar fases anteriores (insight ao final; falha só audita).
- [x] Retenção 18 meses aplicada às novas entidades; purge com contagem na auditoria.
- [x] Documentação atualizada (.env.example, README_RUN, README_DEPLOY, guia de entrega).

---

## 7. Testes recomendados

1. **Métricas por canal:** Rodar ciclo com publicações; verificar registros em `InsightCanal` (e possivelmente recomendações em `RecomendacaoEstrategica`).
2. **Score e classificação:** Conferir que scores e classificações (escalar/pausar) respeitam INSIGHT_SCORE_ESCALAR e INSIGHT_SCORE_PAUSAR.
3. **Auditoria:** Consultar `auditoria_gerencial` com `tipo_decisao='insight'`.
4. **Não regressão:** Executar fluxo Scout → Verificador → Julia → Designer → Publisher e validar que o ciclo completa e que a rota `/executar-cleiton` continua funcionando.
5. **Retenção:** Simular dados antigos (coletado_em/criado_em além de 18 meses) e rodar retenção; conferir purge e registro em auditoria.

---

## 8. Riscos residuais e próximos passos (Fase 6)

- **Risco:** Métricas em modo mock; quando houver APIs reais, implementar em `run_julia_agente_metricas` (INSIGHT_COLETA_MODO=real).
- **Fase 6:** Utilizar recomendações pendentes no dispatch (Cleiton ler `RecomendacaoEstrategica` ao decidir tema/canal/prioridade) e eventual UI para visualizar recomendações e marcar aplicada/descartada.

---

## 9. Como preparar os próximos prompts

Para cada nova fase, manter o padrão:

- **Objetivo da fase** (1–2 frases).
- **AS-IS** (como está antes).
- **TO-BE** (como deve ficar após).
- **Arquivos-alvo** (criar/alterar).
- **Regras de aceite** claras.
- **Testes obrigatórios** (incluindo não regressão e persistência/auditoria).
- **Atualizações de documentação** obrigatórias.
- **Riscos e limites** (o que fica para depois).
- **Checklist:** responsabilidade gerencial x operacional; contrato de payload; status e sucesso/falha/parcial; não regressão; persistência/auditoria; retenção quando impactar dados.
