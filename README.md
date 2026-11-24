# Reframed Rehome Bot

Announces in a Discord channel the pets that wait for a new home on a
[Petz 5 Reframed](https://github.com/PetzReframed/Reframed) server, and says so when one has been adopted.

With rehoming turned on, a player who returns a pet at the Adoption Center may let another player adopt it. The game
server keeps each such pet as a file in a folder of its own. This bot reads that folder: for every pet that newly
waits there it posts one message, with the pet's name, breed, sex, saved picture and the time it was offered. When the
pet has left, the bot edits that same message; it never posts a second one about the same offer.

It has no commands and listens to nothing: it only sends and edits its own messages. It needs no change to the game
server and opens no port. What happened while the bot was stopped is caught up when it starts again.

## What a message looks like

While the pet waits:

> **Biscuit** the Calico is looking for a home.
> Female cat, waiting at the Adoption Center since 23 November 2026 10:16.
> 📎 pet.png

After its adoption, the same message:

> **Biscuit** the Calico has been adopted.
> Female cat, offered 23 November 2026 10:16, adopted 24 November 2026 18:02.
> 📎 pet.png

When the pet left without the bot having seen it out on a visit:

> **Biscuit** the Calico is no longer at the Adoption Center.
> Female cat, offered 23 November 2026 10:16, left 24 November 2026 18:02.
> 📎 pet.png

Times are shown by Discord in each reader's own time zone. When the bot was not watching at the time (it was stopped,
or could not read the folder), the last part reads "adopted some time before …" or "left some time before …", with
the time of the bot's next look.

The name and the breed are text from a pet file. They are posted with every punctuation mark escaped, so they cannot
format the message, mention anybody or make a link.

## What the bot can and cannot know

The game server keeps no list and no history of rehomed pets, only the files:

- `available/<id>.….pet` while a pet waits, `claimed/<id>.….pet` while it is out on a visit with a player who may
  adopt it. A visit that ends without an adoption moves the file back.
- The server deletes the file when it accepts an adoption. Nothing records the adoption, its time or the adopter.

So:

- **Adopted** means: the file was last seen in `claimed/` and is gone. The server deletes a file there only for an
  accepted adoption.
- **No longer at the Adoption Center** means: the file was last seen in `available/` and is gone. Either the offer was
  taken back (the offering game could not give the pet up after all; this happens within moments of the offer), or a
  whole visit and its adoption fitted between two looks of the bot, or the bot was stopped meanwhile. The bot does not
  guess which. A short `POLL_SECONDS` keeps the second case rare.
- The time of an adoption is the time of the look that found the file gone, so it is late by up to `POLL_SECONDS`.
- **A pet adopted before the bot first saw it is never posted**, whatever the settings: nothing of it is left on the
  server.
- `POST_EXISTING` decides about the pets that are waiting (or out on a visit) when the bot starts for the first time.
  By default they are taken as known: not posted, and nothing is said when they are adopted later. With
  `POST_EXISTING=true` they are all posted at that first start, the one offered first first.
- The same pet offered again later is a new offer with a new ID on the server, and gets a new message.
- Nobody who offered or adopted a pet is named: the server removes the previous owner's name and comments from the
  file before it stores it, and records no adopter. The bot posts only what the game itself shows every player in
  **Petz Looking for a Home**: the name, the breed, the sex, the picture and since when the pet waits. The pet's age
  is not posted: the file holds a number that the game turns into a word of its own, which the bot does not have.

Two limits to know about:

- The server's name check (`PETZ_REHOMING_NAME_FILTER`) refuses an offer whose pet name contains a blocked word, so
  such a pet never reaches the folder. A pet stored before a word was added to the list stays in the folder and the
  game hides it; the bot cannot make that check and would post such a pet with `POST_EXISTING=true`. A message already
  posted is not withdrawn when a word is added later. Delete it in Discord: the bot then leaves it alone.
- The picture is the one saved in the pet's file by the offering player's game, 108×80 pixels, posted three times as
  large. Neither the server nor the bot looks at what it shows. Set `ATTACH_PICTURE=false` to post text alone.

## Set-up

1. **Create the bot.** In the [Discord Developer Portal](https://discord.com/developers/applications) create an
   application, open **Bot** and copy the token. No privileged intents are needed.
2. **Invite it** to your server with the permissions **View Channel**, **Send Messages** and **Attach Files** for the
   channel the pets go to (OAuth2 > URL Generator > scope `bot`). Editing its own messages needs no further
   permission.
3. **Copy the channel's ID**: turn on Developer Mode in Discord (Settings > Advanced), right-click the channel and
   choose Copy Channel ID.
4. **Configure and start**, on the machine that runs the game server:

   ```sh
   cp .env.example .env      # fill in DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID and REHOMING_HOST_DIR
   docker compose pull
   docker compose up -d
   docker compose logs -f
   ```

`REHOMING_HOST_DIR` is the game server's rehoming folder on the host: the `rehoming` folder inside the game's data
folder (`PETZ_DATA_DIR` in the game's `.env`). The game server must have rehoming turned on
(`PETZ_REHOMING_ENABLED=true`). It creates `rehoming/available` and `rehoming/claimed` the first time a player opens
the list of waiting pets or offers one. Compose does not create the folder: start the bot once `rehoming` exists.
While the two folders inside it are missing, the bot says so once in its log and keeps looking.

The game server's container keeps these folders private to its own user (UID/GID 10001), so this image runs as the
same user. The folder is mounted read only and the bot never writes to it; the server stops rehoming when it finds a
file there that it does not know. The bot keeps its own record in the `state` volume.

## Settings

All settings are in `.env`; [.env.example](.env.example) describes each.

| Setting | Default | Meaning |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | required | The bot's token. |
| `DISCORD_CHANNEL_ID` | required | The channel to post in. |
| `REHOMING_HOST_DIR` | required | The game server's rehoming folder on the host (used by `compose.yaml`). |
| `POLL_SECONDS` | `5` | How often the folder is looked at. |
| `POST_EXISTING` | `false` | Whether pets already waiting at the bot's first start are posted. |
| `ATTACH_PICTURE` | `true` | Upload the pet's saved picture with its message. |
| `MAX_PET_BYTES` | `1048576` | The largest pet file read; a larger one is not posted. |
| `MAX_PETS` | `10000` | The most entries expected in each of the two folders; a fuller folder is not read at all. |

## Behaviour

- One message for each offer. The bot's record (`pets.json` in the `state` volume) holds, for every pet stored on the
  server, its message's ID and what the message shows; it survives restarts. A pet is forgotten once its file is gone
  and its message was edited.
- Pets are posted in the order they were offered. When Discord does not take a message or an edit (no connection, a
  rate limit, a wrong token or channel), the reason is logged and the same work is done on the next look; nothing is
  skipped. After a rate limit the bot first waits as long as Discord asks, at most a minute.
- A message that was deleted in Discord is not edited and not posted again: the bot tries once, is told that the
  message is unknown, and forgets the pet.
- A pet is taken as gone only when two scans in a row miss its file, because a scan can miss a file the server is
  renaming at that moment. When a folder cannot be read at all, nothing is concluded about any pet.
- A file that does not read as a pet the server stored (damaged, too large, not a pet file) is not posted. The log
  says so once and the file is looked at again on every pass. An offer the server is still writing has another name
  and is not looked at until it is complete.
- Every post carries a nonce made from the pet's ID. Discord answers a repeated nonce with the message it already
  has, for a few minutes. So when a message was posted but could not be recorded (the state volume was full, the
  container was stopped at that moment), posting it again creates no second message within that time. While the bot
  keeps running it remembers the message anyway and records it as soon as it can. A duplicate remains possible only
  when the bot is stopped with an unrecorded post and starts again more than a few minutes later.
- A `pets.json` that is damaged stops the start with a message instead of being replaced: starting from nothing
  could post waiting pets a second time.
- The token is never logged, and neither are pet names: the log names a pet by the server's ID.

## Development

The bot is Python with no dependencies outside the standard library.

```sh
python -m unittest discover -s tests -t .
docker build -t reframed-rehome-bot .
```

The tests make a rehoming folder as the game server keeps one, with pet files built from the parts of the format the
bot reads, and stand in for Discord; they need no token, no network and no game files.

Pushes to `main` publish `ghcr.io/petzreframed/reframed-rehome-bot:main`; a tag `v…` publishes that tag and `latest`
(see [.github/workflows/build.yml](.github/workflows/build.yml)).
