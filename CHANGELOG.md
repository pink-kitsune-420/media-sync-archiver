# Changelog

All notable changes to Media Sync Archiver are listed here.
Version numbers follow [semantic versioning](https://semver.org): MAJOR.MINOR.PATCH.

## [1.0.0] - 2026-10-12

First public release.

- Playlists become TV shows (playlist order), channels become TV shows with one season per year
  (episodes numbered by upload date, so numbers never change), single videos become movies.
- Kodi/Jellyfin-ready library: `.nfo` files, episode thumbnails, 2:3 posters with a title card,
  fanart, landscape and season posters; subtitles, chapters and tags embedded in one remux pass.
- **Update** gets new videos and fills in anything missing; **Repair** also verifies every file and
  re-downloads broken ones; **Restore** brings back videos you deleted (deleted videos otherwise stay
  deleted).
- Gentle on YouTube: each video is downloaded once, channel updates check the RSS feed first,
  thumbnails come from YouTube's image server, failed subtitle downloads back off (1, 3, then 7 days),
  and yt-dlp's polite pauses are on by default.
- Web dashboard with a Settings page, queue, pause/stop, speed limit and optional automatic checks.
- Terminal commands for everything, and a `run.py` launcher that sets up a private environment.
