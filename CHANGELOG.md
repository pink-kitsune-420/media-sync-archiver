# Changelog

All notable changes to Media Sync Archiver are listed here.
Version numbers follow [semantic versioning](https://semver.org): MAJOR.MINOR.PATCH.

## [Unreleased]

- Log files: everything is written to a `logs` folder next to the settings (one file per day, two
  weeks kept). The terminal now only shows the important lines: tasks, new videos, problems and
  one-line errors. A new **Detailed log** setting also records every YouTube request and why each
  video is skipped, fixed or downloaded.
- When the disk is full, downloading pauses with a clear message instead of failing every task that
  follows; press Resume after freeing up space.
- Playlist entries YouTube lists as private or deleted are skipped without requesting them, instead of
  failing (and being reported) on every update.
- Dashboard: an activity entry's Details stay open until you close them, and the library list is
  refreshed right after unsubscribing or queueing an action.
- Playlist episodes you delete keep their number, so the episodes after them are no longer
  renumbered (Kodi keeps their watched status), and Restore puts a video back in its old place.
  Videos that were never downloadable, such as private ones, still take no number.
- Task summaries count renamed files ("file names ×N"); the list of renames is in the log file.
- Fixed: opening the library list while files were being renamed could fail with an error.
- The log says "Started downloading …" instead of a misleading 0% progress line.

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
