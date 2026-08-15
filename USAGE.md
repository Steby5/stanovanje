# Using the bot

How to tell fbwatch what you're looking for, from Discord. If you're setting the
watcher up rather than using it, you want [README.md](README.md) instead.

Every command starts with `!fbw` and is typed in the control channel.

---

## Getting started

Just say what you're looking for:

```
!fbw add oddam + soba + ljubljana
```

That subscribes you on the spot — no one has to set you up first — and your
listings will arrive in this same channel, `@`-mentioning you. Set the channel to
**Notification Settings → Only @mentions** so you're only pinged for your own
criteria, not everyone else's.

Want them somewhere else instead?

```
!fbw channel 1234567890123456789    send my listings to that channel
!fbw channel here                   back to this one
!fbw mention off                    receive them without being pinged
```

You get nothing until you have **at least one trigger word** — that's deliberate,
so nobody is buried under every post in every group on day one.

Check where you stand:

```
!fbw mine
```

```
ana → Discord
Trigger rules (0)
· (none yet)

⚠ Receiving nothing — no trigger words set.
```

---

## Everyday commands

### Add a trigger word

The one you'll use most. Anything matching gets sent to you.

```
!fbw add oddam + soba
```
```
✅ Added `oddam + soba` for ana.
```

### See what you're watching for

```
!fbw mine
```
```
ana → Discord
Trigger rules (2)
· `oddam + soba`
· `garsonjera`
Exclusions
· `!agencija`
```

### Try a rule before you commit to it

Paste a real post and see whether it would have reached you. This is the
fastest way to tell a good rule from a noisy one.

```
!fbw test Oddam lepo sobo v Ljubljani, 400 EUR, na voljo od 1.9.
```
```
✅ YES — this would notify ana
Reason: `oddam + soba`
```

### Stop getting something

```
!fbw remove garsonjera
```
```
🗑 Removed `garsonjera` for ana.
```

### Cut out noise

Excluded words win over triggers — a post containing one is dropped even if it
matches everything else you asked for.

```
!fbw exclude agencija
```
```
🔇 ana won't see posts containing `agencija`.
```

### Other

| Command | What it does |
|---|---|
| `!fbw channel <id>` / `here` | Send your listings elsewhere, or back to this channel |
| `!fbw mention off` / `on` | Receive listings without being pinged |
| `!fbw status` | Is the watcher running, how many cycles, when it last checked |
| `!fbw help` | The command list, tailored to what you're allowed to do |

Changes take effect on the next check — usually within five minutes. You don't
need to restart anything.

### If you share a channel with other people

Everyone sees every post the watcher finds for the group, but each post
`@`-mentions only the people whose trigger words it matched. So set that channel
to **Notification Settings → Only @mentions** and your phone stays quiet until
something is genuinely for you.

The embed says who each post matched and why, so `@ana — garsonjera` tells you at
a glance that this one isn't yours.

---

## Writing trigger words that work

### The syntax

| You type | It matches |
|---|---|
| `stanovanje` | stanovanje, stanovanja, stanovanju — and Ljubljanski word endings generally |
| `oddam + ljubljana` | posts containing **both**, anywhere, in any order |
| `"oddam stanovanje"` | that exact phrase, nothing else |
| `=soba` | the whole word `soba` — not `posoda`, not `sobota` |
| `re:\d{3,4}\s?(eur\|€)` | a price like `450 EUR` or `500€` |

Case and accents never matter: `zelim` finds "Želim", `ISCEM` finds "iščem".

### Slovenian endings are handled

You don't need to guess the stem. Plain words match their declined forms:

- `ljubljana` → "v **Ljubljani**", "**ljubljansko** stanovanje"
- `garsonjera` → "oddam **garsonjero**"
- `soba` → "**sobo**", "**sobi**", "**sobe**", "5 **sob**"

Short words are matched more tightly so `soba` doesn't fire on "sobota". When you
need the exact form, use `=soba` or `"soba"`.

### A strategy that works

**Start with one broad rule, then cut.** A rule that's too narrow fails silently —
you never learn what you missed. A rule that's too broad announces itself
immediately, and you fix it with `exclude`.

```
!fbw add oddam + ljubljana        ← broad
!fbw exclude iscem                ← then cut the people looking, not offering
!fbw exclude agencija
```

**Combine with `+` rather than writing long phrases.** `oddam + soba` catches
"Oddam sobo", "oddam prosto sobo", "sobo oddam" and "Oddam v najem sobo".
`"oddam sobo"` catches only the first.

**Watch for the language of the group.** Slovenian rules don't fire on the
English-language groups. If you're subscribed to those, add English rules too:

```
!fbw add "room for rent"
!fbw add "for rent" + ljubljana
!fbw add sublet
```

**Be careful with exclusions.** `!fbw exclude looking for a room` also kills a
genuine ad saying "if you're looking for a room, I have one free". Prefer narrow,
first-person forms like `"i am looking for"`.

### Worked examples

| Goal | Rule |
|---|---|
| Any room offered in Ljubljana | `oddam + soba + ljubljana` |
| A studio, any wording | `garsonjera` |
| One- or two-room flats | `enosobno` and `dvosobno` |
| Anything with a move-in date | `"na voljo od"` |
| Under 500 € | `re:[1-4]\d{2}\s?(eur\|€)` |
| English-language listings | `"room for rent"`, `sublet` |

---

## Admin commands

Only for people marked as admin. Everyone else gets a polite refusal.

### People

| Command | What it does |
|---|---|
| `!fbw users` | Everyone subscribed, and who's still not receiving anything |
| `!fbw user add ana` | Add a person |
| `!fbw user remove ana` | Remove them (their rules file stays on disk) |
| `!fbw user set ana channel <id>` | Post their listings into that channel |
| `!fbw user set ana webhook <url>` | Or send them through a webhook instead |
| `!fbw user set ana telegram <chat id>` | Notify them on Telegram instead, or as well |
| `!fbw user set ana discord <user id>` | Let them manage their own rules |
| `!fbw user set ana admin true` | Make them an admin |
| `!fbw user set ana groups 12345,67890` | Restrict them to certain groups |
| `!fbw user enable ana` / `disable ana` | Turn their notifications on or off |
| `!fbw for ana add oddam + soba` | Edit someone else's rules |
| `!fbw telegram ids` | Chat ids of people who messaged the Telegram bot |

A new person receives nothing until they have **both** a destination and at least
one trigger word. `!fbw users` flags anyone still waiting.

### Groups and the watcher

| Command | What it does |
|---|---|
| `!fbw group add <url> \| Short name` | Watch another Facebook group |
| `!fbw group remove <name or id>` | Stop watching one |
| `!fbw list` | Every group and every person's setup |
| `!fbw pause` / `!fbw resume` | Mute everyone. New posts are still recorded, so nobody gets a flood on resume |
| `!fbw check` | Check every group right now instead of waiting |
| `!fbw interval 600` | How often to check, in seconds (minimum 60) |

`interval` lasts until the watcher restarts. Edit `config.json` to make it stick.

### A note on linking

Until an admin links their Discord account, **anyone who can post in the control
channel can run admin commands**. That's fine on a private server with one person
in it. Once you add other people, link yourself:

```powershell
python main.py users domin --discord-id 123456789012345678
```

From then on, matching is strict: unlinked people can only run `help` and
`status`. So link the admin before, or at the same time as, everyone else.

---

## The bot looks offline

It always does, and that's normal — it talks to Discord over the REST API and
never opens the connection that Discord uses to decide who's online. An offline
bot that answers commands is working correctly.

What does matter: **commands are only answered while the watcher is running.** If
nobody has `python main.py run` going, the channel isn't being read at all and
your commands sit there unanswered. `!fbw status` is the quick check — a reply
means it's alive.

---

## Not getting notifications?

Work down this list — it's ordered by how often each one is the cause.

1. **`!fbw mine`** — do you have any trigger rules, and does it show a
   destination? A warning line here is the answer most of the time.
2. **`!fbw test <paste a real post>`** — if that says *No*, your rules are the
   problem, not the plumbing. Broaden the rule or check the group's language.
3. **Check for an over-eager exclusion.** `!fbw mine` lists them. One bad
   exclusion silently kills matching posts.
4. **`!fbw status`** — is the watcher actually running, and is it paused? If the
   last cycle was hours ago, it isn't running.
5. **Was the post older than you?** Nobody gets the backlog that existed when
   they were added — only posts from that moment on.
6. **Discord notification settings.** The message may be arriving into a muted
   channel. Right-click the channel → Notification Settings → All Messages.

If the watcher has lost its Facebook session it says so in the channel — that one
needs whoever runs it to log in again.
