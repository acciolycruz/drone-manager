# Contexto e Ambiente do Projeto
- **Projeto:** `drone-manager` (Sistema de gestão de voos, telemetria, vídeos e fotos de drones)
- **Ambiente de Execução:** Ubuntu no WSL2 (Windows 11)
- **Arquitetura & Dados:** Serviços modulares desacoplados da interface (UI), persistência relacional/espacial (PostgreSQL / Supabase com RLS) e processamento local de mídia e metadados de voo.

# Padrões de Código e Arquitetura
- **Separação de Responsabilidades:** Mantenha regras de negócio, acesso a banco de dados, integração de APIs e processamento de mídia isolados da camada de interface (UI).
- **Tipagem Estrita:** Utilize tipagem explícita em todas as funções, parâmetros e retornos (Type Hints no Python / TypeScript strict).
- **Tratamento de Erros:** Nunca deixe blocos `try/except` ou `catch` vazios; registre o erro de forma estruturada (sem expor tokens ou senhas) e retorne mensagens seguras para o usuário.
- **Reuso:** Antes de criar novos utilitários ou componentes, verifique o código existente para evitar duplicação.

# Segurança Inegociável (Baseline)
- **Zero Secrets:** Nunca escreva chaves de API, tokens, senhas ou strings de conexão diretamente no código. Utilize exclusivamente variáveis de ambiente (`os.getenv` / `process.env`).
- **Proteção de `.env`:** Nunca leia, imprima ou modifique arquivos `.env` reais; utilize apenas `.env.example` como referência estrutural.
- **Validação de Entrada:** Toda entrada externa (usuário, arquivos de mídia, logs SRT/EXIF ou APIs) deve ser validada e sanitizada antes do processamento.

# Fluxo de Trabalho e Comunicação
- **Idioma:** Responda sempre em Português do Brasil (pt-BR). Mantenha nomes de variáveis, funções, arquivos e mensagens de commit em Inglês, e comentários explicativos em Português.
- **Confirmação Explícita:** Antes de executar comandos no terminal que alterem esquemas de banco de dados, apaguem arquivos ou modifiquem dependências (`pip install`, `npm install`), peça confirmação explícita.
- **Planejamento Prévio:** Ao propor alterações em múltiplos arquivos, apresente primeiro um resumo objetivo das mudanças antes de editar o código.
- **Entrega Pós-Alteração (Obrigatório):** Sempre que for feita uma alteração no projeto:
  - **Checklist de Teste para o Usuário:** Apresente na janela de contexto uma checklist marcável (`- [ ] ...`) para que o usuário abra a aplicação e realize o teste das novas funções implementadas.
  - **Atualização de Documentação:** O agente responsável deve inserir a nova funcionalidade/alteração na documentação (`README.md`, `GEMINI.md`, etc.).
  - **Publicação no GitHub:** O agente responsável deve commitar e subir as alterações para o GitHub.

# Delegação de Agentes Especialistas (Skills)
Sempre que uma tarefa envolver os domínios abaixo, carregue e execute obrigatoriamente a respectiva skill localizada em `.agent/skills/`:

| Domínio / Tarefa | Skill Obrigatória | Quando Acionar |
| :--- | :--- | :--- |
| **Planejamento e Escopo** | `product-architect` | Planejamento de novas features complexas, quebra de tarefas e definição de MVP antes de codificar. |
| **Geoprocessamento e GPS** | `gis-telemetry-specialist` | Coordenadas GPS, GeoJSON/KML, telemetria SRT/EXIF de drones, projeções (CRS) e mapas. |
| **Vídeo, Imagem e Visão** | `media-pipeline-engineer` | Upload de arquivos grandes, FFmpeg, thumbnails, streaming de vídeo e inferência (YOLO/OpenCV). |
| **Integrações e APIs** | `api-integrator` | Clientes HTTP, webhooks, Supabase/ArcGIS REST, timeouts, retries e validação de payloads. |
| **Banco de Dados e SQL** | `db-architect` | Criação/alteração de tabelas, migrations, índices, otimização de queries e políticas RLS. |
| **Interface e Usabilidade** | `ui-ux-engineer` | Criação ou ajuste de telas, layouts, mapas interativos, responsividade e estados de UI. |
| **Bugs e Causa Raiz** | `bug-hunter` | Erros em runtime, falhas de CORS/rede no WSL2, stack traces e regressões. |
| **Testes Automatizados** | `qa-tester` | Criação de testes unitários/integração, reprodução de bugs e validação de casos de borda. |
| **Segurança e Credenciais** | `security-auditor` | Auditoria de vulnerabilidades, validação de inputs, tokens, exposição de `.env` ou permissões. |
| **Qualidade e Refatoração** | `code-reviewer` | Revisão de código antes de commit, análise de `git diff`, Clean Code e arquitetura. |
| **Infraestrutura e Deploy** | `devops-infra` | Configuração de WSL2, Docker, variáveis de ambiente (`.env.example`), portas e CI/CD. |
| **Documentação Técnica** | `tech-writer` | Atualização de `README.md`, documentação de API, diagramas Mermaid e guias de instalação. |

- Se uma tarefa envolver múltiplos domínios (ex: integrar uma API externa e salvar metadados GPS no banco), acione as skills correspondentes em sequência antes de concluir a entrega.