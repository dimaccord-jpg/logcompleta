# Integrações de IA e Privacidade

Referência auditada em 2026-08-19.

## Boundary de masking

O masking implementado hoje é outbound e field-aware.

- serviço: `app/services/external_ai_masking.py`
- atua sobre `dict`, `list`, `tuple` e escalares já estruturados
- não reescreve o dado persistido internamente
- não persiste o mapa de tokens

## Campos tratados explicitamente

- `display_name`
- `source_file_name`
- `filename`
- `email`
- `customer_email`
- `phone`
- `telefone`
- `cpf`

## Formato dos tokens

- arquivo: `[ARQUIVO_N].ext`
- e-mail: `[EMAIL_N]`
- telefone: `[TEL_N]`
- CPF: `[CPF_N]`

O mapeamento é estável apenas dentro da operação em memória.

## Aplicação nos domínios

O masking outbound foi incorporado às boundaries adequadas de:

- Júlia
- Cleide
- AgenteCompara

Roberto ficou fora desta mudança.

## Limitações reais

Estas limitações precisam permanecer explícitas:

- o serviço não varre texto livre genericamente
- strings sem chave autorizada não são inspecionadas nem alteradas
- OCR e texto livre podem carregar PII não reconhecida
- campos logísticos não entram em anonimização genérica universal
- IDs internos não são removidos automaticamente de forma abrangente
- PDF bruto enviado pela Gemini Files API não é reescrito byte a byte
- no caso de arquivo PDF, o nome/display name pode ser neutralizado, mas o conteúdo binário não é sanitizado universalmente

## Consequência documental

Não é correto prometer:

- anonimização total antes de IA externa
- sanitização universal de documentos
- scanner genérico de PII em qualquer texto ou anexo

O que existe é mascaramento estruturado em pontos específicos de saída.
