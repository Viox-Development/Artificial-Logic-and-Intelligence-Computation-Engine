# Projects: autonomous agent teams

Written from the "Projeler: otonom ajan takımları" section of the Turkish guide ([docs/README.tr.md](../README.tr.md))
and checked against `alice/team.py`, `alice/team_store.py`, `alice/providers/picker.py` and the `[agent] team_*`
defaults in `alice/config.py`. The review council has its own page: [council.md](council.md). Interface labels are in
Turkish (#214); they are given here in English.

A big job (several modules in a large codebase, backend + frontend + tests, a wide review) runs as a **project**. Each
project has a **lead** (Alice) and **agents** that work in parallel. The lead runs in the background, independent of
the chat, and wakes up as events arrive:

1. **Plans:** if no agents were given, it studies the code, splits the job into non-overlapping parts and sets up the
   agents. Each agent gets a task, limits, a write scope, a model and a thinking level: `brain` + `high` for hard
   work, `fast` + `low` for mechanical work.
2. **Watches:** it answers the agents' questions. Decisions that depend on your preference are asked to you; the
   question appears as a card on the Projects page and in the chat, and the project continues once you answer.
3. **Reviews:** every agent that finishes moves to **Awaiting review**. The lead reads the report and the diff, runs
   the tests if needed, then **approves** or **sends it back for changes** with concrete feedback (the agent continues
   where it left off). An agent working on its own git branch is merged into the main folder when approved.
4. **Finishes:** when everyone is approved, it verifies the whole and writes you a **summary**.

## It doesn't get stuck

- If the lead ends its turn without answering an event, it is reminded twice; if it still doesn't close it, the
  project closes by itself.
- If it ends its turn without presenting a plan, it is reminded twice; if it still doesn't, the job goes to a single
  agent ("worker").
- If an agent takes no step for a long time (10 minutes by default, `[agent] team_stall_minutes`), the lead and you
  get a "not progressing" notice. If it stays stuck as long again, it is **recovered automatically**: stopped and
  resumed with the next model in the chain (at most twice; then you are told). Time spent while the model writes a big
  file counts as progress ("Writing file… 12 KB").
- If the conversation grows long and only narrow-context models are available, the agent doesn't fail: the
  conversation is summarized to fit that model (shortening tool definitions if needed) and continues. If that still
  doesn't work, the reason is written plainly ("conversation too long for glm-5.3; gemini cooling down (opens in ~5
  h)") and a **Resume where it left off** button appears.
- **Stop** really stops: the agents, the lead and running councils. Events that arrive meanwhile wait and are handled
  on **Resume**; interrupted councils meet again.
- If a provider says it is "processing" but sends nothing for 5 minutes, the call is cut and the next model is used
  (nothing hangs for hours).
- Agents are told to write big files as a skeleton plus section-by-section edits, not in one go.
- If no model can answer (quota, keys), the agent doesn't go to review with an empty report; it goes to **error**,
  with the reason on screen, and the lead is told.
- If an agent's branch conflicts on approval, it goes back to the agent: it merges the main branch into its own,
  resolves the conflict, and is reviewed and merged again (at most 2 tries).

## Smart model choice

The agents' and the lead's model is `auto` by default: the task's area (design/UI, code, review, writing, simple work,
images) is inferred from the task text, and the models you can reach are ranked by their strength in that area (for
example Claude and Gemini Pro for design, Qwen Coder / DeepSeek / GLM for code, reasoning models for review;
narrow-context models are pushed back for agent work, and speed comes first for simple work). In the council the
critic and checker are picked for review, performance for code, and the user's-eye expert for design or (with
screenshots) images. The choice shows in the agent's title ("design · glm-5.3"); if you write `brain`, `fast` or
`provider:model` yourself, it is left alone. The table is in `alice/providers/picker.py`.

## Tokens

Every step resends the whole conversation, so the conversations of project agents, the lead and council members are
summarized once they pass 128k tokens (`[agent] team_compact_at`, 0 = no limit). Without this limit on 1M-context
models, a single website project could burn about 100M tokens.

## Design work

If the goal is a website or an interface, the lead first has a **design** agent write `DESIGN.md`: who it speaks to and
with what feeling, real references, typeface and color decisions, a layout idea and an explicit "don't" list
(patterns that look like a template). Every agent is shown this document; the council looks at the appearance first,
and "looks like a template" is a major issue.

**AI pattern check:** models keep sliding into the same patterns (mono labels like "AETH-SYS // BRIEFING-01", "> VIEW"
buttons, "128.400+" counter strips, fake technical notes like "FIG. 01", neon glows, gradient text, empty slogans).
These patterns are counted in the site's files (independent of any model) and go into the lead's review notes and the
council's material as file / example / fix; work isn't approved as is while the check isn't clean. Agents check their
own work with the `design_check` tool: screenshots are taken, the patterns counted, and the screenshots critiqued by an
"art director" model that can see images. The forbidden list sits above `DESIGN.md` too.

**Research:** the design agent starts with research: it finds real, good sites on the subject with `web_search` and
looks at each with `site_study` (desktop, the lower part of the page and phone screenshots, analyzed by a model that
can see: layout, type sizes, color proportions, visual language, tone of the copy, what to take). Screenshots and notes
are written to the project's `research/` folder. `DESIGN.md` is based on this research and writes measurements instead
of adjectives ("heading 72px/1.05, like site X"). `design_check` and the final council compare our site side by side
with the reference sites' screenshots.

## Starting a project

Say "do this as a project" in the chat; ALICE starts it with the `team` tool and, if you like, waits for the result in
the chat. Or use the `+` next to Projects in the side menu:

- Write the **goal**. Templates (add a feature, fix a bug, code review, refactor, write tests) add a "definition of
  done" under it.
- **Let Alice plan** (recommended) or **I'll define the agents**: name, model, thinking, workplace, task; under
  "Scope, limits, order", where it may write, its limits and which agents must finish before it starts.
- **Settings:** working folder, the lead's model and thinking level, the **check command** (tests/lint; it looks at
  the project and suggests one, e.g. `python -m pytest -q` or `npm test`), review and council on/off.
- **My templates:** save a project with the Template button (goal, settings, agent definitions) and it appears here;
  one click fills everything. Stored in `~/.alice/project_templates/`.

## The project page

- At the top: status (planning, working, in review, asking you, done, stopped), an approval progress bar, the lead's
  model and the folder. When the project ends, a **Project summary** card.
- On the left, threads grouped by status: **Lead** (Alice's own conversation) and **Team chat**, then **Asking**,
  **Awaiting review**, **Working**, **Queued** ("starts when backend is done"), **Done**, **Stopped / error**.
- Selecting an agent streams everything live in its **Conversation** tab: the task and messages it received, its
  answers, tool calls (click for the output). The **Changes** tab shows a colored diff per file.
- An agent awaiting review has **Approve** / **Request changes** (with feedback). The lead reviews too; you can make
  the call if you want.
- Write to an agent (if it is working it sees it on its next step; if it finished it continues), **Stop**, **Restart**
  (with a clean conversation), **Undo changes** (reverts what it did with the file tools).
- **Add agent** (to a running or finished project; the others are told), **Stop** / **Resume**, **Delete**. Writing to
  the lead gives a finished project new work.
- **Preview:** if the project is a website (`index.html` at the root or under `dist`, `public`, `site`, `src`…), it
  appears in the General group. The site opens in a sandboxed frame on a separate origin with its own preview key: the
  site's code can't reach ALICE or its API, and secret files like `.env` or `.git` are not served. It reloads when
  files change; **Desktop / Phone** view and **Open in new tab**.
- **Token tracking:** the project title shows the total tokens (and the money spent on OpenRouter); hovering shows the
  split between agents, lead and council; each agent's title shows its own usage.
- **Timeline:** everything in the project in order: jobs started and finished, check command results, council
  decisions, approvals, questions, messages.
- In the chat, a small card of the running project (progress, what is happening now) stays visible; questions and
  completion arrive as cards that open the project.

## Notifications and the phone

- "Asking you", "done", "not progressing" and "failed" events show a **desktop notification** (`[agent] team_notify =
  false` turns them off).
- For instant notifications on the phone, install the [ntfy](https://ntfy.sh) app, pick a topic name and add `[notify]
  ntfy = "your-topic"` to `config.toml` (or the full address of your own server). When it's off, nothing is sent
  anywhere. Choose a topic name that is hard to guess; on ntfy.sh anyone who knows the topic can read it.
- `alice phone` also prints the Projects address (`…/?t=…#projects`), and there is a **Projects** link under the quick
  bar. On the phone you can follow a project, answer questions and approve work.

## Change tracking

What the file tools change is always tracked; in a git repository, files changed with `run_command` are included too
(their original content comes from `HEAD`), in the agent's **Changes** tab and its **undo** button. Files already
changed when the project started, or touched by another agent, don't count for it.

## Persistence

Projects are saved to `~/.alice/projects/<id>.json` (conversations, messages, questions, and the original files for
diffs). They are loaded when the app opens again; an interrupted project shows as **stopped** and continues from where
it left off with **Resume** or a message.

## Agents talking to each other

Agents write to each other, to the lead (`alice`) or to everyone (`all`) with `team_send`, and wait for an answer with
`team_wait`. Incoming messages are added to their conversation after every tool step. An agent that joins later is
announced to the others.

## Safety and limits

- Agents use only coding tools (files, shell, web, `http_request`, `git`). Screen, browser and desktop tools share a
  single screen, so they are closed to agents.
- Writing outside the write scope with the file tools is refused; a "read-only" agent can't write at all. Permission
  questions come with the project and agent name (`[project / backend] ...`) and open one at a time; "always allow"
  applies only to the main session.
- At most 5 agents run at once (`[agent] team_max_parallel`), the rest queue; a project has at most 8 agents. The lead's
  step limit per turn is `team_lead_steps` (default 40).
- The scope check is for the file tools; it can't stop `run_command` from writing outside the scope (in a git
  repository that shows up as a change). Project changes aren't undone by the chat's Rewind (each agent has its own
  "Undo changes" button).
- On free models, parallel requests use up quota faster; the router moves to the next model, but in busy projects it
  helps to give mechanical work to `fast`.
