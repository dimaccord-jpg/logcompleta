# Estado Oficial Consolidado

Fotografia validada em 2026-07-16: `homolog@6701a53`, promovida em `producao@0c3a133`, com backup `backup/producao-antes-cleide-insights-20260716`.

## Produto

- Cleiton: copiloto de discovery e gestor da governança operacional.
- Júlia: consultoria operacional autenticada e contexto documental temporário.
- Roberto: BI e previsão quantitativa de fretes em `/fretes`.
- Cleide: auditoria cobrado versus esperado em `/auditoria-frete`.
- `/cleide-bi-frete`: superfície anterior da Cleide, marcada como legado.

## Entrega

- auditoria com tabela negociada, cobertura, fiscal, lote, cálculo, diagnósticos e correções;
- BI executivo com quatro gráficos e filtros locais;
- chat analítico isolado, inicialmente bloqueado e liberado pelo backend após BI válido;
- billing operacional idempotente por linhas no upload e no reprocessamento explícito;
- consumo operacional separado do consumo de IA;
- nenhuma migration, tabela ou coluna nova.

## Ambientes e Git

- `homolog`: desenvolvimento e validação;
- `producao`: branch operacional de produção;
- `main`: branch padrão remota, não operacional nesta fotografia;
- o `render.yaml` ainda aponta produção para `main`; a divergência deve ser conferida/corrigida no processo operacional, nunca presumida.

## Testes e temporários

No commit `6701a53`, 1.896 testes foram aprovados. Este número é uma fotografia, não expectativa fixa para commits futuros.

`app/cleiton_doc_tmp/`, `tt_*.json`, `.tmp_pytest_fixture/`, `.venv/`, caches e bancos locais não fazem parte do deploy. Inspecione JSON antes de limpar para não remover arquivos operacionais fora de pasta temporária.
