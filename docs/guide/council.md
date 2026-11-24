# Review council

Written from the "İnceleme kurulu" part of the projects section of the Turkish guide
([docs/README.tr.md](../README.tr.md)) and checked against `alice/council.py` and the `[agent] team_*` defaults in
`alice/config.py`. See also [projects.md](projects.md).

Work is judged not by a single pair of eyes but by a **council** with different points of view. It is on by default
in projects (it can be turned off in a new project's Settings); in a chat, `/kurul <topic>` asks it about an idea, a
file, a decision or a pull request.

## Plan council

Before any code is written, it doesn't hunt for implementation details; it only checks whether the plan takes the
right steps: coverage (is the whole goal there), splitting (do the parts and their write scopes overlap), order
(dependencies), verification, risk (an approach that would break the job from the start) and, for design work, a
design direction step. The council sees the project folder too. Each criterion gets a 1-5 score (shown on the card),
and each finding is tied to the relevant agent. A plan has no "critical" severity and no veto; the checker doesn't
count "the plan doesn't say so" gaps as errors and lowers their severity. The plan is revised only for a verified
problem that breaks it; the revised plan is reviewed against the earlier objections, with at most one revision round
(if objections remain, the plan starts with notes). Non-blocking suggestions are added to the relevant agent's task as
a "Council note". "Send the plan to the council too" in the new project window turns it off.

## Measured debate

Severities are defined: critical (broken or unsafe), major (doesn't meet the goal), minor (a detail). A conditional
approval with only minor conditions doesn't open another revision round; the work is approved and the suggestions go
to the lead as notes. Items that repeat the same condition in other words, and repeated lessons, are filtered out.

## It looks at the site

When the work touches the appearance (html/css/js…), and in every final council, desktop and phone screenshots of the
site are taken; the **user's-eye** expert reviews them with an image model (the `vision` role): hierarchy, spacing,
typography, overflow on the phone, places that look like a template. Nothing extra to install: without Playwright, the
Edge or Chrome on the computer is used headless (`ALICE_CHROMIUM` can point to one). If neither exists, the council
keeps reviewing the code. Turn it off with `[agent] team_screenshots = false`.

## Roles

- **Critic:** ruthless but with evidence. It looks for bugs, gaps, risks and bad design; every finding must carry
  evidence (file:line, a quote, steps to reproduce).
- **Advocate:** brings out the work's strengths and refutes exaggerated criticism with evidence; it doesn't deny real
  bugs.
- **Checker:** neutral. It tries the claims itself (reads the file, runs tests or code) and marks each finding
  **verified / refuted / unclear**.
- **Experts** (called only when relevant): **security**, **performance**, **user's eye**.

## Flow

The roles first review independently without seeing each other → the checker verifies the findings → debate rounds if
needed (everyone sees the others' votes and the checker's results, agrees with findings or objects with evidence, and
changes their vote if convinced; no new findings are added in the debate, and it ends early if nobody changes their
mind) → the vote.

## Voting

- Votes: **approve**, **conditional approve** ("fine if these are fixed") or **reject**, each with a reason.
- A reject vote whose findings were all refuted, or that rests on no finding, **doesn't count**.
- A relevant expert's vote counts **double** (e.g. the security expert on a security-related change). **Your vote
  counts three times.**
- A verified **critical** finding rejects the work whatever the vote says (**veto**).
- A weighted approval of 60% or more: approved (conditions go to the agent as small fixes). Below 40%: rejected, and
  the verified findings go to the agent as a fix list. In between it is **contested**: the lead (Alice) decides, and
  asks you if it is critical.
- After the debate the first and last votes are shown side by side, so you can see who convinced whom.

## When it meets

- **Plan council:** before code is written, the lead's plan is debated. If it is rejected, Alice revises it from the
  criticism and presents it again; after a second rejection it asks you.
- **Work council:** whenever an agent finishes. The decision is applied automatically. After a fix, a short **check
  round** runs with only the roles that objected (at most 2 fix rounds; then the lead decides).
- **Final council:** when all work is done, it reviews the whole project (at most twice).
- **Ask the council:** at any time, with the button in the project title or on an agent's page.

## What strengthens the evidence

- If the project has a **check command**, it runs automatically before every work / check / final council, and its
  output and exit code go to the council as evidence (`[agent] team_check_timeout`, default 600 s).
- A `REVIEW.md` (or `.alice/REVIEW.md`) in the project is read by the council as **review rules** ("every public
  function must have a test"). `/kurul` reads it too.
- **Lessons:** serious findings the council verified are added to the project's lessons, and go to later agents'
  tasks, change requests and councils as "these mistakes came up in this project, don't repeat them". They are visible
  in the Council thread.
- If a weak model forgets the required JSON, it is asked once for the format only; if that fails too, its vote counts
  as empty.

## Depth and models fit the job

Small work: critic + checker and one round. Normal work: the three roles + the relevant experts and two rounds.
Critical work (security, data, payments, core architecture) and the final council: the whole council, up to three
debate rounds, everyone on the strongest model. The critic and checker use the strong model, the advocate and experts
the fast one. There are no endless loops: the number of rounds, checks and finals is limited.

## In the interface

An agent's page has a **Council** tab: the decision, the approval share, any veto, the vote table (role, model, first
→ last vote, weight, and why a vote didn't count), finding cards (severity, who found it, the checker's result,
objections in the debate), conditions, **your vote** (approve / conditional / reject with a reason; if it changes the
decision, it is applied at once) and the debate transcript. Plan and final councils are in the **Council** thread of
the lead's group. You can always make the call entirely yourself with **Approve / Request changes**.
