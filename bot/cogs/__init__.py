"""
Shared bits for the command cogs.

private() decides whether command replies are "only you can see this".
With public_replies on (the default) they post for everyone in the channel;
"Admins only." refusals always stay private.
"""
_PUBLIC = [True]


def set_public(on: bool) -> None:
    _PUBLIC[0] = bool(on)


def private() -> bool:
    return not _PUBLIC[0]
