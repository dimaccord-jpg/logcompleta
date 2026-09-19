# Governança contextual de dados antes de IA externa (SCRUM-75)

Referência de 2026-09-19. Contrato vivo da fronteira de saída do AgenteFrete.

## Arquitetura

```text
usuário / documento / memória / histórico
                ↓
AgenteFrete
                ↓
Cleiton
                ↓
governança contextual local (CPU, sem HTTP)
                ↓
contexto mínimo autorizado
                ↓
Gemini / busca externa / outro provedor
```

Fachada: `app/services/cleiton_ai_data_governance.py`.

Classificador local: `app/services/cleiton_ai_privacy_classifier.py`.

Aliases temporários: `app/services/cleiton_ai_safe_context.py`.

Não existe modelo neural no repositório. A classificação é determinística, em CPU, sem download em runtime e sem regex. Não participa da franquia de chamadas externas.

## Fronteira de confiança

Tudo o que ainda não passou por Cleiton é não confiável: mensagem atual, histórico, resposta anterior reapresentada, texto extraído de documento e query de busca.

O original autorizado local (store/sessão) não é reescrito. A representação enviada ao provedor é uma cópia minimizada.

## Finalidades

`discovery`, `chat_logistico`, `auditoria_frete`, `extracao_tabela_frete`, `explicacao_calculo`, `comparacao_fretes`, `insights_auditoria`, `redacao_editorial`, `geracao_imagem`, `busca_web`.

A finalidade responde: este dado participa da tarefa? Valores logísticos necessários (origem, destino, peso, frete, GRIS, pedágio, CIF/FOB) permanecem.

## Fail-closed

Se o classificador falhar, estiver indisponível ou não puder garantir minimização:

- o provider externo não é chamado;
- o usuário recebe mensagem recuperável, sem detalhe técnico.

Não há fallback para envio bruto. Não há botão para desativar a proteção.

## Aliases

Tokens estáveis só na operação em memória (`[CPF_1]`, `Empresa 1`, `[CHAVE_1]`, …). O mapping não vai ao provedor, não cruza usuários e não vira memória persistente.

## Documentos

PDF deixa de ser enviado em bytes originais para a Files API. O fluxo padrão extrai texto localmente, classifica e envia só a representação autorizada. PDF visual/escaneado sem texto extraível falha de forma controlada.

TXT/XML/CSV/XLSX/DOCX seguem o mesmo contrato depois da conversão local.

## Relação com SCRUM-125 / 178 / 180

- SCRUM-125: consumir `get_ai_data_protection_status()`; não criar página concorrente nem interruptor de desligar.
- SCRUM-178 / 180: qualquer nova saída externa (provider, busca, upload, prompt de imagem) deve passar por `govern_or_raise` / `cleiton_governed_generate_content`. Não criar wrapper paralelo.
