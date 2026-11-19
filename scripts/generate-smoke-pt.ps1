param(
    [string]$Output = (Join-Path $PSScriptRoot '../data/smoke/input.mp4')
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$cliplabOutput = [IO.Path]::GetFullPath($Output)
$cliplabFolder = Split-Path -Parent $cliplabOutput
New-Item -ItemType Directory -Force -Path $cliplabFolder | Out-Null
$cliplabAudio = Join-Path $cliplabFolder ('speech-' + [guid]::NewGuid() + '.wav')
$cliplabSpeaker = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $cliplabCulture = [System.Globalization.CultureInfo]::GetCultureInfo('pt-BR')
    $cliplabSpeaker.SelectVoiceByHints(
        [System.Speech.Synthesis.VoiceGender]::NotSet,
        [System.Speech.Synthesis.VoiceAge]::NotSet,
        0,
        $cliplabCulture
    )
    if ($cliplabSpeaker.Voice.Culture.Name -ne 'pt-BR') {
        throw 'Instale uma voz de síntese Português (Brasil) no Windows.'
    }
    $cliplabSpeaker.Rate = -1
    $cliplabSpeaker.SetOutputToWaveFile($cliplabAudio)
    $cliplabPrompt = [System.Speech.Synthesis.PromptBuilder]::new($cliplabCulture)
    $cliplabParagraphs = @(
        'O portão parecia fechado. Então por que a luz do outro lado estava acesa? Neste mapa fictício, eu precisava entregar um pacote e voltar para o abrigo antes da chuva.',
        'Comecei pela rua principal porque era o caminho mais rápido. Só que a ponte levantou bem quando eu cheguei. A rota óbvia parou de funcionar em menos de um minuto.',
        'Reparei então em três marcas azuis sobre placas de madeira. A primeira estava perto da ponte. A segunda aparecia ao lado do depósito. A terceira quase desaparecia atrás de um caminhão.',
        'Minha primeira ideia foi seguir o caminhão. Era uma armadilha! Ele dava voltas no estacionamento e me levava de volta ao mesmo cruzamento. As placas azuis contavam outra história.',
        'Voltei a pé e encontrei uma passagem atrás do depósito. Era estreita demais para o carro, mas dava para ver uma escada, um corrimão amarelo e uma caixa de ferramentas.',
        'E aqui veio a surpresa. A escada não levava ao telhado do depósito. Ela apontava para uma varanda que atravessava a rua por cima do congestionamento.',
        'Testei o caminho duas vezes para ter certeza. Pela ponte, a entrega ficava parada quando a cancela subia. Pela varanda, cheguei ao outro lado antes de a chuva começar.',
        'A caixa de ferramentas guardava uma chave simples. Usei a chave no portão iluminado, e o pacote finalmente chegou ao destino. O som metálico do portão foi a melhor recompensa da missão.',
        'A lição desse mapa é simples. Quando a rota principal falha, olhe para o cenário e compare as pistas. A passagem estava escondida à vista de todo mundo.'
    )
    foreach ($cliplabParagraph in $cliplabParagraphs) {
        $cliplabPrompt.AppendText($cliplabParagraph)
        $cliplabPrompt.AppendBreak([TimeSpan]::FromMilliseconds(680))
    }
    $cliplabSpeaker.Speak($cliplabPrompt)
    $cliplabSpeaker.SetOutputToNull()

    & ffmpeg -nostdin -v error -y -f lavfi -i 'testsrc2=size=640x360:rate=24' -i $cliplabAudio -shortest -c:v libx264 -preset ultrafast -crf 23 -pix_fmt yuv420p -c:a aac -movflags '+faststart' $cliplabOutput
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao gerar o vídeo de teste.' }
    & ffprobe -v error -show_entries 'format=duration' -of 'default=nokey=1:noprint_wrappers=1' $cliplabOutput
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao verificar o vídeo de teste.' }
    Write-Output $cliplabOutput
} finally {
    $cliplabSpeaker.Dispose()
    if (Test-Path -LiteralPath $cliplabAudio) { Remove-Item -LiteralPath $cliplabAudio }
}
