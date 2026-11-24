# Voice

Written from the "Ses" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)) and checked against
`alice/voice/` and the `[voice]` defaults in `alice/config.py`. The Turkish section predates `social_mode = "ptt"`;
this page describes it. ALICE speaks Turkish, so the spoken examples are Turkish with their meaning.

ALICE's voice and ears. Install the packages first:

```powershell
pip install -e ".[voice]"          # microphone, playback, push-to-talk, speech detection
pip install -e ".[voice-local]"    # optional: Whisper and the "Hey Alice" model on your computer
alice voice status                 # shows what is ready
```

## 1. Choose a voice (audition)

The candidate voices read the same Turkish sentences in five tones (calm, cheerful, serious, urgent, playful) and the
results open side by side in the browser:

```powershell
alice voice audition                                   # Gemini voices (needs GEMINI_API_KEY)
alice voice audition --local xtts,chatterbox           # local engines too (XTTS: pip install -e ".[voice-xtts]", Chatterbox: alice voice install-local)
alice voice use gemini:Aoede                           # pick the one you like
alice voice library                                    # build the emotion library with this voice
alice voice say "Bingo yakıt, eve dön." --emotion urgent
alice voice models                                     # Gemini voice models on your account (uses no quota)
alice voice install-local                              # installs Chatterbox in its own environment (~/.alice/tts-venv)
```

Local engines clone the voice from the recordings in the emotion library, so whether ALICE speaks from the cloud
(while DCS is open) or from your computer, it sounds the same. In flight mode ALICE doesn't touch the graphics card:
the cloud first, and a local engine on the CPU only as a last resort.

## 2. Talk

```powershell
alice voice           # voice assistant: push-to-talk (Scroll Lock) and "Hey Alice"  (--ptt <key>, --no-wake)
alice voice test      # try the microphone
alice voice on        # start with the core (turn off: alice voice off)
```

- **Push-to-talk:** hold the key, speak, release. The key is `[voice] ptt_key`.
- **"Hey Alice":** hands-free. If you give it a model you trained with
  [openWakeWord](https://github.com/dscripka/openWakeWord) (`wake_model`), it listens fully locally. Otherwise, when
  speech is detected, the start of the sentence is transcribed with a small Whisper model on your computer
  (faster-whisper, `[voice] wake_local = "base"`) to check whether it starts with "Hey Alice"; audio goes to Groq only
  when the wake word is heard. Without faster-whisper every sentence is sent to Groq. `alice voice on` turns voice on
  and starts the background core.
- After ALICE answers you can keep talking without "Hey Alice" for up to 5 seconds (`follow_up`). The time counts
  from when ALICE goes quiet, and it is the moment you start speaking that matters: a sentence started at second 5
  is taken, one started at second 6 isn't (so remarks to the people around you aren't taken as commands). Live
  conversation works the same way: the session closes after 5 seconds of silence (`live_idle`).
- **"Alice sus"** (Alice, hush) stops the conversation, closes the follow-up window and takes nothing as a command
  until you say "Alice" again; the name must then be at the start of the sentence. Push-to-talk always works. Natural
  Turkish forms are recognized ("sus be", "susar mısın", "şşt", "yeter", "kes", "dur", "sessiz ol", "kapa
  çeneni"...), and closing phrases like "tamam teşekkürler", "sağ ol" or "iyi geceler" close the window too. The
  whole sentence must be a hush: "pastayı ikiye kes" (cut the cake in two) or "sus diye bağırdı" (he shouted
  "hush") doesn't silence ALICE. In live conversation a strong word like "sus" also works at the end of a sentence;
  everyday words like "yeter", "kes", "dur" only alone or with the name ("Alice yeter").
- With speakers, ALICE doesn't take its own voice for new speech and cut itself off: while Realtime is answering, the
  microphone stream is muted. So by default wait for ALICE to finish instead of talking over it. With headphones you
  can turn the old barge-in behavior back on with `[voice] live_realtime_echo_guard = false`.
- It doesn't wait for the whole answer; it speaks as soon as the first sentence is ready. If a tool will run, it
  first says it is looking. It doesn't read out code or long paths; it says it put them on the screen.
- When something needs approval, it asks by voice and does it if you say "evet" (yes).
- Speech recognition: Groq's `whisper-large-v3-turbo` (GROQ_API_KEY), with `faster-whisper` on your computer as a
  fallback.

## Intelligence in voice conversations

The live voice model is very fast but weak at knowledge and reasoning. So:

- The live model has a `think` tool. When asked for information, an explanation, advice, a calculation, code, a plan
  or something current, it hands the question to ALICE's brain model (the brain chain). The brain searches the web,
  memory and projects if needed and returns a short spoken answer, which the live model says in its own voice.
  Greetings, the time and commands like open/close/play stay on the fast path. Turn it off with
  `[voice] live_think = false`.
- The last 30 minutes of conversation (voice, live or typed) go into each new live session as "Recent conversation"
  (`live_history_minutes`), so "open it" and "go on" work.
- On the regular voice path used without Live (transcribe, model, speech), questions that need thinking ("why",
  "how", "explain", "compare", "make a plan") go to the brain model instead of the fastest one; commands stay on the
  fast model (`smart_route`).
- Your project names and the names in memory (people, games) are added to the speech recognition hints, so names
  inside Turkish sentences get garbled less.

## Live conversation backends

After "Hey Alice", speech-to-speech conversation can run on two backends:

- **ChatGPT Realtime (default):** uses the OpenAI Realtime compatible `/v1/realtime` endpoint with your ChatGPT
  (Codex) account through the local Claude Code proxy (CLIProxyAPI). This sounds the most natural: the microphone
  streams as 24 kHz PCM, the model answers when you stop, and ALICE's tools run through function calling (with the
  same permission and approval path). If the proxy isn't running, ALICE starts it; if no ChatGPT account is linked,
  it falls back to Gemini Live.
  - **Weak voice model, strong brain:** the live model hands information, planning, code and tool work to ALICE's
    strong model with the `think` tool, then tells the user the strong model's answer (or its question).
  - `[voice] live_backend = "auto" | "chatgpt" | "gemini"` (auto: ChatGPT first, then Gemini).
  - `[voice] live_realtime_model = "gpt-realtime-2.1"`, `live_realtime_voice = "marin"`.
  - After 5 seconds of silence the session and connection close; nothing is sent while nobody is talking. On a quota
    or rate-limit error ALICE goes back to the regular voice path for an hour instead of retrying.
  - Note: this path uses the Codex live voice model. The ChatGPT app's GPT-Live / "Quip" voice isn't offered in the
    API; the conversation is real speech-to-speech, not plain TTS, but not that tone.
  - Needs `~/.alice/cliproxy` installed and a ChatGPT account linked from Connections → Keys → the "Claude Code proxy"
    card.
- **Gemini Live (fallback):** with `GEMINI_API_KEY` the earlier live conversation works as before (Gemini voices,
  `live_models`, `live_voice`).

If neither is available or they can't connect, ALICE goes back to the regular voice path (transcribe → model →
speech) for 5 minutes. Model and voice names are changed in `~/.alice/config.toml` under `[voice]`.

With `[voice] enabled = true` the core (`alice core run`) also starts the voice assistant and reads notifications
aloud. In quiet mode notifications are text only.

## Answer only you, don't wake up by itself

- **Enroll your voice:** `alice voice enroll` has you read six sentences and builds a voiceprint (the offline TitaNet
  model, about 40 MB, downloaded once). After that, another voice saying "Alice" (a friend on Discord, someone in a
  video) doesn't wake it. Push-to-talk and "Alice sus" are not checked. Each comparison's score is written to
  `voice.log`; if it doesn't recognize you, lower `[voice] speaker_threshold` a little. Turn it off with
  `alice voice enroll --off`.
- **Selective waking:** while Discord, TeamSpeak, Zoom or Teams is using the microphone (you are in a voice channel),
  or DCS / War Thunder is open, "Alice" must be at the start of the sentence ("Alice, haritayı aç": open the map) and
  the follow-up window without "Alice" is closed. Telling a friend "şu an Alice'le uğraşıyorum" (I'm busy with Alice)
  doesn't wake it. `[voice] social_mode`: `"wake"` (default, as above), `"ptt"` (the microphone is closed to ALICE
  there, only push-to-talk works) or `false` (off).
- **An answer you didn't like:** say "bu cevap kötüydü" (that answer was bad) or press **Beğenmedim** under the
  answer in the app. The request and answer are added to the `alice eval` test set and tried in later updates
  (`alice eval --feedback`).
- **System → Voice:** listen to the Gemini voices and pick one; in live conversation it applies from the next
  "Hey Alice".
- **Settings → Memory:** see what ALICE knows one by one and delete it. Leftovers from the old "girlfriend" role
  (pet names) are flagged and can be cleared with one click.
