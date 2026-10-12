from mediasync.config import tomllib  # tomli on Python 3.10

from mediasync import config

from .helpers import TempLibraryTestCase


class SettingsTests(TempLibraryTestCase):
    def test_round_trip_through_toml(self):
        settings = config.Settings(libraries=[config.Library("This computer", "~/Shows", "~/Movies"),
                                              config.Library("Drive", "/media/me/Drive/Shows", "/media/me/Drive/Movies")],
                                   subtitle_languages=["en", "de"], gentle=False,
                                   auto_update_hours=24, kodi_password='p"w\\d')
        text = config.to_toml(settings)
        self.assertEqual(config.from_dict(tomllib.loads(text)), settings)
        self.assertIn("[downloads]", text)
        self.assertEqual(text.count("[[libraries]]"), 2)
        self.assertIn("# check subscriptions every N hours", text)

    def test_first_run_creates_the_file_with_defaults(self):
        self.assertFalse(config.config_file().exists())
        settings = config.load()
        self.assertTrue(config.config_file().exists())
        self.assertEqual(settings, config.Settings())

    def test_invalid_values_are_rejected(self):
        twins = [config.Library("A", "~/1", "~/2"), config.Library("a", "~/3", "~/4")]
        for bad in (dict(auto_update_hours=2), dict(max_height=1234), dict(subtitle_languages=[]), dict(port=0),
                    dict(libraries=[]), dict(libraries=twins), dict(libraries=[config.Library("A", "", "~/2")])):
            with self.subTest(**bad), self.assertRaises(ValueError):
                config.Settings(**bad).validate()

    def test_form_values_are_converted(self):
        settings = config.from_dict({"max_height": "720", "gentle": "false", "subtitle_languages": "en, fr",
                                     "unknown": 1})
        self.assertEqual((settings.max_height, settings.gentle, settings.subtitle_languages), (720, False, ["en", "fr"]))

    def test_settings_from_version_1_0_become_the_first_library(self):
        old = tomllib.loads('[library]\nshows_dir = "~/YT_Shows"\nmovies_dir = "~/YT_Movies"\ndownloads_dir = ""\n')
        self.assertEqual(config.from_dict(old).libraries,
                         [config.Library(config.DEFAULT_LIBRARY_NAME, "~/YT_Shows", "~/YT_Movies")])

    def test_apply_expands_paths(self):
        config.apply(config.Settings(libraries=[config.Library("Main", "~/A", "~/B")]))
        self.assertFalse(str(config.SHOWS_DIR).startswith("~"))
        self.assertEqual(config.STAGING_DIR, config.CACHE_DIR / "downloads")
