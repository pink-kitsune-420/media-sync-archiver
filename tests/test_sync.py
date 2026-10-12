"""End-to-end tests of Update/Repair through the real engine, with yt-dlp replaced by a fake."""
import errno
import threading
import time
import unittest
from unittest import mock

from mediasync import media, ytdl
from mediasync.engine import Engine
from mediasync.library import Show, find_videos
from mediasync.subscriptions import subscriptions
from mediasync.sync import Task

from .helpers import (
    TempLibraryTestCase, info, make_image, make_srt, make_video, needs_ffmpeg, write_info,
)

CHAPTERS = [{"start_time": 0, "end_time": 1.5, "title": "Intro"}, {"start_time": 1.5, "end_time": 3, "title": "Main"}]


class FakeSession:
    """Stands in for ytdl.Session: 'downloads' synthetic videos from FakeSession.source."""
    source: dict[str, dict] = {}       # video id -> info dict of the videos "on YouTube"
    playlist: list[str] = []           # ids currently in the playlist
    gate: threading.Event | None = None
    fail_with: Exception | None = None
    calls: list = []

    def __init__(self, url, mode, hooks, limit=None):
        self.hooks = hooks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def resolve_name(self):
        return "Resolved Show"

    def download(self, staging, known, on_video, incremental=False):
        FakeSession.calls.append(("download", set(known), incremental))
        if self.fail_with:
            raise self.fail_with
        self._fetch([vid for vid in self.playlist if vid not in known], staging, on_video)

    def download_ids(self, ids, staging, known, on_video):
        FakeSession.calls.append(("download_ids", list(ids)))
        self._fetch(ids, staging, on_video)

    def _fetch(self, ids, staging, on_video):
        staging.mkdir(parents=True, exist_ok=True)
        for vid in ids:
            while self.gate is not None and not self.gate.wait(0.05):
                self.hooks.check_cancelled()
            self.hooks.check_cancelled()
            video = make_video(staging / f"{vid}.mkv")
            make_srt(staging / f"{vid}.en.srt")
            make_image(staging / f"{vid}.jpg")
            on_video(video, self.source[vid])


FETCH_CALLS: list[dict] = []
NO_SUBTITLES: set[str] = set()  # ids for which the fake YouTube "rate-limits" subtitle downloads


def fake_fetch_extras(needs, staging, hooks, on_done=None):
    FETCH_CALLS.append({vid: set(wanted) for vid, wanted in needs.items()})
    staging.mkdir(parents=True, exist_ok=True)
    for vid, wanted in needs.items():
        # like yt-dlp fetching a plain watch URL: no playlist context
        write_info(staging, {**FakeSession.source[vid], "playlist_index": None})
        if "subtitles" in wanted and vid not in NO_SUBTITLES:
            make_srt(staging / f"{vid}.en.srt")
        if on_done:
            on_done(vid)


def fake_download_thumbnail(video_id, url, dst):
    make_image(dst)
    return True


@needs_ffmpeg
class SyncTests(TempLibraryTestCase):
    def setUp(self):
        super().setUp()
        FakeSession.source = {
            "aaaaaaaaaaa": info("aaaaaaaaaaa", "First", index=1, chapters=CHAPTERS),
            "bbbbbbbbbbb": info("bbbbbbbbbbb", "Second", index=2),
        }
        FakeSession.playlist = ["aaaaaaaaaaa", "bbbbbbbbbbb"]
        FakeSession.gate = None
        FakeSession.fail_with = None
        FakeSession.calls = []
        FETCH_CALLS.clear()
        NO_SUBTITLES.clear()
        self.feed: list[str] | None = None
        for name, fake in (("Session", FakeSession), ("fetch_extras", fake_fetch_extras),
                           ("download_thumbnail", fake_download_thumbnail),
                           ("recent_upload_ids", lambda channel_id: self.feed)):
            patcher = mock.patch.object(ytdl, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.engine = Engine()

    def tearDown(self):
        # stop the engine before the temporary settings are undone, so no thread outlives the test
        self.engine.stop()
        if FakeSession.gate:
            FakeSession.gate.set()
        self.assertTrue(self.engine.wait_until_idle(timeout=30), "engine did not stop")
        super().tearDown()

    def run_and_wait(self, task):
        self.assertTrue(self.engine.enqueue(task))
        self.engine.wait_until_idle()
        return self.engine.history[0]

    def test_new_playlist_is_downloaded_processed_and_subscribed(self):
        report = self.run_and_wait(Task("update", url="https://www.youtube.com/playlist?list=PLx"))

        self.assertEqual(report.downloaded, 2, report.errors)
        self.assertEqual(report.status, "ok", report.errors)
        show = Show("Resolved Show", "playlist")
        first = show.library_dir / "Season 01" / "S01E01 - First [aaaaaaaaaaa].mkv"
        p = media.probe(first)
        self.assertEqual((p.subtitle_streams, p.chapters, p.has_title_tag), (1, 2, True))
        self.assertIsNotNone(p.cover_index)
        for name in (".nfo", "-thumb.jpg"):
            self.assertTrue(first.with_name(first.stem + name).exists(), name)
        for name in ("tvshow.nfo", "poster.jpg", "fanart.jpg"):
            self.assertTrue((show.library_dir / name).exists(), name)
        self.assertTrue((show.metadata_dir / "aaaaaaaaaaa.info.json").exists())
        self.assertFalse(show.staging_dir.exists(), "staging should be cleaned up")
        self.assertEqual(subscriptions.get("Resolved Show").url, "https://www.youtube.com/playlist?list=PLx")

        again = self.run_and_wait(Task("update", url="https://www.youtube.com/playlist?list=PLx"))
        self.assertEqual(again.summary(), "Everything up to date")
        self.assertEqual(FakeSession.calls[-1], ("download", {"aaaaaaaaaaa", "bbbbbbbbbbb"}, False))

    def test_update_adds_missing_subtitles_chapters_metadata_and_artwork(self):
        show = Show("Course", "playlist")
        video = make_video(show.library_dir / "Season 01" / "S01E01 - First [aaaaaaaaaaa].mkv")

        report = self.run_and_wait(Task("update", url="https://x/playlist?list=PL", name="Course"))

        p = media.probe(video)
        self.assertEqual((p.subtitle_streams, p.chapters, p.has_title_tag), (1, 2, True))
        self.assertTrue(video.with_name(video.stem + "-thumb.jpg").exists())
        self.assertIn("About First", video.with_suffix(".nfo").read_text())
        self.assertEqual(report.downloaded, 1)  # "Second" was missing from the library
        for fixed in ("metadata", "subtitles", "chapters", "tags"):
            self.assertIn(fixed, report.fixed)

    def test_refetching_metadata_keeps_episode_numbers(self):
        show = Show("Course", "playlist")
        season = show.library_dir / "Season 01"
        for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
            write_info(show.metadata_dir, FakeSession.source[vid])
        first = make_video(season / "S01E01 - First [aaaaaaaaaaa].mkv")  # no subtitles -> refetch
        second = make_video(season / "S01E02 - Second [bbbbbbbbbbb].mkv")

        self.run_and_wait(Task("update", url="https://x/playlist?list=PL", name="Course"))

        self.assertTrue(first.exists(), sorted(p.name for p in season.iterdir()))
        self.assertTrue(second.exists())
        self.assertEqual(media.probe(first).subtitle_streams, 1)

    def test_deleted_videos_stay_deleted_until_restored(self):
        url = "https://www.youtube.com/playlist?list=PLx"
        self.run_and_wait(Task("update", url=url))
        show = Show("Resolved Show", "playlist")
        first = show.library_dir / "Season 01" / "S01E01 - First [aaaaaaaaaaa].mkv"
        first.unlink()

        update = self.run_and_wait(Task("update", url=url))
        self.assertEqual(update.downloaded, 0)
        self.assertFalse(first.exists(), "Update must not download a deleted video again")
        self.assertIn("aaaaaaaaaaa", FakeSession.calls[-1][1])

        restore = self.run_and_wait(Task("restore", url=url, name="Resolved Show"))
        self.assertEqual(restore.downloaded, 1)
        self.assertTrue(restore.summary().startswith("1 restored video"), restore.summary())
        self.assertTrue(first.exists())

    def test_channel_updates_check_the_feed_first(self):
        url = "https://www.youtube.com/@somechannel"
        for vid in FakeSession.source:
            FakeSession.source[vid]["channel_id"] = "UCxxxxxxxxxxxxxxxxxxxxxx"
        first = self.run_and_wait(Task("update", url=url))
        self.assertEqual(first.downloaded, 2)
        self.assertEqual(subscriptions.get("Resolved Show").mode, "years")
        self.assertEqual(FakeSession.calls[-1][2], False, "first sync lists the whole channel")

        calls_before = len(FakeSession.calls)
        self.feed = ["bbbbbbbbbbb", "aaaaaaaaaaa"]
        quiet = self.run_and_wait(Task("update", url=url))
        self.assertEqual(len(FakeSession.calls), calls_before, "nothing new in the feed: no listing at all")
        self.assertIn("feed", quiet.summary())

        FakeSession.source["ccccccccccc"] = info("ccccccccccc", "Third", upload_date="20210510",
                                                 channel_id="UCxxxxxxxxxxxxxxxxxxxxxx")
        FakeSession.playlist.insert(0, "ccccccccccc")
        self.feed = ["ccccccccccc", "bbbbbbbbbbb"]
        new = self.run_and_wait(Task("update", url=url))
        self.assertEqual(new.downloaded, 1)
        self.assertEqual(FakeSession.calls[-1][2], True, "listing stops at the first known video")

    def test_failed_subtitles_are_not_retried_on_every_update(self):
        show = Show("Course", "playlist")
        write_info(show.metadata_dir, FakeSession.source["aaaaaaaaaaa"])
        make_video(show.library_dir / "Season 01" / "S01E01 - First [aaaaaaaaaaa].mkv")
        FakeSession.playlist = ["aaaaaaaaaaa"]
        NO_SUBTITLES.add("aaaaaaaaaaa")
        task = dict(url="https://x/playlist?list=PL", name="Course")

        first = self.run_and_wait(Task("update", **task))
        self.assertTrue(any("subtitles could not be downloaded" in e for e in first.errors))
        self.assertIn("subtitles", FETCH_CALLS[-1]["aaaaaaaaaaa"])

        FETCH_CALLS.clear()
        self.run_and_wait(Task("update", **task))
        self.assertFalse(any("subtitles" in c.get("aaaaaaaaaaa", ()) for c in FETCH_CALLS),
                         "a recent failure is not retried by Update")

        NO_SUBTITLES.clear()
        repair = self.run_and_wait(Task("repair", **task))
        self.assertIn("subtitles", repair.fixed, "Repair tries again")

    def test_repair_replaces_broken_file_even_if_it_left_the_playlist(self):
        show = Show("Course", "playlist")
        write_info(show.metadata_dir, FakeSession.source["aaaaaaaaaaa"])
        broken = make_video(show.library_dir / "Season 01" / "S01E01 - First [aaaaaaaaaaa].mkv", audio=False)
        FakeSession.playlist = ["bbbbbbbbbbb"]

        report = self.run_and_wait(Task("repair", url="https://x/playlist?list=PL", name="Course"))

        self.assertEqual(report.redownloaded, 1, report.errors)
        self.assertIn(("download_ids", ["aaaaaaaaaaa"]), FakeSession.calls)
        replaced = [v.path for v in find_videos(show) if v.id == "aaaaaaaaaaa"]
        self.assertEqual(len(replaced), 1)
        self.assertEqual(media.probe(replaced[0]).audio_streams, 1)
        self.assertFalse(broken.exists() and media.probe(broken).audio_streams == 0)

    def test_pause_requeues_and_stop_cancels(self):
        FakeSession.gate = threading.Event()
        self.engine.enqueue(Task("update", url="https://x/playlist?list=PL", name="Course"))
        self._wait_for(lambda: self.engine.status()["current"] is not None)

        self.engine.pause()
        self._wait_for(lambda: self.engine.status()["current"] is None)
        status = self.engine.status()
        self.assertEqual(status["state"], "paused")
        self.assertEqual([t["name"] for t in status["queue"]], ["Course"])

        self.engine.resume()
        self._wait_for(lambda: self.engine.status()["current"] is not None)
        self.engine.stop()
        self.engine.wait_until_idle()
        self.assertEqual(self.engine.history[0].status, "cancelled")
        self.assertEqual(self.engine.status()["queue"], [])

    def test_full_disk_pauses_and_resume_continues(self):
        FakeSession.fail_with = OSError(errno.ENOSPC, "No space left on device")
        self.engine.enqueue(Task("update", url="https://x/playlist?list=PL", name="Course"))
        self.assertTrue(self.engine.wait_until_idle(timeout=30))
        status = self.engine.status()
        self.assertEqual(status["state"], "paused")
        self.assertEqual([t["name"] for t in status["queue"]], ["Course"])
        self.assertEqual(list(self.engine.history), [])

        FakeSession.fail_with = None  # space was freed
        self.engine.resume()
        self._wait_for(lambda: self.engine.history)
        self.engine.wait_until_idle()
        self.assertEqual(self.engine.history[0].downloaded, 2)

    def test_duplicate_tasks_are_ignored(self):
        FakeSession.gate = threading.Event()
        task = dict(url="https://x/playlist?list=PL", name="Course")
        self.assertTrue(self.engine.enqueue(Task("update", **task)))
        self.assertFalse(self.engine.enqueue(Task("update", **task)))
        self.assertTrue(self.engine.enqueue(Task("repair", **task)))
        self.engine.stop()
        FakeSession.gate.set()
        self.engine.wait_until_idle()

    def _wait_for(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("timed out")
            time.sleep(0.02)


class UrlTests(unittest.TestCase):
    def test_normalize_url(self):
        self.assertEqual(ytdl.normalize_url("https://www.youtube.com/@chan/"), "https://www.youtube.com/@chan/videos")
        self.assertEqual(ytdl.normalize_url("https://www.youtube.com/@chan/streams"), "https://www.youtube.com/@chan/streams")
        self.assertEqual(ytdl.normalize_url("https://www.youtube.com/playlist?list=PL1"), "https://www.youtube.com/playlist?list=PL1")

    def test_parse_rate(self):
        self.assertEqual(ytdl.parse_rate("500K"), 500 * 1024)
        self.assertEqual(ytdl.parse_rate("2M"), 2 * 1024 * 1024)
        self.assertIsNone(ytdl.parse_rate(""))


if __name__ == "__main__":
    unittest.main()
