# DBot

DBot is a Discord game bot for friends to play **Mafia**, a game of secret roles,
suspicion, and voting. It delivers roles privately, runs timed rounds, announces
eliminations, and checks who wins. It also includes greetings and dice.

## Play Mafia

A game supports **3–6 players** in a server channel. One player is randomly
assigned **Mafia**; everyone else is a **Citizen**.

- **Mafia** secretly chooses someone to eliminate each night and tries to survive
  the daytime vote.
- **Citizens** discuss what happened and vote to eliminate the Mafia.

Doctor and Police roles are not implemented yet. There are no AI players or
automatic channel mutes. Friends can discuss in text or voice and should stay
quiet during the night.

### Start a game

1. Use `!mafia check` to check this channel and your own DM access.
2. Use `!mafia create` to become the host and join automatically.
3. Other players use `!mafia join` in that same channel.
4. The host uses `!mafia start` once 3–6 players have joined.
5. Everyone checks the bot's DMs for their secret role.

Every role DM identifies the server and channel. Mafia DMs include numbered
targets and commands to copy, such as `!mafia kill 3`. No game ID is needed.
During an active game, player numbers stay the same when players are eliminated
or forfeit. Waiting-lobby numbers can change when someone leaves.

With only three players, the first successful night kill gives the Mafia a win.
Five or six players offer more room for discussion and voting.

### Rounds

| Phase | Maximum duration | Can end early when |
| --- | --- | --- |
| Night | 2 minutes | The Mafia submits a final target by DM. |
| Discussion | 2 minutes | More than half of living players use `!mafia ready`. |
| Voting | 1 minute | Every living player votes, including skips. |

If the Mafia does not choose before night ends, nobody is eliminated that night.
Morning announces the result without revealing the victim's role.

During voting, use `!mafia vote 3` to select a suspect or `!mafia vote skip` to
vote for no elimination. Choices are final. A suspect needs **more than half of
all living players' votes** to be eliminated. Ties, skips, and missing votes can
produce no elimination. Players cannot target or vote for themselves.

If neither team has won, another night begins automatically with the same roles
and updated living targets. Eliminated players may watch but cannot act.

### Winning

- **Citizens win** when the Mafia is eliminated.
- **Mafia wins** when the living Mafia equals or outnumbers the living Citizens.
- **Draw** after three consecutive complete rounds without any eliminations.

A forfeit or confirmed server departure can also decide the winner. If everyone
leaves, the game closes without declaring a winner.

## Commands

Unless marked **DM only**, Mafia commands belong in the game's server channel.

| Command | Who can use it | Purpose |
| --- | --- | --- |
| `!mafia` | Anyone | Show Mafia commands. |
| `!mafia check` | Anyone | Check channel permissions and the caller's DMs; once per 30 seconds per member in a server. |
| `!mafia create` | Available player | Create a waiting lobby and become host. |
| `!mafia join` | Available player | Join this channel's waiting lobby. |
| `!mafia leave` | Waiting player | Leave before roles are assigned. |
| `!mafia lobby` | Anyone | Show host, player numbers, and phase without roles. |
| `!mafia start` | Host | Deliver roles and start Night 1. |
| `!mafia kill <number>` | Living Mafia, **DM only** | Lock a final night target. |
| `!mafia ready` | Living player | Mark ready to move from discussion to voting. |
| `!mafia vote <number>` | Living player | Lock a final daytime vote. |
| `!mafia vote skip` | Living player | Lock a vote for no elimination. |
| `!mafia transfer <number>` | Host | Hand hosting to another eligible participant. |
| `!mafia forfeit` | Participant | Permanently leave an active match. |
| `!mafia cancel` | Host | Clear the game and invalidate its role DMs. |
| `!mafia forcecancel` | Member with Manage Server | Stop a game when intervention is needed. |
| `!hello` | Anyone | Receive a greeting. |
| `!roll` or `!roll 20` | Anyone | Roll a die with 2–100 sides; defaults to 6. |
| `!help` | Anyone | Show built-in command help. |

Hosting does not grant extra votes or reveal roles. Other players cannot start
or cancel the host's game. The host can cancel even if eliminated and can cancel
while roles are being delivered.

## Leaving and cleanup

Each channel has its own lobby. Each player can belong to **one active game at a
time**, including waiting lobbies. Eliminated players remain participants until
they forfeit or their game ends. Finished games immediately release their players.

If the host leaves a waiting lobby or forfeits an active game, hosting passes to
the first remaining eligible player. During announcements, wait for the next
phase before forfeiting or transferring hosting.

Forfeits remove that player's actions and votes cast against them. Affected
voters may vote again. A departed night target is cleared. The bot also checks
for confirmed server departures; simply going offline does not remove a player.

| Situation | Default behavior |
| --- | --- |
| Waiting lobby never starts | Expires 10 minutes after creation. |
| Active game receives no valid player action | Closes after 15 minutes of inactivity. |
| Finished game remains in its channel | Clears after 5 minutes, or immediately with host cancellation. |
| Channel becomes inaccessible or messages keep failing | Stops the game and attempts a closure notice. |
| Bot restarts | Ends previous sessions and attempts a channel closure notice, falling back to the host's DMs. |

Maintenance runs every 30 seconds plus request time, so cleanup is not always
instant. Restart notices require the local restart journal to remain available.
If both the channel and host are unreachable, the notice is retained for a later
restart. Games **do not resume** after restart; discard old role DMs.

## Messages and privacy

Night choices are accepted only in DMs. Typing a choice publicly is rejected,
but your original message is still visible to others: always use the bot's DM.
Roles are not displayed in public rosters.

Phase announcements use readable cards with text fallback when Embed Links is
unavailable. Players must allow the bot to DM them. `!mafia check` tests only the
person running it and the current channel; it cannot confirm other players' DM
settings or guarantee future message delivery.

DBot must be running and connected to Discord to respond. The server must allow
it to read commands and send messages, with Message Content Intent enabled.
No Administrator or Manage Roles permission is needed. The restart journal
stores session, channel, and host IDs, without roles, votes, targets, or tokens.
