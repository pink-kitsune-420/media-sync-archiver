"""The dashboard API and the command line, with the engine's threads switched off."""
import contextlib
import io

from mediasync import cli, config
from mediasync.engine import Engine
from mediasync.subscriptions import Subscription, subscriptions
from mediasync.web import create_app

from .helpers import TempLibraryTestCase, info, placeholder, write_info


class WebTests(TempLibraryTestCase):
    def setUp(self):
        super().setUp()
        self.engine = Engine(autostart=False)
        self.client = create_app(self.engine).test_client()

    def test_settings_can_be_read_and_saved(self):
        data = self.client.get("/api/settings").json
        self.assertEqual(data["settings"]["libraries"][0]["shows_dir"], config.default_library().shows_dir)
        self.assertEqual(data["online"], {"Main": True})

        res = self.client.post("/api/settings", json={**data["settings"], "max_height": "720", "port": 9999})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertIn("Restart", res.json["message"])
        self.assertEqual(config.settings.max_height, 720)
        self.assertIn("max_height = 720", config.config_file().read_text())

        bad = self.client.post("/api/settings", json={**data["settings"], "auto_update_hours": 1})
        self.assertEqual(bad.status_code, 400)

    def test_restore_action_is_queued(self):
        show_dir = config.SHOWS_DIR / "Course"
        write_info(show_dir / ".metadata", info("aaaaaaaaaaa", "One", index=1))
        placeholder(show_dir / "Season 01" / "S01E02 - Two [bbbbbbbbbbb].mkv")
        library_entry = next(e for e in self.client.get("/api/library").json if e["name"] == "Course")
        self.assertEqual(library_entry["deleted"], 1)

        res = self.client.post("/api/action", json={"action": "restore", "name": "Course"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual([t["kind"] for t in self.engine.status()["queue"]], ["restore"])

    def test_libraries_can_be_added_and_offline_items_are_left_alone(self):
        settings = self.client.get("/api/settings").json["settings"]
        drive = {"name": "Drive", "shows_dir": str(self.root / "Drive" / "Shows"),
                 "movies_dir": str(self.root / "Drive" / "Movies")}
        res = self.client.post("/api/settings", json={**settings, "libraries": [*settings["libraries"], drive]})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(res.json["online"], {"Main": True, "Drive": True})
        self.assertTrue((self.root / "Drive" / "Shows").is_dir())

        subscriptions.upsert(Subscription("Course", "https://x/playlist?list=PL", library="Drive", archived=True))
        self.unplug(config.get_library("Drive"))
        res = self.client.post("/api/action", json={"action": "update", "name": "Course"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("not connected", res.json["error"])
        self.assertIn("Skipped what is on Drive", self.client.post("/api/sync-all", json={"action": "update"}).json["message"])
        self.assertEqual(self.engine.status()["queue"], [])

        self.assertEqual(self.client.get("/api/settings").json["states"]["Drive"], "not connected")
        res = self.client.post("/api/libraries/create", json={"name": "Drive"})
        self.assertEqual(res.status_code, 400)  # nowhere to create them: nothing is created on this computer
        self.assertFalse((self.root / "Drive").exists())

        self.unplugged.discard("Drive")  # a new, empty drive is plugged in
        self.assertEqual(self.client.get("/api/settings").json["states"]["Drive"], "no folders")
        res = self.client.post("/api/libraries/create", json={"name": "Drive"})
        self.assertEqual(res.json["states"]["Drive"], "connected")

    def test_leftover_download_files_can_be_deleted(self):
        leftovers = config.STAGING_DIR / "Old Show"
        placeholder(leftovers / "aaaaaaaaaaa.en.srt")
        res = self.client.post("/api/action", json={"action": "delete_leftovers", "name": "Old Show"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertFalse(leftovers.exists())
        self.assertNotIn("Old Show", [e["name"] for e in self.client.get("/api/library").json])

        show_dir = config.SHOWS_DIR / "Course"
        placeholder(show_dir / "Season 01" / "S01E01 - One [aaaaaaaaaaa].mkv")
        res = self.client.post("/api/action", json={"action": "delete_leftovers", "name": "Course"})
        self.assertEqual(res.status_code, 400)
        self.assertTrue(show_dir.exists())

    def test_unsubscribed_shows_can_be_subscribed_again(self):
        show_dir = config.SHOWS_DIR / "Course"
        write_info(show_dir / ".metadata", info("aaaaaaaaaaa", "One", index=1))
        (show_dir / ".metadata" / "playlist.info.json").write_text(
            '{"_type": "playlist", "id": "PL1", "title": "Course", "webpage_url": "https://www.youtube.com/playlist?list=PL1"}')
        placeholder(show_dir / "Season 01" / "S01E01 - One [aaaaaaaaaaa].mkv")

        res = self.client.post("/api/action", json={"action": "subscribe", "name": "Course"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(subscriptions.get("Course"), Subscription(
            "Course", "https://www.youtube.com/playlist?list=PL1", "playlist", None, library="Main", archived=True))

    def test_an_address_can_be_entered_when_it_is_unknown(self):
        placeholder(config.SHOWS_DIR / "Course" / "Season 01" / "S01E01 - One [aaaaaaaaaaa].mkv")
        bad = self.client.post("/api/action", json={"action": "subscribe", "name": "Course", "url": "nope"})
        self.assertEqual(bad.status_code, 400)
        url = "https://www.youtube.com/playlist?list=PL1"
        res = self.client.post("/api/action", json={"action": "subscribe", "name": "Course", "url": url})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(subscriptions.get("Course").url, url)

    def test_moving_is_queued_only_to_a_connected_library(self):
        placeholder(config.SHOWS_DIR / "Course" / "Season 01" / "S01E01 - One [aaaaaaaaaaa].mkv")
        self.add_library("Drive", connected=False)
        res = self.client.post("/api/action", json={"action": "move", "name": "Course", "library": "Drive"})
        self.assertEqual(res.status_code, 400)
        self.plug_in(config.get_library("Drive"))
        res = self.client.post("/api/action", json={"action": "move", "name": "Course", "library": "Drive"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual([(t["kind"], t["name"]) for t in self.engine.status()["queue"]], [("move", "Course")])
        self.assertEqual(self.client.get("/api/status").json["libraries"],
                         [{"name": "Main", "online": True, "state": "connected"},
                          {"name": "Drive", "online": True, "state": "connected"}])

    def test_folders_organize_the_list(self):
        for name in ("Course", "Other"):
            placeholder(config.SHOWS_DIR / name / "Season 01" / "S01E01 - One [aaaaaaaaaaa].mkv")
        folder_of = lambda: {e["name"]: e["folder"] for e in self.client.get("/api/library").json}  # noqa: E731
        self.assertEqual(folder_of(), {"Course": "", "Other": ""})

        self.client.post("/api/action", json={"action": "set_folder", "name": "Course", "folder": "  Retro   Computers "})
        self.client.post("/api/action", json={"action": "set_folder", "name": "Other", "folder": "Retro Computers"})
        self.assertEqual(folder_of(), {"Course": "Retro Computers", "Other": "Retro Computers"})
        self.assertTrue((config.SHOWS_DIR / "Course").is_dir())  # nothing moves on disk

        self.client.post("/api/folders/rename", json={"old": "Retro Computers", "new": "Retro"})
        self.client.post("/api/action", json={"action": "set_folder", "name": "Other", "folder": ""})
        self.assertEqual(folder_of(), {"Course": "Retro", "Other": ""})
        res = self.client.post("/api/folders/rename", json={"old": "Retro", "new": ""})
        self.assertIn("1 item stay", res.json["message"])
        self.assertEqual(folder_of(), {"Course": "", "Other": ""})
        self.assertEqual(self.client.get("/api/folders").json["folders"], [])

    def test_empty_folders_can_be_made_and_stay(self):
        self.assertEqual(self.client.post("/api/folders/create", json={"name": " Art "}).status_code, 200)
        self.assertEqual(self.client.post("/api/folders/create", json={"name": "Art"}).status_code, 409)
        self.assertEqual(self.client.post("/api/folders/create", json={"name": "  "}).status_code, 400)
        placeholder(config.SHOWS_DIR / "Course" / "Season 01" / "S01E01 - One [aaaaaaaaaaa].mkv")
        self.client.post("/api/action", json={"action": "set_folder", "name": "Course", "folder": "Art"})
        self.client.post("/api/action", json={"action": "set_folder", "name": "Course", "folder": ""})
        self.assertEqual(self.client.get("/api/folders").json["folders"], ["Art"])  # still there, empty

    def test_bad_urls_are_refused(self):
        self.assertEqual(self.client.post("/api/queue", json={"url": "not a url"}).status_code, 400)


class CliTests(TempLibraryTestCase):
    def test_parser(self):
        args = cli.build_parser().parse_args(["add", "https://youtu.be/x", "--as", "movie", "--limit", "3"])
        self.assertEqual((args.url, args.mode, args.limit), ("https://youtu.be/x", "movie", 3))

    def test_serve_is_the_default_command(self):
        self.assertEqual(cli.normalize_argv([]), ["serve"])
        self.assertEqual(cli.normalize_argv(["--no-browser", "--port", "9"]), ["serve", "--no-browser", "--port", "9"])
        self.assertEqual(cli.normalize_argv(["update", "X"]), ["update", "X"])
        args = cli.build_parser().parse_args(cli.normalize_argv(["--no-browser"]))
        self.assertTrue(args.no_browser)
        self.assertEqual(set(cli.COMMANDS), set(cli.build_parser()._subparsers._group_actions[0].choices))

    def test_guess_mode(self):
        self.assertEqual(cli.guess_mode("https://www.youtube.com/playlist?list=PL1"), "playlist")
        self.assertEqual(cli.guess_mode("https://www.youtube.com/@chan"), "years")
        self.assertEqual(cli.guess_mode("https://www.youtube.com/watch?v=abc"), "movie")
        self.assertEqual(cli.guess_mode("https://www.youtube.com/watch?v=abc&list=PL1&index=3"), "movie")
        self.assertEqual(cli.guess_mode("https://youtu.be/abc?list=PL1"), "movie")

    def test_list(self):
        subscriptions.upsert(Subscription("Wanted", "https://www.youtube.com/@x", "years"))
        placeholder(config.MOVIES_DIR / "A Movie" / "A Movie [aaaaaaaaaaa].mkv", size=4096)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_list(None)
        text = out.getvalue()
        self.assertIn("A Movie", text)
        self.assertIn("not downloaded", text)
