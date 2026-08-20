# Consentimento de Privacidade e Marketing

Referência auditada em 2026-08-19.

## Escopo

Esta camada cobre consentimento para marketing e medição opcional, especialmente:

- Meta Pixel
- OpenAI Ads Pixel

Ela não substitui:

- cookie/sessão necessários da aplicação
- autenticação
- dados operacionais internos

## Cookie de consentimento

- nome: `af_privacy_marketing`
- formato versionado: `v1:accepted` ou `v1:rejected`
- ausência, formato inválido ou versão desconhecida resultam em `unknown`
- TTL configurado: `180` dias
- cookie first-party
- `HttpOnly=true`
- `SameSite=Lax`
- `path=/`
- `secure` acompanha o ambiente da aplicação

## Endpoint oficial

- `POST /api/privacy/marketing-consent`

Contrato aceito:

- payload JSON com `decision`
- decisões válidas: `accepted` ou `rejected`

## Comportamento operacional

- `accepted`: SDKs e eventos opcionais podem operar se a configuração do pixel existir
- `rejected`: SDKs e eventos opcionais não devem operar
- `unknown`: o sistema não trata como aceite

Há também preservação temporária de flags pendentes de marketing em sessão:

- enquanto o consentimento é `unknown`, eventos pendentes podem aguardar decisão
- ao mudar para `accepted`, esses pendentes podem ser consumidos uma vez
- ao mudar para `rejected`, os pendentes são descartados

## O que a rejeição faz

- limpa apenas estado específico de marketing relevante
- não destrói a sessão necessária da aplicação
- não bloqueia onboarding, login ou navegação operacional

## UX existente

- banner de consentimento
- painel de preferências
- alteração posterior da decisão
- acesso às preferências mesmo anonimamente

O problema visual em que o banner poderia cobrir elementos da sidebar já foi corrigido e faz parte do smoke aprovado desta entrega.

## Limites documentados

- não existe consentimento implícito por ausência de cookie
- pixels só devem carregar quando a configuração existir e o consentimento estiver em `accepted`
- esta camada é específica de marketing/medição opcional, não de toda a privacidade da aplicação
