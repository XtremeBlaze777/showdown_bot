"""
Gen 7 Draft League AI Bot
────────────────────────
Requires:
  pip install poke-env anthropic

Local Showdown server:
  git clone https://github.com/smogon/pokemon-showdown
  cd pokemon-showdown && npm install
  node pokemon-showdown start --no-security
"""

"""Primarily vibe coded by Claude Claude Claude"""

import asyncio
import os
import re

from openai import OpenAI  # pip install openai  (reused for Ollama's compatible API)
from poke_env.environment import (
    Battle, Field, Move, MoveCategory,
    Pokemon, PokemonType, SideCondition, Weather,
)
from poke_env.player import Player
from poke_env.ps_client.server_configuration import LocalhostServerConfiguration


# ══════════════════════════════════════════════════════════
# TYPE CHART  (Gen 7)
# ══════════════════════════════════════════════════════════

_CHART: dict[str, dict[str, float]] = {
    "normal":   {"rock": .5, "ghost": 0,  "steel": .5},
    "fire":     {"fire": .5, "water": .5, "rock": .5, "dragon": .5,
                 "grass": 2, "ice": 2, "bug": 2, "steel": 2},
    "water":    {"water": .5, "grass": .5, "dragon": .5,
                 "fire": 2, "ground": 2, "rock": 2},
    "electric": {"electric": .5, "grass": .5, "dragon": .5, "ground": 0,
                 "water": 2, "flying": 2},
    "grass":    {"fire": .5, "grass": .5, "poison": .5, "flying": .5,
                 "bug": .5, "steel": .5, "dragon": .5,
                 "water": 2, "ground": 2, "rock": 2},
    "ice":      {"water": .5, "ice": .5, "fire": .5, "steel": .5,
                 "grass": 2, "ground": 2, "flying": 2, "dragon": 2},
    "fighting": {"poison": .5, "flying": .5, "psychic": .5, "bug": .5, "fairy": .5,
                 "ghost": 0,
                 "normal": 2, "ice": 2, "rock": 2, "dark": 2, "steel": 2},
    "poison":   {"poison": .5, "ground": .5, "rock": .5, "ghost": .5, "steel": 0,
                 "grass": 2, "fairy": 2},
    "ground":   {"grass": .5, "bug": .5, "flying": 0,
                 "fire": 2, "electric": 2, "poison": 2, "rock": 2, "steel": 2},
    "flying":   {"electric": .5, "rock": .5, "steel": .5,
                 "grass": 2, "fighting": 2, "bug": 2},
    "psychic":  {"psychic": .5, "steel": .5, "dark": 0,
                 "fighting": 2, "poison": 2},
    "bug":      {"fire": .5, "fighting": .5, "flying": .5, "ghost": .5,
                 "steel": .5, "fairy": .5,
                 "grass": 2, "psychic": 2, "dark": 2},
    "rock":     {"fighting": .5, "ground": .5, "steel": .5,
                 "fire": 2, "ice": 2, "flying": 2, "bug": 2},
    "ghost":    {"normal": 0, "dark": .5,
                 "psychic": 2, "ghost": 2},
    "dragon":   {"steel": .5, "fairy": 0,
                 "dragon": 2},
    "dark":     {"fighting": .5, "dark": .5, "fairy": .5,
                 "psychic": 2, "ghost": 2},
    "steel":    {"fire": .5, "water": .5, "electric": .5, "steel": .5,
                 "ice": 2, "rock": 2, "fairy": 2},
    "fairy":    {"fire": .5, "poison": .5, "steel": .5,
                 "fighting": 2, "dragon": 2, "dark": 2},
}

def type_effectiveness(move_type: str, def_types: list[str]) -> float:
    mult = 1.0
    chart = _CHART.get(move_type.lower(), {})
    for t in def_types:
        mult *= chart.get(t.lower(), 1.0)
    return mult

def eff_label(mult: float) -> str:
    return {0: "immune(0×)", 0.25: "0.25×", 0.5: "0.5×",
            1.0: "1×", 2.0: "super effective(2×)", 4.0: "4×"}.get(mult, f"{mult}×")


# ══════════════════════════════════════════════════════════
# RELATIVE DAMAGE SCORER
# ══════════════════════════════════════════════════════════

_WEATHER_MOD: dict[Weather, dict] = {
    Weather.SUNNYDAY:       {PokemonType.FIRE: 1.5, PokemonType.WATER: 0.5},
    Weather.RAINDANCE:      {PokemonType.WATER: 1.5, PokemonType.FIRE: 0.5},
    Weather.DESOLATELAND:   {PokemonType.FIRE: 1.5, PokemonType.WATER: 0.0},
    Weather.PRIMORDIALSEA:  {PokemonType.WATER: 1.5, PokemonType.FIRE: 0.0},
}

def score_move(move: Move, attacker: Pokemon, defender: Pokemon,
               weather: Weather | None) -> float:
    if move.category == MoveCategory.STATUS or not move.base_power:
        return 0.0

    atk_types  = [t for t in (attacker.type_1, attacker.type_2) if t]
    def_types  = [t.name for t in (defender.type_1, defender.type_2) if t]

    stab  = 1.5 if move.type in atk_types else 1.0
    eff   = type_effectiveness(move.type.name if move.type else "normal", def_types)
    w_mod = (_WEATHER_MOD.get(weather, {}).get(move.type, 1.0)
             if weather else 1.0)

    return move.base_power * stab * eff * w_mod


# ══════════════════════════════════════════════════════════
# BATTLE STATE BUILDER
# ══════════════════════════════════════════════════════════

def _mon_types(mon: Pokemon) -> list[PokemonType]:
    return [t for t in (mon.type_1, mon.type_2) if t]

def _type_str(mon: Pokemon) -> str:
    return "/".join(t.name for t in _mon_types(mon))

def _hp(mon: Pokemon) -> str:
    return f"{mon.current_hp_fraction * 100:.0f}%"

def _boosts(mon: Pokemon) -> str:
    b = {k: v for k, v in mon.boosts.items() if v}
    return ("  Boosts: " + ", ".join(f"{k}:{'+' if v>0 else ''}{v}" for k,v in b.items())
            if b else "")

def build_state(battle: Battle) -> str:
    me  = battle.active_pokemon
    opp = battle.opponent_active_pokemon
    opp_type_names = [t.name for t in _mon_types(opp)]

    lines: list[str] = ["━━━ BATTLE STATE ━━━", ""]

    # ── Active Pokémon ──────────────────────────────────────
    lines += [
        f"YOUR ACTIVE:  {me.species}  [{_type_str(me)}]  HP: {_hp(me)}",
        f"  Item: {me.item or '?'}  |  Ability: {me.ability or '?'}",
        f"  Status: {me.status.name}" if me.status else "",
        _boosts(me),
        "",
        f"OPPONENT:     {opp.species}  [{_type_str(opp)}]  HP: {_hp(opp)}",
        f"  Item: {opp.item or '?'}  |  Ability: {opp.ability or '?'}",
        f"  Status: {opp.status.name}" if opp.status else "",
        _boosts(opp),
    ]
    if opp.moves:
        lines.append("  Known moves: " + ", ".join(opp.moves))

    # ── Field ───────────────────────────────────────────────
    field_parts = []
    if battle.weather and battle.weather.name != "NONE":
        field_parts.append(f"Weather={battle.weather.name}")
    for f in battle.fields:
        field_parts.append(f"Terrain={f.name}")
    for sc in battle.side_conditions:
        field_parts.append(f"YourSide={sc.name}")
    for sc in battle.opponent_side_conditions:
        field_parts.append(f"OppSide={sc.name}")

    lines += ["", "FIELD: " + (", ".join(field_parts) or "clear"), ""]

    # ── Moves ───────────────────────────────────────────────
    lines.append("YOUR MOVES:")
    for i, mv in enumerate(battle.available_moves, 1):
        eff   = type_effectiveness(mv.type.name if mv.type else "normal", opp_type_names)
        stab  = mv.type in _mon_types(me) if mv.type else False
        score = score_move(mv, me, opp, battle.weather)
        tags  = (["STAB"] if stab else []) + ([eff_label(eff)] if eff != 1.0 else [])
        tag_s = f" [{', '.join(tags)}]" if tags else ""
        pp_s  = f" {mv.current_pp}/{mv.max_pp}PP" if hasattr(mv, "current_pp") else ""
        cat   = mv.category.name
        lines.append(
            f"  {i}. {mv.id:20s}  Type:{mv.type.name if mv.type else '?':9s}"
            f"  Cat:{cat:8s}  BP:{mv.base_power:3d}{pp_s}  score={score:.0f}{tag_s}"
        )

    # ── Z-Moves (Gen 7 specific) ─────────────────────────────
    if battle.available_z_moves:
        lines.append("")
        lines.append("Z-MOVES  [one-time use — very powerful]:")
        for i, zm in enumerate(battle.available_z_moves, 1):
            if zm is None:
                continue
            eff   = type_effectiveness(zm.type.name if zm.type else "normal", opp_type_names)
            stab  = zm.type in _mon_types(me) if zm.type else False
            tags  = (["STAB"] if stab else []) + ([eff_label(eff)] if eff != 1.0 else [])
            tag_s = f" [{', '.join(tags)}]" if tags else ""
            lines.append(
                f"  z{i}. {zm.id:20s}  Type:{zm.type.name if zm.type else '?':9s}"
                f"  BP:{zm.base_power:3d}{tag_s}"
            )

    # ── Mega ─────────────────────────────────────────────────
    if battle.can_mega_evolve:
        lines += ["", f"⚡ MEGA EVOLUTION available for {me.species} (append 'mega' to your move)"]

    # ── Switches ─────────────────────────────────────────────
    lines += ["", "AVAILABLE SWITCHES:"]
    if battle.available_switches:
        for sw in battle.available_switches:
            sw_type_names = [t.name for t in _mon_types(sw)]
            # How hard does the opponent's STAB hit this switch-in?
            incoming = []
            for ot in _mon_types(opp):
                v = type_effectiveness(ot.name, sw_type_names)
                if v != 1.0:
                    incoming.append(f"takes {eff_label(v)} from opp {ot.name}")
            inc_s  = (" | " + ", ".join(incoming)) if incoming else ""
            st_s   = f" [{sw.status.name}]" if sw.status else ""
            lines.append(
                f"  → {sw.species:16s} [{_type_str(sw):14s}] HP:{_hp(sw)}{st_s}{inc_s}"
            )
    else:
        lines.append("  (none available)")

    # ── Teams ─────────────────────────────────────────────────
    lines += ["", "YOUR TEAM:"]
    for mon in battle.team.values():
        marker = "★" if mon == me else ("✗" if mon.fainted else " ")
        st_s   = f" [{mon.status.name}]" if mon.status else ""
        lines.append(f"  {marker} {mon.species:18s} HP:{_hp(mon)}{st_s}")

    lines += ["", "OPPONENT TEAM (known):"]
    for mon in battle.opponent_team.values():
        marker = "★" if mon == opp else ("✗" if mon.fainted else " ")
        st_s   = f" [{mon.status.name}]" if mon.status else ""
        lines.append(f"  {marker} {mon.species:18s} HP:{_hp(mon)}{st_s}")

    lines.append("\n━━━ END STATE ━━━")
    return "\n".join(l for l in lines if l is not None)


# ══════════════════════════════════════════════════════════
# SYSTEM PROMPT
# ══════════════════════════════════════════════════════════

SYSTEM = """You are an expert Gen 7 competitive Pokemon player in a draft league.

KEY GEN 7 RULES:
- Z-Moves: one-time per battle, enormous base power. Use them to secure KOs or break walls.
- Mega Evolution: permanent boost once used. Usually activate turn 1 unless you need to switch.
- No Dynamax, no Tera types — this is Gen 7.
- Tapu terrain abilities (Psychic Surge, Electric Surge, etc.) affect certain Z-moves and attacks.
- Stealth Rock does 25%/50%/100% damage on switch depending on rock weakness.
- Priority moves (Extremespeed, Bullet Punch, Ice Shard) bypass speed — factor this in.
- Burned Pokémon deal 50% physical damage. Paralysis halves speed and may skip turns.

DECISION FRAMEWORK:
1. Can I KO the opponent this turn? Do it.
2. Can they KO me next turn? Switch or use priority.
3. Is a Z-Move needed to break through? Use it.
4. Is Mega activating a good idea right now?
5. Can I set up hazards / boost safely?
6. Is switching to a better matchup worth the hazard chip?

RESPONSE FORMAT (exactly — no other text):
REASONING: <2-3 sentences>
ACTION: <action>

Valid actions:
  move <move_name>
  zmove <move_name>
  mega move <move_name>
  mega zmove <move_name>
  switch <pokemon_name>
"""


# ══════════════════════════════════════════════════════════
# CLAUDE PLAYER
# ══════════════════════════════════════════════════════════

# ── Ollama config ────────────────────────────────────────────
OLLAMA_MODEL = "qwen2.5:14b"   # swap to "llama3.1:8b" if too slow

_ollama = OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama",            # Ollama ignores this but the field is required
)

class ClaudePlayer(Player):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._history: list[dict] = []
        self._battle_tag: str = ""

    # ── Turn decision ────────────────────────────────────────
    async def choose_move(self, battle: Battle) -> str:
        if battle.battle_tag != self._battle_tag:
            self._history = []
            self._battle_tag = battle.battle_tag

        state = build_state(battle)
        user_msg = f"{state}\n\nWhat is the best action this turn?"
        self._history.append({"role": "user", "content": user_msg})

        try:
            resp = _ollama.chat.completions.create(
                model=OLLAMA_MODEL,
                max_tokens=300,
                messages=[{"role": "system", "content": SYSTEM}, *self._history],
            )
            raw = resp.choices[0].message.content.strip()
            self._history.append({"role": "assistant", "content": raw})
            print(f"\n[Turn {battle.turn}]\n{raw}\n")
            return self._parse(raw, battle)

        except Exception as exc:
            print(f"[Ollama error] {exc} — random fallback")
            return self.choose_random_move(battle)

    # ── Team preview ─────────────────────────────────────────
    async def teampreview(self, battle: Battle) -> str:
        my_team  = list(battle.team.values())
        opp_team = list(battle.opponent_team.values())

        prompt = (
            "GEN 7 DRAFT — TEAM PREVIEW\n\n"
            "YOUR TEAM:\n"
            + "\n".join(
                f"  {i+1}. {m.species} [{_type_str(m)}]"
                for i, m in enumerate(my_team)
            )
            + "\n\nOPPONENT TEAM:\n"
            + "\n".join(
                f"  {i+1}. {m.species} [{_type_str(m)}]"
                for i, m in enumerate(opp_team)
            )
            + "\n\nPick the best lead and team order.\n"
              "REASONING: <brief>\nORDER: <digits e.g. 3 1 2 5 4 6>"
        )

        try:
            resp = _ollama.chat.completions.create(
                model=OLLAMA_MODEL,
                max_tokens=200,
                messages=[
                    {"role": "system", "content": "You are an expert Gen 7 draft league player."},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = resp.choices[0].message.content.strip()
            print(f"[Team Preview]\n{raw}")

            m = re.search(r"ORDER:\s*([\d\s]+)", raw, re.I)
            if m:
                nums = m.group(1).strip().split()
                if len(nums) == len(my_team) and all(n.isdigit() for n in nums):
                    return "/team " + "".join(nums)
        except Exception as exc:
            print(f"[Team preview error] {exc}")

        return "/team " + "".join(str(i + 1) for i in range(len(my_team)))

    # ── Response parser ───────────────────────────────────────
    def _parse(self, raw: str, battle: Battle) -> str:
        m = re.search(r"ACTION:\s*(.+)", raw, re.I)
        if not m:
            print("[Parse] No ACTION found — random")
            return self.choose_random_move(battle)

        action = m.group(1).strip().lower()
        is_mega  = "mega"  in action
        is_zmove = "zmove" in action

        # ── Switch ──
        if action.startswith("switch "):
            name = action[7:].strip()
            # exact match first
            for sw in battle.available_switches:
                if name == sw.species.lower():
                    return self.create_order(sw)
            # substring match
            for sw in battle.available_switches:
                if name in sw.species.lower() or sw.species.lower().startswith(name[:5]):
                    return self.create_order(sw)
            print(f"[Parse] Switch '{name}' not found — random")
            return self.choose_random_move(battle)

        # extract move name (strip keywords)
        mv_name = re.sub(r"\b(mega|zmove|move)\b", "", action).strip()
        mv_clean = mv_name.replace(" ", "").replace("-", "")

        # ── Z-Move ──
        if is_zmove and battle.available_z_moves:
            for zm in battle.available_z_moves:
                if zm is None:
                    continue
                zm_clean = zm.id.lower().replace("-", "")
                if mv_clean in zm_clean or zm_clean.startswith(mv_clean[:6]):
                    return self.create_order(zm, mega=is_mega and battle.can_mega_evolve)
            # fallback: first available z-move
            for zm in battle.available_z_moves:
                if zm:
                    print(f"[Parse] Z-move mismatch, using first: {zm.id}")
                    return self.create_order(zm, mega=is_mega and battle.can_mega_evolve)

        # ── Regular move (+ optional mega) ──
        for mv in battle.available_moves:
            mv_id = mv.id.lower().replace("-", "")
            if mv_clean in mv_id or mv_id.startswith(mv_clean[:6]):
                mega = is_mega and battle.can_mega_evolve
                return self.create_order(mv, mega=mega)

        print(f"[Parse] Move '{mv_name}' not found — random")
        return self.choose_random_move(battle)


# ══════════════════════════════════════════════════════════
# TEAM FORMAT CONVERTER
# Converts Showdown's verbose export → poke-env packed format
# ══════════════════════════════════════════════════════════

def to_ps_id(name: str) -> str:
    """'Brave Bird' / 'U-turn' / 'Will-O-Wisp' → 'bravebird' / 'uturn' / 'willowisp'"""
    return re.sub(r"[^a-z0-9]", "", name.lower())

_EV_IDX = {"hp": 0, "atk": 1, "def": 2, "spa": 3, "spd": 4, "spe": 5}

def showdown_to_packed(showdown_text: str) -> str:
    """
    Convert a Showdown verbose team export to poke-env packed format.

    Paste the full team text as-is — blank lines between mons are the separator.

    Example input:
        Staraptor @ Choice Scarf
        Ability: Reckless
        EVs: 252 Atk / 4 Def / 252 Spe
        Jolly Nature
        - Brave Bird
        - Double-Edge
        - Close Combat
        - U-turn

    Returns a newline-joined packed string ready to drop into MY_TEAM.
    """
    packed_mons: list[str] = []
    blocks = re.split(r"\n\s*\n", showdown_text.strip())

    for block in blocks:
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if not lines:
            continue

        # ── Header: "Name @ Item" or just "Name" ──
        if " @ " in lines[0]:
            name, item = lines[0].split(" @ ", 1)
        else:
            name, item = lines[0], ""
        name = name.strip()
        item = item.strip()

        ability   = ""
        nature    = ""
        evs       = [0] * 6        # hp atk def spa spd spe
        ivs       = [31] * 6
        moves:    list[str] = []
        gender    = ""
        shiny     = ""
        level     = ""
        happiness = ""

        for line in lines[1:]:
            if line.startswith("Ability:"):
                ability = line[8:].strip()

            elif line.startswith("EVs:"):
                for part in line[4:].split("/"):
                    m = re.match(r"(\d+)\s+(\w+)", part.strip())
                    if m:
                        stat = m.group(2).lower()
                        if stat in _EV_IDX:
                            evs[_EV_IDX[stat]] = int(m.group(1))

            elif line.startswith("IVs:"):
                for part in line[4:].split("/"):
                    m = re.match(r"(\d+)\s+(\w+)", part.strip())
                    if m:
                        stat = m.group(2).lower()
                        if stat in _EV_IDX:
                            ivs[_EV_IDX[stat]] = int(m.group(1))

            elif line.endswith(" Nature"):
                nature = line[:-7].strip()

            elif line.startswith("- "):
                moves.append(to_ps_id(line[2:]))

            elif line.startswith("Shiny: Yes"):
                shiny = "S"

            elif line.startswith("Level:"):
                level = line[6:].strip()

            elif line.startswith("Happiness:"):
                happiness = line[10:].strip()

            elif line.startswith("Gender:"):
                g = line[7:].strip()
                gender = "M" if g == "M" else ("F" if g == "F" else "")

        # ── Pack EVs: empty string for 0, keep all 6 positions ──
        ev_packed = ",".join(str(v) if v else "" for v in evs)

        # ── Pack IVs: only include if non-standard ──
        iv_packed = (
            ""
            if all(v == 31 for v in ivs)
            else ",".join(str(v) if v != 31 else "" for v in ivs)
        )

        # ── Assemble: name|species|item|ability|moves|nature|evs|gender|ivs|shiny|level|happiness ──
        packed_mons.append("|".join([
            name, "",               # name | species (blank = same as name)
            item, ability,
            ",".join(moves),
            nature,
            ev_packed,
            gender, iv_packed, shiny, level, happiness,
        ]))

    result = "\n".join(packed_mons)
    print("[Team Converter] Output:\n" + result)
    return result


# ══════════════════════════════════════════════════════════
# YOUR TEAM  (paste Showdown packed export here)
# ══════════════════════════════════════════════════════════
# Notes for Gen 7 draft:
#   • Z-Crystals go in the item slot (e.g. Groundium Z, Tapunium Z, Ghostium Z)
#   • Mega Stones are also items (e.g. Garchompite, Lopunnite)
#   • Export your team from Showdown teambuilder → copy packed format here

MY_TEAM = """
Landorus-Therian||Groundium Z|Intimidate|earthquake,uturn,stoneedge,knockoff|Jolly|,252,,4,,252
Tapu Koko||Choice Specs|Electric Surge|thunderbolt,voltswitch,dazzlinggleam,hpice|Timid|,,,252,4,252
Toxapex||Black Sludge|Regenerator|scald,toxic,recover,haze|Bold|252,,252,,,4
Garchomp||Garchompite|Rough Skin|earthquake,stoneedge,dragondance,firefang|Jolly|,252,,4,,252
Celesteela||Leftovers|Beast Boost|heavyslam,leechseed,flamethrower,protect|Sassy|252,4,,,252,
Tapu Lele||Choice Scarf|Psychic Surge|psyshock,moonblast,focusblast,thunderbolt|Timid|,,,252,4,252
"""


# ══════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════

async def main():
    # ══════════════════════════════════════════════════════
    # ACCOUNT SETUP
    # ══════════════════════════════════════════════════════
    # LOCAL SERVER (recommended for testing):
    #   Runs at localhost:8000, no registration needed.
    #   poke-env auto-creates the username you pass in.
    #   Start server first: node pokemon-showdown start --no-security
    #
    # MAIN PS SERVER (showdown.pokemon.com):
    #   You need a real registered account.
    #   Register at pokemonshowdown.com, then pass credentials below.
    #   Remove server_configuration= to use the main server.
    # ══════════════════════════════════════════════════════

    bot = ClaudePlayer(
        battle_format="gen7ou",
        server_configuration=LocalhostServerConfiguration,  # remove for main PS server
        team=MY_TEAM,
        max_concurrent_battles=1,
        # username="MyBotName",        # any name on local; your PS username on main server
        # password="your-ps-password", # only needed on main PS server
    )

    print("═" * 50)
    print(f"  Bot account: {bot.username}")
    print("  Connect to localhost:8000 in the Showdown client")
    print("  and challenge this account to start a battle")
    print("═" * 50)

    # ── Accept one challenge from anyone ──
    await bot.accept_challenges(None, n_challenges=1)

    # ── OR: challenge a specific account (e.g. your alt) ──
    # await bot.send_challenges("your-alt-username", n_challenges=1)

    # ── OR: ladder on main PS server ──
    # await bot.ladder(n_games=10)


if __name__ == "__main__":
    # Make sure Ollama is running: `ollama serve`
    # and the model is pulled:    `ollama pull qwen2.5:14b`
    asyncio.run(main())