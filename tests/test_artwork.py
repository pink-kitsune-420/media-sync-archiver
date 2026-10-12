from PIL import Image

from mediasync import artwork

from .helpers import TempLibraryTestCase, make_image, needs_ffmpeg


class WrapTests(TempLibraryTestCase):
    def test_long_titles_wrap_and_end_with_ellipsis(self):
        font = artwork._font(40, bold=True)
        lines, fits = artwork.wrap("word " * 80, font, 300, 4)
        self.assertEqual(len(lines), 4)
        self.assertFalse(fits)
        self.assertTrue(lines[-1].endswith("…"))

    def test_emoji_are_dropped(self):
        self.assertEqual(artwork.clean_text("Hello 🔥 world ✅  !"), "Hello world !")


@needs_ffmpeg
class PosterTests(TempLibraryTestCase):
    def test_poster_has_title_card(self):
        for size in ("1280x720", "800x800"):  # video thumbnail, square channel avatar
            with self.subTest(source=size):
                poster = self.root / f"poster-{size}.jpg"
                self.assertTrue(artwork.make_poster(make_image(self.root / f"{size}.jpg", size), poster,
                                                    "A fairly long video title that needs to wrap", "Channel"))
                with Image.open(poster) as im:
                    self.assertEqual(im.size, artwork.POSTER_SIZE)
                    histogram = im.convert("L").histogram()
                white = sum(histogram[251:]) / sum(histogram)
                self.assertGreater(white, 0.12, "expected a white title card")

    def test_unreadable_source(self):
        bad = self.root / "bad.jpg"
        bad.write_text("not an image")
        self.assertFalse(artwork.make_poster(bad, self.root / "out.jpg", "Title"))
