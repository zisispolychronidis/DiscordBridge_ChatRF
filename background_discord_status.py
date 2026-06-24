"""
ChatRF Discord Status Dashboard Module
========================================
Maintains a live-updating embed in a Discord channel that shows the current
state of the repeater. The embed is created on first run and then EDITED in
place every `update_interval` seconds - so the channel stays clean with just
one pinned status message.

Dashboard shows:
  - 🟢/🔴 Repeater active (someone transmitting)
  - 🤖 AI Mode on/off
  - Last DTMF command received
  - Last RF transcript (from DiscordBridgeModule if running)
  - Uptime
  - System stats (CPU / RAM) if psutil is available

Dependencies:
    pip install requests psutil  (psutil is optional)

Configuration (config/settings/config.ini):
    [DiscordStatus]
    bot_token        = YOUR_BOT_TOKEN
    status_channel   = CHANNEL_ID_FOR_THE_DASHBOARD
    update_interval  = 5
    callsign         = N0CALL      (falls back to config.CALLSIGN)
"""

from modules.base import BackgroundServiceModule
import time
import requests

# ── Discord REST helpers ──────────────────────────────────────────────────────

DISCORD_API = "https://discord.com/api/v10"

def _discord_post(token, endpoint, payload):
    r = requests.post(
        f"{DISCORD_API}{endpoint}",
        json=payload,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()

def _discord_patch(token, endpoint, payload):
    r = requests.patch(
        f"{DISCORD_API}{endpoint}",
        json=payload,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ── Module ────────────────────────────────────────────────────────────────────

class DiscordStatusModule(BackgroundServiceModule):
    name        = "Discord Status"
    version     = "1.0.0"
    description = "Live-updating repeater status dashboard in Discord"

    UPDATE_INTERVAL = 5  # seconds

    def initialize(self):
        cfg = self.config.config

        self.bot_token      = cfg.get("DiscordStatus", "bot_token",       fallback="")
        self.status_channel = cfg.get("DiscordStatus", "status_channel",  fallback="")
        self.UPDATE_INTERVAL = cfg.getint("DiscordStatus", "update_interval", fallback=self.UPDATE_INTERVAL)
        self.callsign       = cfg.get(
            "DiscordStatus", "callsign",
            fallback=getattr(self.config, "CALLSIGN", "N0CALL"),
        )

        if not self.bot_token or not self.status_channel:
            self.logger.error("[DiscordStatus] bot_token or status_channel not set - disabled.")
            self.enabled = False
            return

        self._dashboard_message_id = None
        self._start_time = time.time()

        # Track last DTMF for display (updated by _intercept_dtmf hook if wired up)
        self._last_dtmf = "-"

        self._last_transcript = "-"

        # Attempt to import psutil for system stats (optional)
        try:
            import psutil
            self._psutil = psutil
        except ImportError:
            self._psutil = None

        self.logger.info("[DiscordStatus] Initialized.")

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        while not self._stop_event.is_set():
            try:
                self._refresh_last_transcript()
                self._refresh_last_dtmf()
                self._update_dashboard()
            except Exception as e:
                self.logger.error(f"[DiscordStatus] Error updating dashboard: {e}")

            self._stop_event.wait(self.UPDATE_INTERVAL)

    # ── Dashboard helpers ─────────────────────────────────────────────────────

    def _refresh_last_transcript(self):
        """Check shared_data for the most recent RF transcript."""
        shared = getattr(self.repeater, "shared_data", {})
        t = shared.get("last_transcript")
        if t:
            self._last_transcript = t[:200] + ("…" if len(t) > 200 else "")

    def _refresh_last_dtmf(self):
        """Check shared_data for the most recent DTMF command."""
        shared = getattr(self.repeater, "shared_data", {})
        self._last_dtmf = shared.get("last_dtmf")

    def _build_embed(self) -> dict:
        """Build the Discord embed payload representing the current status."""
        repeater  = self.repeater
        active    = getattr(repeater, "talking", False)
        ai_mode   = getattr(repeater, "ai_mode_running", False)
        uptime_s  = int(time.time() - self._start_time)
        uptime    = self._fmt_uptime(uptime_s)

        status_icon  = "🟢" if active  else "⚪"
        ai_icon      = "🤖" if ai_mode else "💤"
        active_label = "Active - transmitting" if active else "Idle"
        ai_label     = "AI Mode ON"  if ai_mode else "AI Mode OFF"

        fields = [
            {"name": f"{status_icon}  Repeater", "value": active_label,      "inline": True},
            {"name": f"{ai_icon}  AI",           "value": ai_label,           "inline": True},
            {"name": "⏱  Uptime",                "value": uptime,             "inline": True},
            {"name": "🔢  Last DTMF",             "value": f"`{self._last_dtmf}`",  "inline": True},
            {"name": "📝  Last RF Transcript",    "value": self._last_transcript,   "inline": False},
        ]

        if self._psutil:
            try:
                cpu = self._psutil.cpu_percent(interval=None)
                ram = self._psutil.virtual_memory().percent
                fields.append({
                    "name": "💻  System",
                    "value": f"CPU {cpu:.0f}%  |  RAM {ram:.0f}%",
                    "inline": True,
                })
            except Exception:
                pass

        color = 0x2ECC71 if active else 0x95A5A6  # green when active, grey when idle

        return {
            "embeds": [{
                "title": f"📡  {self.callsign} - ChatRF Repeater Status",
                "color": color,
                "fields": fields,
                "footer": {"text": f"Last updated"},
                "timestamp": self._iso_now(),
            }]
        }

    def _update_dashboard(self):
        payload = self._build_embed()

        if self._dashboard_message_id is None:
            # First run - create the message
            msg = _discord_post(
                self.bot_token,
                f"/channels/{self.status_channel}/messages",
                payload,
            )
            self._dashboard_message_id = msg["id"]
            self.logger.info(f"[DiscordStatus] Dashboard created (message {self._dashboard_message_id}).")
        else:
            # Update in-place
            _discord_patch(
                self.bot_token,
                f"/channels/{self.status_channel}/messages/{self._dashboard_message_id}",
                payload,
            )

    # ── Utility ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_uptime(seconds: int) -> str:
        h, rem = divmod(seconds, 3600)
        m, s   = divmod(rem, 60)
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    @staticmethod
    def _iso_now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    # ── Public hook: called by DTMF handler to track last command ─────────────

    def record_dtmf(self, key: str):
        """
        Optional integration: call this from a DTMF module or the repeater core
        to keep the dashboard's 'Last DTMF' field up to date.

        Example from any DTMFModule.handle_command():
            status_mod = self.repeater.module_manager.get_module("Discord Status")
            if status_mod:
                status_mod.record_dtmf(self.dtmf_command)
        """
        self._last_dtmf = key

    def cleanup(self):
        self.logger.info("[DiscordStatus] Shutting down.")
