import unittest
from unittest import mock

from mediasync import config, library
from mediasync.library import Show
from mediasync.util import is_fragment, video_id_from_name

from .helpers import TempLibraryTestCase, info, placeholder, write_info


class NamingTests(unittest.TestCase):
    def test_video_id_from_name(self):
        self.assertEqual(video_id_from_name("Show - S01E01 - Title [Xy9-Ab_cD12].mkv"), "Xy9-Ab_cD12")
        self.assertEqual(video_id_from_name("Xy9-Ab_cD12.info.json"), "Xy9-Ab_cD12")
        self.assertEqual(video_id_from_name("Introducing. A title [abcdefghijk].mkv"), "abcdefghijk")
        self.assertEqual(video_id_from_name("Pottery Masterclass [the complete course] [Qw3-Er_Ty45].mkv"), "Qw3-Er_Ty45")
        self.assertIsNone(video_id_from_name("my_notes.png"))

    def test_fragments(self):
        for name in ("abc.f399.mp4", "abc.temp.mkv", "abc.mkv.part", "abc.mkv.ytdl"):
            self.assertTrue(is_fragment(name), name)
        self.assertFalse(is_fragment("Title [Xy9-Ab_cD12].mkv"))

    def test_episode_filename_keeps_full_title(self):
        name = library.episode_filename("S01E30", "Pottery Basics Part 28 – Glazes and Kilns",
                                        "aBcD3fGh1jK", ".mkv")
        self.assertEqual(name, "S01E30 - Pottery Basics Part 28 – Glazes and Kilns [aBcD3fGh1jK].mkv")

    def test_longest_names_fit_ecryptfs(self):
        for title in ("x" * 100, "日本語" * 40):
            stem = library.episode_filename("S2021E101", title, "abcdefghijk", ".mkv").removesuffix(".mkv")
            self.assertLessEqual(len((stem + "-fanart.jpg").encode()), 143)

    def test_episode_title_strips_show_name(self):
        self.assertEqual(library.episode_title("Backyard Pottery Club: First Bowl", "Backyard Pottery Club"), "First Bowl")
        self.assertEqual(library.episode_title("Backyard Pottery Club", "Backyard Pottery Club"), "Backyard Pottery Club")


@mock.patch.object(library.media, "extract_cover", lambda video, dst: False)
@mock.patch.object(library.media, "grab_frame", lambda video, dst: False)
class OrganizeTests(TempLibraryTestCase):
    def test_playlist_numbering_follows_playlist_index_and_moves_sidecars(self):
        show = Show("My Course", "playlist")
        meta = show.metadata_dir
        write_info(meta, info("aaaaaaaaaaa", "Part Two", index=2))
        write_info(meta, info("bbbbbbbbbbb", "Part One", index=1))
        old = placeholder(show.library_dir / "Season 01" / "My Course - S01E01 - Part Two [aaaaaaaaaaa].mkv")
        old.with_suffix(".nfo").write_text("old")
        placeholder(show.library_dir / "misc" / "Part One [bbbbbbbbbbb].mp4")

        result = library.organize(show)

        season = show.library_dir / "Season 01"
        self.assertEqual(result.moved, 2)
        self.assertTrue((season / "S01E01 - Part One [bbbbbbbbbbb].mp4").exists())
        moved = season / "S01E02 - Part Two [aaaaaaaaaaa].mkv"
        self.assertTrue(moved.exists())
        nfo = moved.with_suffix(".nfo").read_text()
        self.assertIn("<episode>2</episode>", nfo)
        self.assertIn('<uniqueid type="youtube" default="true">aaaaaaaaaaa</uniqueid>', nfo)
        self.assertTrue((show.library_dir / "tvshow.nfo").exists())
        self.assertFalse((show.library_dir / "misc").exists())

        second = library.organize(show)
        self.assertFalse(second.changed, "organize must be idempotent")

    def test_years_mode_numbers_episodes_by_upload_date(self):
        show = Show("Channel", "years")
        write_info(show.metadata_dir, info("aaaaaaaaaaa", "Newer", upload_date="20220105"))
        write_info(show.metadata_dir, info("bbbbbbbbbbb", "Older", upload_date="20210301"))
        write_info(show.metadata_dir, info("ccccccccccc", "Same day 2", upload_date="20210101", timestamp=200))
        write_info(show.metadata_dir, info("ddddddddddd", "Same day 1", upload_date="20210101", timestamp=100))
        for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"):
            placeholder(show.library_dir / f"x [{vid}].mkv")

        library.organize(show)

        season = show.library_dir / "Season 2021"
        self.assertTrue((season / "S2021E010101 - Same day 1 [ddddddddddd].mkv").exists())
        self.assertTrue((season / "S2021E010102 - Same day 2 [ccccccccccc].mkv").exists())
        self.assertTrue((season / "S2021E030101 - Older [bbbbbbbbbbb].mkv").exists())
        ep = show.library_dir / "Season 2022" / "S2022E010501 - Newer [aaaaaaaaaaa].mkv"
        nfo_text = ep.with_suffix(".nfo").read_text()
        self.assertIn("<season>2022</season>", nfo_text)
        self.assertIn("<episode>10501</episode>", nfo_text)

    def test_years_mode_numbers_never_change_when_older_videos_arrive(self):
        show = Show("Channel", "years")
        write_info(show.metadata_dir, info("aaaaaaaaaaa", "Newer", upload_date="20210601"))
        placeholder(show.library_dir / "x [aaaaaaaaaaa].mkv")
        library.organize(show)
        newer = show.library_dir / "Season 2021" / "S2021E060101 - Newer [aaaaaaaaaaa].mkv"
        self.assertTrue(newer.exists())

        write_info(show.metadata_dir, info("bbbbbbbbbbb", "Older", upload_date="20210201"))
        placeholder(show.library_dir / "y [bbbbbbbbbbb].mkv")
        result = library.organize(show)
        self.assertTrue(newer.exists(), "existing episode must keep its name")
        self.assertEqual(result.moved, 1)

    def test_deleted_videos_are_remembered(self):
        show = Show("Course", "playlist")
        write_info(show.metadata_dir, info("aaaaaaaaaaa", "One", index=1))
        write_info(show.metadata_dir, info("bbbbbbbbbbb", "Two", index=2))
        placeholder(show.library_dir / "Season 01" / "S01E01 - One [aaaaaaaaaaa].mkv")
        self.assertEqual(library.deleted_ids(show), {"bbbbbbbbbbb"})

    def test_duplicates_keep_mkv(self):
        show = Show("Dupes", "playlist")
        write_info(show.metadata_dir, info("aaaaaaaaaaa", "Video", index=1))
        placeholder(show.library_dir / "Season 01" / "a [aaaaaaaaaaa].mp4", size=5000)
        placeholder(show.library_dir / "Season 01" / "a [aaaaaaaaaaa].mkv", size=10)
        library.organize(show)
        files = sorted(p.name for p in (show.library_dir / "Season 01").iterdir())
        self.assertEqual(files, ["S01E01 - Video [aaaaaaaaaaa].mkv", "S01E01 - Video [aaaaaaaaaaa].nfo"])

    def test_junk_is_removed_but_user_files_are_kept(self):
        show = Show("Movie Title", "movie")
        write_info(show.metadata_dir, info("aaaaaaaaaaa", "Movie Title"))
        video = placeholder(show.library_dir / "Movie Title [aaaaaaaaaaa].mkv")
        (show.library_dir / "Movie Title [aaaaaaaaaaa].f399.mp4").write_bytes(b"x")
        (show.library_dir / "Old Name [aaaaaaaaaaa].nfo").write_text("orphan")
        (show.library_dir / "tvshow.nfo").write_text("leftover")
        (show.library_dir / "cache.txt").write_text("youtube aaaaaaaaaaa")
        (show.library_dir / "my_notes.png").write_bytes(b"user file")

        library.organize(show)

        names = sorted(p.name for p in show.library_dir.iterdir() if not p.name.startswith("."))
        self.assertEqual(names, [video.name, video.with_suffix(".nfo").name, "my_notes.png"])
        self.assertIn("<movie>", video.with_suffix(".nfo").read_text())

    def test_incoming_video_replaces_broken_copy(self):
        show = Show("Course", "playlist")
        write_info(show.metadata_dir, info("aaaaaaaaaaa", "Video", index=1))
        old = placeholder(show.library_dir / "Season 01" / "S01E01 - Video [aaaaaaaaaaa].mkv", size=10)
        incoming = placeholder(show.library_dir / ".tmp-aaaaaaaaaaa.mkv", size=5)
        library.organize(show, incoming={"aaaaaaaaaaa": incoming})
        self.assertEqual(old.stat().st_size, 5)
        self.assertFalse(incoming.exists())

    def test_catalog_lists_subscriptions_and_folders(self):
        from mediasync.subscriptions import Subscription, subscriptions
        subscriptions.upsert(Subscription("Wanted", "https://www.youtube.com/@x", "years"))
        placeholder(config.MOVIES_DIR / "A Movie" / "A Movie [aaaaaaaaaaa].mkv", size=2048)
        entries = {e["name"]: e for e in library.catalog(subscriptions.all())}
        self.assertEqual(entries["Wanted"]["location"], "missing")
        self.assertEqual(entries["A Movie"]["mode"], "movie")
        self.assertEqual(entries["A Movie"]["videos"], 1)
        self.assertFalse(entries["A Movie"]["subscribed"])


if __name__ == "__main__":
    unittest.main()
