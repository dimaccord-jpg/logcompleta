# Diagnostico de Homologacao e Publicacao

Este documento complementa o `README.md` principal com foco exclusivo em go/no-go de homologacao.
Use o `README.md` da raiz como fonte unica do estado funcional e visual atual.

## Estado Atual Confirmado

- pacote funcional da fase atual integrado localmente
- homolog validada no commit `17675d0`
- producao atualizada e aprovada apos merge `bad8990`
- apos o push, `origin/producao` ficou no commit `bad8990`
- o ambiente local retornou para `homolog`, limpo e sincronizado
- estabilizacao da auditoria documental da Cleide promovida sem nova migration
- tabela temporaria da auditoria Cleide estabilizada como artefato tecnico temporario, descartavel e sujeito a validacao humana
- owner operacional da tabela temporaria: Cleiton
- chat da Cleide permanece separado da extracao/atualizacao da tabela temporaria
- modal da tabela temporaria mantido em modo somente leitura
- a validacao da tabela temporaria e humana e governada; nao e uma nova conversa de IA
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
9. tabela temporaria validada como artefato temporario com revisao humana obrigatoria
10. chat da Cleide validado sem recriar, alterar ou sobrescrever a `temp_table`
11. nenhum temporario local entrou em commit
12. producao aprovada apos deploy

## Ponto de atencao Render

- a configuracao versionada em `render.yaml` ainda aponta o servico de producao para `branch: main`
- o fluxo operacional aprovado desta entrega usa `producao` como branch de producao
- manter esse item documentado como divergencia a validar no painel do Render antes de nova publicacao, sem forcar alteracao de codigo nesta tarefa

## Referencia Principal

Detalhes funcionais, fluxos, regras criticas e experiencia visual vigente ficam consolidados no `README.md` da raiz.
