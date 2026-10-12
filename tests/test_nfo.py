"""Which YouTube tags end up in the .nfo files."""
import unittest

from mediasync import nfo
from mediasync.metadata import VideoInfo


def video(vid: str, *tags: str) -> VideoInfo:
    return VideoInfo(vid, title=vid, channel="Brandon James", tags=list(tags))


class TagTests(unittest.TestCase):
    def test_a_show_gets_the_tags_its_episodes_share_most(self):
        episodes = [video(f"v{i}", "pixel art", "aseprite", "Brandon James", f"episode topic {i}") for i in range(6)]
        episodes += [video("v6", "aseprite", "Sprites"), video("v7", "sprite", "pixel art", "game dev")]
        tags = nfo.pick_tags(episodes, "Pixel Art 101", "Brandon James")
        self.assertEqual(tags[:2], ["Pixel Art", "Aseprite"])  # on 7 videos each; "pixel art" is usually listed first
        self.assertIn("Sprites", tags)  # "Sprites" and "sprite" count as one tag, on two videos
        self.assertNotIn("Brandon James", tags)  # just the channel name
        self.assertFalse(any("episode topic" in t.lower() for t in tags))  # only on one episode each
        self.assertNotIn("Game Dev", tags)

    def test_the_number_of_tags_is_limited(self):
        episodes = [video(f"v{i}", *(f"tag{n}" for n in range(30))) for i in range(4)]
        self.assertEqual(nfo.pick_tags(episodes, "Show", "Channel"), [f"Tag{n}" for n in range(10)])

    def test_movies_get_their_creators_first_tags(self):
        info = video("aaaaaaaaaaa", "commodore 64", "C64", "basic", "retro computing", "tutorial", "snake", "game",
                     "8-bit", "programming", "beginner", "brandon james")
        text = nfo.movie_nfo(info, video_id="aaaaaaaaaaa", title="Coding Snake")
        tags = [line.strip()[5:-6] for line in text.splitlines() if line.strip().startswith("<tag>")]
        self.assertEqual(tags, ["Commodore 64", "C64", "Basic", "Retro Computing", "Tutorial", "Snake", "Game", "8-Bit"])

    def test_hashtags_count_as_tags(self):
        info = VideoInfo("PxZGoiWvA4A", title="6502 Assembly Language: Getting Started", channel="Some Channel",
                         description="Intro.\nWikipedia: https://en.wikipedia.org/wiki/MOS_6502#History\n"
                                     "#Assembly_language #6502 #Assembly #PixelArt")
        self.assertEqual(nfo.pick_tags([info], info.title, info.channel),
                         ["Assembly language", "6502", "Assembly", "Pixel Art"])
