# ChatRF Discord Integration

This set of modules bridges your ChatRF repeater with a Discord server, giving you a live status dashboard, a two-way RF↔Discord bridge, and a full remote admin interface - all without touching the radio.

---

## Modules

### `background_discord_status.py` - Live Status Dashboard
Maintains a single auto-updating embed in a Discord channel showing the current state of the repeater. The message is created on first boot and edited in-place every few seconds, so your status channel stays clean.

**Shows:**
- 🟢/⚪ Repeater active or idle
- 🤖 AI mode on/off
- Uptime
- Last DTMF command received
- Last RF transcript (if the bridge module is also running)
- CPU and RAM usage (if `psutil` is installed)

---

### `background_discord_bridge.py` - RF ↔ Discord Bridge
Handles two-way communication between the repeater and Discord.

**RF → Discord:** Picks up transcripts queued by `event_discord_rf.py` and posts them to a log channel as they come in.

**Discord → RF:** Polls a channel for new messages from any user and speaks them over the air via piper-tts, waiting for the channel to be clear first.

---

### `event_discord_rf.py` - RF Transmission Recorder
Hooks into the repeater's transmission events to capture audio and transcribe it.

- `on_transmission_start` - clears the audio buffer so the new transmission is recorded fresh
- `on_transmission_end` - drains the buffer, writes a WAV file, transcribes it with Whisper, and queues the result for the bridge to post

This module does no Discord I/O itself - it only feeds transcripts to `background_discord_bridge.py` via `shared_data`.

---

### `background_discord_admin.py` - Remote Admin Interface
Lets trusted Discord users control the repeater remotely via a dedicated admin channel. Commands are rejected silently from anyone not in the trusted users list, and every command is logged to an optional audit channel.

**Commands:**

| Command | Description |
|---|---|
| `!help` | List all available commands |
| `!ping` | Repeater speaks a live confirmation over the air |
| `!say <text>` | Speak any text via piper-tts |
| `!play` | Play an attached WAV or MP3 file over the air |
| `!logs [repeater\|aimode] [n]` | Post the last `n` lines of a log file (default: repeater, 20 lines) |
| `!errors [repeater\|aimode] [n]` | Post the last `n` lines of an error log file |
| `!modules` | List all loaded modules grouped by type with enabled/disabled state |
| `!sayat <HH:MM> <text>` | Schedule a one-off TTS announcement at a specific time (24h) |
| `!id` | Immediately transmit the station CW ID |
| `!restart` | Speak a warning then restart the Python process |
| `!reboot` | Speak a warning then reboot the host system |

---

## Installation

### 1. Install dependencies

```bash
pip install requests psutil
```

`psutil` is optional - CPU/RAM stats in the dashboard are skipped if it isn't installed.

### 2. Copy the modules

Drop all four `.py` files into your ChatRF `modules/` directory:

```
modules/
├── background_discord_status.py
├── background_discord_bridge.py
├── event_discord_rf.py
└── background_discord_admin.py
```

ChatRF will pick them up automatically on next start.

### 3. Create a Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a new application.
2. Under **Bot**, enable the **Message Content Intent**.
3. Copy the bot token, you'll need it for the config below.
4. Invite the bot to your server with at minimum the `Read Messages`, `Send Messages`, and `Manage Messages` permissions.

### 4. Configure `config.ini`

Add the following sections to your `config/settings/config.ini`:

```ini
[DiscordStatus]
bot_token      = YOUR_BOT_TOKEN
status_channel = CHANNEL_ID_FOR_DASHBOARD
update_interval = 5

[DiscordBridge]
bot_token      = YOUR_BOT_TOKEN
rf_log_channel = CHANNEL_ID_FOR_RF_TRANSCRIPTS
to_rf_channel  = CHANNEL_ID_FOR_MESSAGES_TO_RF
poll_interval  = 5

[DiscordAdmin]
bot_token      = YOUR_BOT_TOKEN
admin_channel  = CHANNEL_ID_FOR_ADMIN_COMMANDS
audit_channel  = CHANNEL_ID_FOR_AUDIT_LOG
trusted_users  = YOUR_DISCORD_USER_ID
poll_interval  = 3
```

All three modules can share the same bot token. To find a channel ID, right-click a channel in Discord with Developer Mode enabled (Settings → Advanced → Developer Mode) and click **Copy Channel ID**. Do the same for your own user ID under your profile.

---

## Recommended Channel Layout

```
📡 your-server
├── 📋 repeater-status     ← DiscordStatus dashboard (pin the bot's message)
├── 📻 rf-log              ← DiscordBridge posts transcripts here
├── 💬 to-rf               ← Anyone can type here to be spoken over the air
└── 🔧 repeater-admin      ← Admin commands (restrict access to trusted members)
    └── 📑 admin-audit     ← Optional audit log of every command run
```

---

## Notes

- For `!reboot` to work, the user running ChatRF needs passwordless sudo for the reboot command. Add the following to `/etc/sudoers` (replace `pi` with your username):
  ```
  pi ALL=(ALL) NOPASSWD: /sbin/reboot
  ```
- `!sayat` schedules announcements relative to the system clock on the Raspberry Pi. Make sure your timezone is set correctly (`timedatectl set-timezone Europe/Athens`).
- The bridge module waits for the repeater to go idle before speaking Discord messages over the air, so it won't step on an active transmission.
