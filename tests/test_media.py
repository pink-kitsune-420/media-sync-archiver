import unittest

from mediasync import media
from mediasync.metadata import VideoInfo

from .helpers import TempLibraryTestCase, info, make_image, make_srt, make_video, needs_ffmpeg


@needs_ffmpeg
class MediaTests(TempLibraryTestCase):
    def test_remux_adds_subtitles_chapters_tags_and_cover_in_one_pass(self):
        src = make_video(self.root / "src.mp4", seconds=4)
        meta = VideoInfo.from_json(info("aaaaaaaaaaa", "A = title; #1", chapters=[
            {"start_time": 0, "end_time": 2, "title": "Intro"}, {"start_time": 2, "end_time": 4, "title": "Rest"}]))
        for ext in (".mkv", ".mp4"):
            with self.subTest(container=ext):
                out = self.root / f"out{ext}"
                media.remux(src, out, info=meta, subtitle=make_srt(self.root / "s.srt"),
                            cover=make_image(self.root / "c.jpg"))
                p = media.probe(out)
                self.assertEqual((p.video_streams, p.audio_streams, p.subtitle_streams, p.chapters), (1, 1, 1, 2))
                self.assertTrue(p.has_title_tag)
                self.assertIsNotNone(p.cover_index)
                self.assertTrue(media.extract_cover(out, self.root / f"thumb{ext}.jpg"))
                self.assertIsNone(media.verify(out, meta))

    def test_problems_are_detected(self):
        good = make_video(self.root / "good.mkv", seconds=20)
        silent = make_video(self.root / "silent.mkv", audio=False)
        truncated = self.root / "truncated.mkv"
        truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
        empty = self.root / "empty.mkv"
        empty.write_bytes(b"")

        self.assertIsNone(media.verify(good, None))
        self.assertEqual(media.find_problem(silent, None), "no audio stream")
        self.assertIsNone(media.find_problem(silent, VideoInfo.from_json(info("aaaaaaaaaaa", "x", acodec="none"))))
        self.assertIn("truncated", media.verify(truncated, None) or "")
        self.assertIn("unreadable", media.find_problem(empty, None))


if __name__ == "__main__":
    unittest.main()
