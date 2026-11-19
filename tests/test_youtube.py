import pytest

from cliplab import youtube
from cliplab.config import MIB, Config

ID = "YE7VzlLtp-4"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"availability": "private"}, "público"),
        ({"availability": "unlisted"}, "público"),
        ({"live_status": "is_live"}, "transmissão"),
        ({"duration": 60}, "duração"),
    ],
)
def test_import_rejects_ineligible_metadata_before_downloading(
    tmp_path, monkeypatch, changes, message
):
    class Extractor:
        def __init__(self, options):
            self.match = options["match_filter"]

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def extract_info(self, url, *, download):
            assert download and url == youtube.watch_url(ID)
            assert self.match({"id": ID}, incomplete=True) is None
            data = {"id": ID, "availability": "public", "duration": 100, **changes}
            assert self.match(data)
            return data

    monkeypatch.setattr(youtube, "YoutubeDL", Extractor)
    monkeypatch.setattr(youtube.shutil, "which", lambda name: name)
    with pytest.raises(youtube.YouTubeUnavailable, match=message):
        youtube.download_video(
            ID,
            tmp_path,
            Config(tmp_path, min_free=MIB),
            61,
            cancel=lambda: False,
            progress=lambda _: None,
        )
    assert not list(tmp_path.iterdir())
