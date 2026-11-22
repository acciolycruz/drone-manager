# GEMINI.md — Contexto, Arquitetura e Regras do Projeto Drone Manager

Este arquivo documenta a arquitetura, regras de negócio, convenções de código e o estado atual do **Drone Manager** para orientar a continuidade do desenvolvimento.

---

## 1. Visão Geral do Projeto

**Drone Manager** é um Sistema de Informação Geográfica (SIG) e gerenciador local de voos, fotos e vídeos de drones (focado no **DJI Mini 3**, mas com suporte a mídias e reconstrução de voos de outros drones, como Phantom 4, via EXIF/GPS).

- **Ambiente de Execução:** Linux (WSL2 integrado ao Windows, acessando discos externos em `/mnt/e/...` ou formato Windows `E:\...`).
- **Filosofia de Design:** Minimalista, enxuto, local-first, sem frameworks pesados no frontend e concentrado em **dois arquivos principais** (`app.py` e `static/index.html`), mantendo alta legibilidade e zero dependências desnecessárias.
- **Idioma:** Toda a interface (UI), mensagens de erro, comentários e respostas ao usuário devem ser em **Português do Brasil (pt-BR)**, com linguagem clara e acessível.

---

## 2. Estrutura de Diretórios e Arquivos

```text
/home/dudu/drone-manager/
├── app.py                  # Backend monolítico FastAPI + SQLite + integrações CLI/HTTP
├── static/
│   └── index.html          # Frontend SPA (Vanilla JS, CSS, Leaflet, Chart.js, CesiumJS)
├── data/
│   ├── drone.db            # Banco de dados SQLite local
│   ├── logs/               # Arquivos DJIFlightRecord_*.txt ou FlightRecord_*.txt do app DJI Fly
│   ├── logs_ignorados/     # Logs descartados/inválidos (sem voo ou corrompidos)
│   ├── media/              # Pasta padrão local para fotos/vídeos/.SRT (além das pastas em media_roots)
│   └── thumbs/             # Miniaturas JPEG (400px) geradas sob demanda ({media_id}.jpg)
├── start.sh                # Script de inicialização (ativa .venv, cargo, variáveis de ambiente e uvicorn)
├── backup.sh               # Script de backup para HD externo (/mnt/e/Backup_DroneManager)
├── requirements.txt        # Dependências Python: fastapi, uvicorn, pillow
├── README.md               # Documentação inicial de instalação e uso
├── TODO.md                 # Backlog de funcionalidades futuras (agrupadas por área)
└── *.bak*                  # Histórico de backups incrementais de app.py e static/index.html (até .bak19)
```

---

## 3. Dependências e Variáveis de Ambiente

### Ferramentas Externas (CLI no PATH)
1. **`dji-log`** (`dji-log-cli` instalado via Cargo em `~/.cargo/bin/dji-log`): decodifica logs `.txt` da DJI em JSON (requer `--api-key` para logs v13+).
2. **`exiftool`**: leitura em lote (`-j -n`) de metadados EXIF/QuickTime de fotos (`.jpg`, `.jpeg`, `.dng`) e vídeos (`.mp4`, `.mov`).
3. **`ffmpeg`**: extração de frame em `t=1s` para miniaturas de vídeo.

### Variáveis de Ambiente (configuradas em `start.sh`)
- `DJI_API_KEY`: Chave da DJI Developer Open API para descriptografar logs v13+.
- `TZ_OFFSET`: Fuso horário local em horas (padrão `-3`, horário de Brasília/Rio).
- `LOGS_DIR`: Pasta de logs de voo (padrão `data/logs`).
- `MEDIA_DIR`: Pasta principal padrão de mídias (em `start.sh` aponta para `/mnt/e/DJI`).
- `CESIUM_TOKEN`: Token do Cesium Ion para terreno 3D mundial (`CesiumWorldTerrain`) no replay 3D.
- `BACKUP_DIR` (opcional): Caminho para verificação de mídias copiadas em backup.
- `AIRSPACE_TILES` (opcional): URL extra de tiles de espaço aéreo.
- `PROP_LIMIT_H`: Limite de horas para aviso de troca de hélices (padrão `50`).

---

## 4. Arquitetura do Backend (`app.py`)

O backend é construído com **FastAPI** e **SQLite3** (`data/drone.db`), utilizando migrações automáticas na inicialização (`CREATE TABLE IF NOT EXISTS` e `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`).

### 4.1. Esquema do Banco de Dados (`data/drone.db`)

1. **`flights`** (Voos importados de logs ou reconstruídos de fotos):
   - `id` (INTEGER PK), `file` (TEXT UNIQUE — caminho do log ou `photos:{first_media_id}`), `start` (TEXT ISO UTC), `duration` (REAL, s), `distance` (REAL, m), `max_alt` (REAL, m), `max_speed` (REAL, m/s), `battery_sn` (TEXT), `lat` (REAL), `lon` (REAL), `place` (TEXT — bairro/cidade via Nominatim), `weather` (TEXT JSON — Open-Meteo), `track` (TEXT JSON), `events` (TEXT JSON), `notes` (TEXT).
   - Colunas adicionadas via migração:
     - `air` (TEXT JSON): resultado da verificação de espaço aéreo no DECEA (`checked`, `hits`, `near`, `errors`, `sev`).
     - `bat` (TEXT JSON): telemetria detalhada da bateria no voo (`start`, `end`, `vmin`, `dev`, `est`, `full`, `tmax`, `tmin`, `wmax`, `wavg`, `amax`, `wh`, `wh_km`).
     - `source` (TEXT DEFAULT `'log'`): `'log'` (log real DJI), `'srt'` (voo reconstruído de trilha `.SRT` de vídeo) ou `'photos'` (voo reconstruído por agrupamento de fotos com GPS).
     - `name` (TEXT DEFAULT `''`): nome personalizado dado pelo usuário ao voo.
     - `fav` (INTEGER DEFAULT `0`): voo marcado como favorito (`0` ou `1`).
     - `meta` (TEXT JSON): metadados avançados do voo (`home`: lat/lon/rth_alt/max_dist VLOS; `wind_est`: vento estimado em altitude via pitch/roll; `mech`: contadores de alerta `vib_s`, `force_s`, `comp_s`, `gimb_s`; `drone`: modelo, nome e seriais da aeronave/RC/câmera).

   - **Formato de `track` (JSON):** lista de pontos (1 por segundo):
     `[0:sec, 1:lat, 2:lon, 3:alt_m, 4:spd_ms, 5:bat_pct, 6:sats, 7:yaw_deg, 8:gimbal_pitch_deg, 9:pitch_deg, 10:roll_deg, 11:zspd_ms, 12:rc_sticks, 13:rc_sig, 14:pwr_cells]`
     *(Nota: índices `9..14` trazem atitude `pitch`/`roll`, velocidade vertical `zspd`, joysticks RC `[thr, rud, ele, ail]`, sinal RC `%` e telemetria elétrica `[V, A, W, c1, c2...]`).*

2. **`media`** (Fotos e Vídeos):
   - `id` (INTEGER PK), `path` (TEXT UNIQUE), `kind` (`'photo'` ou `'video'`), `taken` (TEXT ISO local), `lat` (REAL), `lon` (REAL), `alt` (REAL), `w` (INT), `h` (INT), `size` (INT), `sha1` (TEXT — hash parcial rápido: tamanho + primeiro 1MB + último 1MB), `flight_id` (INT FK), `tags` (TEXT CSV), `fav` (INT), `backed` (INT).
   - Colunas adicionadas via migração:
     - `manual` (INTEGER DEFAULT `0`): `1` quando o vínculo `flight_id` foi definido manualmente pelo usuário (impede que `link_media()` sobrescreva).
     - `sync_adj` (REAL DEFAULT `0`): ajuste fino em segundos (`-120` a `+120`) da sincronia do vídeo com o replay do voo.
     - `meta` (TEXT JSON): metadados ópticos EXIF/XMP/SRT (`model`, `iso`, `shutter`, `fnum`, `ev`, `focal`, `focal35`, `gyaw`, `gpitch`, `groll`, `rel_alt`, `proj`, `pano360`, `has_srt`, `srt_pts`).

3. **`media_roots`** (Pastas de mídia configuráveis — ex.: HDs externos, cartões, pastas por drone):
   - `id` (INTEGER PK), `name` (TEXT), `path` (TEXT UNIQUE — normalizado para Linux `/mnt/...`), `tag` (TEXT DEFAULT `''` — etiqueta aplicada automaticamente às novas mídias daquela pasta), `recursive` (INTEGER DEFAULT `1`), `enabled` (INTEGER DEFAULT `1`), `last_scan` (TEXT ISO).

4. **`spots`** (Locais favoritos salvos com clique direito no mapa):
   - `id` (INTEGER PK), `name` (TEXT), `lat` (REAL), `lon` (REAL), `notes` (TEXT).

5. **`kv`** (Chave-valor de configurações/estado):
   - `k` (TEXT PK), `v` (TEXT) — usado por exemplo para `prop_base` (horas acumuladas no momento da última troca de hélices).

### 4.2. Regras de Negócio Importantes no Backend

- **Importação de Logs (`import_log`):**
  - Executa `dji-log <arquivo> --api-key <API_KEY>`.
  - Filtra apenas frames com GPS válido e recorta somente o período no ar (`not isOnGround`).
  - Rejeita logs onde o drone não decolou ou teve `maxalt < 1 m`.
  - Detecta e traduz para português eventos de voo (`STATE_PT`, `ACTION_PT`), bateria em 20%/10%, avisos de tensão, GPS fraco (< 8 satélites), sinal de RC fraco (<= 20% por >= 3s), início/fim de vídeo, fotos e avisos do app.
  - Extrai métricas de saúde da bateria (`binfo`): carga inicial/final, tensão mínima (mV), desvio máximo entre células (`cellVoltageDeviation`, ignorado se `isCellVoltageEstimated` for verdadeiro na maioria dos frames), capacidade total (`fullCapacity` mediana) e temperaturas mín/máx.
  - Faz geocodificação reversa no Nominatim (com `time.sleep(1)` para respeitar rate limit) e busca clima histórico horário na Open-Meteo Archive API.

- **Indexação e Associação de Mídias (`index_media` & `link_media`):**
  - Percorre todas as pastas habilitadas em `media_roots`. Se uma pasta não estiver montada (`not rp.is_dir()`), registra aviso sem falhar nem apagar nada.
  - Para vídeos, prioriza coordenadas e data/hora do arquivo `.SRT`/`.srt` de mesmo nome; se não houver `.SRT`, usa `CreateDate` do MP4 ajustado por `TZ_OFFSET`.
  - `link_media()` associa mídias (`flight_id IS NULL AND manual=0`) a voos de log seguindo a ordem de prioridade:
    1. Vídeo cujo timestamp bate (margem de 5s) com um evento `"Início de gravação de vídeo"` no log;
    2. Mídia capturada entre o início e o fim do voo;
    3. Vídeo iniciado até 10 min antes da decolagem quando o voo já começou gravando (`t == 0`);
    4. Voo mais próximo dentro de uma janela de 10 min de folga.
  - Adiciona tags automáticas (`YYYY-MM` e nome do `place`).

- **Reconstrução de Voos (`reconstruct_flights`):**
  - Para fotos com GPS que não têm voo associado e são anteriores ao primeiro log registrado (`taken < MIN(start)` dos logs), agrupa fotos com intervalo <= 30 min e distância <= 5 km (mínimo de 2 fotos) criando um voo aproximado com `source='photos'`.

- **Espaço Aéreo DECEA / GEOAISWEB (`airspace_info` & `air_check`):**
  - Integra com `https://geoaisweb.decea.mil.br/geoserver/ows` via WMS (`GetFeatureInfo`) e WFS (`GetFeature`).
  - Verifica intersecção (Point-in-Polygon com suporte a buracos) dos pontos do trajeto contra camadas `ICA:eac_p` (Proibida, sev 3), `ICA:eac_r` (Restrita, sev 3), `ICA:eac_d` (Perigosa, sev 2), `ICA:CTR` (sev 3), `ICA:ATZ` (sev 3), `ICA:fiz` (sev 2), além da distância até aeródromos/helipontos (`ICA:airport_heliport`, gerando atenção sev 2 se `<= 9.0 km`).

- **Caminhos Windows/WSL (`norm_path` & `to_win`):**
  - Converte caminhos `E:\DJI` para `/mnt/e/DJI` internamente no Linux e exibe de volta como `E:\DJI` na interface para comodidade do usuário.
  - Suporta remapeamento (`remap=1` em `PATCH /api/roots/{rid}`) quando uma pasta muda de letra de unidade ou diretório, atualizando o prefixo de `path` na tabela `media`.
  - **Regra de Ouro de Segurança:** Nunca deletar arquivos de mídia do disco do usuário. `DELETE /api/roots/{rid}` apenas remove a pasta da configuração (e opcionalmente remove os registros do banco e miniaturas se `forget=1`).

---

## 5. Arquitetura do Frontend (`static/index.html`)

SPA em arquivo único dividida em uma barra lateral (`#side`, inicializada por `initSidebar()`) e área de conteúdo (`#content`) com 7 seções/abas gerenciadas por `show(t)`:

1. **Mapa (`#mapa`):**
   - Mapa Leaflet em tela cheia com troca de mapas-base persistida no `localStorage` (`Satélite` Esri, `Mapa` OSM, `Relevo` OpenTopoMap, `Claro`, `Escuro`).
   - Camadas sobrepostas: Voos (polilinhas coloridas, tracejadas se `source=='photos'`), Fotos geolocalizadas (`MarkerClusterGroup`), Locais favoritos (`spots`) e camadas WMS do DECEA/GEOAISWEB + botão `"ℹ Consultar espaço aéreo"` no clique.
   - Clique com botão direito adiciona um novo local favorito.

2. **Voos (`#voos`):**
   - **Lista (`#flist`):** Filtros por texto, datas, altura mínima, duração mínima, bateria, origem (`log` vs `photos`) e favoritos (`★`), ordenação clicável nas colunas, chip de status do espaço aéreo (🟢/🟡/🔴) e botão para verificar espaço aéreo em lote.
   - **Detalhe do Voo (`#fdet`):**
     - Edição inline de nome do voo e botão de favorito.
     - Cartões de resumo (duração, distância, altura máx., vel. máx., bateria, carga usada, tensão mín., desvio entre células, temperatura, capacidade e clima).
     - Exportação de **KML**, **GPX** e **Relatório para Impressão/PDF** (`/api/flights/{id}/report`).
     - **Player Sincronizado Vídeo + Mapa 2D/3D (`#fplayer`):**
       - Se o voo tiver vídeos associados, exibe o player de vídeo lado a lado com o mapa e sincroniza o cursor do voo com o tempo do vídeo (alinhado pelo evento `"Início de gravação"` + ajuste manual `sync_adj` de `-5s`/`-1s`/`+1s`/`+5s`).
       - Velocidades de replay (`0.5×` a `8×`) persistidas no `localStorage`.
       - **Modo 3D (CesiumJS):** Carregado sob demanda (`loadCesium()`), amostra a altitude real do terreno (`sampleTerrainMostDetailed`), desenha a polilinha e parede vertical no terreno, exibe o ícone do drone, linha de prumo no solo, vetor de direção ("nariz" laranja), **cone de visão 3D da câmera** (calculado via matriz East-North-Up com `yaw` e `gimbal.pitch`), miniaturas das fotos tiradas ao longo do trajeto e câmeras `Seguir`, `Geral` e `Topo`.
       - **Modo Tela Cheia (`toggleFs`):** Leva o mapa 2D/3D para tela cheia com o vídeo em Picture-in-Picture flutuante no canto inferior esquerdo e a barra de controle de replay.
     - Gráfico interativo Chart.js (Altura, Velocidade, Bateria) com linha de cursor sincronizada, botões de eventos clicáveis (`goT`), bloco de verificação do DECEA, campo de anotações e galeria de mídias do voo.

3. **Fotos (`#fotos` -> `#midia`) & Vídeos (`#videos` -> `#midia`):**
   - Galeria responsiva com lazy loading de miniaturas, indicação visual de mídia em disco desconectado (`m.offline`), filtros por texto/tag, data, proximidade geográfica (`lat,lon,km`), favoritas e "sem voo associado".
   - Exportação de fotos selecionadas em `.zip` (`/api/export/photos`, pronto para OpenDroneMap).
   - Modal (`#modal`) para visualizar foto/vídeo, favoritar, editar tags (álbuns) e alterar manualmente o vínculo com um voo (`Automático`, `Nenhum voo` ou voo específico).

4. **Estatísticas (`#stats`):**
   - **Painel Geral (`loadPanel` / `renderPanel`):** Filtro por ano ou todo o período, cartões de resumo, **Calendário de Voos em SVG estilo GitHub** (clicável para filtrar os voos daquele dia), gráficos de horário de decolagem, dia da semana, distribuição de duração e horas por bateria, cartões de voos recordistas ("Voos em destaque") e **Mapa de Calor (`leaflet.heat`)** das rotas e decolagens.
   - **Saúde das Baterias (`loadBatHealth`):** Tabela diagnóstica por serial de bateria (voos, horas, capacidade atual em mAh, % de queda de capacidade, desvio máximo em mV, temperatura máxima, uso médio e status 🟢 Boa / 🟡 Observar / 🔴 Atenção) + gráficos de evolução temporal de capacidade (mAh) e desvio entre células (mV).
   - **Manutenção e Biblioteca (`loadStats`):** Barra de desgaste das hélices com botão de reset, tabela geral de baterias, contagem de mídias sem voo/sem backup e listagem de arquivos duplicados (mesmo hash SHA-1).

5. **Locais (`#locais`):**
   - Tabela de locais favoritos salvos e exclusão.

6. **Configurações (`#config`):**
   - Gerenciamento completo de `media_roots` (ativar/desativar, adicionar, editar, navegador visual de pastas dos discos `/mnt/...` via `/api/fs/list`, etiqueta automática por pasta, opção de subpastas, remapeamento de caminho e remoção segura com opção de manter ou esquecer as mídias no banco).

7. **Busca em Linguagem Natural sem IA (`nlParse` / `nlRun`):**
   - Barra de busca rápida na lateral (`#nlq`) que interpreta expressões em português por regex/regras determinísticas (ex.: *"voos acima de 80 m em 2025"*, *"voos com mais de 10 min em março"*, *"voos na Barra com bateria baixa"*, *"voos perto de aeroporto"*, *"fotos de outubro de 2026 no Recreio"*, *"fotos sem voo"*), exibindo chips do que foi entendido em `#nlbar` e filtrando automaticamente a aba correspondente.

---

## 6. Convenções de Código e Regras para o Agente

1. **Estilo de Código Compacto e Consistente:**
   - Tanto `app.py` quanto `static/index.html` usam um estilo conciso, direto e sem boilerplate excessivo. Mantenha o mesmo padrão ao adicionar ou editar funções.
2. **Backups Incrementais (`.bakN`):**
   - O projeto mantém backups numerados (`app.py.bak19`, `static/index.html.bak19`) antes de grandes alterações. Ao realizar mudanças estruturais significativas se solicitado, preserve a segurança dos arquivos existentes.
3. **Migrações de Banco Seguras:**
   - Nunca apague ou recrie `data/drone.db`. Qualquer nova coluna ou tabela deve ser adicionada no bloco de inicialização do `app.py` com `CREATE TABLE IF NOT EXISTS` ou checando `PRAGMA table_info(...)` antes de rodar `ALTER TABLE`.
4. **Proteção Total aos Arquivos do Usuário:**
   - Jamais execute comandos destrutivos sobre as pastas de mídia (`/mnt/e/DJI`, `data/media`, `data/logs`). Operações de remoção no app devem afetar apenas o banco SQLite e o cache de miniaturas (`data/thumbs/`).
5. **Compatibilidade Offline / Discos Externos Desconectados:**
   - Como as mídias frequentemente ficam em HDs externos (`/mnt/e/...`), qualquer nova funcionalidade que acesse arquivos de mídia deve lidar graciosamente com o caso em que o disco está desconectado (`Path(p).is_dir()` / `Path(p).exists()`), sem quebrar a listagem ou o escaneamento.
6. **Execução do Servidor:**
   - O app roda no ambiente virtual `.venv` via `./start.sh` (ou `uvicorn app:app`). O binário `dji-log` fica em `~/.cargo/bin`.
7. **Entrega Pós-Alteração (Obrigatório):**
   - Sempre que for feita uma alteração:
     - Exibir na janela de contexto uma **checklist marcável** (`- [ ] ...`) para que o usuário abra a aplicação e teste as novas funções.
     - Atualizar a documentação (`README.md` / `GEMINI.md`).
     - Subir as alterações para o GitHub (`git commit` + `git push`).
