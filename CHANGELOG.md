# Changelog

All notable changes to Media Sync Archiver are listed here.
Version numbers follow [semantic versioning](https://semver.org): MAJOR.MINOR.PATCH.

## [Unreleased]

### Fixed
- The log file no longer contains terminal color codes from yt-dlp.

## [1.0.1] - 2026-10-12

Bug fixes and quality-of-life improvements. Settings, subscriptions and libraries from 1.0.0 keep
working as they are.

### Added
- **Log files:** everything is written to a `logs` folder next to the settings, one file per day
  (two weeks are kept). `python3 run.py config` and the Settings page show where.
- **Detailed log** setting (Settings › Troubleshooting): also records every YouTube request and why
  each video is skipped, fixed or downloaded.

### Changed
- The terminal only shows the important lines: tasks, new videos, problems, and errors as one line.
  File renames, yt-dlp's own warnings and full error details go to the log file.
- Playlist episodes you delete keep their number, so the episodes after them are no longer
  renumbered (Kodi keeps their watched status), and Restore puts a video back in its old place.
  Videos that were never downloadable, such as private ones, still take no number. If you deleted
  playlist episodes with 1.0.0, the episodes after them get their original numbers back.
- Playlist entries that YouTube lists as private or deleted are skipped without requesting them,
  instead of failing (and being reported as a problem) on every update.
- Task summaries count renamed files ("file names ×N").

### Fixed
- A full disk no longer makes every following task fail: downloading pauses, the dashboard says why,
  and Resume continues where it stopped.
- An activity entry's **Details** no longer collapse a second after opening them.
- Unsubscribed items now disappear from the library list right away instead of after a page reload.
- Opening the library list while files were being renamed could fail with an error.
- The log no longer shows a misleading "0.0%" line for every download.

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
