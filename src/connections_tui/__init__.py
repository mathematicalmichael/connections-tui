#!/usr/bin/env python3
# Terminal Connections (NYT) — using rich for better terminal compatibility
# Keys: arrows/WASD move • Space select • Enter submit • f shuffle • c clear • q quit

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Set, Tuple

import readchar
from rich.console import Console
from rich.live import Live
from rich.text import Text

try:
    import termios  # type: ignore

    HAS_TERMIOS = True
except Exception:
    HAS_TERMIOS = False

NYT_URL_TEMPLATE = "https://www.nytimes.com/svc/connections/v2/{date}.json"
EASIEST_TO_HARDEST_COLORS = [
    "green",
    "yellow",
    "cyan",
    "magenta",
]


@dataclass
class Group:
    title: str
    words: Set[str]
    # difficulty may exist in NYT JSON, but we don't rely on it:
    difficulty: int | None = None
    # positions for v2 API format: list of (word, position) tuples
    positions: List[Tuple[str, int]] = field(default_factory=list)


@dataclass
class GameState:
    date_str: str
    groups: List[Group]
    remaining_words: List[str]  # words not yet solved, in board order
    solved: List[Tuple[str, List[str], int]] = field(
        default_factory=list
    )  # (title, words, difficulty)
    selection_idx: Set[int] = field(default_factory=set)  # indices in remaining_words
    strikes: int = 0
    max_strikes: int = 4
    one_away_msg: str | None = None
    # For optimized redrawing
    last_cursor: int = -1
    needs_full_redraw: bool = True


def load_puzzle_from_json(obj: dict) -> List[Group]:
    """
    Parse NYT Connections JSON from v2 API format:
        data["categories"][i]["title"] -> category name
        data["categories"][i]["cards"] -> list of {"content": word, "position": pos}
    """
    if "categories" not in obj:
        raise ValueError("Unexpected JSON: missing 'categories' (v2 format only)")

    groups: List[Group] = []
    categories = obj["categories"]
    for i, category in enumerate(categories):
        title = category["title"]
        cards = category.get("cards", [])

        if not isinstance(cards, list) or len(cards) != 4:
            raise ValueError(f"Category '{title}' doesn't have exactly 4 cards.")

        members = [card["content"] for card in cards]
        # Store position info for board layout
        positions = [(card["content"], card["position"]) for card in cards]

        # Assume difficulty is category order (0-3)
        difficulty = i
        group = Group(title=title, words=set(members), difficulty=difficulty)
        # Store positions for later use in board creation
        group.positions = positions
        groups.append(group)

    return groups


def fetch_nyt_puzzle(date_str: str) -> List[Group]:
    url = NYT_URL_TEMPLATE.format(date=date_str)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        return load_puzzle_from_json(data)
    except urllib.error.HTTPError as e:
        # Helpful hint if timezone/date mismatch
        if e.code == 404:
            raise RuntimeError(
                f"No puzzle JSON found for {date_str} (HTTP 404). "
                "Connections goes by NYT's server date; try a different date with -d."
            ) from e
        raise
    except Exception as e:
        raise RuntimeError(f"Failed fetching puzzle JSON from {url}: {e}") from e


def make_initial_board(groups: List[Group]) -> List[str]:
    # For v2 format, use position data to create initial board layout
    # For v1 format, fall back to shuffled board

    # Check if we have position data (v2 format)
    has_positions = any(hasattr(g, "positions") and g.positions for g in groups)

    if has_positions:
        # Create a list of 16 words positioned according to v2 data
        board = [None] * 16
        for g in groups:
            if hasattr(g, "positions") and g.positions:
                for word, position in g.positions:
                    if 0 <= position < 16:
                        board[position] = word

        # Fill any None slots with remaining words (shouldn't happen with valid data)
        words = [w for w in board if w is not None]
        if len(words) != 16:
            # Fallback: collect all words and shuffle
            words = []
            for g in groups:
                words.extend(sorted(g.words, key=str.lower))
            random.shuffle(words)

        return words
    else:
        # Legacy v1 behavior: collect and shuffle
        words: List[str] = []
        for g in groups:
            words.extend(sorted(g.words, key=str.lower))
        random.shuffle(words)
        return words


def submit_selection(state: GameState) -> Tuple[bool, str]:
    """Return (did_match_group, message). Implements 'one away' logic and strikes."""
    if len(state.selection_idx) != 4:
        return False, "Select exactly 4 words."

    chosen = {state.remaining_words[i] for i in state.selection_idx}
    # Check exact match
    for g in state.groups:
        if g.words.issubset(set(state.remaining_words)) and chosen == g.words:
            # Mark solved: remove from remaining, append to solved list
            solved_words = sorted(chosen, key=str.lower)
            state.solved.append((g.title, solved_words, int(g.difficulty or 0)))
            # Remove those tiles from the board:
            state.remaining_words = [
                w for w in state.remaining_words if w not in chosen
            ]
            state.selection_idx.clear()
            state.one_away_msg = None
            return True, f"Solved: {g.title}"

    # Not an exact group — check one-away (3/4 in any unsolved group)
    for g in state.groups:
        if g.words.issubset(set(state.remaining_words)):
            inter = g.words.intersection(chosen)
            if len(inter) == 3:
                state.strikes += 1
                state.one_away_msg = (
                    f"One away... (strike {state.strikes}/{state.max_strikes})"
                )
                return False, state.one_away_msg

    # Otherwise, it's a strike
    state.strikes += 1
    state.one_away_msg = None
    return False, f"Incorrect set (strikes: {state.strikes}/{state.max_strikes})"


def all_groups_solved(state: GameState) -> bool:
    return len(state.remaining_words) == 0


def chunk(seq: List[str], n: int) -> List[List[str]]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


def render_board_tile(
    word: str,
    is_cursor: bool,
    is_selected: bool,
    col_width: int,
) -> Text:
    """Render a single board tile."""
    opening = "[ "
    closing = " ]"
    available = col_width - len(opening) - len(closing)

    # Truncate word if too long
    if len(word) > available:
        word_part = word[:available]
    else:
        word_part = word

    # Center the word
    left_pad = (available - len(word_part)) // 2
    right_pad = available - len(word_part) - left_pad
    tile_text = opening + (" " * left_pad) + word_part + (" " * right_pad) + closing

    # Pad to exact width
    if len(tile_text) < col_width:
        tile_text += " " * (col_width - len(tile_text))
    elif len(tile_text) > col_width:
        tile_text = tile_text[:col_width]

    text = Text(tile_text)

    # Selection takes priority over cursor
    if is_selected:
        # Selected: white background with black text
        text.stylize("reverse", 0, len(tile_text))
        text.stylize("bold", 0, len(tile_text))
    elif is_cursor:
        # Cursor: reverse video (highlighted but not selected)
        text.stylize("reverse", 0, len(tile_text))

    return text


def render_display(state: GameState, cursor: int, use_ascii: bool = False) -> Text:
    """Render the entire game display as a Rich Text object."""
    output_parts = []

    # Header
    header = Text(f"NYT Connections — {state.date_str}", style="bold")
    output_parts.append(header)
    output_parts.append("")

    # Strikes
    strikes_left = state.max_strikes - state.strikes
    if use_ascii:
        heart_full, heart_empty = "O", "x"
    else:
        heart_full, heart_empty = "❤", "♡"
    hearts = heart_full * strikes_left + heart_empty * (
        state.max_strikes - strikes_left
    )
    strikes_text = Text(f"Strikes: {hearts}")
    output_parts.append(strikes_text)
    output_parts.append("")

    # Solved groups
    if state.solved:
        output_parts.append(Text("Solved groups:", style="bold"))
        for title, words, diff_rank in state.solved:
            # Ensure diff_rank in 0..3
            if not isinstance(diff_rank, int) or diff_rank < 0 or diff_rank > 3:
                diff_rank = 0
            color = EASIEST_TO_HARDEST_COLORS[diff_rank]
            line = Text(f" - {title}: {', '.join(words)}")
            line.stylize(f"on {color}", 0, len(line))
            line.stylize("black", 0, len(line))
            output_parts.append(line)
    else:
        output_parts.append(Text("Solved groups: (none yet)"))
    output_parts.append("")

    # Board
    board_cols = 4
    total_tiles = len(state.remaining_words)

    if total_tiles == 0:
        output_parts.append(
            Text(
                "🎉 All groups solved! Press n=next, p=prev, q=quit.",
                style="bold green",
            )
        )
    else:
        grid = chunk(state.remaining_words, board_cols)

        # Calculate column widths
        max_word_len_per_col = [0] * board_cols
        for row in grid:
            for c, word in enumerate(row):
                max_word_len_per_col[c] = max(max_word_len_per_col[c], len(word))

        col_widths = []
        for max_len in max_word_len_per_col:
            col_w = max(18, max_len + 6)  # +6 for spacing/padding
            col_widths.append(col_w)

        # Render board rows
        for r, row in enumerate(grid):
            row_text = Text()
            for c, word in enumerate(row):
                idx = r * board_cols + c
                col_w = col_widths[c]
                is_cursor = idx == cursor
                is_selected = idx in state.selection_idx
                tile = render_board_tile(word, is_cursor, is_selected, col_w)
                row_text.append(tile)
                if c < len(row) - 1:
                    row_text.append(" ")
            output_parts.append(row_text)

    output_parts.append("")

    # Footer messages
    if state.strikes >= state.max_strikes:
        output_parts.append(
            Text(
                "💥 Out of mistakes! Press q to quit or c to reveal.", style="bold red"
            )
        )

    if all_groups_solved(state):
        output_parts.append(
            Text("🎉 Perfect! Press n=next, p=prev, q=quit.", style="bold green")
        )

    if state.one_away_msg:
        output_parts.append(Text(state.one_away_msg, style="yellow"))

    msg = "WASD=move, [Space]=select, [Enter]=submit. shu[f]fle, [c]lear, [q]uit"
    output_parts.append(Text(msg, style="dim"))

    # Combine all parts
    result = Text()
    for i, part in enumerate(output_parts):
        if isinstance(part, Text):
            result.append(part)
        else:
            result.append(str(part))
        if i < len(output_parts) - 1:
            result.append("\n")

    return result


def load_day_into_state(state: GameState, day_offset: int):
    """
    Replace the current puzzle in-place with another day's puzzle,
    resetting strikes/solved/selection and rebuilding the board.
    """
    d0 = datetime.strptime(state.date_str, "%Y-%m-%d").date()
    d = d0 + timedelta(days=day_offset)
    date_str = d.strftime("%Y-%m-%d")
    groups = fetch_nyt_puzzle(date_str)
    state.date_str = date_str
    state.groups = groups
    state.remaining_words = make_initial_board(groups)
    state.solved.clear()
    state.selection_idx.clear()
    state.strikes = 0
    state.one_away_msg = None
    # Reset redraw tracking
    state.last_cursor = -1
    state.needs_full_redraw = True


def _save_terminal_state():
    """Best-effort snapshot of terminal settings (POSIX)."""
    if not HAS_TERMIOS:
        return None
    if not sys.stdin.isatty():
        return None
    try:
        return termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        return None


def _restore_terminal_state(saved) -> None:
    """Best-effort restore of terminal settings + common ANSI resets."""
    try:
        # Reset styles, show cursor, exit alt-screen (in case Live didn't unwind cleanly)
        sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
    except Exception:
        pass

    if not HAS_TERMIOS or saved is None:
        return
    if not sys.stdin.isatty():
        return
    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
    except Exception:
        pass


def _read_key() -> str:
    """Read a single keypress and normalize special keys."""
    try:
        key = readchar.readkey()
    except KeyboardInterrupt:
        return "CTRL_C"
    except Exception:
        return ""

    if key == readchar.key.UP:
        return "UP"
    if key == readchar.key.DOWN:
        return "DOWN"
    if key == readchar.key.LEFT:
        return "LEFT"
    if key == readchar.key.RIGHT:
        return "RIGHT"
    if key == readchar.key.ENTER:
        return "\n"
    if key == readchar.key.SPACE:
        return " "
    return key


def main_loop(state: GameState, use_ascii: bool = False):
    """Main game loop using rich Live display."""
    console = Console()
    cursor = 0

    def render():
        return render_display(state, cursor, use_ascii)

    saved_term = _save_terminal_state()
    try:
        with Live(
            render(), console=console, refresh_per_second=20, screen=True
        ) as live:
            while True:
                # Always paint first so the UI is responsive even if input blocks
                live.update(render())

                key = _read_key()
                if key == "" or key == "CTRL_C":
                    break

                if key.lower() == "q":
                    break

                total_tiles = len(state.remaining_words)

                # Handle arrow keys and movement
                if key == "LEFT" or key.lower() in ("a", "h"):
                    if total_tiles:
                        cursor = (cursor - 1) % total_tiles
                elif key == "RIGHT" or key.lower() in ("d", "l"):
                    if total_tiles:
                        cursor = (cursor + 1) % total_tiles
                elif key == "UP" or key.lower() in ("w", "k"):
                    if total_tiles:
                        cursor = (cursor - 4) % total_tiles
                elif key == "DOWN" or key.lower() in ("s", "j"):
                    if total_tiles:
                        cursor = (cursor + 4) % total_tiles
                elif key == " ":
                    if total_tiles:
                        if cursor in state.selection_idx:
                            state.selection_idx.remove(cursor)
                        else:
                            if len(state.selection_idx) < 4:
                                state.selection_idx.add(cursor)
                elif key.lower() == "c":
                    if state.strikes >= state.max_strikes and state.remaining_words:
                        # Reveal all (post-fail convenience)
                        for g in state.groups:
                            if g.words & set(state.remaining_words):
                                words_sorted = sorted(list(g.words), key=str.lower)
                                state.solved.append(
                                    (g.title, words_sorted, int(g.difficulty or 0))
                                )
                                state.remaining_words = [
                                    w for w in state.remaining_words if w not in g.words
                                ]
                        state.selection_idx.clear()
                    else:
                        state.selection_idx.clear()
                        state.one_away_msg = None
                elif key in ("\r", "\n"):  # Enter
                    if total_tiles:
                        ok, feedback = submit_selection(state)
                        if not ok:
                            # Show error message briefly
                            state.one_away_msg = feedback
                            live.update(render())
                            time.sleep(0.6)
                            if "One away" not in feedback:
                                state.one_away_msg = None
                        else:
                            # Reset cursor onto a valid tile
                            total_tiles = len(state.remaining_words)
                            if total_tiles:
                                cursor = min(cursor, total_tiles - 1)
                elif key.lower() == "f":  # shuffle board
                    # Keep selected words selected by value after shuffle
                    selected_words = {
                        state.remaining_words[i] for i in state.selection_idx
                    }
                    random.shuffle(state.remaining_words)
                    state.selection_idx = {
                        i
                        for i, w in enumerate(state.remaining_words)
                        if w in selected_words
                    }
                elif key == "n":
                    # Load next day's puzzle, but only after completion
                    if all_groups_solved(state):
                        try:
                            load_day_into_state(state, +1)
                            cursor = 0
                        except Exception as e:
                            state.one_away_msg = f"Couldn't load next day: {e}"
                elif key == "p":
                    # Load previous day's puzzle, but only after completion
                    if all_groups_solved(state):
                        try:
                            load_day_into_state(state, -1)
                            cursor = 0
                        except Exception as e:
                            state.one_away_msg = f"Couldn't load previous day: {e}"

                # Update cursor bounds
                total_tiles = len(state.remaining_words)
                if total_tiles > 0:
                    cursor = max(0, min(cursor, total_tiles - 1))
                state.last_cursor = cursor
    finally:
        try:
            console.show_cursor(True)
        except Exception:
            pass
        _restore_terminal_state(saved_term)
        # Ensure prompt starts on a clean line
        try:
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            pass


def parse_args():
    ap = argparse.ArgumentParser(
        description="Play NYT Connections in your terminal (fetches the official daily puzzle JSON)."
    )
    ap.add_argument(
        "-d", "--date", dest="date", help="Puzzle date YYYY-MM-DD (default: today)"
    )
    ap.add_argument(
        "-f",
        "--file",
        dest="file",
        help="Play from a local JSON file instead of fetching",
    )
    ap.add_argument(
        "-s", "--seed", type=int, help="Random seed for reproducible shuffles"
    )
    ap.add_argument(
        "-a",
        "--ascii",
        action="store_true",
        help="Use ASCII-only characters for strikes display (hearts)",
    )
    return ap.parse_args()


def main():
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    if args.date:
        dt = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        # Use local system date; you can pass -d if NYT's day ticks over earlier/later than you
        dt = date.today()
    date_str = dt.strftime("%Y-%m-%d")

    if args.file:
        data = json.loads(Path(args.file).read_text("utf-8"))
        groups = load_puzzle_from_json(data)
    else:
        groups = fetch_nyt_puzzle(date_str)

    board = make_initial_board(groups)
    state = GameState(date_str=date_str, groups=groups, remaining_words=board)
    main_loop(state, use_ascii=args.ascii)


def run():
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        sys.stderr.write(f"error: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    run()
