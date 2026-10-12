from mediasync.config import tomllib  # tomli on Python 3.10

from mediasync import config

from .helpers import TempLibraryTestCase


class SettingsTests(TempLibraryTestCase):
    def test_round_trip_through_toml(self):
        settings = config.Settings(shows_dir="~/Shows", subtitle_languages=["en", "de"], gentle=False,
                                   auto_update_hours=24, kodi_password='p"w\\d')
        text = config.to_toml(settings)
        self.assertEqual(config.from_dict(tomllib.loads(text)), settings)
        self.assertIn("[downloads]", text)
        self.assertIn("# check subscriptions every N hours", text)

    def test_first_run_creates_the_file_with_defaults(self):
        self.assertFalse(config.config_file().exists())
        settings = config.load()
        self.assertTrue(config.config_file().exists())
        self.assertEqual(settings, config.Settings())

    def test_invalid_values_are_rejected(self):
        for bad in (dict(auto_update_hours=2), dict(max_height=1234), dict(subtitle_languages=[]), dict(port=0)):
            with self.subTest(**bad), self.assertRaises(ValueError):
                config.Settings(**bad).validate()

    def test_form_values_are_converted(self):
        settings = config.from_dict({"max_height": "720", "gentle": "false", "subtitle_languages": "en, fr",
                                     "unknown": 1})
        self.assertEqual((settings.max_height, settings.gentle, settings.subtitle_languages), (720, False, ["en", "fr"]))

    def test_apply_expands_paths(self):
        config.apply(config.Settings(shows_dir="~/A", movies_dir="~/B"))
        self.assertFalse(str(config.SHOWS_DIR).startswith("~"))
        self.assertEqual(config.STAGING_DIR, config.CACHE_DIR / "downloads")
