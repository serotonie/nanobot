# AI Agent Memory in nanobot

nanobot keeps recent chat history separate from long-term memory like your preferences, background, and ongoing project knowledge. You can inspect, edit, and restore long-term memory at any time.

> [!NOTE]
> An agent replying "Got it, I'll remember that" does not automatically guarantee a preference was written to long-term memory. You can verify or update it directly using the steps below.

## How Memory Works

<<<<<<< HEAD
It separates memory into layers, because different kinds of remembering deserve different tools:

- `session.messages` holds the living short-term conversation.
- `memory/history.jsonl` is the running archive of compressed past turns.
- `SOUL.md`, `USER.md`, and `memory/MEMORY.md` are the durable knowledge files.
- `GitStore` records how those durable files change over time.

This keeps the system light in the moment, but reflective over time.

## The Flow

Long-term memory is updated in two stages.

### Stage 1: Consolidator

When a conversation grows large, nanobot summarizes the conversation covered by compaction and appends the result to `memory/history.jsonl`. The model continues with the summary and any messages after it. The original messages remain in your saved chat history, but messages covered by the summary are no longer sent to the model verbatim. Each summary preserves useful long-term facts and a short handoff for active work.

Compaction also runs after a configured period of inactivity, or when you send `/compact`. See [Auto Compact](./configuration.md#auto-compact) for idle timing and how to disable automatic idle compaction.

Before a model request exceeds the configured context window (with space reserved for output and a safety buffer), nanobot compacts the accepted conversation history and continues with the summary and pending input. If the provider rejects an input that local estimation considered small enough, nanobot also compacts and retries that model request once. Completed local tools are not executed again. A request that already streamed output or started a provider-hosted tool is not replayed. If compaction fails, the remaining input still cannot fit, or the retry is rejected again, the error is surfaced instead of retrying indefinitely.

Automatic compaction does not post lifecycle notices to built-in chat channels by default. This only silences chat messages: compaction still runs, and WebUI/TUI retain structured status and history. Manual `/compact` keeps its start and outcome feedback. Set `channels.showCompactionNotices: true` to enable automatic notices globally, or set `showCompactionNotices` in a channel's configuration to override that default. Omitted or `null` channel overrides inherit the global value; existing explicit QQ overrides are preserved.

This file is updated as conversations are summarized. It uses one JSON record per line so nanobot can track which summaries have already been processed; it is primarily an internal working history, not a permanent archive.

Each line is a JSON object:

```json
{"cursor": 42, "timestamp": "2026-04-03 00:02", "content": "- User prefers dark mode\n- Decided to use PostgreSQL"}
=======
```mermaid
flowchart LR
    C["Active Chat"] -->|"/compact or auto-summarize"| H["Summaries (history.jsonl)"]
    H -->|"/dream or periodic schedule"| R["Long-Term Memory (SOUL, USER, MEMORY, wiki/)"]
    C -->|"Direct 'Remember...' request"| R
>>>>>>> 0dc8679edcc6061fbcdf3de13c7e16000e9d1b65
```

nanobot organizes memory into four layers so conversations stay fast while useful knowledge builds up over time:

| Layer | Where it lives | What it holds |
|---|---|---|
| Active conversation | Session history | Recent messages in the current topic or chat |
| Conversation summaries | `memory/history.jsonl` | Compact summaries of older turns created when a chat grows long or when you run `/compact` |
| Resident memory | `SOUL.md`, `USER.md`, `memory/MEMORY.md` | Core personality, user preferences, and key facts loaded into every conversation |
| Knowledge wiki | `memory/wiki/` | Detailed Markdown notes read on demand when relevant to a task |

Long-term memory updates in two ways:

1. **Direct updates:** When you ask nanobot to remember something (or share a lasting preference) in a chat with write access, it can update the relevant memory file right away.
2. **Compaction + Dream:** As conversations grow long (or after idle time, or when you run `/compact`), nanobot summarizes older messages and saves the summary to `memory/history.jsonl`. Periodically (or when you run `/dream`), **Dream** reviews new summaries and makes focused edits to your resident memory files and `memory/wiki/`.

Every Dream update is versioned with Git in your agent workspace, so you can inspect what changed and roll back if needed.

## Where Memory Files Live

All memory files belong to your **agent workspace** (`~/.nanobot/workspace/` by default):

```text
workspace/
├── SOUL.md              # Agent voice and communication style
├── USER.md              # Stable preferences and facts about you
├── prompts/
│   └── dream.md         # Optional custom instructions for Dream
└── memory/
    ├── MEMORY.md        # Core facts and links to wiki pages
    ├── wiki/            # Optional detailed Markdown notes
    │   └── index.md     # Topic index read on demand
    └── history.jsonl    # Recent conversation summaries
```

Picking a different **project workspace** in the WebUI changes where the agent works on files, not where its memory lives. That means one agent keeps continuous memory across projects and topics.

> [!IMPORTANT]
> Starting a new topic or choosing a different project does **not** isolate long-term memory. If you need separate memory for different clients or users, run [Multiple Instances](./multiple-instances.md).

## Save and Check a Preference

In any WebUI or terminal chat, state a clear preference:

```text
Remember that for Acme release notes I prefer concise bullet points and British spelling.
```

- If the chat has write access to your agent workspace, nanobot can save it right away. Ask which file it updated, then verify in a new topic.
- If you are in a **Restricted** chat inside a different project folder, nanobot can read `memory/wiki/` but cannot write to your agent memory files. Switch to the agent workspace, edit the file yourself, or run `/compact` followed by `/dream` to consolidate recent chats.

## Memory Commands

Use these slash commands in chat to inspect, run, or restore memory:

| Command | What it does |
|---|---|
| `/compact` | Summarize the current conversation now (full chat history remains visible) |
| `/dream` | Run Dream consolidation now |
| `/dream-log` | Show the latest Dream memory change |
| `/dream-log <sha>` | Show a specific Dream memory change |
| `/dream-restore` | List recent Dream memory versions |
| `/dream-restore <sha>` | Restore memory files and wiki pages to the state before that change |
| `/dream-prompt` | Show the current instructions guiding Dream |
| `/dream-prompt init` | Create an editable `prompts/dream.md` guide for this workspace |

> [!NOTE]
> `/dream-restore <sha>` restores all managed memory files and wiki pages together (not a single page). If you have uncommitted manual edits in memory files, commit or revert them before restoring.

## Guiding Dream

Most users can leave Dream's built-in instructions as they are. To customize how Dream organizes memory for one workspace, run:

```text
/dream-prompt init
```

Edit `workspace/prompts/dream.md` in plain Markdown. Dream follows your guide automatically before reviewing new summaries. Delete or empty the file to return to nanobot's defaults.

## Configuration

Turn Dream on or off in **Settings → Capabilities → Memory consolidation**. Dream runs automatically while `nanobot gateway` is running.

To change how often Dream runs or pick a dedicated model preset for it, configure `agents.defaults.dream` in `~/.nanobot/config.json`:

<details>
<summary>Manual configuration</summary>

```json
{
  "agents": {
    "defaults": {
      "dream": {
        "enabled": true,
        "intervalH": 2,
        "modelOverride": null
      }
    }
  }
}
```

| Field | Meaning |
|---|---|
| `enabled` | Turn periodic Dream consolidation on or off |
| `intervalH` | How often Dream runs, in hours |
| `cron` | Optional cron expression override (takes precedence over `intervalH`, e.g. `"0 */4 * * *"`) |
| `modelOverride` | Optional named model preset from `modelPresets` to use for Dream |

To adjust automatic idle compaction, see [Auto Compact](./configuration.md#auto-compact). To show automatic compaction notices in chat apps (hidden by default; manual `/compact` always shows feedback), set `channels.showCompactionNotices: true`.

</details>
