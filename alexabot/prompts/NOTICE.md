# Where the prompt text comes from

Not sent to the model; `alexabot/agent.py` renders only `system.md`,
`voice.md`, `tool-result.md`, `turn.md` and one of `memory-on.md` /
`memory-off.md` (Ada's own text; the `[Memory: ...]` note follows the
bracketed-note shape of the tool hint below).

`voice.md` and `tool-result.md` are adapted from the files of the same names
in KayLerch/alexa-skill-mcp-bridge, `packages/agent/prompts/`, at commit
`ca2c2ef` (Apache License 2.0, Copyright the alexa-skill-mcp-bridge authors).
The bracketed `[Frontend hint: ...]` sentence in `alexabot/agent.py`
`turn_prompt` follows that repository's `prompts/tool-hint.md`, and the
`{{placeholder}}` rule (a missing value raises) follows its
`src/agent/prompt.ts` `renderPrompt`.

Changes made here:

- `voice.md`: the rules kept are "At most N short sentences", "No markdown,
  lists, URLs, code, emoji, or symbols", "Ask exactly one question at a time
  ... Put the question last", "Name at most N things"; the examples were
  changed to electronics ("three point three volts"), and the travel-specific
  rules (summarising several search results, confirming by use) were dropped.
- `tool-result.md`: "If a result is marked as an error, apologize in one
  short sentence, say what the user can try instead, and stop. Do not repeat
  the error text" is kept; the rest is Ada's own (the review honesty rules,
  KiCad, ordering) and replaces "Prefer the structured data": here the
  structured data is shown on the page and the `speech` field is what is said.
- `system.md` and `turn.md` are Ada's own.

The Apache License 2.0 text: https://www.apache.org/licenses/LICENSE-2.0
