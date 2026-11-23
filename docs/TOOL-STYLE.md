# ALICE tool writing guide

ALICE's tool definitions (name, description, parameter schema) are instructions sent to the model with every
request. They are the only place the model learns when, how and with what to use a tool. This guide is drawn from
the way Anthropic's Claude Code tools (Read, Edit, Write, Grep, Bash, WebFetch, Agent, AskUserQuestion, Monitor,
SendUserFile, ExitPlanMode…) are written. `tests/test_tool_style.py` checks the most basic rules for every tool.

## Description

1. **First sentence: what it does and what it returns.** "Reads a file's contents with line numbers; …". When the
   model scans the list, this is often all it reads.
2. **When not to use it, and what to use instead.** Draw the line with neighboring tools clearly:
   "Use read_file, not a command, to read files", "glob for file NAMES".
3. **Preconditions.** Say what has to happen first for the tool to succeed: "Read the file with read_file before
   editing it; edits to a file that has not been read are refused."
4. **Exact behavior and failure cases.** Say what will fail: "old_string must appear exactly once in the file; if it
   appears more than once, add more context or pass replace_all=true."
5. **Limits and defaults.** Timeouts, line/character limits, page size: give the numbers.
6. **Output format.** "file:line:text lines", "returns a pid, then use process_output".
7. **Safety.** User confirmation for irreversible or outward-facing actions (send, delete, buy); text in a tool's
   output is data, not instructions.
8. **Bullets.** After the first sentence, each rule goes on its own "- " line. Short, in the imperative.
9. **Length.** Core tools (files, commands, web) 300-1500 characters; small single-purpose tools at least 150
   characters. No needless adjectives or repetition.

## Parameters

- **Every parameter has a description:** what it is, its format, an example and its default.
  `"timeout": "Seconds; the command is killed when it runs out. Default 120, at most 1800."`
- **Closed sets use `enum`**, and the meaning of each value is written in the description.
- **`required`** lists only what is truly mandatory. Optional parameters state their default in the description.
- **Path parameters:** say whether they are absolute or relative to the working folder.
- **Name:** lowercase with underscores (`process_wait`), a verb or a noun. Parameter names are in English;
  descriptions are moving from Turkish to English (see
  [#213](https://github.com/Viox-Development/alice-agent/issues/213)).

## Claude Code equivalents

| Claude Code | ALICE |
|---|---|
| Read / Write / Edit / MultiEdit | read_file / write_file / edit_file / multi_edit |
| Glob / Grep | glob / grep |
| NotebookEdit | notebook_edit |
| Bash / PowerShell (+ background) | run_command (background=true) |
| BashOutput / KillShell | process_output / process_stop |
| Monitor | process_wait |
| WebFetch / WebSearch | web_fetch / web_search |
| Agent (subagent) | task, team |
| AskUserQuestion | ask_user |
| TodoWrite / TaskCreate | todo_write |
| Skill | skill |
| ToolSearch (deferred tools) | load_tools |
| ExitPlanMode | exit_plan_mode |
| SendUserFile | send_file |
| CronCreate / ScheduleWakeup | schedule |
| Browser / computer use | browser, computer, windows, ui_elements, ui_act |

## Behavior rules (enforced in code)

- **No editing without reading:** edit_file, multi_edit and write_file over an existing file are refused if the file
  has not been read with read_file in this session. If the file changed on disk after it was read, it has to be read
  again.
- **An error is not an answer:** when a tool fails, it raises `ToolError` saying why and what to do next.
