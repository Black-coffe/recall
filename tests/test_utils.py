"""Тесты для app.utils.* (Phase 5.1 split)."""
from app.utils.youtube_id import extract_youtube_id
from app.utils.files import (
    allowed_file,
    is_video_file,
    is_audio_file,
    format_srt_timestamp,
)


# ===== youtube_id =====

class TestExtractYoutubeId:
    def test_standard(self):
        assert extract_youtube_id("https://youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short(self):
        assert extract_youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_with_query(self):
        assert extract_youtube_id("https://youtu.be/dQw4w9WgXcQ?si=ABC") == "dQw4w9WgXcQ"

    def test_embed(self):
        assert extract_youtube_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_live(self):
        assert extract_youtube_id("https://www.youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_shorts(self):
        assert extract_youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_mobile(self):
        assert extract_youtube_id("https://m.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_music(self):
        assert extract_youtube_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_with_timestamp(self):
        assert extract_youtube_id("https://youtube.com/watch?v=dQw4w9WgXcQ&t=42") == "dQw4w9WgXcQ"

    def test_invalid_url(self):
        assert extract_youtube_id("https://example.com/video") is None

    def test_empty(self):
        assert extract_youtube_id("") is None
        assert extract_youtube_id(None) is None


# ===== files =====

class TestAllowedFile:
    def test_audio(self):
        assert allowed_file("song.mp3")
        assert allowed_file("audio.wav")
        assert allowed_file("speech.flac")

    def test_video(self):
        assert allowed_file("video.mp4")
        assert allowed_file("clip.mkv")

    def test_uppercase_ext(self):
        assert allowed_file("FILE.MP3")

    def test_unknown_ext(self):
        assert not allowed_file("doc.pdf")
        assert not allowed_file("text.txt")

    def test_no_ext(self):
        assert not allowed_file("noext")
        assert not allowed_file("")
        assert not allowed_file(None)


class TestIsVideoFile:
    def test_yes(self):
        assert is_video_file("clip.mp4")
        assert is_video_file("clip.MOV")

    def test_audio_is_not_video(self):
        assert not is_video_file("song.mp3")
        assert not is_video_file("song.wav")


class TestIsAudioFile:
    def test_yes(self):
        assert is_audio_file("song.mp3")
        assert is_audio_file("song.FLAC")

    def test_video_is_not_audio(self):
        assert not is_audio_file("clip.mp4")


class TestFormatSrtTimestamp:
    def test_zero(self):
        assert format_srt_timestamp(0) == "00:00:00,000"

    def test_seconds_only(self):
        assert format_srt_timestamp(5.5) == "00:00:05,500"

    def test_minutes(self):
        assert format_srt_timestamp(125) == "00:02:05,000"

    def test_hours(self):
        assert format_srt_timestamp(3725.123) == "01:02:05,123"

    def test_uses_comma_decimal(self):
        """SRT використовує кому, не крапку, для мс."""
        result = format_srt_timestamp(1.5)
        assert "," in result
        assert "." not in result
