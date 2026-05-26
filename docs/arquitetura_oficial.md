# Arquitetura Oficial

Data de consolidacao: `2026-05-26`

Este documento registra a arquitetura oficial do LogCompleta / Agentefrete no estado real atual.

## 1. Principios oficiais

Nao criar:

- rota paralela
- blueprint paralelo
- bypass operacional
- fallback mascarando erro
- gambiarra operacional

Sempre:

- causa raiz primeiro
- auditoria
- observabilidade
- governanca
- trilho oficial
- testes completos
- contratos claros
- runtime rastreavel

## 2. Organograma oficial

Fluxo oficial consolidado:

`Roberto -> Estrategia -> Cleiton -> Governanca + missao + orquestracao -> Julia -> Redacao + imagem + publicacao`

Em paralelo operacional e subordinada ao trilho do Cleiton:

`Cleide -> Documentos + perguntas + IA operacional`

## 3. Responsabilidades por agente

### Roberto

- estrategia
- produto
- direcionamento operacional
- camada estrategica

### Cleiton

- governanca
- missao operacional
- retencao
- orquestracao
- decisao operacional
- execucao sistemica

### Julia

- editorial
- redacao
- imagem
- publicacao
- conteudo publico

### Cleide

- documentos
- upload
- perguntas
- leitura operacional
- extracao contextual
- suporte operacional IA

## 4. Fronteiras oficiais

### Julia

- a rota oficial do chat continua `/api/chat_julia`;
- o pipeline oficial continua `pauta -> redacao -> imagem -> qualidade -> publicacao`;
- artigo em fallback de redacao nao gera imagem, nao publica e encerra o pipeline com auditoria;
- conteudo publico usa a superficie `/noticia/<id>` com share e SEO canonicos.

### Cleide

- pagina publica oficial: `/auditoria-frete`;
- namespace oficial: `cleide`;
- health oficial: `/api/cleide/health`;
- template oficial de upload: `/api/cleide/template`;
- upload oficial: `/api/cleide/upload`;
- status oficial: `/api/cleide/upload/status`;
- limpeza oficial: `/api/cleide/upload/clear`;
- filtro analitico oficial: `/api/cleide/dashboard/filter`;
- chat oficial: `/api/chat_cleide`.

### Roberto

- upload oficial: `/api/roberto/upload`;
- limpeza oficial do upload: `/api/roberto/clear_upload`;
- chat oficial: `/api/chat_roberto`;
- superficie principal: `/fretes`.

## 5. Trilha oficial de governanca

Toda operacao relevante continua sob governanca do Cleiton:

- autorizacao operacional por franquia
- identidade de consumo por `conta_id` / `franquia_id` / `usuario_id`
- `IaConsumoEvento`
- `ProcessingEvent`
- auditoria gerencial
- billing tecnico
- reconciliacao de franquia

Nao existe contrato oficial que permita consumo de IA, upload operacional ou publicacao por fora dessa trilha.

## 6. Runtime homolog e producao

Contrato oficial de ambiente:

- `APP_ENV` e obrigatorio e aceita apenas `dev`, `homolog` ou `prod`;
- nao existe fallback implicito para `dev`;
- `DATABASE_URL` deve ser PostgreSQL;
- `PUBLIC_BASE_URL` define a base publica canonica do ambiente;
- `settings.data_dir` e o diretorio persistente oficial para storage operacional;
- homolog e producao forcam `debug=False`.

## 7. Regras de promocao

Para homolog e producao, o documento oficial precisa refletir o estado promovido, nao apenas alteracoes em homolog.

Checklist minimo:

- contratos de rota confirmados em codigo
- regras de fallback confirmadas em testes
- runtime efetivo confirmado em `settings.py`
- observabilidade confirmada em payload, logs ou persistencia
- promocao respaldada pela suite automatizada
