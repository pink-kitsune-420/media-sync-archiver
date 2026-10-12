# Changelog

All notable changes to Media Sync Archiver are listed here.
Version numbers follow [semantic versioning](https://semver.org): MAJOR.MINOR.PATCH.

## [Unreleased]

### Added
- **Several libraries** (Settings › Libraries), for example one on this computer and one on an
  external drive, each with its own Shows and Movies folder. You choose the library when adding
  something (`add --library NAME` on the command line); the first one is the default.
- **External drives that aren't always connected:** while a library's folders can't be found, its
  items are shown as *drive not connected* and skipped by Update all, Repair all and automatic
  checks. Nothing is downloaded for them, and no folders are created where the drive was.
- A show's folder moved to another library by hand is found there and updated in its new place.
- A show that was downloaded before but whose folder is gone is marked *folder missing* instead of
  being downloaded again from scratch; **Download again** (`update --download-again NAME`) does
  that on purpose.
- **Move to …** (in the "…" menu, or `move NAME --to LIBRARY`): moves a show or movie to another
  library. Onto another drive it is copied first and the original removed only once the copy is
  complete; there must be enough free space, and an interruption leaves everything as it was.
- **Subscribe** (in the "…" menu) for shows in the library that you aren't subscribed to, so
  unsubscribing can be undone without pasting the URL again. If the YouTube address isn't known,
  it asks for it.
- The dashboard notices a drive being plugged in or unplugged within a second.
- Unsubscribing and other changes to subscriptions are written to the log.

### Changed
- Settings from 1.0 become the first library, called "This computer". Subscriptions from 1.0 count
  as downloaded, so a show whose folder is away is never downloaded again by accident.

### Fixed
- Shows from older versions without a `playlist.info.json` no longer show "no URL": the address
  is taken from their videos' saved information (no YouTube requests).
- Files left over from a download that didn't finish, for a show that isn't in a connected library,
  no longer show up as a show with Update/Repair buttons that do nothing. They're listed as
  *leftover download files* with a **Delete leftovers** button.
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
