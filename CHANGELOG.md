# Changelog

Versions are date-based: `YYYY.MM.DD.N` (date + number of the change that day).

## 2026.10.23.1
- Panel: new **+15 min** button (smallest grant step); the correction button is now **-15 min** (was -30).
- README: documented `/subtract` and the 1-1440 minute range.

## 2026.10.19.1
- New **-30 min** button and `POST /profiles/{name}/subtract` to correct over-granted time; blocks immediately when time reaches 0.
- `/grant` and `/subtract` validate `minutes` in 1-1440 (a negative `/grant` used to subtract time).

## 2026.10.11.1
- Initial public release.
