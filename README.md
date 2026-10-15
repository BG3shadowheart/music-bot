# Self-hosted Discord Music Bot (Railway)

Multi-source music bot: **YouTube Music (OAuth) → SoundCloud → YouTube → yt-dlp**, with automatic fallback, 24/7 mode, autoplay, filters, playlists, lyrics and state persistence. Built on discord.py 2.7+ (DAVE), wavelink 3.5+ and Lavalink v4.2.2+.

## Repository layout

```
main.py
requirements.txt
Dockerfile              # bot service
README.md
lavalink/
  Dockerfile            # Lavalink service (v4.2.2 + yt-dlp)
  application.yml       # OAuth, source config, DAVE logging
```

## 1. Create the Discord application

1. Go to https://discord.com/developers/applications → **New Application** → **Bot** → copy the token.
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**.
3. Invite the bot with scopes `bot` and `applications.commands` and permissions: View Channels, Send Messages, Embed Links, Read Message History, Connect, Speak.

## 2. Deploy Lavalink on Railway

1. Push this repo to GitHub. In Railway: **New Project → Deploy from GitHub repo**.
2. Create a service named **`lavalink`**. In **Settings → Source**, set **Root Directory** to `/lavalink`.
3. Variables for `lavalink`:

   | Variable | Value |
   |---|---|
   | `LAVALINK_SERVER_PASSWORD` | a long random string |
   | `PORT` | `2333` |
   | `LAVALINK_BIND` | `::` (only if your environment uses IPv6-only private networking; otherwise omit) |
   | `YOUTUBE_OAUTH_ENABLED` | `false` by default; set `true` only after deciding to enable YouTube OAuth |
   | `YOUTUBE_OAUTH_REFRESH_TOKEN` | Leave empty unless you have a valid refresh token |

4. Do **not** generate a public domain; the bot connects over Railway's private network.
5. Deploy and open the service logs.

## 3. Enable YouTube OAuth (optional)

Lavalink starts with YouTube OAuth disabled, so a missing or invalid refresh token cannot take the whole music backend offline. Voice connections and non-YouTube sources remain available.

To enable YouTube OAuth:

1. Set `YOUTUBE_OAUTH_ENABLED=true` on the Lavalink service and leave `YOUTUBE_OAUTH_REFRESH_TOKEN` empty. Deploy and wait for the plugin to print its device login URL and code in the Lavalink logs.
2. Open the URL and enter the code using a **throwaway Google account** (never your main account).
3. Copy the newly issued refresh token from the Lavalink logs into `YOUTUBE_OAUTH_REFRESH_TOKEN` and keep `YOUTUBE_OAUTH_ENABLED=true`.
4. Redeploy Lavalink. Do not share the refresh token in chat or commit it to GitHub.

If Lavalink crashes with `Invalid status code for oauth2 token fetch: 400`, clear `YOUTUBE_OAUTH_REFRESH_TOKEN`, set `YOUTUBE_OAUTH_ENABLED=false`, and redeploy. Obtain a fresh token before enabling OAuth again.

## 4. Deploy the bot

1. In the same Railway project, add another service from the same repo with **Root Directory** `/` (uses the root `Dockerfile`). Name it `bot`.
2. Add a **Volume** to `bot`, mounted at `/data` (stores `state.json`, playlists and 24/7 state).
3. Variables for `bot`:

   | Variable | Required | Description |
   |---|---|---|
   | `DISCORD_TOKEN` | yes | Bot token |
   | `LAVALINK_URI` | yes | `http://${{lavalink.RAILWAY_PRIVATE_DOMAIN}}:${{lavalink.PORT}}` |
   | `LAVALINK_PASSWORD` | yes | Same value as `LAVALINK_SERVER_PASSWORD` |
   | `RAILWAY_RUN_UID` | yes | `0` (lets the bot write to the mounted volume) |
   | `STATE_FILE` | no | Default `/data/state.json` |
   | `BOT_PREFIX` | no | Default `.` |
   | `DEFAULT_VOLUME` | no | Default `60` |
   | `IDLE_TIMEOUT_SECONDS` | no | Default `300` |
   | `SYNC_SLASH_COMMANDS` | no | Default `true` (slash commands mirror the prefix commands) |
   | `YTDLP_COOKIES_FILE` | no | Path to a cookies file for yt-dlp fallback |
   | `LOG_LEVEL` | no | `INFO` (use `DEBUG` to see per-source failures) |
   | `EMPTY_LEAVE_SECONDS` | no | Default `600`. Leave after nobody has listened this long (`0` = never). Music is paused immediately when no humans are present. |
   | `SIBLING_BOT_IDS` | multi-bot only | Comma-separated user IDs of the OTHER bot(s). Enables multi-bot routing. |

   The reference variable keeps the URI aligned with Lavalink's actual private domain and listening port. With the example `PORT=2333`, Railway resolves it to a URL such as `http://music-bot.railway.internal:2333` if that is the private domain shown in Lavalink's Settings.

4. Deploy. Healthy startup logs include `lavalink node ready` and `lavalink version ok ... (DAVE capable)`.

## 5. Commands

`.join` `.leave` `.play <query|url>` `.stop` `.skip` `.pause` `.resume` `.queue` `.nowplaying` `.volume <0-100>` `.loop <track|queue|off>` `.shuffle` `.seek <seconds>` `.lyrics` `.filter <off|bassboost|nightcore|vaporwave|8d|karaoke|tremolo|vibrato|soft|treble>` `.playlist save|load|list|delete <name>` `.247 <on|off>` `.autoplay <on|off>` `.help`

## Listener-aware playback

- Music pauses when no real (non-bot) user is in the voice channel, and resumes when one joins. Other bots do not count as listeners.
- Music pauses if the bot is server muted or server deafened, and resumes when unmuted/undeafened.
- A manual `.pause` is never auto-resumed; use `.resume`.
- `.join` / `.play` from another voice channel moves an idle bot there. If it is actively playing for listeners, it stays put.

## Multiple bots (optional)

Run the same code as a second Railway service with a different Discord token and a different `STATE_FILE`/volume.
Set `SIBLING_BOT_IDS` on each bot to the other bot's user ID, and enable **Server Members Intent** for each application.
If the first bot is busy with listeners in one channel, `.join` / `.play` from another channel is answered by the free bot instead.
Slash commands are not routed: pick the bot you want in Discord's command picker.

## DAVE notes

- Lavalink must be **4.2.2 or newer**; the bot logs an error at startup if it is older.
- The bot's `BotPlayer` always sends `channelId` in the voice payload to Lavalink.
- Discord voice close code `4017` in the bot logs means Lavalink is too old for DAVE.
- Lavalink DAVE debug logging is enabled in `application.yml` (`club.minnced.discord.jdave: DEBUG`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot logs repeatedly show a Lavalink websocket connection retry | Make sure Lavalink is running, then set the bot's `LAVALINK_URI` to `http://${{lavalink.RAILWAY_PRIVATE_DOMAIN}}:${{lavalink.PORT}}` and confirm the password matches. |
| "Music backend is offline" | Check `LAVALINK_URI`/password match; the bot reconnects automatically every 10 s. |
| Every YouTube source fails | Re-run OAuth (step 3) with a fresh throwaway account; SoundCloud and yt-dlp still serve as fallbacks. |
| State is lost on redeploy | Confirm the volume is mounted at `/data` and `RAILWAY_RUN_UID=0` is set. |
| Slash commands missing | Wait a few minutes after first start, or reinvite with the `applications.commands` scope. |
| Prefix commands ignored | Enable **Message Content Intent** in the Developer Portal. |
