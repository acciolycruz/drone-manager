---
description: Executa uma auditoria técnica completa acionando em sequência todos os 11 agentes especialistas de domínio, qualidade, segurança, infra e documentação.
---

Execute uma revisão completa das alterações recentes (ou dos arquivos/pastas indicados) seguindo rigorosamente esta ordem de execução:

1. **Telemetria e Geoprocessamento (`@gis-telemetry-specialist`):**
   - Caso existam módulos lidando com GPS, metadados EXIF/SRT, rotas ou mapas, valide o sistema de referência (WGS 84 / EPSG:4326), a ordem `[longitude, latitude]` em GeoJSON, cálculos métricos e tratamento de perda de sinal.
2. **Pipeline de Mídia e Vídeo (`@media-pipeline-engineer`):**
   - Caso existam fluxos de upload, FFmpeg, OpenCV/YOLO ou geração de thumbnails, verifique leitura em *chunks*, fechamento explícito de streams, consumo de RAM no WSL2 e limpeza de arquivos temporários.
3. **Integrações e APIs (`@api-integrator`):**
   - Caso existam clientes HTTP, webhooks ou chamadas a serviços externos (Supabase, ArcGIS REST, Nominatim, Open-Meteo, DECEA/GEOAISWEB), valide *timeouts* explícitos, políticas de *retry*/backoff, tratamento de erros de rede e validação de *payloads*.
4. **Banco de Dados e SQL (`@db-architect`):**
   - Caso existam alterações em schemas, queries ou migrations, valide integridade referencial, índices em chaves estrangeiras/filtros e políticas de Row Level Security (RLS).
5. **Diagnóstico de Bugs e Regressões (`@bug-hunter`):**
   - Analise fluxos críticos em busca de erros em *runtime*, exceções silenciosas, falhas de CORS/rede no WSL2 ou potenciais regressões introduzidas pelas alterações recentes, realizando análise de causa raiz (RCA) caso identifique anomalias.
6. **Testes e Casos de Borda (`@qa-tester`):**
   - Verifique se as funções novas ou alteradas possuem testes unitários cobrindo caminho feliz, valores nulos/vazios e falhas externas. Crie ou atualize os testes faltantes.
7. **Segurança da Informação (`@security-auditor`):**
   - Audite o código em busca de chaves expostas, falhas de sanitização de inputs, controle de acesso e dependências vulneráveis.
8. **Revisão Final de Código (`@code-reviewer`):**
   - Realize a revisão estrutural de Clean Code, tipagem estrita, separação de responsabilidades e gargalos de performance sobre o código consolidado.
9. **Interface e Usabilidade (`@ui-ux-engineer`):**
   - Caso existam alterações visuais, revise hierarquia, responsividade e estados de *loading*, erro e lista vazia.
10. **Infraestrutura e Ambiente (`@devops-infra`):**
    - Confira dependências fixadas, compatibilidade com WSL2 e sincronia das variáveis em `.env.example` (sem ler o `.env` real).
11. **Documentação Técnica (`@tech-writer`):**
    - Atualize o `README.md`, diagramas Mermaid ou guias de uso caso tenha ocorrido mudança estrutural, de configuração ou de comportamento.

# Formato da Entrega Final
Ao concluir todas as etapas aplicáveis, apresente um **Resumo Executivo Consolidado** em tabela contendo:
- **Agente**
- **Status** (`Aprovado` | `Ajustes Realizados` | `Pendência Bloqueante` | `Não Aplicável`)
- **Principais Achados / Ações**