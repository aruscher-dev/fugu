"""Per-game glue for Phase 9's multi-game sep-CMA-ES extension (PLAN.md
phase table: "gtbench extension (connect_four/breakthrough + kuhn_poker)").
Phase 7's kuhn_poker-only pilot (scripts/phase7_cmaes_kuhn_pilot.py)
hardcoded that one game's construction directly; this factors out the
per-game bits scripts/phase9_gtbench_extension.py needs to loop the same
CMA-ES mechanism over multiple `vendor/gtbench` games -- and fixes a real
upstream landmine found while doing so, see "why never .reset()" below.

Why every game here is played by constructing a **fresh instance per match**
rather than ever calling `.reset()` on a shared one: confirmed against the
real `jinhaoduan/GTBench` source (cloned into this session's sandbox, which
has outbound network access) that `gamingbench.games.openspiel_adapter.
OpenSpielGame.reset()` --

    def reset(self):
        self.game = pyspiel.load_game(self.game_name)
        self.env = self.game.new_initial_state()

-- is only correct when `self.game_name` is still both (a) the exact string
`pyspiel.load_game()` needs, and (b) missing any extra construction kwargs
the subclass's own `__init__` passed. Both assumptions break, differently,
for two of this phase's three games (confirmed by actually calling
`.reset()` on real instances of each, not guessed):

  - `ConnectFour.__init__` calls `super().__init__("connect_four")` (the
    real pyspiel game id -- loads fine), then immediately overwrites
    `self.game_name = 'connect4'` (a *display* name, used only for
    `env_name`/prompt-template lookup -- see `prompts/system_prompts/
    __init__.py`'s `mapping` dict, which is keyed `'connect4'`).
    `reset()` then calls `pyspiel.load_game('connect4')` -- not a real
    pyspiel game id -- which raises `OpenSpiel exception: Unknown game
    'connect4'` on every single call. Not a rare edge case: this makes
    `game.reset()` unconditionally crash for ConnectFour, full stop.
  - `Breakthrough.__init__` calls `super().__init__("breakthrough")`
    (loads pyspiel's default 8x8/768-action board), then immediately
    re-does `self.game = pyspiel.load_game("breakthrough", {'columns':
    3})` (the smaller 3-column/288-action board this project actually
    wants -- cheaper per-rollout generation cost, in keeping with this
    project's "truncated"/budget-conscious CMA-ES phases). `reset()`
    reloads via `self.game_name` alone, silently dropping the
    `{'columns': 3}` kwarg -- confirmed in the sandbox:
    `game.game.num_distinct_actions()` is 288 right after construction,
    768 after just one `.reset()` call. Unlike ConnectFour's crash, this
    is silent: a CMA-ES fitness function that calls `game.reset()` before
    every match would have every match after the very first one in a
    whole run quietly play on the wrong (much bigger, much more
    expensive) board.

`kuhn_poker` itself has no such bug (`self.game_name` is never reassigned
after `super().__init__`), but rather than carry a game-specific exception
list a future 4th game could silently fall outside of, every game here uses
the same fresh-instance-per-match discipline uniformly. Confirmed in the
sandbox this produces identical, correct behavior for kuhn_poker too (a
`game.reset()` call there was never actually broken -- this is purely
defensive uniformity, not a workaround needed for that one game).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class GameSpec:
    key: str                       # this project's own game key (CLI flag value, report/checkpoint filename stem)
    make: Callable[[], object]     # fresh gamingbench Game instance -- never call .reset(), see module docstring
    worker_max_tokens: int         # generous enough for this game's move-token format; games all want short, tag-only output


def _make_kuhn_poker():
    from gamingbench.games.kuhn_poker import KuhnPoker
    return KuhnPoker()


def _make_connect_four():
    from gamingbench.games.connect_four import ConnectFour
    return ConnectFour()


def _make_breakthrough():
    from gamingbench.games.breakthrough import Breakthrough
    return Breakthrough()


# Order matters only for the default --games CLI value below (kuhn_poker
# first, matching Phase 7's precedent and PLAN.md's own phase-table wording
# "connect_four/breakthrough + kuhn_poker" listing it as included).
GAME_SPECS = {
    "kuhn_poker": GameSpec("kuhn_poker", _make_kuhn_poker, worker_max_tokens=64),
    "connect_four": GameSpec("connect_four", _make_connect_four, worker_max_tokens=64),
    "breakthrough": GameSpec("breakthrough", _make_breakthrough, worker_max_tokens=96),
}
