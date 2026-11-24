# Token accounting

The installed Codex CLI 0.169.0-alpha.11.1 writes private rollout JSONL files beneath `$CODEX_HOME/sessions` or `~/.codex/sessions`. The report resolves explicit IDs first, followed by `CODEX_THREAD_ID` and `CODEX_SESSION_ID`. Without an ID it matches the recorded working directory exactly and chooses the latest modified rollout when the selection is unambiguous. An unfinished matching rollout can make that fallback ambiguous, including after an interrupted process.

## Recorded usage

- A native `token_usage_record` carries the individual request counters in `usage`, cumulative turn counters in `turn_token_usage`, and cumulative thread counters in `thread_token_usage`. Repeated response IDs and mirrored `token_count` events are excluded from request sums.
- Legacy `token_count` events carry the last request and cumulative session usage. Start and completion boundaries determine turn deltas where the baseline and end counters are present. A `turn.completed` event with usage supplies an aggregate for that turn. An isolated event stream can only report the turns it contains.
- Cache hits are a subset of input, and reasoning tokens are a subset of output. The session total represents recorded consumption across requests. Window utilization and headroom use the latest comparable request input as a proxy and may lag the live conversation or change after compaction.

## Estimated composition

The parser classifies visible response items once and estimates tokens from UTF-8 byte length. Earlier user and assistant messages form the conversation history category. Recognizable tagged instructions, tool call arguments, and paired result types receive separate categories. Terminal file-read classification is heuristic. Mixed operations can be assigned to their dominant identifiable source. A visible subtotal can diverge considerably from recorded input due to tokenization, framing, request timing, opaque compaction items, and hidden instructions or tool schemas.

Compaction resets the reconstruction to the logged replacement history, when present. Only visible textual summaries are estimated. Retained UI context and encrypted reasoning are excluded. The report prints aggregates and generic recommendations, without echoing payloads, commands, or paths.
