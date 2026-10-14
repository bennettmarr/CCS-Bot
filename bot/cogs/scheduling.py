"""
/schedule - one team proposes, the other confirms.

The team that proposes cannot confirm its own proposal. The Confirm button
checks the presser holds the *other* team's role, so nobody can lock in a time
on their own. Admins can confirm either way.

Buttons survive restarts: the view is registered with a fixed custom_id at
startup, and the match is looked up from the thread or channel it lives in,
not from anything held in memory.
"""
from __future__ import annotations

from datetime import timezone

import discord
from discord import app_commands
from discord.ext import commands

from ..matching import check, is_admin, member_team, member_teams, resolve
from ..store import iso, now_utc
from ..times import discord_ts, parse, short
from . import private


class ConfirmView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Confirm this time",
        style=discord.ButtonStyle.success,
        custom_id="ccs:confirm_time",
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        store = self.bot.store
        fx = resolve(store, interaction.channel)
        if fx is None:
            return await interaction.response.send_message(
                "I can't tell which match this is.", ephemeral=private()
            )
        st = store.get(fx["id"])
        prop = st.get("proposed")
        if not prop:
            return await interaction.response.send_message(
                "There's no time waiting to be confirmed.", ephemeral=private()
            )

        admin = is_admin(store, interaction.user)
        teams = member_teams(store, interaction.user, fx)

        # Admins override everything. They can confirm on a team's behalf when
        # a roster has gone quiet, whether or not they hold the roles.
        if not admin:
            if not teams:
                return await interaction.response.send_message(
                    "You're not on either team in this match.", ephemeral=private()
                )
            # holding the proposing team's role at all disqualifies you, even
            # if you also hold the other one
            if prop["by_team"] in teams:
                other = fx["away"] if prop["by_team"] == fx["home"] else fx["home"]
                return await interaction.response.send_message(
                    f"Your own team proposed this time — {other} has to confirm it.",
                    ephemeral=private(),
                )

        from datetime import datetime

        dt = datetime.fromisoformat(prop["iso"])
        if dt < now_utc():
            return await interaction.response.send_message(
                "That time has already passed — propose a new one with "
                "`/schedule`.",
                ephemeral=private(),
            )
        proposal = dict(prop)
        store.set(
            fx["id"],
            status="scheduled",
            scheduled_iso=prop["iso"],
            proposed=None,
        )
        button.disabled = True
        await interaction.response.edit_message(view=self)

        by_admin = admin and (not teams or prop["by_team"] in teams)
        confirming = (
            fx["away"] if proposal["by_team"] == fx["home"] else fx["home"]
        )
        if teams:
            confirming = next(iter(teams - {proposal["by_team"]}), confirming)
        await self.bot.on_time_confirmed(
            fx,
            dt,
            interaction,
            by_admin=by_admin,
            proposal=proposal,
            confirmed_by=interaction.user,
            confirming_team=confirming,
        )


    @discord.ui.button(
        label="Wrong time",
        style=discord.ButtonStyle.secondary,
        custom_id="ccs:decline_time",
    )
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        store = self.bot.store
        fx = resolve(store, interaction.channel)
        if fx is None:
            return await interaction.response.send_message(
                "I can't tell which match this is.", ephemeral=private()
            )
        st = store.get(fx["id"])
        prop = st.get("proposed")
        if not prop:
            return await interaction.response.send_message(
                "There's no time waiting to be confirmed.", ephemeral=private()
            )
        teams = member_teams(store, interaction.user, fx)
        admin = is_admin(store, interaction.user)
        if not teams and not admin:
            return await interaction.response.send_message(
                "You're not on either team in this match.", ephemeral=private()
            )

        # either side can knock it back - the proposing team withdrawing a
        # mistyped time is just as valid as the other team saying no
        withdrawn = bool(teams) and prop["by_team"] in teams
        store.set(fx["id"], proposed=None)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        who = interaction.user.display_name
        side = next(iter(teams)) if teams else None
        hr = store.team_role_id(fx["home"])
        ar = store.team_role_id(fx["away"])
        pings = f"<@&{hr}> <@&{ar}>"
        if not withdrawn:
            pings += f" <@{prop['by_user']}>"
        await interaction.followup.send(
            content=pings,
            embed=discord.Embed(
                title="Time withdrawn" if withdrawn else "Time declined",
                description=(
                    f"**{fx['home']}** vs **{fx['away']}**\n\n"
                    + (
                        "The proposing team took it back. "
                        if withdrawn
                        else "That time doesn't work. "
                    )
                    + "Propose another with `/schedule`."
                ),
                colour=0x95A5A6,
            ).add_field(
                name="Withdrawn by" if withdrawn else "Declined by",
                value=f"{interaction.user.mention} ({who})"
                + (f" — **{side}**" if side else "  ·  admin"),
                inline=False,
            ),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )
        await self.bot.log(
            f"{'WITHDRAWN' if withdrawn else 'DECLINED'} {fx['home']} vs {fx['away']} "
            f"by {who}"
        )


class Scheduling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="schedule", description="Propose a time for this match (Eastern)."
    )
    @app_commands.describe(
        time="e.g. tuesday 8pm · 9/27 9pm · sept 27 8pm · tomorrow 7:30pm (Eastern)"
    )
    async def schedule(self, interaction: discord.Interaction, time: str):
        store = self.bot.store
        fx = resolve(store, interaction.channel)
        ok, team, why = check(store, interaction.user, fx)
        if not ok:
            return await interaction.response.send_message(why, ephemeral=private())

        dt = parse(time)
        if dt is None:
            return await interaction.response.send_message(
                "I couldn't read that time. Any of these work:\n"
                "`tuesday 8pm` · `9/27 9pm` · `sept 27 8pm` · `tomorrow 7:30pm`\n"
                "It's always Eastern.",
                ephemeral=private(),
            )
        if dt < now_utc():
            return await interaction.response.send_message(
                "That time has already passed.", ephemeral=private()
            )

        st = store.get(fx["id"])
        if st["status"] == "reported":
            return await interaction.response.send_message(
                "This match has already been reported.", ephemeral=private()
            )

        proposing = team or fx["home"]
        other = fx["away"] if proposing == fx["home"] else fx["home"]
        store.set(
            fx["id"],
            proposed={
                "by_team": proposing,
                "iso": iso(dt),
                "by_user": interaction.user.id,
                "by_name": interaction.user.display_name,
                "by_admin": team is None,
                "at": iso(now_utc()),
                "nudges": 0,
            },
        )

        other_role = store.team_role_id(other)
        embed = discord.Embed(
            title="Proposed time",
            description=f"**{fx['home']}** vs **{fx['away']}**\n\n"
            f"{discord_ts(dt)}\n({discord_ts(dt, 'R')})",
            colour=0xF1C40F,
        )
        who = interaction.user.display_name
        embed.add_field(
            name="Proposed by",
            value=f"{interaction.user.mention} ({who}) — **{proposing}**"
            + ("  ·  admin" if team is None else ""),
            inline=False,
        )
        embed.set_footer(
            text=f"{other} needs to confirm — or press Wrong time to knock it back"
        )
        await interaction.response.send_message(
            content=f"<@&{other_role}>",
            embed=embed,
            view=ConfirmView(self.bot),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await self.bot.log(
            f"`/schedule` {fx['home']} vs {fx['away']} — {proposing} proposed "
            f"{short(dt)} ({interaction.user})"
        )


async def setup(bot):
    await bot.add_cog(Scheduling(bot))
