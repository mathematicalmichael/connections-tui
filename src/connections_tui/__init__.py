#!/usr/bin/env python3
# Terminal Connections (NYT) — no external deps, Python 3.10+
# Keys: arrows/WASD move • Space select • Enter submit • f shuffle • c clear • q quit

from __future__ import annotations

import argparse
import curses
import json
import random
import sys
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Set, Tuple

NYT_URL_TEMPLATE = "https://www.nytimes.com/svc/connections/v2/{date}.json"
EASIEST_TO_HARDEST_COLORS = [
    curses.COLOR_GREEN,
    curses.COLOR_YELLOW,
    curses.COLOR_CYAN,
    curses.COLOR_MAGENTA,
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


@staticmethod
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


def draw_centered(stdscr, y: int, text: str, attr=0):
    h, w = stdscr.getmaxyx()
    x = max(0, (w - len(text)) // 2)
    stdscr.addstr(y, x, text[: max(0, w - x)], attr)


def chunk(seq: List[str], n: int) -> List[List[str]]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


def draw_board_tile(
    stdscr,
    word: str,
    row: int,
    col: int,
    board_cols: int,
    top: int,
    col_w: int,
    col_ws: list,
    attr: int,
    w: int,
):
    """Draw a single board tile at the specified position."""
    # Create tile exactly col_w wide with centered word
    opening = "[ "
    closing = " ]"
    opening_len = len(opening)
    closing_len = len(closing)
    available = col_w - opening_len - closing_len
    word_len = len(word)

    if word_len > available:
        # Word too long: minimal padding (truncate if necessary)
        word_part = word[:available]
        tile = opening + word_part + closing
    else:
        # Center the word within available space
        left_pad = (available - word_len) // 2
        right_pad = available - word_len - left_pad
        tile = opening + (" " * left_pad) + word + (" " * right_pad) + closing

    # Ensure tile is exactly col_w (pad/truncate if needed)
    if len(tile) < col_w:
        tile += " " * (col_w - len(tile))
    elif len(tile) > col_w:
        tile = tile[:col_w]

    x = sum(col_ws[:col]) if col_ws else col * col_w  # Use precalculated widths
    yline = top + row
    # Truncate only if it exceeds terminal width
    if x + col_w >= w:
        tile = tile[: w - x]
    stdscr.addstr(yline, x, tile, attr)


def redraw_board_area(stdscr, state: GameState, board_start_y: int, w: int):
    """Redraw only the board area, preserving other content."""
    board_cols = 4
    grid = chunk(state.remaining_words, board_cols)
    total_tiles = len(state.remaining_words)

    if total_tiles == 0:
        draw_centered(
            stdscr, board_start_y, "🎉 All groups solved! Press n=next, p=prev, q=quit."
        )
        return

    # Clear the board area first
    h, _ = stdscr.getmaxyx()
    for y in range(board_start_y, h - 3):  # Leave space for footer
        stdscr.move(y, 0)
        stdscr.clrtoeol()

    # Calculate consistent column widths based on max word length across all rows
    max_word_len_per_col = [0] * board_cols
    for row in grid:
        for c, word in enumerate(row):
            max_word_len_per_col[c] = max(max_word_len_per_col[c], len(word))

    col_ws = []
    for max_len in max_word_len_per_col:
        col_w = max(18, max_len + 6)  # +6 for spacing/padding
        col_ws.append(col_w)

    # Draw the board
    for r, row in enumerate(grid):
        for c, word in enumerate(row):
            idx = r * board_cols + c
            col_w = col_ws[c]
            attr = 0
            if idx == state.last_cursor:
                attr |= curses.A_REVERSE | curses.A_BOLD
            if idx in state.selection_idx:
                attr |= curses.color_pair(1)  # Selection highlight
            draw_board_tile(
                stdscr, word, r, c, board_cols, board_start_y, col_w, col_ws, attr, w
            )


def run_curses(state: GameState, use_ascii: bool = False):
    curses.wrapper(lambda stdscr: main_loop(stdscr, state, use_ascii))


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


def main_loop(stdscr, state: GameState, use_ascii: bool = False):
    curses.curs_set(0)
    stdscr.nodelay(False)
    curses.start_color()
    curses.use_default_colors()
    # Selection highlight
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)
    sel_attr = curses.color_pair(1)

    # Solved category color pairs, mapped by difficulty 0..3:
    # 0→green, 1→yellow, 2→cyan, 3→magenta
    # We'll use pair indices 2..5 for convenience.
    color_pairs_by_diff = {}
    for diff_rank, pair_idx in zip(range(4), range(2, 6)):
        fg = curses.COLOR_BLACK
        bg = EASIEST_TO_HARDEST_COLORS[diff_rank]
        curses.init_pair(pair_idx, fg, bg)
        color_pairs_by_diff[diff_rank] = curses.color_pair(pair_idx)

    if use_ascii:
        heart_full, heart_empty = "O", "x"
    else:
        heart_full, heart_empty = "❤", "♡"

    msg = "WASD=move, [Space]=select, [Enter]=submit. shu[f]fle, [c]lear, [q]uit"
    cursor = 0
    board_start_y = 0  # Will be calculated on first draw

    while True:
        h, w = stdscr.getmaxyx()

        # Only do full redraw when necessary
        if state.needs_full_redraw:
            stdscr.clear()

            # Header
            draw_centered(stdscr, 0, f"NYT Connections — {state.date_str}")
            strikes_left = state.max_strikes - state.strikes
            hearts = heart_full * strikes_left + heart_empty * (
                state.max_strikes - strikes_left
            )
            draw_centered(stdscr, 1, f"Strikes: {hearts}")

            # Solved groups
            y = 3
            if state.solved:
                stdscr.addstr(y, 2, "Solved groups:")
                y += 1
                for _, (title, words, diff_rank) in enumerate(state.solved):
                    # Ensure diff_rank in 0..3
                    if not isinstance(diff_rank, int) or diff_rank < 0 or diff_rank > 3:
                        diff_rank = 0
                    color = color_pairs_by_diff[diff_rank]
                    line = f" - {title}: {', '.join(words)}"
                    stdscr.addstr(y, 2, line[: max(0, w - 4)], color)
                    y += 1
            else:
                stdscr.addstr(y, 2, "Solved groups: (none yet)")
                y += 1

            board_start_y = y + 1
            state.needs_full_redraw = False

        # Update cursor position first, then redraw so highlight matches movement
        total_tiles = len(state.remaining_words)
        if total_tiles > 0:
            cursor = max(0, min(cursor, total_tiles - 1))
        state.last_cursor = cursor

        # Redraw board area (this is where cursor movement happens)
        redraw_board_area(stdscr, state, board_start_y, w)

        # Footer messages (only redraw if they might have changed)
        if state.one_away_msg:
            draw_centered(stdscr, h - 3, state.one_away_msg)
        draw_centered(stdscr, h - 2, msg)

        if state.strikes >= state.max_strikes:
            draw_centered(
                stdscr, h - 4, "💥 Out of mistakes! Press q to quit or c to reveal."
            )

        if all_groups_solved(state):
            draw_centered(stdscr, h - 4, "🎉 Perfect! Press n=next, p=prev, q=quit.")

        stdscr.refresh()

        # Input
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            break
        elif ch in (curses.KEY_LEFT, ord("a"), ord("A"), ord("h")):
            if total_tiles:
                cursor = (cursor - 1) % total_tiles
        elif ch in (curses.KEY_RIGHT, ord("d"), ord("D"), ord("l")):
            if total_tiles:
                cursor = (cursor + 1) % total_tiles
        elif ch in (curses.KEY_UP, ord("w"), ord("W"), ord("k")):
            if total_tiles:
                cursor = (cursor - 4) % total_tiles
        elif ch in (curses.KEY_DOWN, ord("s"), ord("S"), ord("j")):
            if total_tiles:
                cursor = (cursor + 4) % total_tiles
        elif ch == ord(" "):
            if total_tiles:
                if cursor in state.selection_idx:
                    state.selection_idx.remove(cursor)
                else:
                    if len(state.selection_idx) < 4:
                        state.selection_idx.add(cursor)
        elif ch == ord("c") or ch == ord("C"):
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
                state.needs_full_redraw = True
            else:
                state.selection_idx.clear()
                state.one_away_msg = None
        elif ch in (10, 13):  # Enter
            if total_tiles:
                ok, feedback = submit_selection(state)
                if not ok:
                    # little flash on error — also refresh the hearts immediately
                    strikes_left = state.max_strikes - state.strikes
                    hearts = heart_full * strikes_left + heart_empty * (
                        state.max_strikes - strikes_left
                    )
                    draw_centered(stdscr, 1, f"Strikes: {hearts}")
                    draw_centered(stdscr, h - 5, feedback)
                    stdscr.refresh()
                    time.sleep(0.6)
                else:
                    # reset cursor onto a valid tile
                    total_tiles = len(state.remaining_words)
                    if total_tiles:
                        cursor = min(cursor, total_tiles - 1)
                    state.needs_full_redraw = True
        elif ch in (ord("f"), ord("F")):  # shuffle board
            # Keep selected words selected by value after shuffle
            selected_words = {state.remaining_words[i] for i in state.selection_idx}
            random.shuffle(state.remaining_words)
            state.selection_idx = {
                i for i, w in enumerate(state.remaining_words) if w in selected_words
            }
        elif ch == ord("n"):
            # Load next day's puzzle, but only after completion
            if all_groups_solved(state):
                try:
                    load_day_into_state(state, +1)
                    cursor = 0
                except Exception as e:
                    state.one_away_msg = f"Couldn't load next day: {e}"
        elif ch == ord("p"):
            # Load previous day's puzzle, but only after completion
            if all_groups_solved(state):
                try:
                    load_day_into_state(state, -1)
                    cursor = 0
                except Exception as e:
                    state.one_away_msg = f"Couldn't load previous day: {e}"

        # Immediate redraw so movement/selection reflects the latest input
        h, w = stdscr.getmaxyx()
        if not state.needs_full_redraw:
            total_tiles = len(state.remaining_words)
            if total_tiles > 0:
                cursor = max(0, min(cursor, total_tiles - 1))
            state.last_cursor = cursor
            redraw_board_area(stdscr, state, board_start_y, w)
            if state.one_away_msg:
                draw_centered(stdscr, h - 3, state.one_away_msg)
            draw_centered(stdscr, h - 2, msg)
            if state.strikes >= state.max_strikes:
                draw_centered(
                    stdscr, h - 4, "💥 Out of mistakes! Press q to quit or c to reveal."
                )
            if all_groups_solved(state):
                draw_centered(
                    stdscr, h - 4, "🎉 Perfect! Press n=next, p=prev, q=quit."
                )
            stdscr.refresh()


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
    run_curses(state, use_ascii=args.ascii)


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
