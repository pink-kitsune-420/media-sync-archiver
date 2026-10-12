"""Several libraries, and libraries on drives that are not always connected."""
from mediasync import config, library, sync
from mediasync.library import Show
from mediasync.subscriptions import Subscription, subscriptions

from mediasync.util import write_json

from .helpers import TempLibraryTestCase, info, placeholder, write_info


def make_show(lib: config.Library, name: str = "Course") -> Show:
    show = Show(name, "playlist", lib.name)
    write_info(show.metadata_dir, info("aaaaaaaaaaa", "One", index=1))
    placeholder(show.library_dir / "Season 01" / "S01E01 - One [aaaaaaaaaaa].mkv")
    return show


class LocateTests(TempLibraryTestCase):
    def test_a_moved_folder_is_followed(self):
        drive = self.add_library("Drive")
        subscriptions.upsert(Subscription("Course", "https://x/playlist?list=PL", library="Main", archived=True))
        make_show(drive)

        where = library.locate("Course", "playlist", subscriptions.get("Course"))
        self.assertEqual((where.status, where.show.library), ("library", "Drive"))
        self.assertEqual(subscriptions.get("Course").library, "Drive")

    def test_unplugged_drive_is_offline_not_new(self):
        drive = self.add_library("Drive")
        make_show(drive)
        subscriptions.upsert(Subscription("Course", "u"))
        library.locate("Course", "playlist", subscriptions.get("Course"))  # seen once: remembered
        self.assertEqual(subscriptions.get("Course"), Subscription("Course", "u", library="Drive", archived=True))
        self.unplug(drive)

        where = library.locate("Course", "playlist", subscriptions.get("Course"))
        self.assertEqual(where.status, "offline")
        self.assertIn("Drive is not connected", where.note)

        self.plug_in(drive)
        self.assertEqual(library.locate("Course", "playlist", subscriptions.get("Course")).status, "library")

    def test_a_downloaded_show_whose_folder_is_gone_is_missing(self):
        drive = self.add_library("Drive")
        self.unplug(drive)
        subscriptions.upsert(Subscription("Course", "u", library="Main", archived=True))
        where = library.locate("Course", "playlist", subscriptions.get("Course"))
        self.assertEqual(where.status, "missing")
        self.assertIn("may be on Drive", where.note)

    def test_subscriptions_from_version_1_0_are_never_downloaded_from_scratch(self):
        write_json(config.SUBSCRIPTIONS_FILE, {"subscriptions": [
            {"name": "Course", "url": "u", "mode": "playlist", "limit": None}]})  # 1.0: no library recorded
        old = subscriptions.get("Course")
        self.assertEqual((old.library, old.archived), ("", True))
        self.assertEqual(library.locate("Course", "playlist", old).status, "missing")

        drive = self.add_library("Drive", connected=False)
        self.assertEqual(library.locate("Course", "playlist", old).status, "offline")
        self.plug_in(drive)
        make_show(drive)
        self.assertEqual(library.locate("Course", "playlist", old).status, "library")
        self.assertEqual(subscriptions.get("Course"), Subscription("Course", "u", library="Drive", archived=True))

    def test_new_things_go_to_the_chosen_library(self):
        self.add_library("Drive")
        where = library.locate("Course", "playlist", None, wanted_library="Drive")
        self.assertEqual((where.status, where.show.library), ("new", "Drive"))
        self.assertEqual(where.show.library_dir, config.get_library("Drive").shows_path / "Course")

    def test_writing_to_an_unplugged_library_is_refused(self):
        drive = self.add_library("Drive", connected=False)
        with self.assertRaises(library.LibraryOffline):
            Show("Course", "playlist", drive.name).require_online()
        self.assertFalse((self.root / "Drive").exists())

    def test_finished_downloads_wait_in_staging_while_unplugged(self):
        self.add_library("Drive", connected=False)
        show = Show("Course", "playlist", "Drive")
        staged = placeholder(show.staging_dir / "aaaaaaaaaaa.mkv")
        with self.assertRaises(library.LibraryOffline):
            sync.process_download(show, staged, None, sync.Report("update", "Course"))
        self.assertTrue(staged.exists())
        self.assertFalse((self.root / "Drive").exists())


class CatalogTests(TempLibraryTestCase):
    def test_catalog_lists_every_library_and_offline_items(self):
        drive = self.add_library("Drive")
        make_show(drive, "On The Drive")
        make_show(config.default_library(), "At Home")
        subscriptions.upsert(Subscription("Gone", "u", library="Drive", archived=True))
        entries = {e["name"]: e for e in library.catalog(subscriptions.all())}
        self.assertEqual(entries["On The Drive"]["library"], "Drive")
        self.assertEqual(entries["At Home"]["library"], "Main")
        self.assertEqual(entries["Gone"]["location"], "missing")

        self.unplug(drive)
        entries = {e["name"]: e for e in library.catalog(subscriptions.all())}
        self.assertNotIn("On The Drive", entries)  # unsubscribed and out of sight
        self.assertEqual(entries["Gone"]["location"], "offline")


    def test_leftover_download_files_are_their_own_kind_of_entry(self):
        drive = self.add_library("Drive", connected=False)
        placeholder(config.STAGING_DIR / "Old Show" / "aaaaaaaaaaa.info.json", size=2048)
        entry = next(e for e in library.catalog(subscriptions.all()) if e["name"] == "Old Show")
        self.assertEqual((entry["location"], entry["videos"], entry["size"]), ("leftovers", 0, 2048))
        self.assertIn("may be on Drive", entry["note"])

        self.plug_in(drive)
        make_show(drive, "Old Show")  # its drive is back: a normal entry with an unfinished download
        entry = next(e for e in library.catalog(subscriptions.all()) if e["name"] == "Old Show")
        self.assertEqual((entry["location"], entry["library"], entry["partial"]), ("library", "Drive", True))


class PrepareLibrariesTests(TempLibraryTestCase):
    def test_new_libraries_get_folders_and_renames_keep_subscriptions(self):
        main = config.default_library()
        subscriptions.upsert(Subscription("Course", "u", library="Main", archived=True))
        new = config.Library("Drive", str(self.root / "Drive" / "Shows"), str(self.root / "Drive" / "Movies"))
        renamed = config.Library("Home", main.shows_dir, main.movies_dir)

        self.assertEqual(library.prepare_libraries([main], [renamed, new]), [])
        self.assertTrue(new.shows_path.is_dir() and new.movies_path.is_dir())
        self.assertEqual(subscriptions.get("Course").library, "Home")

    def test_folders_that_cannot_be_created_are_reported(self):
        blocker = self.root / "not-a-drive"
        blocker.write_text("a file where the drive's folder would be")
        lib = config.Library("Drive", str(blocker / "Shows"), str(blocker / "Movies"))
        self.assertEqual(library.prepare_libraries([], [lib]), ["Drive"])
