"""Who may do what.

Four roles, each including the ones before it:

  viewer    reads everything except the user list
  buyer     enters and edits the day's work: notes, orders, quotes, requests, counts, drafts
  approver  decides what spends or changes the books: approves and cancels baskets, sends mail to
            suppliers, awards a price, confirms payments, adjusts stock, deletes records, runs the agents
  admin     sets the program up: users, rules and limits, modes, mail and chat settings, wallet,
            connections, backups, the bank

`required_role` is the whole table. A write the table does not name needs a buyer, a delete an approver, so
a new route is never open to a viewer by accident. The middleware in server.py applies it to every request,
and a test walks every route to be sure none escapes it.
"""

import re

from .account import RANK

# (methods, path pattern, role). The first line that matches wins.
RULES = [
    ({"POST"}, r"^/(unlock|lock|exit)$", "viewer"),
    ({"GET"}, r"^/api/users$", "admin"),
    ({"*"}, r"^/api/users(/.*)?$", "admin"),
    ({"POST"}, r"^/api/account/password$", "viewer"),

    # money and credentials
    ({"*"}, r"^/api/wallet/(deposit|withdraw)$", "admin"),
    ({"*"}, r"^/api/(bank|payer|bank-drop)(/.*)?$", "admin"),
    ({"*"}, r"^/api/channels(/.*)?$", "admin"),
    ({"POST"}, r"^/api/payments/.*$", "approver"),

    # how the agents behave
    ({"*"}, r"^/api/charters/.*$", "admin"),
    ({"POST"}, r"^/api/policies/run$", "approver"),
    ({"*"}, r"^/api/policies(/.*)?$", "admin"),
    ({"POST"}, r"^/api/(plutus|judge)/mode$", "admin"),
    ({"POST"}, r"^/api/replenish/auto$", "admin"),
    ({"POST"}, r"^/api/(plutus|judge|replenish)/run$", "approver"),

    # program settings
    ({"*"}, r"^/api/(mail/settings|mail/oauth/.*|chat/settings|slack/settings|slack/check|models/.*)$", "admin"),
    ({"*"}, r"^/api/(backups|archive/run)$", "admin"),
    ({"*"}, r"^/api/(calendar/.*|rates.*|terms|units|vocabulary/merge)$", "admin"),

    # decisions
    ({"POST"}, r"^/api/gate/\d+/(approve|cancel)$", "approver"),
    ({"POST"}, r"^/api/award$", "approver"),
    ({"POST"}, r"^/api/outbox/\d+/send$", "approver"),
    ({"POST"}, r"^/api/stockctl/(adjust|transfer)$", "approver"),
    ({"POST"}, r"^/api/counting/\d+/settle$", "approver"),
    ({"POST"}, r"^/api/disputes/\d+/(credit|close)$", "approver"),
    ({"DELETE"}, r"^/api/.*$", "approver"),
]

DEFAULT_WRITE = "buyer"


def required_role(method, path):
    """The lowest role that may make this request."""
    method = method.upper()
    for methods, pattern, role in RULES:
        if ("*" in methods or method in methods) and re.match(pattern, path):
            return role
    if method in ("GET", "HEAD", "OPTIONS"):
        return "viewer"
    return DEFAULT_WRITE


def allowed(role, method, path):
    return RANK.get(role, -1) >= RANK[required_role(method, path)]
