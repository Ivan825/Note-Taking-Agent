import pytest

from app.validation import extract_video_id


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=aircAruvnKk",
        "https://youtube.com/watch?v=aircAruvnKk&t=42s",
        "http://m.youtube.com/watch?v=aircAruvnKk",
        "https://youtu.be/aircAruvnKk",
        "https://youtu.be/aircAruvnKk?si=abc",
        "https://www.youtube.com/shorts/aircAruvnKk",
        "https://www.youtube.com/embed/aircAruvnKk",
        "https://www.youtube.com/live/aircAruvnKk",
        "www.youtube.com/watch?v=aircAruvnKk",
    ],
)
def test_valid_urls(url):
    assert extract_video_id(url) == "aircAruvnKk"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "aircAruvnKk",
        "https://vimeo.com/123456",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/channel/UCYO_jab_esuFRV4b17AJtAw",
        "https://evil.com/watch?v=aircAruvnKk",
        "ftp://youtube.com/watch?v=aircAruvnKk",
        "https://www.youtube.com.evil.com/watch?v=aircAruvnKk",
    ],
)
def test_invalid_urls(url):
    assert extract_video_id(url) is None
