"""Console vs. log file, the detailed log switch, and recognising a full disk."""
import errno
import io
import logging
from datetime import date
from unittest import mock

from mediasync import config, logs, ytdl
from mediasync.util import is_disk_full

from .helpers import TempLibraryTestCase


class LoggingTests(TempLibraryTestCase):
    def setUp(self):
        super().setUp()
        self.console = io.StringIO()
        patcher = mock.patch("sys.stderr", self.console)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._remove_handlers)
        self.log_dir = logs.setup("test")
        self.log = logging.getLogger("mediasync.test")

    def _remove_handlers(self):
        for handler in logs._handlers:
            logging.getLogger().removeHandler(handler)
            handler.close()
        logs._handlers.clear()
        logs.set_detailed(False)

    def log_file(self) -> str:
        for handler in logs._handlers:
            handler.flush()
        return (config.LOG_DIR / f"mediasync-{date.today().isoformat()}.log").read_text(encoding="utf-8")

    def test_console_gets_important_lines_and_the_file_gets_everything(self):
        self.log.info("Update Course (playlist)")
        self.log.log(logs.DETAIL, "Course: renaming a -> b")
        self.log.debug("only with the detailed log")
        logging.getLogger(logs.YTDLP_LOGGER).warning("yt-dlp: some warning")

        self.assertEqual(self.log_dir, config.LOG_DIR)
        console, file = self.console.getvalue(), self.log_file()
        self.assertIn("Update Course (playlist)", console)
        self.assertNotIn("renaming", console)
        self.assertNotIn("some warning", console)
        self.assertIn("Update Course (playlist)", file)
        self.assertIn("DETAIL  Course: renaming a -> b", file)
        self.assertIn("WARNING yt-dlp: some warning", file)
        self.assertNotIn("only with the detailed log", file)

    def test_detailed_log_adds_debug_lines(self):
        logs.set_detailed(True)
        self.log.debug("every request")
        self.assertIn("DEBUG   every request", self.log_file())
        self.assertNotIn("every request", self.console.getvalue())

    def test_tracebacks_only_go_to_the_file(self):
        try:
            raise ValueError("boom")
        except ValueError:
            self.log.exception("Task failed: Course")
        console = self.console.getvalue()
        self.assertIn("Task failed: Course: boom (details in the log file)", console)
        self.assertNotIn("Traceback", console)
        self.assertIn("Traceback", self.log_file())

    def test_old_log_files_are_pruned(self):
        for day in range(1, 20):
            (config.LOG_DIR / f"mediasync-2020-01-{day:02d}.log").write_text("old")
        self._remove_handlers()
        logs.setup("test")
        self.assertEqual(len(list(config.LOG_DIR.glob("mediasync-*.log"))), logs.KEEP_DAYS)


class SmallHelperTests(TempLibraryTestCase):
    def test_is_disk_full(self):
        self.assertTrue(is_disk_full(OSError(errno.ENOSPC, "No space left on device")))
        self.assertTrue(is_disk_full("unable to open for writing: [Errno 28] No space left on device: 'x.part'"))
        self.assertFalse(is_disk_full(OSError(errno.ENOENT, "No such file or directory")))
        self.assertFalse(is_disk_full("HTTP Error 403: Forbidden"))

    def test_gentle_pauses_are_shown_instead_of_looking_stuck(self):
        hooks = mock.Mock()
        logger = ytdl._Logger(hooks)
        logger.debug("[youtube] Sleeping 0.75 seconds ...")  # between page requests: too short to mention
        logger.debug("[download] Sleeping 12.93 seconds ...")
        hooks.waiting.assert_called_once_with(12.93)

    def test_private_and_deleted_playlist_entries_are_skipped_without_a_request(self):
        self.assertIsNotNone(ytdl._skip_unavailable({"id": "aaaaaaaaaaa", "title": "[Private video]"}, incomplete=True))
        self.assertIsNotNone(ytdl._skip_unavailable({"id": "aaaaaaaaaaa", "title": "[Deleted video]"}, incomplete=True))
        self.assertIsNone(ytdl._skip_unavailable({"id": "aaaaaaaaaaa", "title": "Pixel Art 101"}, incomplete=True))
