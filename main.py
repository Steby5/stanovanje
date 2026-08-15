#!/usr/bin/env python3
"""fbwatch - watch Facebook groups and ping Discord when an interesting post appears.

    python main.py login          log in to Facebook once (opens a real window)
    python main.py export-session save that login, to move to a headless machine
    python main.py import-session load a login exported from another machine
    python main.py check          poll every group once, send notifications
    python main.py run            poll forever on an interval
    python main.py seed           record current posts as "seen", notify nothing
    python main.py users          add or edit a subscriber (own words + own webhook)
    python main.py telegram-ids   find Telegram chat ids to link to people
    python main.py test-discord   send a test message to the webhook
    python main.py test-control   check the Discord bot used for commands
    python main.py test-keywords  try your trigger words against some text
    python main.py dump GROUP     save page HTML + screenshot for troubleshooting
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from fbwatch.config import WEBHOOK_ENV_VAR, Config
from fbwatch.facebook import BrowserUnavailable
from fbwatch.matcher import KeywordMatcher
from fbwatch.models import load_groups
from fbwatch.notify import DiscordNotifier
from fbwatch.runner import Watcher

log = logging.getLogger("fbwatch")


def setup_logging(cfg: Config, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    # The Windows console defaults to a legacy code page, which turns Slovenian
    # characters into mojibake and can raise UnicodeEncodeError mid-log.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(cfg.log_path, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        print(f"warning: cannot write to log file {cfg.log_path}: {exc}", file=sys.stderr)

    # Playwright is chatty at DEBUG and drowns out our own lines.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def require_webhook(cfg: Config) -> bool:
    if cfg.webhook_configured:
        return True
    if (cfg.discord_webhook_url or "").strip():
        log.error(
            "config.json still has the example webhook URL in it. Replace "
            '"discord_webhook_url" with the real one from Discord '
            "(Edit Channel -> Integrations -> Webhooks -> Copy Webhook URL)."
        )
    else:
        log.error(
            "No Discord webhook configured. Put it in config.json as "
            '"discord_webhook_url", or set the %s environment variable.',
            WEBHOOK_ENV_VAR,
        )
    return False


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_login(cfg: Config, args) -> int:
    from fbwatch.facebook import FacebookScraper

    log.info("Opening a browser window - log in to Facebook there.")
    log.info("The session is saved to %s and reused from then on.", cfg.profile_path)
    with FacebookScraper(cfg, headless=False) as scraper:
        if scraper.is_logged_in():
            log.info("Already logged in - nothing to do.")
            return 0
        if scraper.interactive_login(timeout_seconds=args.timeout):
            log.info("Logged in. You can close the window; run `python main.py check` next.")
            return 0
    log.error("Timed out waiting for login.")
    return 1


def require_recipients(watcher: Watcher) -> bool:
    """At least one person must have both a destination and trigger words."""
    if watcher.active_subscribers:
        return True
    if not watcher.subscribers:
        log.error("No subscribers configured.")
        return False
    for sub in watcher.subscribers:
        log.error("'%s' is receiving nothing: %s", sub.name, sub.why_idle())
    log.error(
        "Set someone up with:  python main.py users <name> --webhook <url>   "
        "(or --telegram <chat id>), then add trigger words to their keywords file."
    )
    return False


def cmd_export_session(cfg: Config, args) -> int:
    """Save the logged-in session so it can be moved to a headless machine."""
    from fbwatch.facebook import FacebookScraper

    out = cfg.path(args.output)
    with FacebookScraper(cfg) as scraper:
        if not scraper.is_logged_in():
            log.error("Not logged in on this machine. Run:  python main.py login")
            return 2
        cookies = scraper.export_cookies()

    if not cookies:
        log.error("No Facebook cookies found in %s", cfg.profile_path)
        return 1

    out.write_text(json.dumps(cookies, indent=1), encoding="utf-8")
    log.info("Wrote %d cookie(s) to %s", len(cookies), out)
    log.warning(
        "That file IS your Facebook login - anyone holding it is signed in as you. "
        "Move it over a private channel, then delete it from both machines."
    )
    return 0


def cmd_import_session(cfg: Config, args) -> int:
    """Load a session exported elsewhere, so a headless machine can start."""
    from fbwatch.facebook import FacebookScraper

    src = cfg.path(args.input)
    if not src.exists():
        log.error("No such file: %s", src)
        log.error("Create it on a machine with a screen:  python main.py export-session")
        return 1
    try:
        cookies = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("%s is not valid JSON: %s", src, exc)
        return 1
    if not isinstance(cookies, list):
        log.error("%s should contain a list of cookies", src)
        return 1

    with FacebookScraper(cfg) as scraper:
        loaded = scraper.import_cookies(cookies)
        if not loaded:
            log.error("No usable Facebook cookies in %s", src)
            return 1
        if scraper.is_logged_in():
            log.info("Imported %d cookie(s) - the session works. You can delete %s now.",
                     loaded, src.name)
            return 0

    log.error(
        "Imported %d cookie(s) but Facebook still sees no session. The export may be "
        "stale - log in again on the other machine and re-export.", loaded,
    )
    return 2


def cmd_check(cfg: Config, args) -> int:
    from fbwatch.facebook import FacebookScraper, LoginRequired

    watcher = Watcher(cfg)
    watcher.reload_inputs()
    if not args.dry_run and not require_recipients(watcher):
        return 1
    log.info(
        "Checking %d group(s) for %d subscriber(s)...",
        len(watcher.groups), len(watcher.active_subscribers),
    )

    with FacebookScraper(cfg) as scraper:
        if not scraper.is_logged_in():
            log.error("Not logged in. Run:  python main.py login")
            return 2
        try:
            totals = watcher.run_cycle(scraper, notify=not args.dry_run)
        except LoginRequired as exc:
            log.error("Facebook wants a login: %s", exc)
            return 2

    log.info(
        "Done: %d post(s) read, %d new, %d matched, %d notification(s), %d error(s)",
        totals["seen"], totals["new"], totals["matched"], totals["sent"], totals["errors"],
    )
    return 1 if totals["errors"] and not totals["seen"] else 0


def cmd_run(cfg: Config, args) -> int:
    watcher = Watcher(cfg)
    watcher.reload_inputs()
    if not require_recipients(watcher):
        return 1
    return watcher.run_forever()


def cmd_seed(cfg: Config, args) -> int:
    """Mark everything currently visible as seen, without notifying."""
    from fbwatch.facebook import FacebookScraper, LoginRequired

    watcher = Watcher(cfg)
    watcher.reload_inputs()
    original = cfg.notify_on_first_run
    cfg.notify_on_first_run = False

    log.info("Seeding state from %d group(s) - no notifications will be sent.", len(watcher.groups))
    with FacebookScraper(cfg) as scraper:
        if not scraper.is_logged_in():
            log.error("Not logged in. Run:  python main.py login")
            return 2
        try:
            for group in watcher.groups:
                posts = scraper.scrape_group(group)
                for sub in watcher.subscribers:
                    if not sub.watches(group):
                        continue
                    for post in posts:
                        watcher.store.add(sub.name, group.slug, post.post_id)
                log.info("%s: recorded %d post(s)", group.name, len(posts))
        except LoginRequired as exc:
            log.error("Facebook wants a login: %s", exc)
            return 2
        finally:
            watcher.store.save(force=True)
            cfg.notify_on_first_run = original

    log.info("State written to %s. From now on only newer posts are notified.", cfg.state_path)
    return 0


def cmd_test_discord(cfg: Config, args) -> int:
    if not require_webhook(cfg):
        return 1
    notifier = DiscordNotifier(cfg)
    ok = notifier.send_text(
        "**fbwatch is connected.** You'll get a message here when a new group "
        "post matches your trigger words."
    )
    log.info("Test message sent." if ok else "Could not send the test message.")
    return 0 if ok else 1


def cmd_test_control(cfg: Config, args) -> int:
    """Verify the bot token and control channel, and say hello in it."""
    from fbwatch.control import DiscordControl

    watcher = Watcher(cfg)
    watcher.reload_inputs()
    control = DiscordControl(cfg, watcher)

    if not cfg.control_enabled:
        log.error('control_enabled is false in config.json - nothing to test.')
        return 1

    problem = control.verify()
    if problem:
        log.error("Discord control is not working: %s", problem)
        log.error(
            "Checklist: bot created at discord.com/developers, token pasted into "
            "discord_bot_token, bot invited to your server, and the channel id in "
            "discord_control_channel_id."
        )
        return 1

    control.reply(
        f":satellite: **fbwatch is listening here.** Type `{cfg.command_prefix} help` "
        "for the command list."
    )
    log.info("Control channel works - check Discord for the message.")
    log.info("Commands are read while `python main.py run` is going.")
    return 0


def cmd_test_keywords(cfg: Config, args) -> int:
    """Check a piece of text against everyone's trigger words."""
    watcher = Watcher(cfg)
    watcher.reload_inputs()

    subscribers = watcher.subscribers
    if args.user:
        from fbwatch.subscribers import find_subscriber

        only = find_subscriber(subscribers, args.user)
        if only is None:
            log.error("No subscriber called %r. Known: %s",
                      args.user, ", ".join(s.name for s in subscribers))
            return 1
        subscribers = [only]

    text = " ".join(args.text) if args.text else sys.stdin.read()
    if not text.strip():
        log.error("Give some text to test: python main.py test-keywords \"oddam sobo v Ljubljani\"")
        return 1

    print()
    print(f"  text: {text.strip()[:300]}")
    print()
    for sub in subscribers:
        matcher = sub.matcher
        result = matcher.match(text)
        notify = result.matched and bool(matcher.includes)
        reason = result.reason if matcher.includes else "no trigger words set"
        print(f"  {sub.name:<16} {'NOTIFY' if notify else '  --  '}   {reason}")
    print()
    return 0


def cmd_dump(cfg: Config, args) -> int:
    from fbwatch.facebook import FacebookScraper

    groups = load_groups(cfg.groups_path)
    if args.group:
        needle = args.group.lower()
        groups = [g for g in groups if needle in g.slug.lower() or needle in g.name.lower()]
        if not groups:
            log.error("No group in %s matches %r", cfg.groups_path.name, args.group)
            return 1
    target = groups[0]

    out_dir = cfg.path("debug")
    with FacebookScraper(cfg, headless=args.headless) as scraper:
        info = scraper.dump(target, out_dir)

    posts = info.pop("posts", [])
    (out_dir / f"dump_{target.slug}.json").write_text(
        json.dumps(posts, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("URL             : %s", info["url"])
    log.info("feed items      : %s", info["feed_items"])
    log.info("login marker    : %s", info["login_marker"] or "(none - session looks fine)")
    log.info("posts parsed    : %d", len(posts))
    for post in posts[:3]:
        log.info(
            "  - [%s] %s | %s",
            post.get("text_source"),
            (post.get("author") or "?")[:30],
            (post.get("text") or "")[:90].replace("\n", " "),
        )
    log.info("Wrote %s.{html,png,json}", info["files"])
    return 0


def cmd_list(cfg: Config, args) -> int:
    """Show what the config currently resolves to."""
    watcher = Watcher(cfg)
    watcher.reload_inputs()

    print(f"\nGroups ({len(watcher.groups)}) from {cfg.groups_path}:")
    for g in watcher.groups:
        print(f"  - {g.name:<40} {g.slug}")

    mode = "multi-user" if cfg.subscribers_path.exists() else "single-user (no subscribers.json)"
    print(f"\nSubscribers ({len(watcher.subscribers)}) — {mode}:")
    for sub in watcher.subscribers:
        marks = [m for m in ("admin" if sub.admin else "", "" if sub.enabled else "disabled") if m]
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        rules = len(sub.matcher.includes) if sub.matcher else 0
        where = watcher.dispatcher.describe(sub)
        print(f"  - {sub.name:<16}{suffix}")
        print(f"      rules   : {rules} from {sub.keywords_path(cfg).name}")
        print(f"      sends to: {where}")
        if sub.groups:
            print(f"      groups  : {', '.join(sub.groups)}")
        if not sub.deliverable:
            print(f"      IDLE    : {sub.why_idle()}")

    control_state = (
        "on" if (cfg.control_enabled and cfg.discord_bot_token and cfg.discord_control_channel_id)
        else "off (no bot token / channel id)"
    )
    telegram_state = "configured" if (cfg.telegram_bot_token or "").strip() else "off (no token)"
    print(f"\nDiscord commands : {control_state}")
    print(f"Telegram         : {telegram_state}")
    print(f"State            : {cfg.state_path} ({watcher.store.count()} post ids remembered)")
    print(f"Profile          : {cfg.profile_path}")
    print()
    return 0


def cmd_users(cfg: Config, args) -> int:
    """Add or inspect subscribers from the command line."""
    from fbwatch.subscribers import (
        Subscriber,
        ensure_keywords_file,
        find_subscriber,
        save_subscribers,
    )

    watcher = Watcher(cfg)
    watcher.reload_inputs()

    if not args.name:
        return cmd_list(cfg, args)

    existing = find_subscriber(watcher.subscribers, args.name)
    if existing is None:
        sub = Subscriber(name=args.name)
        subscribers = list(watcher.subscribers) + [sub]
        log.info("Adding subscriber '%s'", args.name)
    else:
        sub = existing
        subscribers = watcher.subscribers

    if args.webhook is not None:
        sub.discord_webhook_url = args.webhook
    if args.channel is not None:
        sub.discord_channel_id = args.channel
    if args.telegram is not None:
        sub.telegram_chat_id = args.telegram
    if args.discord_id is not None:
        sub.discord_user_id = args.discord_id
    if args.admin:
        sub.admin = True
    if args.disable:
        sub.enabled = False

    save_subscribers(cfg, subscribers)
    path = ensure_keywords_file(cfg, sub)
    sub.load_matcher(cfg)

    log.info("Saved %s", cfg.subscribers_path)
    log.info("Trigger words for '%s': %s", sub.name, path)
    if not sub.deliverable:
        log.warning("'%s' is receiving nothing: %s", sub.name, sub.why_idle())
    return 0


def cmd_telegram_ids(cfg: Config, args) -> int:
    """List Telegram chat ids of people who messaged the bot."""
    from fbwatch.telegram import recent_chats, verify_token

    ok, detail = verify_token(cfg)
    if not ok:
        log.error("Telegram bot not usable: %s", detail)
        log.error(
            "Create a bot by messaging @BotFather on Telegram, then put the token in "
            "config.json as \"telegram_bot_token\"."
        )
        return 1
    log.info("Connected as @%s", detail)

    chats = recent_chats(cfg)
    if not chats:
        log.info(
            "Nobody has messaged the bot yet. Ask each person to open @%s and send /start, "
            "then run this again. (Telegram blocks bots from messaging strangers first.)",
            detail,
        )
        return 0

    print("\nRecent Telegram chats:")
    for chat in chats:
        handle = f" (@{chat['username']})" if chat["username"] else ""
        print(f"  {chat['chat_id']:<16} {chat['name']}{handle}")
    print("\nLink one with:")
    print("  python main.py users <name> --telegram <chat id>\n")
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fbwatch",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-c", "--config", type=Path, default=None, help="path to config.json")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="log in to Facebook once and save the session")
    p.add_argument("--timeout", type=int, default=600, help="seconds to wait (default 600)")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("export-session", help="save the login for a headless machine")
    p.add_argument("--output", default="session.json", help="where to write it")
    p.set_defaults(func=cmd_export_session)

    p = sub.add_parser("import-session", help="load a login exported elsewhere")
    p.add_argument("--input", default="session.json", help="the exported file")
    p.set_defaults(func=cmd_import_session)

    p = sub.add_parser("check", help="poll every group once")
    p.add_argument("--dry-run", action="store_true", help="log matches instead of sending them")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("run", help="poll forever")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("seed", help="mark current posts as seen without notifying")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("test-discord", help="send a test message to the webhook")
    p.set_defaults(func=cmd_test_discord)

    p = sub.add_parser("test-control", help="check the Discord bot token and channel")
    p.set_defaults(func=cmd_test_control)

    p = sub.add_parser("test-keywords", help="test trigger words against some text")
    p.add_argument("text", nargs="*", help="text to test (or pipe it on stdin)")
    p.add_argument("--user", help="test only this subscriber's rules")
    p.set_defaults(func=cmd_test_keywords)

    p = sub.add_parser("dump", help="save page HTML/screenshot for troubleshooting")
    p.add_argument("group", nargs="?", help="group name or id (default: the first one)")
    p.add_argument("--headless", action="store_true", default=False, help="don't show the window")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("list", help="show the resolved groups, subscribers and paths")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("users", help="add or edit a subscriber")
    p.add_argument("name", nargs="?", help="subscriber name (omit to list everyone)")
    p.add_argument("--webhook", help="their Discord webhook URL")
    p.add_argument("--channel", help="Discord channel id to post into via the bot")
    p.add_argument("--telegram", help="their Telegram chat id")
    p.add_argument("--discord-id", help="their Discord user id, so they can manage their own rules")
    p.add_argument("--admin", action="store_true", help="let them manage people and groups")
    p.add_argument("--disable", action="store_true", help="stop notifying them")
    p.set_defaults(func=cmd_users)

    p = sub.add_parser("telegram-ids", help="list Telegram chat ids that messaged the bot")
    p.set_defaults(func=cmd_telegram_ids)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = Config.load(args.config)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    setup_logging(cfg, args.verbose)
    try:
        return args.func(cfg, args)
    except BrowserUnavailable as exc:
        for line in str(exc).splitlines():
            log.error("%s", line)
        return 3
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1
    except ValueError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
