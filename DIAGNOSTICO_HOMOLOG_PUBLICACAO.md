# Diagnóstico de Homologação e Publicação

Fotografia operacional de 2026-07-16:

- `homolog@6701a53` homologado;
- `producao@0c3a133` promovido;
- backup `backup/producao-antes-cleide-insights-20260716`;
- 1.896 testes aprovados no commit de referência;
- nenhuma migration, tabela ou coluna criada.

## Go / No-Go

No-Go se: worktree contém resíduo não inspecionado; suíte falha; migration necessária está ausente; branch do painel Render não corresponde à promoção; autenticação, autorização, billing ou health check falham.

Go somente após: suíte completa; validação de `/health`; login e `next`; Júlia; Roberto; fluxo ponta a ponta da Cleide; BI/chat pós-BI; CTA de plano; eventos operacionais/IA; inspeção de temporários e schema.

## Branch e Render

O fluxo validado promove `homolog` para `producao` com backup e merge `--no-ff`. `main` não é a branch operacional de produção desta fotografia. Como o `render.yaml` ainda contém `branch: main` para `logcompleta-web-prod`, confirme o painel e resolva a divergência antes de disparar produção. Não presuma que o YAML desatualizado muda o processo validado.

Procedimento completo: `app/README_DEPLOY.md`. Contrato da auditoria: `docs/cleide_auditoria_operacional.md`.
