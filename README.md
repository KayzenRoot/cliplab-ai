# ClipLab AI

Estúdio local para transformar gravações autorizadas em cortes verticais com transcrição e revisão editorial.

## Instalação e início

Instale Python **3.12** e uma distribuição do FFmpeg com FFprobe, libass e libx264 no PATH. No Windows, o pacote `winget install --id Gyan.FFmpeg --exact` fornece as ferramentas de mídia. No Ubuntu, instale `ffmpeg` e `fonts-dejavu-core`, além de Python 3.12 e seu módulo `venv`.

Clone o repositório e, dentro dele, use um dos scripts:

**Windows (PowerShell)**

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

**Linux**

```bash
bash start.sh
```

Os scripts criam `.venv`, instalam as dependências e iniciam o serviço em [127.0.0.1:8765](http://127.0.0.1:8765). Mantenha o terminal aberto enquanto usar o estúdio. A primeira transcrição baixa os pesos do modelo `base` multilíngue. Depois disso, a transcrição pode rodar sem conexão, com o modelo disponível no cache. Também é possível configurar uma pasta local de pesos CTranslate2 com `CLIPLAB_WHISPER_MODEL`. [faster-whisper](https://github.com/SYSTRAN/faster-whisper)

## Gerar o primeiro corte

1. Arraste uma gravação MP4, MKV ou MOV com áudio, informe autoria, origem e licença, e confirme a autorização comercial. Use o arquivo original ou uma cópia obtida de forma autorizada, como a exportação dos próprios vídeos pelo YouTube Studio. A URL, quando preenchida, identifica a fonte. [Ajuda do YouTube](https://support.google.com/youtube/answer/56100?hl=pt-BR)
2. Escolha de um a cinco cortes, os limites de duração, idioma, preset e trilha. Clique em **Gerar cortes com IA** e acompanhe a fila. O preset **Gameplay** preserva o enquadramento. **Dossiê Leonida** aplica identidade grafite, ciano e magenta e uma pergunta editorial quando disponível.
3. Confira a prévia, revise a precisão das legendas e ajuste início, fim, título, gancho ou descrição. Revise o resultado atualizado e aprove os cortes desejados.
4. Use **Exportar MP4** nos cortes aprovados. Baixe o MP4 final, o SRT ou o ZIP com as mídias aprovadas e exportadas, a transcrição e os metadados editoriais. Em trilhas licenciadas, o ZIP inclui os créditos informados no cadastro quando a faixa foi aplicada.

A trilha procedural é sintetizada localmente. Para usar uma faixa própria ou licenciada, cadastre o arquivo, origem, autoria, licença comercial e crédito exigido. A mistura reduz a trilha durante a fala. Quando a mistura falha, o vídeo sai sem trilha e o estúdio sinaliza a ocorrência.

## Operação local

Copie `.env.example` para `.env` para alterar as configurações. Os padrões permitem 12 GiB de upload, oito horas de gravação e preservam 768 MiB de espaço livre, além da estimativa da operação. A fila executa uma transcrição ou renderização por vez. **Ollama** é opcional. Configure um modelo instalado localmente com `CLIPLAB_OLLAMA_MODEL` e habilite a avaliação semântica no projeto. O classificador sugere notas para janelas existentes e a seleção determinística assume se o modelo estiver indisponível. [API do Ollama](https://docs.ollama.com/api/generate)

O diretório `data/` contém `input/`, `output/`, `cache/`, `music/`, `logs/`, `tmp/` e o banco SQLite. O progresso e os cortes sobrevivem ao reinício. O reprocessamento reaproveita a transcrição concluída do mesmo projeto. Mantenha esse diretório em armazenamento privado e local.

O tempo de execução depende da duração da gravação, do hardware e do codec. O Whisper usa CPU/int8 em blocos e a interface gera primeiro uma prévia de 540 × 960. A seleção usa sinais da fala e pode encontrar menos cortes válidos do que o pedido. Ela ainda não identifica eventos pela imagem. Revise nomes próprios, jargões, trechos com música alta e avisos de baixa confiança antes de publicar. A nota é uma estimativa editorial. Direitos sobre a fonte, edição autoral e critérios da plataforma permanecem parte da revisão.

## Testes

```text
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest -q
```

Execute os comandos de teste com o Python do ambiente virtual. O CI usa mídia sintética e não baixa pesos do Whisper. Para repetir o smoke real com voz sintética em português, rode `scripts/generate-smoke-pt.ps1` no Windows com a voz Microsoft Maria Desktop instalada. Com o aplicativo aberto, execute `python scripts/smoke_api.py` no ambiente virtual. O script cria uma entrada de teste na fila e salva MP4, SRT, ZIP e relatório FFprobe em `data/smoke/export/`.

## Fontes do projeto

O Work Order CLIP-MVP-001 rege esta entrega. Seu texto fornecido tem SHA-256 `f5810c4c9237957e875ed3cce4c7ed5e9f54d9f34aa79aff0d5561625bfb5312`. Leia o Source Pack e o Checkpoint antes de alterar o escopo. Decisões aprovadas, escopo, critérios de conclusão, arquitetura e requisitos orientam mudanças nessa ordem. Git e testes registram o que foi executado. [Source Pack](docs/SOURCE-PACK.md) · [Checkpoint](docs/CHECKPOINT.md) · [Evidência](docs/EVIDENCE.md)
