# Diagnostico de Homologacao e Publicacao

Este documento complementa o `README.md` principal com foco exclusivo em go/no-go de homologacao.
Use o `README.md` da raiz como fonte unica do estado funcional e visual atual.

## Estado Atual Confirmado

- pacote funcional atual integrado em `homolog`
- homolog validada no commit `efd54b5`
- producao atualizada e aprovada no commit `3d5332b`
- o ambiente local retornou para `homolog`, limpo e sincronizado
- a promocao recente ocorreu por cherry-pick seletivo
- a auditoria documental da Cleide segue sem nova migration
- tabela temporaria da auditoria Cleide estabilizada como artefato tecnico temporario, descartavel e sujeito a validacao humana
- owner operacional da tabela temporaria: Cleiton
- chat da Cleide permanece separado da extracao/atualizacao da tabela temporaria
- a validacao da tabela temporaria e humana e governada; nao e uma nova conversa de IA
- o BI executivo da auditoria em `/auditoria-frete` opera com 4 graficos executivos
- documentos legais estabilizados com storage persistente em `settings.data_dir`, sem dependencia operacional de `app/static/...`
- rota publica `/termos-de-uso` restaurada como entrada canonica para fluxo publico

## Escopo Operacional Sensivel

Nao tratar como opcional:

- governanca operacional por franquia
- autorizacao operacional usada pelo chat da Julia e pelos fluxos documentais governados
- identidade de consumo por conta, franquia e usuario
- execucao real da virada de ciclo em `/cron/executar-cleiton`
- migrations da cadeia ativa do ambiente
- disco persistente do Render montado e coerente com `APP_DATA_DIR` ou `RENDER_DISK_PATH`
- telas admin alinhadas com o backend
- garantia de que `app/cleiton_doc_tmp/` e residuos `tt_*.json`, `.cleanup_meta.json` e `.tmp_repro_unit*` nao entrem em versionamento
- garantia de que nenhum `.db` local ou banco embarcado foi versionado para essa entrega

## Go / No-Go

### No-Go

- migrations existentes do ambiente nao executadas ou nao confirmadas
- schema do ambiente nao validado
- health checks ou fluxos reais nao validados
- regressao que faca o chat da Cleide alterar a tabela temporaria

### Go

Somente quando todos forem verdadeiros:

1. migrations existentes preexistentes aplicadas sem erro
2. schema validado no banco alvo
3. health checks ok
4. cron protegido validado
5. telas admin ok
6. chat Julia validado com autorizacao por franquia
7. upload Roberto e chat Roberto validados no fluxo real da `/fretes`
8. `/auditoria-frete` validado com upload, status documental e `temp_table`
9. tabela temporaria validada como artefato temporario com revisao humana
10. preview/apply/undo de correcao validados quando houver diagnostico suportado
11. chat da Cleide validado sem recriar, alterar ou sobrescrever a `temp_table`
12. nenhum temporario local entrou em commit
13. producao aprovada apos deploy

## Ponto de atencao Render

- a operacao aprovada desta entrega usa `producao` como branch de producao
- manter a confirmacao do painel do Render como etapa obrigatoria antes de nova publicacao

## Historico operacional recente

- `homolog` contem `efd54b5`
- a promocao segura recente foi feita em `producao` por cherry-pick seletivo
- o commit promovido em producao foi `3d5332b`

## Referencia Principal

Detalhes funcionais, fluxos, regras criticas e experiencia visual vigente ficam consolidados no `README.md` da raiz.
