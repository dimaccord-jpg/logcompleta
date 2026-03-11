# Checklist de validação – Camada gerencial Cleiton (Tópico 1)

Use este checklist após deploy ou alterações na orquestração.

## Rotas e compatibilidade

- [ ] **/executar-cleiton** — POST (usuário logado) executa ciclo gerencial e redireciona com flash de sucesso.
- [ ] **Rotas de login/fretes/home** — Sem regressão; login, cálculo de fretes e página inicial funcionando.

## Indicadores da Home

- [ ] **Coleta desacoplada** — `python -m app.finance` atualiza `app/indices.json` sem depender de acesso web.
- [ ] **Contrato de leitura** — Home (`/`) lê o JSON e extrai o último item de `historico` para renderização do ticker.
- [ ] **Renderização** — Dólar, Petróleo, BDI e FBX aparecem preenchidos no topo da página inicial.

## Comportamento do Cleiton

- [ ] **Janela de publicação** — Fora da janela configurada (ex.: 6h–22h), o ciclo registra "ignorado" na auditoria e não despacha.
- [ ] **Frequência** — Dois ciclos seguidos dentro do intervalo (ex.: 3h) resultam em o segundo ser "ignorado por frequência".
- [ ] **Prioridade e retries** — Payload enviado aos agentes contém `prioridade` e `tentativa_atual`; regras vêm de `ConfigRegras` (sem hardcode).
- [ ] **Nenhuma geração de conteúdo no Cleiton** — O orquestrador apenas decide e despacha; o conteúdo final é gerado pela Júlia (ou outros agentes operacionais).

## Auditoria e persistência

- [ ] **Auditoria persistida** — Tabela `auditoria_gerencial` (bind `gerencial`) recebe registros para cada decisão (orquestracao, dispatch, purge_dados, purge_imagens).
- [ ] **Missões** — Tabela `missao_agente` registra `mission_id`, tipo, status; dispatch atualiza status (enviado/sucesso/falha).

## Retenção

- [ ] **18 meses dados** — Registros em `noticias_portal` e `leads` mais antigos que o limite são removidos (ou política aplicada); evento registrado em `auditoria_gerencial` (purge_dados).
- [ ] **2 meses imagens** — Referências a imagens antigas (ex.: `url_imagem` em notícias) são limpas; evento registrado (purge_imagens).

## Ambiente

- [ ] **APP_ENV** — Com `APP_ENV=dev`, `APP_ENV=homolog` ou `APP_ENV=prod`, o arquivo carregado é `app/.env.{dev|homolog|prod}` (caminho absoluto), independente do CWD ao rodar scripts.

## Evidências sugeridas

- Log do ciclo: "Cleiton orquestrador: iniciando ciclo gerencial" e "ciclo gerencial encerrado".
- Consulta ao banco: `SELECT * FROM auditoria_gerencial ORDER BY created_at DESC LIMIT 10;` (bind gerencial).
- Após rodar retenção: entradas com `tipo_decisao` em ('purge_dados', 'purge_imagens').
