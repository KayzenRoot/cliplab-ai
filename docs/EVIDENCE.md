# Evidência · CLIP-MVP-001

Execução em 19/11/2026. Base do PR `beb8033948da04514a3e1936f85b034b69dddc00`. Implementação auditada `85c789d723a85cf49cfd82b72c680d7b62467181`. [Pull request #1](https://github.com/KayzenRoot/cliplab-ai/pull/1)

## Verificações de código

| Verificação | Resultado |
| --- | --- |
| `python -m ruff check .` | Aprovado localmente e no CI Linux. |
| `python -m pytest -q` | 40 testes aprovados no CI Linux, incluindo FFmpeg real. |
| Teste focado local de API | 14 testes aprovados no commit auditado. |
| Teste focado local de mídia | 5 testes de probe, extração e atraso da faixa aprovados na reauditoria. |
| `node --check cliplab/static/studio.js` e `bash -n start.sh` | Aprovado. |
| `python -m pip check` | Nenhuma dependência quebrada no ambiente Windows usado. |

[Execução do CI no commit de implementação auditado](https://github.com/KayzenRoot/cliplab-ai/actions/runs/44533861699)

O CI gera mídia própria e cobre MP4/MKV/MOV, arquivo corrompido, caminhos e origem, gravação de licença, fila e retomada, cancelamento, limites de disco/escrita, limpeza temporária, seleção e fallback, legendas, prévia, exportação e ZIP. A avaliação com Ollama e os resultados de Whisper na suíte automatizada usam respostas controladas. O smoke abaixo usa faster-whisper e FFmpeg reais.

## Smoke integral em Windows

Python 3.12.10, faster-whisper 1.2.1 em CPU/int8 com modelo multilíngue base e FFmpeg/FFprobe 9.0.2. O script Windows iniciou Uvicorn em 127.0.0.1:8765. A gravação sintética foi produzida com o script `scripts/generate-smoke-pt.ps1`, vídeo de teste FFmpeg e narração original do projeto sintetizada pela voz Microsoft Maria Desktop. O script `scripts/smoke_api.py` realizou a ingestão, verificou o JSON da transcrição, obteve prévia com HTTP 206, alterou título e descrição, aprovou e exportou. O smoke usou o preset Dossiê Leonida e a trilha procedural.

| Medida | Resultado |
| --- | --- |
| Identificador local | `0e80d363c3c9405a9ed1f1105ee20f9a` |
| Duração útil da gravação | 163,658821 s |
| Transcrição | Português, 23 segmentos e 265 palavras temporizadas |
| Intervalo selecionado | 55,820–149,040 s |
| Prévia no Chrome | 540 × 960, com reprodução e avanço manual |
| MP4 final por FFprobe | 93,233333 s, 1080 × 1920, 9:16, 30/1 fps, H.264 e AAC |
| Integridade | Decodificação integral com `ffmpeg -v error -xerror -i <mp4> -f null -` |
| SRT | 39 blocos contidos no MP4, primeiro bloco alinhado à primeira palavra do intervalo |
| ZIP | CRC válido, MP4, SRT, JSON editorial e transcrição original em JSON e SRT |
| SHA-256 do MP4 | `897951edc4d01da9aca72cd4ade43a78e4423ed1a86edb0796e19d29f3233706` |
| SHA-256 do ZIP | `ef30134e9bd15a6c0931ee462a9771640d3f996f748119f12de3a3f09c9f5a9a` |

O teste de interface em Chrome verificou upload pelo navegador, histórico após recarga, reprocessamento, cadastro de uma faixa licenciada, estado de aprovação, rascunho preservado, links de exportação e ausência de rolagem horizontal a 1440, 390 e 320 px.

### Repetição após a auditoria

O commit auditado passou novamente pelo fluxo completo em uma fila e um diretório de dados vazios. A fonte foi uma cópia dos primeiros 100 s da narração com um padrão sintético de 320 × 180. O projeto local `8ca8f3e2552a4b2d83b89d15411dd172` produziu 16 segmentos em português e 163 palavras com timestamps. A seleção usou o intervalo 8,380–99,360 s. A edição, a aprovação e a exportação com trilha procedural foram concluídas.

FFprobe confirmou 90,980000 s, 1080 × 1920, 9:16, 30/1 fps, H.264 e AAC. A decodificação integral passou, o SRT tinha 38 blocos com o primeiro sincronizado à fala, e o ZIP passou na verificação de CRC. O SHA-256 do MP4 foi `46333e4dd04fe4ebc057468b9304f0b7efc07a77975c284683bb559919a1cc64`.

## Auditoria e correções

- O primeiro teste de transcrição revelou um escalar NumPy incompatível com JSON. A regressão usa escalares NumPy no teste controlado. O reprocessamento do mesmo projeto depois da correção concluiu a transcrição e a exportação.
- A revisão independente inicial encontrou quatro problemas médios, incluindo aprovação de intervalos com representação fracionária, geração concorrente de ZIP, duração dos streams com áudio atrasado e ausência de um limite de saída PCM. O commit auditado corrige esses caminhos e acrescenta regressões.
- Duas revisões independentes das áreas de API, fila, exportação, mídia, seleção e interface aprovaram o commit auditado. Nenhum achado HIGH ou CRITICAL conhecido permaneceu aberto.

## Limites da evidência

Integração com uma instância real do Ollama, gravações com várias horas e material real de gameplay permanecem sem execução neste ciclo. A aplicação executou integralmente no Windows. A suíte executou no Linux hospedado e o script Linux passou na verificação de sintaxe. O modo automático usa sinais da fala, e o operador revisa eventuais erros de transcrição, recortes e atribuições antes de publicar.
