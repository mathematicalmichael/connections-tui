# NYT Connections in the Terminal

[![PyPI version](https://badge.fury.io/py/connections-tui.svg)](https://badge.fury.io/py/connections-tui)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A lightweight, dependency-free TUI for playing the NYT Connections game in your terminal. It fetches the official daily puzzle and lets you play with a simple, clean interface using Python's built-in `curses` library.

## Preview

Here's a glimpse of what it looks like in action:
```
$ connections-tui -d 2024-08-23
              NYT Connections — 2024-08-23
                     Strikes: ❤❤❤❤❤

Solved groups: (none yet)

  [ BUFF ]          [ CREAM ]         [ FINISH ]        [ FAWN ]
  [ TAN ]           [ MONITOR ]       [ GLOVES ]        [ BOA ]
  [ CHECK ]         [ TERMINAL ]      [ TIE ]           [ FAN ]
  [ SCREEN ]        [ CORSET ]        [ DISPLAY ]       [ BASK ]

Use arrows/WASD to navigate, [Space]=Select, [Enter]=Submit, f=shuffle, c=clear, q=quit
```

## Features

- Play the official NYT Connections puzzle for any day.
- No external dependencies, just Python 3.8+.
- Smooth keyboard navigation (Arrow keys or WASD).
- Shuffle, select, and submit words.
- Strike tracking with hearts (`❤`) or ASCII (`O`/`x`) for compatibility.
- "One away" hints when you're close.
- After solving, you can immediately load the next or previous day's puzzle.
- Load puzzles from local JSON files for offline play or custom games.
- Set a random seed for reproducible shuffles.

## Installation and Usage

Arguably the simplest way to run the project is with `uv`:

```bash
uvx connections-tui@latest
```

You can of course install directly from PyPI:
```bash
pip install -U connections-tui
```

and run the console command in your terminal:
```bash
connections-tui
```

### Command-line Options

You can customize the game with these arguments:

| Argument | Shorthand | Description |
|---|---|---|
| `--date <YYYY-MM-DD>` | `-d` | Play the puzzle for a specific date. (Default: today) |
| `--file <path/to/file.json>` | `-f` | Play from a local JSON puzzle file. |
| `--seed <integer>` | `-s` | Set the random seed for reproducible board shuffles. |
| `--ascii` | `-a` | Use ASCII characters (`O`/`x`) for strikes instead of hearts. |

Example:
```bash
# Play yesterday's puzzle with ASCII hearts
connections-tui -d $(date -v-1d +%Y-%m-%d) --ascii
```

## Keybindings

| Key | Action |
|---|---|
| `Arrow Keys`, `WASD`, `HJKL` | Move cursor |
| `Space` | Select/deselect a word (up to 4) |
| `Enter` | Submit your selection of 4 words |
| `f` | Shuffle the remaining words on the board |
| `c` | Clear your current selection |
| `q` | Quit the game |
| `n` | (After solving) Load the next day's puzzle |
| `p` | (After solving) Load the previous day's puzzle |

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

