"""Small Discord cards for phase announcements; no game rules live here."""

import discord

from mafia_game import EliminationOutcome, GameState, Role

NIGHT_COLOR = 0x7563D9
MORNING_COLOR = 0xEDB84A
VOTING_COLOR = 0x5399E3
MAFIA_COLOR = 0xDA5E70
CITIZEN_COLOR = 0x3EB98B


def duration_text(seconds: float) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        minutes = seconds / 60
        return f"{minutes:g} minute{'s' if minutes != 1 else ''}"
    return f"{seconds:g} second{'s' if seconds != 1 else ''}"


def night_card(number: int, seconds: float) -> discord.Embed:
    card = discord.Embed(
        title=f"🌙 Night {number}", description="The town falls silent.", color=NIGHT_COLOR
    )
    if number == 1:
        card.add_field(name="Your role", value="Check your DMs. Keep it secret!", inline=False)
    card.add_field(name="Night action", value="Mafia: choose a target in your DM.", inline=False)
    card.set_footer(text=f"Up to {duration_text(seconds)} • Morning starts automatically")
    return card


def game_over_card(game: GameState, outcome: EliminationOutcome, result: str) -> discord.Embed:
    mafia_wins = outcome.winner == Role.MAFIA
    team = "Mafia wins" if mafia_wins else "Citizens win"
    reason = (
        "The Mafia now equals or outnumbers the Citizens."
        if mafia_wins
        else "All Mafia players have been eliminated."
    )
    card = discord.Embed(
        title=f"🏆 {team}!",
        description=f"{result}\n\n{reason}",
        color=MAFIA_COLOR if mafia_wins else CITIZEN_COLOR,
    )
    mafia_count = sum(game.roles[player] == Role.MAFIA for player in outcome.survivors)
    citizens = len(outcome.survivors) - mafia_count
    card.add_field(
        name="Remaining",
        value=f"{mafia_count} Mafia • {citizens} Citizen{'s' if citizens != 1 else ''}",
        inline=False,
    )
    card.add_field(name="Play again", value="Host: `!mafia cancel` → `!mafia create`", inline=False)
    card.set_footer(text=f"Game over • Round {game.night_number}")
    return card


def morning_card(
    game: GameState, members: dict, outcome: EliminationOutcome, discussion_seconds: float
) -> discord.Embed:
    result = (
        f"{members[outcome.victim_id].mention} was eliminated overnight."
        if outcome.victim_id is not None
        else "Nobody was eliminated overnight."
    )
    if outcome.winner:
        return game_over_card(game, outcome, result)
    card = discord.Embed(
        title=f"☀️ Morning {game.night_number}", description=result, color=MORNING_COLOR
    )
    card.add_field(name="Alive", value=f"{len(outcome.survivors)} players", inline=True)
    card.add_field(
        name="Discussion begins", value=f"Up to {duration_text(discussion_seconds)}", inline=True
    )
    majority = len(outcome.survivors) // 2 + 1
    card.add_field(
        name="Ready to vote?",
        value=f"`!mafia ready` • {majority} ready players start voting.",
        inline=False,
    )
    return card


def voting_card(game: GameState, members: dict, seconds: float) -> discord.Embed:
    card = discord.Embed(
        title=f"🗳️ Voting • Day {game.night_number}",
        description="Choose a suspect or skip this vote.",
        color=VOTING_COLOR,
    )
    roster = "\n".join(
        f"{number}. {members[player].mention}"
        for number, player in enumerate(game.players, start=1)
        if player in game.alive
    )
    card.add_field(name="Living players", value=roster, inline=False)
    card.add_field(name="Vote", value="`!mafia vote <number>` or `!mafia vote skip`", inline=False)
    card.add_field(
        name="To eliminate", value=f"{game.majority} votes needed • Votes are final", inline=False
    )
    card.set_footer(text=f"Up to {duration_text(seconds)} • Ends early when everyone votes")
    return card


def vote_result_card(game: GameState, members: dict, outcome: EliminationOutcome) -> discord.Embed:
    result = (
        f"{members[outcome.victim_id].mention} was eliminated by the vote."
        if outcome.victim_id is not None
        else "No player received a majority. Nobody was eliminated."
    )
    if outcome.winner:
        return game_over_card(game, outcome, result)
    card = discord.Embed(title="⚖️ Voting result", description=result, color=VOTING_COLOR)
    card.add_field(name="Next", value=f"Night {game.night_number + 1} begins now.", inline=False)
    card.set_footer(text=f"Day {game.night_number} • {len(outcome.survivors)} players alive")
    return card


def card_text(card: discord.Embed) -> str:
    """Keep the same information readable when embeds are unavailable."""
    sections = [f"**{card.title}**", card.description or ""]
    sections.extend(f"**{field.name}**\n{field.value}" for field in card.fields)
    if card.footer.text:
        sections.append(card.footer.text)
    return "\n\n".join(section for section in sections if section)


async def send_card(destination, card: discord.Embed):
    # Contexts wrap a channel; workers send to the channel directly.
    channel = getattr(destination, "channel", destination)
    guild = getattr(channel, "guild", None)
    member = getattr(guild, "me", None)
    if guild is not None and (member is None or not channel.permissions_for(member).embed_links):
        return await destination.send(card_text(card))
    try:
        return await destination.send(embed=card)
    except discord.Forbidden:
        # Permissions may have changed since the cached pre-check. A plain send
        # still succeeds when only Embed Links was removed; other denials propagate.
        return await destination.send(card_text(card))
