import asyncio
import random
import re
import string
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import discord
from redbot.core import checks, commands


ENTRY_SECONDS = 60
VOTE_SECONDS = 30
MAX_ENTRIES = 25


@dataclass
class AcroGame:
    channel_id: int
    guild_id: int
    letters: str
    entries: Dict[int, str] = field(default_factory=dict)
    phase: str = "submitting"
    task: Optional[asyncio.Task] = None
    vote_message: Optional[discord.Message] = None


class VoteButton(discord.ui.Button):
    def __init__(
        self,
        number: int,
        author_id: int,
        view_ref: "VoteView",
    ):
        super().__init__(
            label=str(number),
            style=discord.ButtonStyle.secondary,
            row=(number - 1) // 5,
        )
        self.number = number
        self.author_id = author_id
        self.vote_view = view_ref

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.bot:
            return

        if interaction.user.id == self.author_id:
            await interaction.response.send_message(
                "You can't vote for your own entry.",
                ephemeral=True,
            )
            return

        self.vote_view.votes[interaction.user.id] = self.number - 1

        await interaction.response.send_message(
            f"Vote recorded for entry #{self.number}. "
            "Press another button if you want to change it.",
            ephemeral=True,
        )


class VoteView(discord.ui.View):
    def __init__(self, ordered_entries: List[Tuple[int, str]]):
        super().__init__(timeout=VOTE_SECONDS)
        self.ordered_entries = ordered_entries
        self.votes: Dict[int, int] = {}

        for index, (author_id, _sentence) in enumerate(ordered_entries, start=1):
            self.add_item(VoteButton(index, author_id, self))

    async def disable_all(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True


class Acro(commands.Cog):
    """Anonymous acronym sentence game."""

    __version__ = "1.0.0"

    def __init__(self, bot):
        self.bot = bot
        self.games: Dict[int, AcroGame] = {}

    def cog_unload(self):
        for game in self.games.values():
            if game.task and not game.task.done():
                game.task.cancel()

    @commands.command(name="acro")
    @commands.guild_only()
    @commands.bot_has_permissions(
        manage_messages=True,
        send_messages=True,
        embed_links=True,
    )
    async def acro(self, ctx: commands.Context):
        """
        Start an acronym round.

        A random sequence of 3-6 letters is posted. Players submit a sentence
        whose words begin with those letters in order. Valid submissions are
        immediately deleted and revealed anonymously when voting begins.
        """
        channel_id = ctx.channel.id

        if channel_id in self.games:
            await ctx.send("There is already an acronym round running here.")
            return

        count = random.randint(3, 6)
        letters = "".join(random.choices(string.ascii_uppercase, k=count))

        game = AcroGame(
            channel_id=channel_id,
            guild_id=ctx.guild.id,
            letters=letters,
        )
        self.games[channel_id] = game
        game.task = asyncio.create_task(self._run_game(ctx, game))

    @commands.command(name="acrostop")
    @commands.guild_only()
    @checks.mod_or_permissions(manage_messages=True)
    async def acrostop(self, ctx: commands.Context):
        """Stop the active acronym round in this channel."""
        game = self.games.get(ctx.channel.id)

        if not game:
            await ctx.send("There isn't an acronym round running here.")
            return

        if game.task and not game.task.done():
            game.task.cancel()

        self.games.pop(ctx.channel.id, None)
        await ctx.send("Acronym round stopped.")

    @commands.Cog.listener()
    async def on_message_without_command(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        game = self.games.get(message.channel.id)
        if not game or game.phase != "submitting":
            return

        if len(game.entries) >= MAX_ENTRIES and message.author.id not in game.entries:
            return

        words = self._extract_words(message.content)

        if len(words) != len(game.letters):
            return

        initials = "".join(word[0].upper() for word in words)

        if initials != game.letters:
            return

        # Store the latest valid submission from this user.
        game.entries[message.author.id] = message.content.strip()

        # Delete the valid entry as quickly as Discord permits.
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass
        except discord.HTTPException:
            pass

    async def _run_game(self, ctx: commands.Context, game: AcroGame):
        try:
            letters_display = "  ".join(f"**{letter}**" for letter in game.letters)

            start_embed = discord.Embed(
                title="ACRONYM ROUND",
                description=letters_display,
                color=await ctx.embed_color(),
            )
            start_embed.add_field(
                name="How to play",
                value=(
                    f"Create a {len(game.letters)}-word sentence where each word "
                    "starts with the letters above, in order.\n\n"
                    "Valid entries will disappear immediately and stay hidden "
                    f"until voting begins.\n\nYou have **{ENTRY_SECONDS} seconds**."
                ),
                inline=False,
            )

            await ctx.send(embed=start_embed)

            await asyncio.sleep(ENTRY_SECONDS)

            game.phase = "voting"

            if len(game.entries) < 2:
                if not game.entries:
                    await ctx.send("Round over. No valid entries were submitted.")
                else:
                    only_author_id, only_sentence = next(iter(game.entries.items()))
                    member = ctx.guild.get_member(only_author_id)
                    name = member.mention if member else f"<@{only_author_id}>"
                    await ctx.send(
                        f"Only one valid entry was submitted, so there is no vote.\n"
                        f"**{name}:** {only_sentence}"
                    )
                return

            ordered_entries = list(game.entries.items())
            random.shuffle(ordered_entries)

            lines = []
            for index, (_author_id, sentence) in enumerate(ordered_entries, start=1):
                lines.append(f"**{index}.** {sentence}")

            vote_embed = discord.Embed(
                title="VOTING TIME",
                description="\n\n".join(lines),
                color=await ctx.embed_color(),
            )
            vote_embed.set_footer(
                text=(
                    f"Vote anonymously with the numbered buttons. "
                    f"You have {VOTE_SECONDS} seconds. Self-votes are blocked."
                )
            )

            view = VoteView(ordered_entries)
            game.vote_message = await ctx.send(embed=vote_embed, view=view)

            await view.wait()

            await view.disable_all()
            try:
                await game.vote_message.edit(view=view)
            except discord.HTTPException:
                pass

            await self._announce_result(ctx, ordered_entries, view.votes)

        except asyncio.CancelledError:
            raise
        finally:
            self.games.pop(game.channel_id, None)

    async def _announce_result(
        self,
        ctx: commands.Context,
        ordered_entries: List[Tuple[int, str]],
        votes: Dict[int, int],
    ):
        if not votes:
            await ctx.send("Voting closed. No votes were cast.")
            return

        totals = [0] * len(ordered_entries)
        for entry_index in votes.values():
            if 0 <= entry_index < len(totals):
                totals[entry_index] += 1

        highest = max(totals)
        winning_indexes = [
            index for index, total in enumerate(totals) if total == highest
        ]

        if len(winning_indexes) == 1:
            index = winning_indexes[0]
            author_id, sentence = ordered_entries[index]
            member = ctx.guild.get_member(author_id)
            winner = member.mention if member else f"<@{author_id}>"

            embed = discord.Embed(
                title="ACRONYM WINNER",
                description=f"**{sentence}**",
                color=await ctx.embed_color(),
            )
            embed.add_field(name="Winner", value=winner, inline=True)
            embed.add_field(name="Votes", value=str(highest), inline=True)
            await ctx.send(embed=embed)
            return

        tie_lines = []
        for index in winning_indexes:
            author_id, sentence = ordered_entries[index]
            member = ctx.guild.get_member(author_id)
            winner = member.mention if member else f"<@{author_id}>"
            tie_lines.append(f"**{winner}** — {sentence}")

        embed = discord.Embed(
            title="ACRONYM TIE",
            description="\n".join(tie_lines),
            color=await ctx.embed_color(),
        )
        embed.set_footer(text=f"Each tied entry received {highest} vote(s).")
        await ctx.send(embed=embed)

    @staticmethod
    def _extract_words(text: str) -> List[str]:
        """
        Extract sentence words while allowing apostrophes and hyphens.

        Examples:
        "Tiny Bears Scream!" -> ["Tiny", "Bears", "Scream"]
        "Don't Break Stuff"  -> ["Don't", "Break", "Stuff"]
        """
        return re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", text)
