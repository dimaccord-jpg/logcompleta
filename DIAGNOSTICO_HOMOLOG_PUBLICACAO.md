# Diagnostico de Homologacao e Publicacao

Documento complementar ao `README.md`, focado em go/no-go operacional.

## Estado atual confirmado

- `homolog` local e `origin/homolog` estao em `d02ce15`
- a versao aprovada em producao no repositório esta em `producao@6efa2e2`
- a auditoria documental da Cleide segue sem migration propria nesta entrega
- a `temp_table` continua artefato tecnico temporario, descartavel e sujeito a revisao humana
- owner operacional da `temp_table`: Cleiton
- o chat da Cleide permanece separado da extracao e da atualizacao da `temp_table`
- a validacao da `temp_table` e humana e governada
- o BI executivo da auditoria em `/auditoria-frete` opera com 4 graficos executivos de impacto financeiro
- o `render.yaml` versionado define homolog em `homolog` e producao em `main` com deploy manual

## Escopo operacional sensivel

Nao tratar como opcional:

- governanca operacional por franquia
- autorizacao operacional usada por Julia, Roberto e fluxos documentais governados
- identidade de consumo por conta, franquia e usuario
- execucao real do ciclo em `/cron/executar-cleiton`
- cadeia de migrations ja existente no ambiente alvo
- disco persistente coerente com `APP_DATA_DIR` ou `RENDER_DISK_PATH`
- telas admin alinhadas com backend e `ConfigRegras`
- garantia de que `app/cleiton_doc_tmp/` e residuos locais nao entram em versionamento
- garantia de que nenhum `.db` local foi publicado

## Go / No-Go

### No-Go

- migrations preexistentes do ambiente nao executadas ou nao confirmadas
- schema do ambiente nao validado
- health checks ou fluxos reais nao validados
- regressao que faca o chat da Cleide alterar a `temp_table`
- divergencia nao resolvida entre branch efetiva do Render e branch planejada para a promocao

### Go

Somente quando todos forem verdadeiros:

1. cadeia de migrations preexistente do ambiente validada
2. schema do banco alvo validado
3. health checks ok
4. cron protegido validado
5. telas admin ok
6. chat Julia validado com autorizacao por franquia
7. upload Roberto e chat Roberto validados em `/fretes`
8. `/auditoria-frete` validado com upload, status documental e `temp_table`
9. `temp_table` validada como artefato temporario com revisao humana
10. preview/apply/undo de correcao validados quando houver diagnostico suportado
11. chat da Cleide validado sem recriar, alterar ou sobrescrever a `temp_table`
12. nenhum temporario local entrou em commit
13. producao validada apos deploy manual quando a promocao for executada

## Ponto de atencao Render

- o `render.yaml` versionado hoje usa `main` como branch do servico de producao
- o repositório ainda possui `producao@6efa2e2` como referencia aprovada
- por isso, a confirmacao do painel do Render continua obrigatoria antes de nova publicacao

## Historico operacional recente

- `homolog` contem `d02ce15`
- `producao` contem `6efa2e2`
- ambos os commits carregam a mesma mensagem de entrega: `feat: aprimora auditoria de frete da Cleide`

## Referencia principal

Detalhes funcionais, fluxos, regras criticas e experiencia visual vigente ficam consolidados no `README.md` da raiz.
