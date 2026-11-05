# Bot rules

The two bots and what each may do on its own.

| File | Bot | What it does |
|---|---|---|
| `plutus.md` | Plutus, the buyer | runs the purchasing: orders from the DDMRP on a rhythm, sends the orders, approves and places baskets, pays matched bills, from a wallet a person funds |
| `terminus.md` | Terminus, the gate keeper | reads names, then approves or cancels the baskets the gate holds, inside its limits |

Each file has three parts:

- **Free to**: what the bot is trusted to decide itself.
- **Never**: what it must never do.
- **Limits**: numbers, one per line as `- name = number : what it means`.

How the files are used:

- The text goes to the model in its instructions, so it knows where it stands.
- The limits are then applied again in code to whatever the model answers, so a model that
  ignores its instructions changes nothing: a draft line over a limit is cut or corrected.
- The app reads this folder when `BOT_RULES_DIR` is set (in `~/.config/stock-manager/settings.env`).
  Without it, the same text built into the program is used. A change to a file takes effect on the
  next request, with no restart.
- A limit set on the screen (Settings, Rules for the two bots) overrides the number in the file
  until it is reset, and "reset" goes back to the number here.
- A limit name the program does not know is ignored, and a missing one falls back to the built-in
  value, so a half-edited file cannot switch a rule off.
- Policies (what may go through without a person) are separate: Settings, Policies.
