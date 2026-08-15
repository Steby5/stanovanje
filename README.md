# fbwatch

[![tests](https://github.com/Steby5/stanovanje/actions/workflows/tests.yml/badge.svg)](https://github.com/Steby5/stanovanje/actions/workflows/tests.yml)

Watches Facebook groups for new posts and sends the interesting ones to Discord
or Telegram — with the post text embedded and a direct link to the post.

Built for apartment hunting, where the good listings are gone in an hour and the
groups are 90% noise.

- **Groups** to watch live in `groups.txt`, one per line.
- **Trigger words** live in `keywords.txt`. Only posts matching them are sent, so
  you don't get pinged for every post in the group.
- Matching handles **Slovenian inflection** — `ljubljana` matches "v Ljubljani",
  `soba` matches "sobo"/"sobi"/"sobe".
- Each post is notified **once**; state survives restarts.
- **Several people** can share one watcher, each with their own trigger words and
  their own destination. Facebook is still scraped once per cycle.
- Configurable **from Discord** — add trigger words from your phone.

> **Personal-use tool.** It drives a real logged-in browser at a deliberately
> gentle pace to read groups you are already a member of. Automated access is
> against Facebook's terms of service; you use it on your own account at your own
> risk. Don't point it at groups you haven't joined, and don't crank the poll
> interval down.

---

## Setup

### 1. Install

```powershell
git clone https://github.com/Steby5/stanovanje.git
cd stanovanje

pip install -r requirements.txt
python -m playwright install chromium
```

Then create your own config from the examples — these are gitignored, so your
groups, trigger words and webhook stay on your machine:

```powershell
copy config.example.json config.json
copy groups.example.txt groups.txt
copy keywords.example.txt keywords.txt
```

### 2. Create a Discord webhook

In Discord: **Server Settings → Integrations → Webhooks → New Webhook**, pick the
channel, then **Copy Webhook URL**. (On a channel you own: right-click the channel
→ Edit Channel → Integrations → Webhooks.)

Paste the URL into the `config.json` you created above:

```powershell
notepad config.json
```

Or keep it out of the file entirely:

```powershell
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
```

Check it works — this should post a message in your channel:

```powershell
python main.py test-discord
```

### 3. Log in to Facebook (once)

```powershell
python main.py login
```

A real Chrome window opens. Log in normally (including 2FA). The session is saved
into `browser_profile/` and reused from then on, so you only do this once — until
Facebook expires it, at which point the watcher tells you on Discord.

> Most housing groups are private, so the watcher can only see groups **your own
> account has already joined**. Join them in the browser first.

### 4. List your groups

Edit `groups.txt`:

```
https://www.facebook.com/groups/123456789012345 | Najem stanovanj LJ
https://www.facebook.com/groups/some-group-name  | Študentska stanovanja
```

Anything works: a full URL, a bare group id, or a vanity name. The part after `|`
is just the label shown in Discord.

### 5. Set your trigger words

Edit `keywords.txt` (see [Trigger words](#trigger-words) below), then test it
without touching Facebook:

```powershell
python main.py test-keywords "Oddam lepo sobo v Ljubljani, 400 EUR"
```

### 6. Run it

```powershell
python main.py check    # one pass, to see that it works
python main.py run      # keep watching, every ~5 minutes
```

The first time a group is polled, existing posts are recorded silently so you
don't get 15 notifications at once. From then on, only genuinely new posts are
sent. Leave `run` going in a terminal; `Ctrl+C` stops it.

---

## Trigger words

One rule per line in `keywords.txt`. A post is sent when it matches **at least one**
rule and **no** `!` exclusion.

| Line | Matches |
|---|---|
| `stanovanje` | stanovanje, stanovanja, stanovanju, … |
| `oddam + ljubljana` | posts containing **both** words, in any order |
| `"oddam stanovanje"` | that exact phrase only |
| `=soba` | the whole word `soba` — not `posoda` |
| `re:\d{3,4}\s?(eur\|€)` | a price like `450 EUR` or `500€` |
| `!agencija` | **excludes** any post containing `agencija` |

Matching ignores case and diacritics, so `zelim` matches "Želim" and `ISCEM`
matches "iščem".

**Slovenian endings are handled.** Plain terms match declined forms: `ljubljana`
matches "v Ljubljani", `garsonjera` matches "garsonjero", `soba` matches
"sobo"/"sobi"/"sobe". Short words are matched more tightly so `soba` doesn't
trigger on "sobota". When you need the exact form, use `=soba` or `"soba"`.

Exclusions always win over triggers. A file with **no trigger words receives
nothing** — that way adding a new person doesn't bury them under every post in
every group. The watcher warns on startup if someone is in that state.

Both `groups.txt` and the keyword files are re-read on every cycle — edit them
while the watcher is running and the changes apply on the next pass, no restart
needed.

---

## Commands

| Command | What it does |
|---|---|
| `python main.py login` | Log in to Facebook once, save the session |
| `python main.py check` | Poll every group once and send notifications |
| `python main.py check --dry-run` | Same, but log matches instead of sending them |
| `python main.py run` | Poll forever on an interval |
| `python main.py seed` | Mark everything currently visible as seen, notify nothing |
| `python main.py list` | Show the groups, rules and paths currently in effect |
| `python main.py users [name] [--webhook/--telegram/...]` | Add or edit a subscriber |
| `python main.py telegram-ids` | Find Telegram chat ids to link to people |
| `python main.py test-discord` | Send a test message to the webhook |
| `python main.py test-control` | Check the bot token used for Discord commands |
| `python main.py test-keywords "..."` | Try everyone's rules against some text |
| `python main.py dump [group]` | Save page HTML + screenshot for troubleshooting |

Add `-v` for debug logging. Everything is also written to `fbwatch.log`.

---

## Configuring it from Discord (optional)

You can add trigger words, add groups, pause and check from Discord itself
instead of editing files on the PC:

```
!fbw add oddam + garsonjera
!fbw group add https://www.facebook.com/groups/123456 | Nova skupina
!fbw test Oddam sobo v Ljubljani, 400 EUR
!fbw pause
```

This needs a **bot token** in addition to the webhook — a webhook can only send
messages, never read them.

### Setting up the bot

1. Go to <https://discord.com/developers/applications> → **New Application**, name
   it anything.
2. **Bot** in the sidebar → **Reset Token** → **Copy**. That's the token; treat it
   like a password.
3. On the same page, scroll to **Privileged Gateway Intents** and turn on
   **MESSAGE CONTENT INTENT**. Without it the bot can't read your commands.
4. **OAuth2 → URL Generator**: tick **bot**, then under permissions tick
   **View Channels**, **Send Messages**, **Read Message History**. Open the URL it
   generates at the bottom and add the bot to your server.
5. Get the channel id: in Discord, **Settings → Advanced → Developer Mode** on,
   then right-click your channel → **Copy Channel ID**.

Put both in `config.json`:

```json
"discord_bot_token": "paste-the-token-here",
"discord_control_channel_id": "1234567890123456789"
```

Or keep the token out of the file with `$env:DISCORD_BOT_TOKEN = "..."`.

Check it:

```powershell
python main.py test-control
```

Commands are read while `python main.py run` is going, within a few seconds.

### Commands

| Command | What it does |
|---|---|
Anyone in the channel:

| Command | What it does |
|---|---|
| `!fbw add <rule>` | Add a trigger word to **my** list |
| `!fbw remove <rule>` | Remove one of my trigger words |
| `!fbw exclude <term>` | Never notify **me** on posts containing it |
| `!fbw mine` | My rules and where my notifications go |
| `!fbw test <text>` | Would this post notify me? |
| `!fbw status` | What the watcher is doing, cycles, uptime |
| `!fbw help` | The command list |

Admins only:

| Command | What it does |
|---|---|
| `!fbw users` | Everyone subscribed, and who's still idle |
| `!fbw user add <name>` | Add a person |
| `!fbw user remove <name>` | Remove a person |
| `!fbw user set <name> webhook\|telegram\|discord\|admin <value>` | Configure them |
| `!fbw user enable\|disable <name>` | Turn someone's notifications on/off |
| `!fbw for <name> add <rule>` | Edit someone else's rules |
| `!fbw telegram ids` | Chat ids of people who messaged the Telegram bot |
| `!fbw group add <url> \| name` | Watch another group |
| `!fbw group remove <name or id>` | Stop watching a group |
| `!fbw list` | Groups and everyone's setup |
| `!fbw pause` / `!fbw resume` | Mute / unmute notifications for everyone |
| `!fbw check` | Poll every group right now |
| `!fbw interval <seconds>` | Change the poll interval (until restart) |

Commands edit `groups.txt`, the keyword files and `subscribers.json` in place, so
those files stay the single source of truth whether you change them from Discord
or in a text editor, and changes survive a restart.

`pause` keeps *recording* new posts while muted, so resuming doesn't dump
everything that piled up in the meantime.

**Who can use it:** by default, anyone who can post in that channel. On a private
server that's just you. Once you add other people (below) it splits into
self-service and admin commands. To lock the channel down entirely, list the
Discord user ids allowed to touch it (right-click a user → Copy User ID):

```json
"control_allowed_user_ids": ["123456789012345678"]
```

Set `"control_enabled": false` to switch the whole thing off.

---

## More than one person

Several people can share one watcher. Facebook is still scraped **once** per
cycle — the posts are then filtered per person and delivered to their own
destination, so adding people costs nothing extra on the Facebook side.

Each person gets:

- their **own trigger words** (`keywords/<name>.txt`)
- their **own destination** — a Discord webhook, a Telegram chat, or both
- their **own history**, so nobody sees a post twice and nobody gets a backlog
  when they're added

Without a `subscribers.json` the watcher runs single-user off `config.json` and
`keywords.txt`, exactly as before.

### Adding someone

From the command line:

```powershell
python main.py users ana --webhook https://discord.com/api/webhooks/...
notepad keywords\ana.txt
```

Or from Discord, as an admin:

```
!fbw user add ana
!fbw user set ana webhook https://discord.com/api/webhooks/...
!fbw for ana add oddam + soba
```

A new person receives nothing until they have **both** a destination and at
least one trigger word. `!fbw users` and `python main.py list` show who's still
waiting on what.

### Letting people manage their own words

Link their Discord account and they can run the everyday commands themselves,
each affecting only their own list:

```
!fbw user set ana discord 987654321098765432
```

Then Ana types `!fbw add garsonjera`, `!fbw mine`, `!fbw test <text>` and only
her rules change. Admin commands (`user ...`, `group ...`, `pause`, `interval`)
stay restricted to people with `"admin": true`.

Full file format in `subscribers.example.json`. A person can also watch a subset
of groups:

```json
"marko": {
  "keywords_file": "keywords/marko.txt",
  "telegram_chat_id": "123456789",
  "groups": ["504531176006435"]
}
```

---

## Telegram

Telegram is a **notifications-only** destination — configuration stays in
Discord. One bot serves everyone; each person is reached by their own chat id.

1. Message **@BotFather** on Telegram → `/newbot` → follow the prompts → copy the
   token.
2. Put it in `config.json`:

   ```json
   "telegram_bot_token": "123456789:AAxxxxxxxxxxxxxxxxxxxxx"
   ```

3. Have each person open your new bot and send it `/start`. Telegram refuses to
   let a bot message someone who hasn't written to it first, so this step is not
   optional.
4. Find their chat id and link it:

   ```powershell
   python main.py telegram-ids
   python main.py users marko --telegram 123456789
   ```

   Or from Discord: `!fbw telegram ids`, then
   `!fbw user set marko telegram 123456789`.

A person can have both a webhook and a chat id — they'll get the post in both
places, and a failure on one doesn't cost them the other.

---

## Settings (`config.json`)

The ones you're most likely to touch:

| Setting | Default | Meaning |
|---|---|---|
| `discord_webhook_url` | — | Where notifications go |
| `poll_interval_seconds` | `300` | How often to check (minimum 60) |
| `posts_per_group` | `15` | How far down each feed to read |
| `notify_on_first_run` | `false` | Set `true` to be notified about the existing backlog too |
| `headless` | `true` | Set `false` to watch the browser work |
| `include_images` | `true` | Put the post's first photo in the embed |
| `notify_errors` | `true` | Warn on Discord if the session dies |
| `discord_bot_token` | — | Enables the Discord commands above |
| `discord_control_channel_id` | — | Which channel to read commands from |
| `control_allowed_user_ids` | `[]` | Restrict who may reconfigure (empty = anyone in the channel) |

Full list in `config.example.json`.

---

## Running it in the background

To have it start with Windows, create a scheduled task:

```powershell
schtasks /create /tn "fbwatch" /sc onlogon /rl highest ^
  /tr "pythonw %CD%\main.py run"
```

(run that from the project folder, or write the full path to `main.py` yourself)

`pythonw` runs it without a console window. Check `fbwatch.log` for what it's
doing, and `schtasks /end /tn fbwatch` to stop it.

---

## Troubleshooting

**"Not logged in"** — run `python main.py login` again. Facebook expires sessions
periodically, especially if you log in elsewhere.

**"no posts found - are you a member of this group?"** — open the group in a normal
browser with the same account and confirm you can see posts. Private groups need
membership.

**No notifications, but the group is active** — check your rules with
`python main.py test-keywords "<paste a post here>"`. If that says YES but nothing
arrives, run `python main.py check -v` and look for `skip` lines in the output.

**Posts come through with empty or garbled text** — Facebook changed its markup.
Run `python main.py dump` and look at `debug/dump_<group>.json`: if `text_source`
says `fallback`, the primary selectors in `fbwatch/extract_js.py` need updating;
the saved `.html` shows the current structure.

**Nothing at all happens** — check `fbwatch.log`. If Facebook is showing a
checkpoint (unusual-activity prompt), run `python main.py login` and clear it in
the visible browser window.

---

## How it works

`main.py` is the CLI; the logic is in `fbwatch/`:

| File | Role |
|---|---|
| `config.py` | Loads `config.json`, validates it |
| `models.py` | `Group`/`Post` types, parses `groups.txt`, derives post ids |
| `matcher.py` | Parses a keyword file and decides what's interesting |
| `subscribers.py` | Who gets notified, about what, and where |
| `control.py` | Reads commands from Discord and edits the config files |
| `facebook.py` | Drives Chromium, loads feeds, expands "See more" |
| `extract_js.py` | The JavaScript that pulls posts out of the page |
| `notify.py` | Builds the Discord embed, handles rate limits and retries |
| `telegram.py` | The same for Telegram |
| `delivery.py` | Routes one post to one person's destinations |
| `store.py` | Remembers what each person has seen |
| `runner.py` | The poll loop and the per-subscriber fan-out |

Each group's feed is loaded sorted newest-first (`?sorting_setting=CHRONOLOGICAL`),
once per cycle regardless of how many people are subscribed. Posts are identified
by the numeric id in their permalink, falling back to a hash of author + text when
Facebook doesn't expose one. Seen ids are kept per person in `state.json` for 30
days; state written by an older single-user install is inherited by the admin on
first run rather than replayed.

If a delivery fails everywhere, the post is *un*-recorded for that person so the
next cycle retries it rather than losing it — and only for them.

### Tests

```powershell
python -m unittest discover -s tests -v
```

170 tests. Most run in milliseconds; `test_extractor.py` launches a real Chromium
against `tests/fixture_feed.html` to check the page-extraction JavaScript, and
skips itself if Playwright isn't installed.

---

## Notes

Scraping is done with a real browser at a deliberately gentle pace — one page load
per group per cycle, randomised delays between groups, and a randomised interval
between cycles. Polling much faster than the default is what gets accounts
rate-limited, so `poll_interval_seconds` is floored at 60.

`config.json`, `browser_profile/`, `state.json` and `fbwatch.log` contain your
webhook, your bot token, your Facebook session and your history — they're in
`.gitignore`; keep them out of anywhere public. If a bot token ever leaks, reset
it in the developer portal.
