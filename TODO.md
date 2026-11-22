# TODO — Backlog de Funcionalidades (Drone Manager)

Lista de funcionalidades planejadas para futuras versões do **Drone Manager**, agrupadas por área.

> **Nota:** A funcionalidade **Previsão de Janela de Voo ("Posso Voar?")** já foi implementada e está disponível no aplicativo.

---

## 1. Análise Avançada de Telemetria (estilo *Airdata UAV* / *FlightReader*)
- [ ] **Coloração dinâmica da rota no mapa:** permitir colorir a trilha do voo por **Sinal RC (`rc_sig`)**, **Altitude**, **Velocidade**, **Satélites** ou **Consumo (`W`)** para identificar zonas de interferência e desempenho.
- [ ] **Mapa de Vento em Altitude:** exibir setas/rosa dos ventos ao longo do trajeto usando `wind_est` (já salvo em `meta`) e calcular eficiência (`Wh/km`) a favor vs. contra o vento.
- [ ] **Índice de Saúde do Voo (*Flight Health Score*):** nota automática por voo cruzando `vib_s`, `comp_s`, `gimb_s`, queda brusca de tensão sob carga (*voltage sag* por célula) e perdas de sinal RC.
- [ ] **Comparador de Voos / Baterias:** sobrepor gráficos de dois voos ou duas baterias no mesmo local para comparar degradação e desempenho.

---

## 2. Planejamento e Fotogrametria (estilo *DJI Pilot 2* / *Litchi* / *WebODM*)
- [ ] **Gerador Automático de Grade de Mapeamento (*Grid / Double Grid / Órbita*):** desenhar um polígono no Planejador Pré-Voo e gerar automaticamente *waypoints* em zigue-zague com cálculo de **sobreposição frontal/lateral**, **GSD desejado**, total de fotos e divisão por bateria.
- [ ] **Exportação em KMZ nativo DJI (WPML):** exportar missões no formato `.kmz` (WPML) compatível com **DJI Fly / DJI Pilot 2**.
- [ ] **Integração local com NodeODM / WebODM:** enviar fotos selecionadas direto para uma instância local do **OpenDroneMap** via API, acompanhar o processamento e importar o **ortomosaico GeoTIFF** gerado como camada no mapa.

---

## 3. Mídia, Vídeo e Inspeção Visual (estilo *DJI Fly* / *Telemetry Overlay*)
- [ ] **Timeline Contínua de Multi-Clipes no Voo:** unificar múltiplos arquivos `.MP4` gerados no mesmo voo na barra de replay sem precisar trocar de vídeo manualmente.
- [ ] **Comparação Temporal (*Before / After* no mesmo GPS):** selecionar duas fotos de datas diferentes tiradas na mesma coordenada/ângulo e comparar com slider divisor (*split view*).
- [ ] **Preview com LUT Rec.709 no Player:** opção para aplicar correção de cor rápida no navegador em vídeos gravados em perfil flat (`D-Cinelike` / `D-Log M`).
- [ ] **Exportação de Legenda `.ASS` / `.CSV` para Edição:** exportar a telemetria sincronizada para uso direto em softwares como DaVinci Resolve, Premiere ou *Telemetry Overlay*.
- [ ] **Busca Semântica Local em Fotos (CLIP/embeddings offline):** complementar o YOLO atual permitindo buscar fotos por descrição visual offline (ex.: *"pôr do sol"*, *"telhado"*, *"lagoa"*).

---

## 4. Operação, Manutenção e Conformidade (estilo *DroneLogbook* / *SARPAS*)
- [ ] **Assistente de Importação de Cartão SD:** ao conectar um cartão de drone (`/mnt/.../DCIM`), detectar mídias novas por hash, copiá-las de forma organizada (`AAAA-MM-DD_Local`) para o HD principal e disparar o scan.
- [ ] **Manutenção por Componente:** histórico e alertas de horas/voos além das hélices para **motores**, **gimbal**, **baterias (ciclos estimados)** e **calibração de IMU/bússola**.
- [ ] **Checklist Pré/Pós-Voo e Perfil do Piloto:** checklist configurável salvo por voo + dados do piloto/drone (SISANT/ANAC, apólice RETA, protocolo SARPAS) impressos automaticamente no relatório HTML/PDF.
- [ ] **Suporte a outros formatos de log:** importar **Litchi CSV**, **Airdata CSV**, **ULog (PX4)** e **DataFlash `.bin` (ArduPilot)** para unificar voos de drones FPV ou outros fabricantes no mesmo banco.
