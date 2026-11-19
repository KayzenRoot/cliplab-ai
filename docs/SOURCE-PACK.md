# Source Pack · CLIP-MVP-001

## Visão geral

ClipLab AI é um estúdio de vídeo local, de usuário único, para gerar e revisar até cinco cortes verticais de uma gravação. Dossiê Leonida é o primeiro preset editorial. O formato também atende gravações fora do nicho gamer.

## Requisitos

- Ingestão pela URL de um vídeo público do YouTube, com declaração de direitos e registro automático de título, canal e origem. A API também aceita arquivos MP4/MKV/MOV para clientes existentes. Validar container, tamanho, duração, integridade e disco.
- Transcrição real com faster-whisper, CPU/int8, VAD, palavras temporizadas, idioma automático/pt-BR, sinalização de baixa confiança e cache JSON com exportação SRT.
- Seleção editorial baseada na fala, limites de frases, pausa, densidade, gancho, desfecho e baixa sobreposição. Selecionar 1–5 cortes válidos, entre 61 e 180 s, padrão 75–100 s. Ollama local opcional e fallback determinístico.
- Layouts Gameplay e Dossiê Leonida, título, gancho, legenda ASS com palavra ativa, SRT, prévia e final MP4 1080 × 1920, 30 FPS, H.264/AAC.
- Trilha procedural original ou entrada licenciada com atribuição e mistura com redução durante a fala. Continuidade sem trilha se a mistura falhar.
- Revisar texto e tempo, aprovar/descartar, exportar MP4/SRT/ZIP. Exibir etapas reais, avisos e justificativa. Registro local das fontes e da trilha.

## Escopo

O fluxo do estúdio começa com o link de um vídeo público do YouTube disponível sem login, inclusive lives já arquivadas. A declaração de direitos continua obrigatória. A entrada de arquivo autorizada permanece na API para compatibilidade. Interface em português e presets locais. Captura contínua, outros provedores de vídeo, SaaS, pagamentos, publicação automática e timeline avançada ficam fora do escopo.

## Arquitetura

Python 3.12, FastAPI/Jinja2 e JavaScript nativo. SQLite guarda configurações, direitos, trabalho e pedidos de renderização. Um worker local serializa transcrição e renderizações, retoma operações interrompidas e reaproveita transcrições completas. FFmpeg/FFprobe tratam mídia local. Persistir estágios CREATED, VALIDATING, EXTRACTING_AUDIO, TRANSCRIBING, ANALYZING, SELECTING, RENDERING, VERIFYING e READY_FOR_REVIEW, com FAILED, CANCELLED e BLOCKED.

## Segurança

Bind em 127.0.0.1. Restrição de hosts e cabeçalho de aplicação em mutações contra requisições cross-origin. Identificadores gerados no servidor, sem caminhos ou comandos de shell fornecidos pela interface. Processos FFmpeg com protocolos restritos, reserva de disco e limites de execução. Log, transcrição, vídeos e cache fora do Git. Ollama acessível apenas em loopback quando configurado. A declaração de direitos é fornecida pelo operador.

## Plano de testes

Ruff e pytest. Validar formatos, integridade, caminhos, fila e recuperação, períodos de fala, deduplicação, fallback, metadados de direitos, rotas, legendas e ZIP. FFmpeg real com fonte sintética horizontal e áudio. Smoke local separado com faster-whisper real em CPU, revisão e exportação final. CI sem baixar modelos.

## Instalação local

Scripts Windows e Linux verificam Python e FFmpeg/FFprobe, preparam ambiente virtual e iniciam em 127.0.0.1:8765. Configuração em .env.example. Componentes, vídeos e modelos de teste ficam no armazenamento local ignorado pelo Git.

## Backlog

Benchmark em gravações de horas e dispositivos de entrada. Avaliação editorial com acervo autorizado. Captura de lives em andamento depende de um método autorizado. Avaliar importação oficial, edição avançada, detecção de rostos, publicação autorizada e processamento distribuído conforme demanda.

## Definition of Done

Source Pack anterior à implementação. Interface local verificada, importação autorizada de fixture, transcrição real, seleção válida, legendas sincronizadas, trilha compatível, fluxo de revisão concluído, MP4 reproduzível no formato especificado e ZIP correto. Testes e auditoria sem defeitos HIGH/CRITICAL conhecidos. README utilizável e evidência com limitações e resultado verificável.

## Registro de decisões

| ID | Decisão |
| --- | --- |
| D01 | SQLite e um worker local. A fila persiste operações e recupera interrupções. |
| D02 | faster-whisper CPU/int8 e classificação determinística por padrão. Ollama pontua candidatos existentes quando optado e disponível. |
| D03 | Prévia de 540 × 960 na fila inicial. Exportação de 1080 × 1920 sob aprovação. |
| D04 | Trilha opcional. Metadados de licença e atribuição entram no pacote de exportação. |
| D05 | Na entrega inicial, a origem do YouTube servia de referência. Substituída por D07 na interface. |
| D06 | A primeira transcrição pode baixar pesos para cache local. A mídia é processada na máquina. |
| D07 | A interface envia somente o link e a declaração de direitos. O worker importa a mídia pública na etapa IMPORTING, valida o arquivo e o reaproveita em novas tentativas do mesmo projeto. |
