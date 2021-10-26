import asyncio
import json
import logging
import random
from typing import Literal, Optional

import discord
from async_rediscache import RedisCache
from discord.ext import commands

from bot.bot import Bot
from bot.constants import Colours, ERROR_REPLIES, Icons, Roles
from bot.utils.converters import WrappedMessageConverter
from bot.utils.decorators import whitelist_override

log = logging.getLogger(__name__)

# Number of seconds to wait for other users to bookmark the same message
TIMEOUT = 120
BOOKMARK_EMOJI = "📌"
MESSAGE_NOT_FOUND_ERROR = (
    "You must either provide a valid message to bookmark, or reply to one."
    "\n\nThe lookup strategy for a message is as follows (in order):"
    "\n1. Lookup by '{channel ID}-{message ID}' (retrieved by shift-clicking on 'Copy ID')"
    "\n2. Lookup by message ID (the message **must** have been sent after the bot last started)"
    "\n3. Lookup by message URL"
)


class AlreadyBookmarkedError(commands.UserInputError):
    """Raised when a user tries to bookmark a message they have already bookmarked."""


class Bookmark(commands.Cog):
    """Creates personal bookmarks by relaying a message link to the user's DMs."""

    # A lookup of what messages a member has bookmarked.
    # Used to stop members from bookmarking the same message twice.
    # {member id: json dumps list of message ids}
    member_bookmarked_messages = RedisCache()

    def __init__(self, bot: Bot):
        self.bot = bot

    async def get_member_bookmarked_messages(self, member: discord.Member) -> list[int]:
        """De-serialise and return the messages a user has bookmarked."""
        members_bookmarked_messages = await self.member_bookmarked_messages.get(member.id, "[]")
        return json.loads(members_bookmarked_messages)

    async def update_member_bookmarked_messages(
        self,
        member: discord.Member,
        message_id: int,
        add_or_remove: Literal["add", "remove"]
    ) -> None:
        """De-serialise, run specified action, serialise, and store the messages a user has bookmarked."""
        members_bookmarked_messages = await self.get_member_bookmarked_messages(member)
        if message_id not in members_bookmarked_messages:
            # Member deleted a DM message other than a bookmark
            return

        action = list.append if add_or_remove == "add" else list.remove
        action(members_bookmarked_messages, message_id)
        await self.member_bookmarked_messages.set(member.id, json.dumps(members_bookmarked_messages))

    @staticmethod
    def build_bookmark_dm(target_message: discord.Message, title: str) -> discord.Embed:
        """Build the embed to DM the bookmark requester."""
        embed = discord.Embed(
            title=title,
            description=target_message.content,
            colour=Colours.soft_green
        )
        embed.add_field(
            name="Wanna give it a visit?",
            value=f"[Visit original message]({target_message.jump_url})"
        )
        embed.set_author(name=target_message.author, icon_url=target_message.author.display_avatar.url)
        embed.set_thumbnail(url=Icons.bookmark)

        return embed

    @staticmethod
    def build_error_embed(message: str) -> discord.Embed:
        """Builds an error embed for when a bookmark requester has DMs disabled."""
        return discord.Embed(
            title=random.choice(ERROR_REPLIES),
            description=message,
            colour=Colours.soft_red
        )

    async def maybe_send_bookmark(
        self,
        target_message: discord.Message,
        title: str,
        member: discord.Member
    ) -> discord.Message:
        """Send and return the message a the user with the given embed, raise error if they have already bookmarked."""
        members_bookmarked_messages = await self.get_member_bookmarked_messages(member)

        if target_message.id in members_bookmarked_messages:
            raise AlreadyBookmarkedError

        embed = self.build_bookmark_dm(target_message, title)
        message = await member.send(embed=embed)

        await self.update_member_bookmarked_messages(member, message.id, "add")

        return message

    async def action_bookmark(
        self,
        channel: discord.TextChannel,
        member: discord.Member,
        target_message: discord.Message,
        title: str
    ) -> None:
        """Sends the bookmark DM, or sends an error embed when a user bookmarks a message."""
        try:
            await self.maybe_send_bookmark(target_message, title, member)
        except discord.Forbidden:
            error_embed = self.build_error_embed(f"{member.mention}, please enable your DMs to receive the bookmark.")
        except AlreadyBookmarkedError:
            error_embed = self.build_error_embed(f"{member.mention}, you have already bookmarked this message!")
        else:
            log.info(f"{member} bookmarked {target_message.jump_url} with title '{title}'")
            return
        await channel.send(embed=error_embed)

    @commands.group(name="bookmark", aliases=("bm", "pin"), invoke_without_command=True)
    @commands.guild_only()
    @whitelist_override(roles=(Roles.everyone,))
    async def bookmark(
        self,
        ctx: commands.Context,
        target_message: Optional[WrappedMessageConverter],
        *,
        title: str = "Bookmark"
    ) -> None:
        """
        Send the author a link to the specified bookmark via DMs.

        Users can either give a message as an argument, or reply to a message.

        Bookmarks can subsequently be deleted by using the `bookmark delete` command.
        """
        target_message = target_message or getattr(ctx.message.reference, "resolved", None)
        if not target_message:
            raise commands.UserInputError(MESSAGE_NOT_FOUND_ERROR)

        # Prevent users from bookmarking a message in a channel they don't have access to
        permissions = target_message.channel.permissions_for(ctx.author)
        if not permissions.read_messages:
            log.info(f"{ctx.author} tried to bookmark a message in #{target_message.channel} but has no permissions.")
            embed = self.build_error_embed(f"{ctx.author.mention} You don't have permission to view this channel.")
            await ctx.send(embed=embed)
            return

        await self.action_bookmark(ctx.channel, ctx.author, target_message, title)

        # Keep track of who has already bookmarked, so users can't spam reactions and cause loads of DMs
        bookmarked_users = [ctx.author.id]

        reaction_embed = discord.Embed(
            description=(
                f"React with {BOOKMARK_EMOJI} to be sent your very own bookmark to "
                f"[this message]({ctx.message.jump_url})."
            ),
            colour=Colours.soft_green
        )
        reaction_message = await ctx.send(embed=reaction_embed)
        await reaction_message.add_reaction(BOOKMARK_EMOJI)

        def event_check(reaction: discord.Reaction, user: discord.Member) -> bool:
            """Make sure that this reaction is what we want to operate on."""
            return (
                # Conditions for a successful pagination:
                all((
                    # Reaction is on this message
                    reaction.message.id == reaction_message.id,
                    # User has not already bookmarked this message
                    user.id not in bookmarked_users,
                    # Reaction is the `BOOKMARK_EMOJI` emoji
                    str(reaction.emoji) == BOOKMARK_EMOJI,
                    # Reaction was not made by the Bot
                    user.id != self.bot.user.id
                ))
            )

        while True:
            try:
                reaction, user = await self.bot.wait_for("reaction_add", timeout=TIMEOUT, check=event_check)
            except asyncio.TimeoutError:
                log.debug("Timed out waiting for a reaction")
                break
            log.trace(f"{user} has successfully bookmarked from a reaction, attempting to DM them.")
            await self.action_bookmark(ctx.channel, user, target_message, title)
            await reaction.remove()
            bookmarked_users.append(user.id)

        await reaction_message.delete()

    @commands.dm_only()
    @bookmark.command(name="delete", aliases=("del",))
    async def delete_bookmark(
        self,
        ctx: commands.Context,
        message_to_delete: Optional[WrappedMessageConverter]
    ) -> None:
        """
        Delete the referenced DM message by the user.

        Users can either give a message as an argument, or reply to a message.
        """
        message_to_delete = message_to_delete or getattr(ctx.message.reference, "resolved", None)
        if not message_to_delete:
            raise commands.UserInputError(MESSAGE_NOT_FOUND_ERROR)

        if message_to_delete.channel != ctx.channel:
            raise commands.UserInputError(":x: You can only delete messages in your own DMs!")
        await message_to_delete.delete()
        await self.update_member_bookmarked_messages(ctx.author, message_to_delete.id, "remove")


def setup(bot: Bot) -> None:
    """Load the Bookmark cog."""
    bot.add_cog(Bookmark(bot))
