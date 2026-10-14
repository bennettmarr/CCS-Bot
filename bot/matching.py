"""
Working out which match a command was run in, and who is allowed to run it.

Two kinds of place a match lives:

  threads   the bot made them, so it recorded thread_id -> fixture
  channels  Nation made them by hand, so nothing was recorded. Instead we read
            the channel's permission overwrites: if exactly two of the roles
            with access map to teams, and those two teams play each other this
            week, that is the match. No naming convention to get wrong, and it
            keeps working when he renames or moves things.

Admin, caster and staff roles are ignored because they are not in teams.json.
"""
from __future__ import annotations

from typing import Optional, Tuple

import discord


def teams_from_overwrites(store, channel: discord.abc.GuildChannel) -> list:
    found = []
    for target, ow in channel.overwrites.items():
        if not isinstance(target, discord.Role):
            continue
        if ow.view_channel is False:
            continue
        team = store.team_for_role(target.id)
        if team:
            found.append(team)
    return found


def resolve(store, channel) -> Optional[dict]:
    """Return the fixture for this channel or thread, or None."""
    # a thread the bot opened
    if isinstance(channel, discord.Thread):
        fx = store.by_thread(channel.id)
        if fx:
            return fx
        parent = channel.parent
        if parent is not None:
            teams = teams_from_overwrites(store, parent)
            if len(teams) == 2:
                return store.find_fixture(*teams)
        return None

    # a channel someone made by hand
    fx = store.by_channel(channel.id)
    if fx:
        return fx
    teams = teams_from_overwrites(store, channel)
    if len(teams) == 2:
        fx = store.find_fixture(*teams)
        if fx:
            store.set(fx["id"], channel_id=channel.id)
        return fx
    return None


def is_admin(store, member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    admin_id = int(store.cfg("admin_role_id"))
    return any(r.id == admin_id for r in member.roles)


def member_teams(store, member: discord.Member, fixture: dict) -> set:
    """
    Every side of this match the member belongs to - normally one, but
    someone can hold both roles (staff, a player rostered twice). Returning
    the full set means a check never silently picks the convenient one.
    """
    out = set()
    for r in member.roles:
        team = store.team_for_role(r.id)
        if team in (fixture["home"], fixture["away"]):
            out.add(team)
    return out


def member_team(store, member: discord.Member, fixture: dict) -> Optional[str]:
    """One side, for display. Prefers home when the member holds both."""
    teams = member_teams(store, member, fixture)
    if not teams:
        return None
    return fixture["home"] if fixture["home"] in teams else next(iter(teams))


def check(store, member: discord.Member, fixture: dict) -> Tuple[bool, Optional[str], str]:
    """(allowed, team_or_None_for_admin, message_if_refused)"""
    if fixture is None:
        return False, None, "I can't tell which match this is. Run this in a match thread or channel."
    team = member_team(store, member, fixture)
    if team:
        return True, team, ""
    if is_admin(store, member):
        return True, None, ""
    return (
        False,
        None,
        f"You're not on either team in this match ({fixture['home']} vs {fixture['away']}).",
    )
