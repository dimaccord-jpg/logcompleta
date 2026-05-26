# Troubleshooting Operacional

Data de consolidacao: `2026-05-26`

## 1. Regra geral

Investigar nesta ordem:

1. causa raiz
2. auditoria
3. observabilidade
4. contrato oficial
5. runtime por ambiente

Nao corrigir incidente com rota paralela, bypass ou fallback mascarando erro.

## 2. Julia publica artigo sem qualidade esperada

Validar:

- `redacao_status`
- `redacao_fallback`
- `redacao_motivo`
- auditoria do pipeline

Regra oficial:

- se artigo entrou em fallback, o pipeline correto bloqueia antes de imagem e publicacao.

## 3. Imagem publicada sumiu

Validar:

- se a URL publicada aponta para `/media/generated/...`
- se o arquivo existe em `settings.data_dir/generated`
- se a pagina renderizou fallback read-only

Regra oficial:

- render pode cair para `/static/img/fallback-capa-v1.svg`
- `NoticiaPortal.url_imagem` nao deve ser regravada por isso
- retencao nao pode sobrescrever patrimonio editorial

## 4. Share ou SEO com host errado

Validar:

- `PUBLIC_BASE_URL`
- canonical
- `og:url`
- `share_url_abs`

Contrato oficial:

- `canonical == og:url == share_url_abs`
- producao usa `https://www.agentefrete.com.br`
- homolog usa o host homolog configurado

## 5. Conteudo despublicado ainda aparece

Validar:

- se a operacao foi `POST /admin/noticias/<id>/despublicar`
- se `publicado_em=None`
- se `status_publicacao=despublicado`

Regra oficial:

- conteudo despublicado sai da superficie publica e dos filtros publicos.

## 6. Cleide nao responde ou cai em fallback

Validar no payload:

- `ai_used`
- `fallback_used`
- `policy_blocked`
- `context_status`
- `error_code`

Interpretacao:

- `provider_error`: indisponibilidade do provider
- `fallback_bloqueio_semantico`: politica de seguranca
- `fallback_contexto_indisponivel`: falta de contexto
- `fallback_intent_desconhecida`: pergunta fora do conjunto resolvivel

## 7. Upload Cleide falha

Validar:

- autenticacao
- autorizacao operacional por franquia
- extensao do arquivo
- tamanho maximo
- template usado
- status JSON retornado

Contrato relevante:

- template baixa sem login por `/api/cleide/template`
- upload real exige login e autorizacao operacional
- falha de upload deve orientar causa, nao apenas falhar silenciosamente

## 8. Dashboard admin sem Customer Insight

Estado correto atual:

- o bloco visual de Customer Insight esta ocultado
- backend permanece preservado

Isso nao e regressao, e contrato atual de interface administrativa.

## 9. Divergencia entre homolog e producao

Validar:

- `APP_ENV`
- `PUBLIC_BASE_URL`
- storage em `settings.data_dir`
- segredos e runtime de IA do ambiente

Documentar sempre o estado promovido, nao apenas o comportamento local ou de homolog.

## 10. Validacao final

Antes de encerrar incidente:

- registrar a causa raiz
- anexar evidencia de auditoria
- confirmar contrato oficial afetado
- confirmar se houve impacto em share, SEO, IA, billing ou publicacao
