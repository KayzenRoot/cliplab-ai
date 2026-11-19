import json

import pytest

from cliplab import highlights
from cliplab.highlights import HighlightEngine, editorial_metadata

GAME_STORY = [
    "Por que o porto de Leonida parece diferente quando o jogador volta à noite?",
    "Primeiro, a luz do farol percorre o canal enquanto a patrulha atravessa a ponte.",
    "Os reflexos mostram duas embarcações menores próximas da margem, com a corrente para leste.",
    "Depois, olhamos o minimapa e comparamos a inclinação da rota na passagem inferior.",
    "O atalho termina num galpão e permite ouvir o rádio da patrulha antes de seguir pela praia.",
    "No final, o resultado aparece quando voltamos ao porto e reconhecemos o caminho inteiro.",
    "O mapa de Vice City ajuda a localizar a curva, a praia e o galpão da gravação.",
    "Assim fica mais fácil comparar as duas rotas com o farol e a ponte ao fundo.",
]
SECOND_STORY = [
    "Como funciona a montagem de um motor elétrico com as peças encontradas na oficina?",
    "Primeiro conectamos os terminais do enrolamento principal ao suporte isolado do conjunto.",
    "Depois a bancada recebe um multímetro para medir a resistência entre as ligações.",
    "Cada bobina aquece durante o ensaio por causa da corrente sustentada ao longo do circuito.",
    "A experiência mostra uma vibração inesperada quando o rotor se aproxima do limite de torque.",
    "Ajustamos o alinhamento pela placa de suporte e repetimos a observação por mais alguns segundos.",
    "O resultado final confirma que o eixo vibra menos com a bobina central alinhada.",
    "Medimos novamente a temperatura externa e guardamos as peças sobressalentes nas caixas azuis.",
]


def story(lines, start=0):
    return [
        {"start": start + index * 12, "end": start + index * 12 + 11, "text": line}
        for index, line in enumerate(lines)
    ]


def test_grounded_candidates_respect_duration_and_avoid_overlap():
    transcript = {"segments": story(GAME_STORY) + story(SECOND_STORY, 112)}
    clips = HighlightEngine().select(transcript, duration=210, minimum=75, maximum=100, count=5)

    assert len(clips) == 2
    first, second = sorted(clips, key=lambda clip: clip["start"])
    assert first["end"] <= second["start"]
    assert all(75 <= clip["end"] - clip["start"] <= 100 for clip in clips)
    assert all(0 <= clip["score"] <= 100 and clip["rationale"] for clip in clips)
    assert first["title"] in " ".join(GAME_STORY)
    assert "#Leonida" in first["hashtags"]
    assert "motor elétrico" in second["description"]
    assert "#Leonida" not in second["hashtags"]


def test_repeated_transcript_does_not_fill_the_requested_count():
    transcript = {"segments": story(GAME_STORY) + story(GAME_STORY, 130)}
    clips = HighlightEngine().select(transcript, duration=230, minimum=75, maximum=100, count=5)

    assert len(clips) == 1


def test_rejects_silent_sparse_and_too_short_recordings():
    engine = HighlightEngine()
    assert engine.select({"segments": []}, 200, 75, 100, 3) == []
    assert engine.select({"segments": story(GAME_STORY[:4])}, 50, 61, 100, 3) == []
    sparse = {"segments": [{"start": 0, "end": 90, "text": "Sim sim sim sim."}]}
    assert engine.select(sparse, 120, 75, 100, 3) == []


def test_manual_range_uses_only_words_within_clip():
    transcript = {
        "segments": [
            {
                "start": 0,
                "end": 10,
                "text": "Leonida aparece. O motor funcionou.",
                "words": [
                    {"start": 1, "end": 2, "text": "Leonida"},
                    {"start": 2, "end": 3, "text": "aparece."},
                    {"start": 5, "end": 5.3, "text": "O"},
                    {"start": 6, "end": 7, "text": "motor"},
                    {"start": 8, "end": 9, "text": "funcionou."},
                ],
            }
        ]
    }

    sample = editorial_metadata(transcript, 5, 10)
    assert sample["summary"] == "O motor funcionou."
    assert sample["hashtags"] == []


def test_uses_distinct_sources_and_rejects_candidates_with_long_silence():
    transcript = {"segments": story(GAME_STORY[:3]) + story(GAME_STORY[3:6], 75)}
    assert HighlightEngine().select(transcript, 120, 75, 100, 1) == []


def test_ollama_unavailable_falls_back_to_exact_deterministic_candidates(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ConnectionRefusedError

    monkeypatch.setattr(highlights, "_ollama_post", unavailable)
    transcript = {"segments": story(GAME_STORY)}
    expected = HighlightEngine().select(transcript, 100, 75, 100, 1)
    engine = HighlightEngine(ollama_model="qwen3:4b")
    assert engine.select(transcript, 100, 75, 100, 1) == expected
    assert "Ollama" in engine.warning


@pytest.mark.parametrize(
    "raw",
    [
        '{"scores": [{"id": 1, "score": 90, "start": 90}]}',
        '{"scores": [{"id": 2, "score": 90}]}',
        '{"scores": [{"id": 1, "score": true}]}',
        '{"scores": [{"id": 1, "score": 101}]}',
        '{"scores": [{"id": 1, "score": 80, "score": 90}]}',
        '{"scores": []}',
    ],
)
def test_ollama_rejects_malformed_assessments(raw):
    with pytest.raises(ValueError):
        highlights._parse_scores({"done": True, "response": raw}, {1})


def test_ollama_only_changes_scores_and_warns_for_a_remote_model(monkeypatch):
    calls = []

    def local_response(host, port, path, payload, timeout):
        calls.append((host, port, path, payload))
        if path == "/api/show":
            return {"details": {"format": "gguf"}}
        entries = json.loads(payload["prompt"])
        assert all(set(entry) == {"id", "excerpt"} for entry in entries)
        return {
            "done": True,
            "response": json.dumps(
                {"scores": [{"id": entry["id"], "score": 85} for entry in entries]}
            ),
        }

    monkeypatch.setattr(highlights, "_ollama_post", local_response)
    transcript = {"segments": story(GAME_STORY)}
    expected = HighlightEngine().select(transcript, 100, 75, 100, 1)
    engine = HighlightEngine(ollama_model="qwen3:4b", ollama_url="http://localhost:11434")
    [output] = engine.select(transcript, 100, 75, 100, 1)
    assert calls[0] == ("127.0.0.1", 11434, "/api/show", {"model": "qwen3:4b"})
    assert "avaliação semântica local" in output["rationale"]
    assert output["score"] > expected[0]["score"]
    for field in ("start", "end", "title", "description", "summary"):
        assert output[field] == expected[0][field]
    assert engine.warning is None

    def remote_response(host, port, path, payload, timeout):
        assert path == "/api/show"
        return {"remote_host": "https://example.org"}

    monkeypatch.setattr(highlights, "_ollama_post", remote_response)
    assert engine.select(transcript, 100, 75, 100, 1) == expected
    assert engine.warning is not None


def test_ollama_rejects_nonlocal_endpoint_before_sending_request(monkeypatch):
    def unexpected_request(*args):
        pytest.fail("O endereço não local não deve receber uma solicitação.")

    monkeypatch.setattr(highlights, "_ollama_post", unexpected_request)
    engine = HighlightEngine(ollama_model="qwen3:4b", ollama_url="http://example.org")
    assert engine.select({"segments": story(GAME_STORY)}, 100, 75, 100, 1)
    assert engine.warning is not None


def test_candidate_can_end_at_exact_minimum_inside_silence():
    clips = HighlightEngine().select({"segments": story(GAME_STORY[:5])}, 70, 61, 61, 1)
    assert [(clip["start"], clip["end"]) for clip in clips] == [(0, 61)]


def test_cancelled_selection_skips_ollama(monkeypatch):
    def unexpected_request(*args):
        pytest.fail("Uma avaliação cancelada não deve iniciar uma requisição.")

    monkeypatch.setattr(highlights, "_ollama_post", unexpected_request)
    engine = HighlightEngine(ollama_model="qwen3:4b")
    assert not engine.select({"segments": story(GAME_STORY)}, 100, 75, 100, 1, cancel=lambda: True)
