"""
ChatRF Discord Bridge Module
============================
Bridges RF transmissions to a Discord channel and allows Discord users
to send messages that get spoken over the repeater.

Features:
  - RF → Discord: Posts transcripts queued by event_discord_rf.py to Discord
  - Discord → RF: Polls a Discord channel for new messages and speaks them via piper-tts

Dependencies:
    pip install requests

Configuration (config/settings/config.ini):
    [DiscordBridge]
    bot_token        = YOUR_BOT_TOKEN
    rf_log_channel   = CHANNEL_ID_FOR_RF_TRANSCRIPTS
    to_rf_channel    = CHANNEL_ID_FOR_MESSAGES_TO_SEND_OVER_AIR
    poll_interval    = 5
"""

from modules.base import BackgroundServiceModule
import time
import requests


# ── Discord REST helpers ──────────────────────────────────────────────────────

def _discord_get(token, endpoint):
    r = requests.get(
        f"https://discord.com/api/v10{endpoint}",
        headers={"Authorization": f"Bot {token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()

def _discord_post(token, endpoint, payload):
    r = requests.post(
        f"https://discord.com/api/v10{endpoint}",
        json=payload,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ── Module ────────────────────────────────────────────────────────────────────

class DiscordBridgeModule(BackgroundServiceModule):
    name        = "Discord Bridge"
    version     = "1.2.0"
    description = "Two-way bridge between RF transmissions and a Discord server"

    POLL_INTERVAL = 5  # seconds between Discord polls

    def initialize(self):
        cfg = self.config.config

        self.bot_token      = cfg.get("DiscordBridge", "bot_token",      fallback="")
        self.rf_log_channel = cfg.get("DiscordBridge", "rf_log_channel", fallback="")
        self.to_rf_channel  = cfg.get("DiscordBridge", "to_rf_channel",  fallback="")
        self.POLL_INTERVAL  = cfg.getint("DiscordBridge", "poll_interval", fallback=self.POLL_INTERVAL)

        if not self.bot_token:
            self.logger.error("[DiscordBridge] No bot_token set - module disabled.")
            self.enabled = False
            return

        # Track last processed Discord message to avoid replaying old ones on startup
        self._last_message_id = None
        self._init_last_message_id()

        self.logger.info("[DiscordBridge] Initialized.")

    def _init_last_message_id(self):
        """Seed _last_message_id so we don't replay old messages on startup."""
        try:
            messages = _discord_get(
                self.bot_token,
                f"/channels/{self.to_rf_channel}/messages?limit=1",
            )
            if messages:
                self._last_message_id = messages[0]["id"]
        except Exception as e:
            self.logger.warning(f"[DiscordBridge] Could not seed last message id: {e}")

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        while not self._stop_event.is_set():
            try:
                self._flush_transcript_to_discord()
                self._poll_discord_to_rf()
            except Exception as e:
                self.logger.error(f"[DiscordBridge] Loop error: {e}")
            self._stop_event.wait(self.POLL_INTERVAL)

    # ── RF → Discord ──────────────────────────────────────────────────────────

    def _flush_transcript_to_discord(self):
        """Post any transcript queued by event_discord_rf.py to Discord."""
        shared = getattr(self.repeater, "shared_data", {})
        transcript = shared.pop("pending_discord_transcript", None)
        if not transcript:
            return
        self.logger.info(f"[DiscordBridge] Posting transcript: {transcript!r}")
        if not hasattr(self.repeater, 'shared_data'):
            self.repeater.shared_data = {}
        self.repeater.shared_data['last_transcript'] = transcript.strip()
        try:
            _discord_post(
                self.bot_token,
                f"/channels/{self.rf_log_channel}/messages",
                {"content": f"📻 **RF transmission:** {transcript}"},
            )
        except Exception as e:
            self.logger.error(f"[DiscordBridge] Failed to post transcript: {e}")

    # ── Discord → RF ──────────────────────────────────────────────────────────

    def _poll_discord_to_rf(self):
        """Fetch new messages in the to-RF channel and speak each one."""
        if not self.to_rf_channel:
            return
        try:
            endpoint = f"/channels/{self.to_rf_channel}/messages?limit=10"
            if self._last_message_id:
                endpoint += f"&after={self._last_message_id}"
            messages = _discord_get(self.bot_token, endpoint)
        except Exception as e:
            self.logger.error(f"[DiscordBridge] Discord poll failed: {e}")
            return

        if not messages:
            return

        # Discord returns newest-first; process oldest-first
        for msg in reversed(messages):
            if msg.get("author", {}).get("bot"):
                continue
            content = msg.get("content", "").strip()
            if not content:
                continue
            author = msg["author"].get("username", "Unknown")
            self.logger.info(f"[DiscordBridge] Speaking message from {author}: {content!r}")
            self._wait_for_silence()
            try:
                self.repeater.speak_with_piper(f"Μήνυμα στο ντίσκορντ από {author}: {content}")
            except Exception as e:
                self.logger.error(f"[DiscordBridge] speak_with_piper error: {e}")

        self._last_message_id = messages[0]["id"]  # newest

    def _wait_for_silence(self, timeout=30.0):
        """Block until the repeater is not transmitting (or timeout)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not getattr(self.repeater, "talking", False):
                return
            time.sleep(0.5)
        self.logger.warning("[DiscordBridge] Timed out waiting for silence before speaking.")

    def cleanup(self):
        self.logger.info("[DiscordBridge] Shutting down.")
