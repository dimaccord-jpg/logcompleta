# Deploy em Homolog/Produção

Este documento complementa o `README.md` principal com a sequência segura de deploy.

## Premissas

- `APP_ENV` definido explicitamente;
- `DATABASE_URL` apontando para o banco correto;
- segredos no provedor, não em arquivo versionado;
- persistência configurada para runtime;
- migrations disponíveis e estratégia de execução definida.

## Pacote Que Deve Subir Junto

- governança operacional Cleiton;
- identidade de consumo por conta/franquia;
- upload Roberto com billing técnico;
- painel admin;
- pipeline editorial e chat da Júlia;
- migrations da cadeia ativa;
- testes mínimos da Fase 2.

## Sequência Segura

1. publicar código no serviço alvo;
2. validar variáveis e persistência;
3. executar migrations no banco alvo;
4. confirmar `current` e `head`;
5. validar health checks;
6. validar cron protegido;
7. validar telas admin;
8. validar fluxos reais:
   - chat da Júlia
   - upload Roberto
   - detalhe de notícia/artigo

## Checklist Pós-Deploy

- `GET /health/liveness` retorna 200;
- `GET /health/readiness` retorna 200;
- `/cron/executar-cleiton` protegido corretamente;
- chat Júlia respeita autorização por franquia;
- chat Júlia mantém markdown seguro, sugestões e busca web contextual;
- páginas `/noticia/<id>` exibem botão `Voltar Para Home`;
- upload Roberto continua operacional.

## Riscos de Regressão

- publicar sem migrations válidas;
- alterar governança operacional sem revisar chat/upload;
- publicar frontend sem backend correspondente ou vice-versa;
- tratar homolog como concluída sem validar schema e fluxos reais.

## Referência Principal

Use o `README.md` da raiz como fonte principal do cenário atual.
