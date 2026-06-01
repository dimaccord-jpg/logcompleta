# Execucao Local

Data de consolidacao: `2026-05-29`  
Commit de referencia: `20fa165`

## 1. Objetivo

Executar o projeto localmente e validar o estado atual do onboarding, dashboard e agentes.

## 2. Premissas

- `APP_ENV=dev`
- `DATABASE_URL` apontando para PostgreSQL
- `PUBLIC_BASE_URL` coerente com o host local
- `APP_DATA_DIR` configurado

## 3. Validacao manual minima

1. abrir a Home
2. conversar com o Copilot
3. confirmar sessao anonima
4. confirmar limite de 5 interacoes
5. usar `Nova conversa`
6. validar CTA de login
7. validar dashboard admin
8. validar Roberto
9. validar Cleide

## 4. Pontos que merecem atencao

- a Home usa onboarding discovery, nao Julia operacional;
- Julia operacional exige login;
- onboarding nao abate franquia;
- hidden terms dependem da migration nova;
- a word cloud preserva historico bruto.
