"""Configure DBot and start its Discord connection only when run as a script."""

import os
import random
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from mafia import Mafia
from mafia_restart import RestartJournal


class DBot(commands.Bot):
    async def setup_hook(self):
        # A cog groups related commands and their shared game state.
        journal = RestartJournal(Path(__file__).with_name(".local") / "restart.sqlite3")
        await self.add_cog(Mafia(restart_journal=journal, bot=self))


# Intents tell Discord which events our bot needs to receive.
intents = discord.Intents.default()
intents.message_content = True

bot = DBot(
    command_prefix="!",
    intents=intents,
    allowed_mentions=discord.AllowedMentions.none(),
)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}. Try !mafia, !hello, or !help in Discord.")


@bot.command()
async def hello(ctx):
    """Say hello to the person using the command."""
    await ctx.send(f"Hello, {ctx.author.display_name}! I'm DBot.")


@bot.command()
async def roll(ctx, sides: int = 6):
    """Roll a die. Example: !roll 20 (defaults to 6 sides)."""
    if not 2 <= sides <= 100:
        await ctx.send("Choose between 2 and 100 sides. Example: !roll 20")
        return

    result = random.randint(1, sides)
    await ctx.send(f"You rolled {result} on a {sides}-sided die!")


@roll.error
async def roll_error(ctx, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send("Use a whole number. Example: !roll 20")
    else:
        # Unexpected errors should stay visible in the terminal for debugging.
        raise error


def main():
    # Importing the module for tests must not load local credentials or log in.
    load_dotenv(Path(__file__).with_name(".env"))
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "paste_your_bot_token_here":
        raise SystemExit("Missing DISCORD_TOKEN. Copy .env.example to .env and add your bot token.")
    bot.run(token)


if __name__ == "__main__":
    main()
