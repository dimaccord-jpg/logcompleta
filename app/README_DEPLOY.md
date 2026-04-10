# Deploy em Homolog/Producao

Este arquivo e um anexo operacional curto.
Use o `README.md` da raiz como fonte principal do cenario atual.

## Sequencia Segura

1. publicar codigo no servico alvo;
2. validar variaveis de ambiente e persistencia;
3. executar migrations no banco alvo;
4. confirmar `current` e `head`;
5. validar health checks;
6. validar cron protegido;
7. validar fluxos reais principais:
   - chat da Julia
   - upload Roberto
   - `/admin/agentes/roberto`
   - `/fretes` para admin e usuario comum
   - `/perfil`

## Lembretes de Risco

- nao publicar sem migrations validas;
- nao quebrar o trilho oficial de governanca do Cleiton;
- nao tratar upload Roberto como homologado sem validar upload, BI, ranking e heatmap;
- nao usar este arquivo como fonte funcional principal.

## Referencia Principal

Qualquer mudanca funcional relevante deve ser refletida primeiro no `README.md` da raiz.
Mudancas de experiencia visual aprovadas tambem devem constar primeiro la, para evitar divergencia entre deploy e documentacao.
