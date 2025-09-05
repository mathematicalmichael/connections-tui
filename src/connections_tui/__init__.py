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

NYT_URL_TEMPLATE = "https://www.nytimes.com/svc/connections/v1/{date}.json"
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
    The NYT Connections JSON (per public docs & community code) has:
        data["groups"][<category>]["members"] -> list[str] of 4 words
    We'll parse robustly and ignore other metadata.
    """
    if "groups" not in obj:
        raise ValueError("Unexpected JSON: missing 'groups'")

    # Collect raw groups with possible difficulty levels (some puzzles omit them)
    groups: List[Group] = []
    for title, payload in obj["groups"].items():
        # payload could be dict with "members" (normal), or directly a list in some dumps
        if isinstance(payload, dict):
            members = (
                payload.get("members") or payload.get("words") or payload.get("tiles")
            )
            difficulty = (
                payload.get("level") if isinstance(payload.get("level"), int) else None
            )
        else:
            members = payload
            difficulty = None
        if not isinstance(members, list) or len(members) != 4:
            raise ValueError(f"Group '{title}' doesn't look like 4-word list.")
        groups.append(Group(title=title, words=set(members), difficulty=difficulty))

    # Normalize difficulties to 0..3 if present; otherwise assign by order.
    # Accept common shapes like {0,1,2,3} or {1,2,3,4}.
    diffs = [g.difficulty for g in groups]
    if all(isinstance(d, int) for d in diffs) and None not in diffs:
        mn, mx = min(diffs), max(diffs)
        # If levels are 1..4, shift to 0..3; if already 0..3 leave as is.
        if {mn, mx} == {1, 4} or (mn == 1 and mx == 4):
            for g in groups:
                g.difficulty = g.difficulty - 1
        # If not in 0..3 after this, clamp just in case
        for g in groups:
            g.difficulty = max(0, min(3, int(g.difficulty)))
    else:
        # Fallback: assign by encounter order (stable)
        for i, g in enumerate(groups):
            g.difficulty = i  # 0..3
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
                "Connections goes by NYT’s server date; try a different date with -d."
            ) from e
        raise
    except Exception as e:
        raise RuntimeError(f"Failed fetching puzzle JSON from {url}: {e}") from e


def make_initial_board(groups: List[Group]) -> List[str]:
    # If NYT provides a starting order we could use it, but groups cover all 16,
    # so we build and shuffle a board to mimic the tile grid.
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
    attr: int,
    w: int,
):
    """Draw a single board tile at the specified position."""
    tile = f"[ {word} ]"
    x = 2 + col * col_w
    yline = top + row
    # Truncate if needed
    if x + len(tile) >= w - 1:
        tile = tile[: max(0, w - x - 1)]
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

    # Draw the board
    for r, row in enumerate(grid):
        for c, word in enumerate(row):
            idx = r * board_cols + c
            # Layout spacing
            col_w = max(18, max(len(wd) + 4 for wd in row))
            attr = 0
            if idx == state.last_cursor:
                attr |= curses.A_REVERSE | curses.A_BOLD
            if idx in state.selection_idx:
                attr |= curses.color_pair(1)  # Selection highlight
            draw_board_tile(
                stdscr, word, r, c, board_cols, board_start_y, col_w, attr, w
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
                        state.solved.append((g.title, words_sorted))
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
        "--file", dest="file", help="Play from a local JSON file instead of fetching"
    )
    ap.add_argument("--seed", type=int, help="Random seed for reproducible shuffles")
    ap.add_argument(
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
