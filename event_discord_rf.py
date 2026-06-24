"""
ChatRF Discord RF Event Module
================================
Hooks into repeater transmission events to drive recording and transcription.

How it works:
  - repeater.py pushes every raw audio chunk into shared_data['audio_buffer']
    while the signal is above threshold.
  - on_transmission_start() clears that buffer so we start fresh.
  - on_transmission_end() drains the buffer, writes a WAV, transcribes it
    with Whisper, and queues the result for the Discord bridge to post.

Requires background_discord_bridge.py to also be in modules/.
"""

from modules.base import EventModule
import threading
import tempfile
import wave
import contextlib
import os


class DiscordRFEventModule(EventModule):
    name        = "Discord RF Event"
    version     = "1.2.0"
    description = "Records transmissions via shared buffer and queues Whisper transcripts for Discord"

    def initialize(self):
        self._bridge = None  # resolved lazily on first use
        self.logger.info("[DiscordRFEvent] Initialized.")

    # ── Event hooks ───────────────────────────────────────────────────────────

    def on_transmission_start(self):
        """Clear the audio buffer so we only capture this transmission."""
        if not hasattr(self.repeater, 'shared_data'):
            self.repeater.shared_data = {}
        self.repeater.shared_data['audio_buffer'] = []
        self.logger.debug("[DiscordRFEvent] Audio buffer cleared - ready to record.")

    def on_transmission_end(self):
        """
        Drain the buffer the main loop has been filling and transcribe it.
        Run in a background thread so we don't block the repeater.
        """
        # Snapshot and clear the buffer immediately so the next transmission
        # starts fresh even if transcription takes a while.
        shared = getattr(self.repeater, 'shared_data', {})
        frames = shared.pop('audio_buffer', [])

        if not frames:
            self.logger.debug("[DiscordRFEvent] No audio frames captured.")
            return

        threading.Thread(
            target=self._transcribe_and_queue,
            args=(frames,),
            daemon=True,
        ).start()

    # ── Transcription ─────────────────────────────────────────────────────────

    def _transcribe_and_queue(self, frames: list):
        audio_path = None
        try:
            audio_path = self._write_wav(frames)
            if audio_path is None:
                return

            with contextlib.closing(wave.open(audio_path,'r')) as f:
                frames = f.getnframes()
                rate = f.getframerate()
                duration = frames / float(rate)

            if duration < 3:
                self.logger.debug("Audio duration too short for transcription.")
                return

            self.logger.debug(f"[DiscordRFEvent] Transcribing {audio_path}…")
            transcript = self.repeater.transcribe_audio_whisper(audio_path)

            if not transcript or not transcript.strip():
                self.logger.debug("[DiscordRFEvent] Empty transcript - nothing to post.")
                return

            self.logger.info(f"[DiscordRFEvent] Transcript ready: {transcript!r}")

            if not hasattr(self.repeater, 'shared_data'):
                self.repeater.shared_data = {}
            self.repeater.shared_data['pending_discord_transcript'] = transcript.strip()

        except Exception as e:
            self.logger.error(f"[DiscordRFEvent] Error in transcription thread: {e}", exc_info=True)
        finally:
            if audio_path:
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

    def _write_wav(self, frames: list) -> str | None:
        """Write raw PCM frames to a temporary WAV file and return its path."""
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(self.repeater.config.CHANNELS)
                wf.setsampwidth(
                    self.repeater.p.get_sample_size(self.repeater.config.FORMAT)
                )
                wf.setframerate(self.repeater.config.RATE)
                wf.writeframes(b"".join(frames))
            self.logger.debug(f"[DiscordRFEvent] WAV written: {tmp.name}")
            return tmp.name
        except Exception as e:
            self.logger.error(f"[DiscordRFEvent] Failed to write WAV: {e}")
            return None

    # ── Bridge resolver ───────────────────────────────────────────────────────

    def _get_bridge(self):
        if self._bridge is not None:
            return self._bridge
        try:
            for module in self.repeater.module_manager.service_modules:
                if module.name == "Discord Bridge":
                    self._bridge = module
                    return self._bridge
        except Exception as e:
            self.logger.error(f"[DiscordRFEvent] Error resolving bridge module: {e}")
        return None

    def cleanup(self):
        self.logger.info("[DiscordRFEvent] Shutting down.")
