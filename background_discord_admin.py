"""
ChatRF Discord Admin Module
=============================
Lets trusted Discord users control the repeater remotely via a dedicated
admin channel. Commands are prefixed with ! and only accepted from Discord
user IDs listed in the whitelist.

Commands:
  !ping                        - Repeater speaks a live confirmation over the air
  !say <text>                  - Speak any text via piper-tts over the air
  !play <attach>               - Play an uploaded WAV or MP3 file over the air
  !logs [repeater|aimode] [n]  - Post the last n lines of a log file (default: repeater, 20 lines)
  !errors [repeater|aimode] [n]- Post the last n lines of an error log file
  !modules                     - List all loaded modules with status
  !sayat <HH:MM> <text>        - Schedule a one-off TTS announcement at a specific time
  !id                          - Immediately transmit the station CW ID
  !restart                     - Restart the Python process (os.execv)
  !reboot                      - Reboot the host system (requires sudo)

Configuration (config/settings/config.ini):
    [DiscordAdmin]
    bot_token      = YOUR_BOT_TOKEN          ; can share with DiscordBridge
    admin_channel  = CHANNEL_ID              ; the #admin-commands channel
    audit_channel  = CHANNEL_ID              ; optional, log every command here
    trusted_users  = 123456789,987654321     ; comma-separated Discord user IDs
    poll_interval  = 3                       ; seconds between polls

Dependencies:
    pip install requests
"""

from modules.base import BackgroundServiceModule
import os
import sys
import time
import tempfile
import threading
from datetime import datetime
import requests

DISCORD_API = "https://discord.com/api/v10"


# ── Discord REST helpers ──────────────────────────────────────────────────────

def _get(token, endpoint):
    r = requests.get(
        f"{DISCORD_API}{endpoint}",
        headers={"Authorization": f"Bot {token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()

def _post(token, endpoint, payload=None, files=None):
    headers = {"Authorization": f"Bot {token}"}
    if files:
        r = requests.post(f"{DISCORD_API}{endpoint}", headers=headers, data=payload or {}, files=files, timeout=15)
    else:
        headers["Content-Type"] = "application/json"
        r = requests.post(f"{DISCORD_API}{endpoint}", headers=headers, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()

def _reply(token, channel_id, content):
    """Post a plain text message to a channel, splitting if over 2000 chars."""
    # Discord message limit is 2000 characters
    for chunk in [content[i:i+1990] for i in range(0, len(content), 1990)]:
        _post(token, f"/channels/{channel_id}/messages", {"content": chunk})


# ── Module ────────────────────────────────────────────────────────────────────

class DiscordAdminModule(BackgroundServiceModule):
    name        = "Discord Admin"
    version     = "1.1.0"
    description = "Remote repeater control via trusted Discord users"

    POLL_INTERVAL = 3

    def initialize(self):
        cfg = self.config.config

        self.bot_token     = cfg.get("DiscordAdmin", "bot_token",     fallback="")
        self.admin_channel = cfg.get("DiscordAdmin", "admin_channel", fallback="")
        self.audit_channel = cfg.get("DiscordAdmin", "audit_channel", fallback="")
        self.POLL_INTERVAL = cfg.getint("DiscordAdmin", "poll_interval", fallback=self.POLL_INTERVAL)

        # Known log files
        self.LOG_FILES = {
            "repeater": "logs/repeater.log",
            "aimode":   "logs/aimode.log",
        }
        self.ERROR_FILES = {
            "repeater": "logs/repeater_errors.log",
            "aimode":   "logs/aimode_errors.log",
        }

        # Scheduled announcements: list of (datetime, text)
        self._scheduled: list[tuple[datetime, str]] = []
        self._schedule_lock = threading.Lock()

        raw_ids = cfg.get("DiscordAdmin", "trusted_users", fallback="")
        self.trusted_users = {uid.strip() for uid in raw_ids.split(",") if uid.strip()}

        if not self.bot_token or not self.admin_channel:
            self.logger.error("[DiscordAdmin] bot_token or admin_channel not set - disabled.")
            self.enabled = False
            return

        if not self.trusted_users:
            self.logger.warning("[DiscordAdmin] No trusted_users configured - all commands will be rejected.")

        # Seed last message id so we don't replay history on startup
        self._last_message_id = None
        self._seed_last_message_id()

        self.logger.info(f"[DiscordAdmin] Ready. Trusted users: {self.trusted_users}")

    def _seed_last_message_id(self):
        try:
            msgs = _get(self.bot_token, f"/channels/{self.admin_channel}/messages?limit=1")
            if msgs:
                self._last_message_id = msgs[0]["id"]
        except Exception as e:
            self.logger.warning(f"[DiscordAdmin] Could not seed message id: {e}")

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        while not self._stop_event.is_set():
            try:
                self._poll_commands()
                self._fire_scheduled()
            except Exception as e:
                self.logger.error(f"[DiscordAdmin] Poll error: {e}")
            self._stop_event.wait(self.POLL_INTERVAL)

    def _poll_commands(self):
        endpoint = f"/channels/{self.admin_channel}/messages?limit=10"
        if self._last_message_id:
            endpoint += f"&after={self._last_message_id}"

        try:
            messages = _get(self.bot_token, endpoint)
        except Exception as e:
            self.logger.error(f"[DiscordAdmin] Failed to fetch messages: {e}")
            return

        if not messages:
            return

        for msg in reversed(messages):  # oldest first
            if msg.get("author", {}).get("bot"):
                continue
            author_id  = msg["author"]["id"]
            author_tag = msg["author"].get("username", "unknown")
            content    = msg.get("content", "").strip()

            if not content.startswith("!"):
                continue

            if author_id not in self.trusted_users:
                self.logger.warning(f"[DiscordAdmin] Rejected command from untrusted user {author_tag} ({author_id}): {content!r}")
                _reply(self.bot_token, self.admin_channel, f"⛔ {author_tag}, you are not in the trusted users list.")
                continue

            self.logger.info(f"[DiscordAdmin] Command from {author_tag}: {content!r}")
            self._audit(f"🛠️ **{author_tag}** ran: `{content}`")

            # Dispatch in a thread so the poll loop isn't blocked
            threading.Thread(
                target=self._dispatch,
                args=(msg, content, author_tag),
                daemon=True,
            ).start()

        self._last_message_id = messages[0]["id"]

    # ── Command dispatcher ────────────────────────────────────────────────────

    def _dispatch(self, msg, content, author_tag):
        parts   = content.split(None, 1)
        command = parts[0].lower()
        args    = parts[1].strip() if len(parts) > 1 else ""

        try:
            if command == "!ping":
                self._cmd_ping()
            elif command == "!say":
                self._cmd_say(args)
            elif command == "!play":
                self._cmd_play(msg)
            elif command == "!logs":
                self._cmd_logs(args)
            elif command == "!errors":
                self._cmd_errors(args)
            elif command == "!modules":
                self._cmd_modules()
            elif command == "!sayat":
                self._cmd_sayat(args)
            elif command == "!id":
                self._cmd_id()
            elif command == "!restart":
                self._cmd_restart()
            elif command == "!reboot":
                self._cmd_reboot()
            elif command == "!help":
                _reply(self.bot_token, self.admin_channel, """**The available commands are:**

`!ping`: Waits for channel silence, then speaks a live confirmation over the air
`!say`: Speaks any text via piper-tts
`!play`: Downloads the attached WAV or MP3 and plays it
`!logs`: Posts the last n lines (default 20, max 100) from `repeater.log` or `aimode.log`
`!errors`: Posts the last n lines (default 20, max 100) from `repeater_errors.log` or `aimode_errors.log`
`!modules`: Lists all loaded modules
`!sayat`: Schedules a one-off TTS announcement at a specific time
`!id`: Immediately transmit the station CW ID
`!restart`: Speaks a warning, then does `os.execv` to restart the Python process in-place
`!reboot`: Speaks a warning, then runs `sudo reboot`
`!help`: Lists all available commands""")
            else:
                _reply(self.bot_token, self.admin_channel,
                       f"❓ Unknown command `{command}`. Use `!help` to see available commands.")
        except Exception as e:
            self.logger.error(f"[DiscordAdmin] Error executing {command}: {e}", exc_info=True)
            _reply(self.bot_token, self.admin_channel, f"💥 Error running `{command}`: {e}")

    # ── Commands ──────────────────────────────────────────────────────────────

    def _cmd_ping(self):
        """Speak a live confirmation over the air."""
        self._wait_for_silence()
        callsign = getattr(self.config, "CALLSIGN", "this repeater")
        self.repeater.play_tone(800, 1, 1)
        self.repeater.speak_with_piper(f"Ο σταθμός {callsign} είναι ενεργός και ανταποκρίνεται.")
        _reply(self.bot_token, self.admin_channel, "✅ Ping spoken over the air.")

    def _cmd_say(self, text):
        """Speak arbitrary text over the air."""
        if not text:
            _reply(self.bot_token, self.admin_channel, "⚠️ Usage: `!say <text>`")
            return
        self._wait_for_silence()
        self.repeater.speak_with_piper(text)
        _reply(self.bot_token, self.admin_channel, f"✅ Spoken: _{text}_")

    def _cmd_play(self, msg):
        """
        Download the first attachment from the message and play it.
        Accepts WAV or MP3.
        """
        attachments = msg.get("attachments", [])
        if not attachments:
            _reply(self.bot_token, self.admin_channel,
                   "⚠️ Please attach a WAV or MP3 file to your `!play` message.")
            return

        attachment = attachments[0]
        filename   = attachment.get("filename", "")
        url        = attachment.get("url", "")
        ext        = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        if ext not in ("wav", "mp3"):
            _reply(self.bot_token, self.admin_channel,
                   f"⚠️ Unsupported file type `.{ext}`. Please upload a WAV or MP3.")
            return

        # Download to a temp file
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            _reply(self.bot_token, self.admin_channel, f"💥 Failed to download attachment: {e}")
            return

        tmp = tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False)
        tmp.write(r.content)
        tmp.close()

        try:
            self._wait_for_silence()
            self.repeater.play_audio(tmp.name)
            _reply(self.bot_token, self.admin_channel, f"✅ Played `{filename}` over the air.")
        except Exception as e:
            _reply(self.bot_token, self.admin_channel, f"💥 Playback error: {e}")
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass

    def _cmd_logs(self, args):
        """!logs [repeater|aimode] [n] - tail a log file."""
        parts  = args.split()
        target = "repeater"
        n      = 20

        for part in parts:
            if part.lower() in self.LOG_FILES:
                target = part.lower()
            elif part.isdigit():
                n = max(1, min(int(part), 100))

        self._tail_file(self.LOG_FILES[target], n, "📋")

    def _cmd_errors(self, args):
        """!errors [repeater|aimode] [n] - tail an error log file."""
        parts  = args.split()
        target = "repeater"
        n      = 20

        for part in parts:
            if part.lower() in self.ERROR_FILES:
                target = part.lower()
            elif part.isdigit():
                n = max(1, min(int(part), 100))

        self._tail_file(self.ERROR_FILES[target], n, "🚨")

    def _tail_file(self, path: str, n: int, icon: str):
        """Read the last n lines of a file and post them to Discord."""
        if not os.path.exists(path):
            _reply(self.bot_token, self.admin_channel, f"⚠️ File not found: `{path}`")
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = "".join(lines[-n:]).strip()
        if not tail:
            _reply(self.bot_token, self.admin_channel, f"{icon} `{path}` is empty.")
            return
        _reply(self.bot_token, self.admin_channel,
               f"{icon} Last {n} lines of `{path}`:\n```\n{tail}\n```")

    def _cmd_modules(self):
        """!modules - list all loaded modules grouped by type."""
        try:
            mm = self.repeater.module_manager
            categories = {
                "DTMF":       mm.dtmf_modules,
                "Periodic":   mm.periodic_modules,
                "Background": mm.service_modules,
                "Event":      mm.event_modules,
            }
        except Exception as e:
            _reply(self.bot_token, self.admin_channel, f"💥 Could not read module list: {e}")
            return

        lines = []
        total = 0
        for category, modules in categories.items():
            if not modules:
                continue
            lines.append(f"\n**{category}**")
            # For DTMF, pair each module with its command key
            if category == "DTMF":
                items = mm.dtmf_modules.items()
            else:
                items = ((None, m) for m in modules)
            for key, m in items:
                enabled = "✅" if getattr(m, "enabled", True) else "❌"
                name    = getattr(m, "name",        "Unknown")
                version = getattr(m, "version",     "?")
                desc    = getattr(m, "description", "")
                key_str = f" [`{key}`]" if key is not None else ""
                lines.append(f"\n{enabled} **{name}**{key_str} v{version} - {desc}")
                total += 1

        if not total:
            _reply(self.bot_token, self.admin_channel, "📦 No modules loaded.")
            return

        _reply(self.bot_token, self.admin_channel,
               f"📦 **Loaded modules ({total}):**{''.join(lines)}")

    def _cmd_sayat(self, args):
        """!sayat <HH:MM> <text> - schedule a one-off TTS announcement."""
        parts = args.split(None, 1)
        if len(parts) < 2:
            _reply(self.bot_token, self.admin_channel,
                   "⚠️ Usage: `!sayat HH:MM <text>`  (24-hour time, today or tomorrow)")
            return

        time_str, text = parts[0], parts[1].strip()
        try:
            now       = datetime.now()
            scheduled = datetime.strptime(time_str, "%H:%M").replace(
                year=now.year, month=now.month, day=now.day
            )
            # If the time has already passed today, schedule for tomorrow
            if scheduled <= now:
                from datetime import timedelta
                scheduled += timedelta(days=1)
        except ValueError:
            _reply(self.bot_token, self.admin_channel,
                   f"⚠️ Invalid time `{time_str}`. Use 24-hour HH:MM format (e.g. `14:30`).")
            return

        with self._schedule_lock:
            self._scheduled.append((scheduled, text))

        _reply(self.bot_token, self.admin_channel,
               f"⏰ Scheduled at **{scheduled.strftime('%H:%M')}**: _{text}_")
        self.logger.info(f"[DiscordAdmin] Scheduled announcement at {scheduled}: {text!r}")

    def _fire_scheduled(self):
        """Check and fire any due scheduled announcements."""
        now = datetime.now()
        with self._schedule_lock:
            due      = [item for item in self._scheduled if item[0] <= now]
            self._scheduled = [item for item in self._scheduled if item[0] > now]

        for scheduled_time, text in due:
            self.logger.info(f"[DiscordAdmin] Firing scheduled announcement: {text!r}")
            try:
                self._wait_for_silence()
                self.repeater.speak_with_piper(text)
                _reply(self.bot_token, self.admin_channel,
                       f"⏰ Scheduled announcement spoken: _{text}_")
            except Exception as e:
                self.logger.error(f"[DiscordAdmin] Failed to speak scheduled announcement: {e}")

    def _cmd_id(self):
        """!id - immediately transmit the station CW ID."""
        callsign = getattr(self.config, "CALLSIGN", None)
        if not callsign:
            _reply(self.bot_token, self.admin_channel,
                   "⚠️ No callsign configured in `[CW] callsign`.")
            return
        self._wait_for_silence()
        try:
            self.repeater.play_text_morse(callsign)
            _reply(self.bot_token, self.admin_channel,
                   f"✅ CW ID transmitted: `{callsign}`")
            self.logger.info(f"[DiscordAdmin] CW ID transmitted: {callsign}")
        except Exception as e:
            _reply(self.bot_token, self.admin_channel, f"💥 CW ID failed: {e}")

    def _cmd_restart(self):
        """Restart the Python process in-place using os.execv."""
        _reply(self.bot_token, self.admin_channel, "🔄 Restarting repeater process…")
        self._wait_for_silence()
        self.repeater.speak_with_piper("Επανεκκίνηση του τσατ αρέφ, παρακαλώ περιμένετε.")
        time.sleep(2)
        self.logger.info("[DiscordAdmin] Restarting Python process via os.execv.")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _cmd_reboot(self):
        """Reboot the host system."""
        _reply(self.bot_token, self.admin_channel, "🔁 Rebooting system…")
        self._wait_for_silence()
        self.repeater.speak_with_piper("Επανεκκίνηση του συστήματος, παρακαλώ περιμένετε.")
        time.sleep(2)
        self.logger.info("[DiscordAdmin] Issuing system reboot.")
        os.system("sudo reboot")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _wait_for_silence(self, timeout=30.0):
        """Block until the repeater is idle (or timeout)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not getattr(self.repeater, "talking", False):
                return
            time.sleep(0.5)
        self.logger.warning("[DiscordAdmin] Timed out waiting for channel silence.")

    def _audit(self, message):
        """Post to the optional audit channel."""
        if not self.audit_channel:
            return
        try:
            _post(self.bot_token, f"/channels/{self.audit_channel}/messages", {"content": message})
        except Exception as e:
            self.logger.warning(f"[DiscordAdmin] Audit log failed: {e}")

    def cleanup(self):
        self.logger.info("[DiscordAdmin] Shutting down.")