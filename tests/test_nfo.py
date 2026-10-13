"""Which YouTube tags end up in the .nfo files."""
import unittest

from mediasync import nfo
from mediasync.metadata import VideoInfo


def video(vid: str, *tags: str, title: str = "", description: str = "") -> VideoInfo:
    return VideoInfo(vid, title=title or vid, description=description, channel="Clay Corner", tags=list(tags))


def movie_tags(info: VideoInfo, title: str) -> list[str]:
    text = nfo.movie_nfo(info, video_id=info.id, title=title)
    return [line.strip()[5:-6] for line in text.splitlines() if line.strip().startswith("<tag>")]


class TagTests(unittest.TestCase):
    def test_a_show_gets_the_tags_its_episodes_share_most(self):
        episodes = [video(f"v{i}", "pottery", "ceramics", "Clay Corner", f"episode topic {i}") for i in range(6)]
        episodes += [video("v6", "ceramics", "Glazes"), video("v7", "glaze", "pottery", "kiln repair")]
        tags = nfo.pick_tags(episodes, "Backyard Pottery Club", "Clay Corner")
        self.assertEqual(tags[:2], ["Pottery", "Ceramics"])  # on 7 videos each; "pottery" is usually listed first
        self.assertIn("Glazes", tags)  # "Glazes" and "glaze" count as one tag, on two videos
        self.assertNotIn("Clay Corner", tags)  # just the channel's name
        self.assertFalse(any("episode topic" in t.lower() for t in tags))  # only on one episode each
        self.assertNotIn("Kiln Repair", tags)

    def test_the_number_of_tags_is_limited(self):
        episodes = [video(f"v{i}", *(f"tag{n}" for n in range(30))) for i in range(4)]
        self.assertEqual(nfo.pick_tags(episodes, "Show", "Channel"), [f"Tag{n}" for n in range(10)])

    def test_movies_get_their_creators_first_tags(self):
        info = video("aaaaaaaaaaa", "log cabin", "off grid", "DIY", "WOODWORKING", "building a cabin in 30 days",
                     "don't try this", "#timber", "class", "glass", "classes", "news", "new")
        self.assertEqual(movie_tags(info, "Building a Cabin in 30 Days"),
                         ["Log Cabin", "Off Grid", "DIY", "Woodworking", "Don't Try This", "Timber", "Class", "Glass"])

    def test_plurals_count_as_one_tag(self):
        self.assertEqual([nfo._singular(w) for w in ("glazes", "kilns", "class", "glass", "news", "campus", "analysis")],
                         ["glaze", "kiln", "class", "glass", "news", "campus", "analysis"])
        tags = nfo.pick_tags([video("v1", "news", "new", "Kilns", "kiln")], "Show", "Channel")
        self.assertEqual(tags, ["News", "New", "Kilns"])

    def test_hashtags_count_as_tags(self):
        info = video("aaaaaaaaaaa", title="Night Run #trailrunning",
                     description="Gear list: https://example.org/gear#Shoes\n(#offgrid) #CabinLife #ClayCorner #shorts")
        self.assertEqual(nfo.pick_tags([info], "Trail Running Diaries", "Clay Corner"),
                         ["Trailrunning", "Offgrid", "Cabin Life"])
