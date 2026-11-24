# Smartwatch and phone

Written from the "Akıllı saat ve telefon" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)) and
checked against `alice/app/phone.py`, `alice/app/phone_apk.py`, `alice/app/call.py`, `alice/core/rules.py` and
`.github/workflows/android.yml`. The phone app's interface is in Turkish; names are given with their meaning.

## Smartwatch

The Huawei Watch GT 2 Pro has no heart rate broadcast of its own, so the data comes through Gadgetbridge on the
phone. There are two ways:

1. **Exported database (every few minutes):** turn on automatic export in Gadgetbridge, sync the file to the computer
   with a tool like Syncthing, and set that file or folder as `[watch] export_path` in `config.toml`. The core imports
   heart rate, steps, stress and SpO2 whenever the file changes. By hand: `alice watch import Gadgetbridge.db`.
2. **Live:** `alice phone` (or `[phone] enabled = true`) opens a bridge on the local network. An automation on the
   phone (Tasker, Automate, HTTP Shortcuts) can send the heart rate to `POST http://<computer>:8765/api/watch` with
   the header `X-Alice-Token: <token>` and the body `{"hr": 88}`.

`alice watch status` measures how often data arrives and says what is possible with that rate: live, every few
minutes, or rarely.

With watch data:

- You can ask "why was my heart rate high today?" or "how many steps did I take today?" (the `metrics` tool).
- If your heart rate stays above 125 for 10 minutes outside a flight, ALICE asks whether you are OK. It checks with
  the fast decision model first, to avoid false alarms.
- The flight report includes a heart rate chart.

## ALICE on the phone (Android app)

1. On the computer, open **System → Setup → ALICE on the phone** (or run `alice phone`). A QR code and an address
   appear.
2. Scan the QR code with the phone's camera. The phone and the computer must be on the same Wi-Fi. If the app isn't
   installed yet, the page opens in the browser: **Daha fazla (More) → Install the Android app** downloads the APK
   from the computer. Android asks for the "unknown sources" permission once.
3. Open the app and scan the QR code again (or choose "open with ALICE" when the camera reads it).

The app has five tabs:

- **Chat:** past chats (long press: rename, pin, delete), the steps card, adding images, mode/model/thinking choice.
  Tapping the microphone turns on the phone's speech recognition; what you say is transcribed live and sent when you
  stop.
- **Projects:** cards, answering an agent's question from its card, the agents' conversation, review (approve / ask
  for changes), new project.
- **Computer:** live screen (tap: full size), CPU/RAM/GPU/temperature with small charts of the last 10 minutes, the
  song playing, open windows (tap: bring to front), type/open/copy to clipboard on the computer, lock/sleep/HUD/shut
  down.
- **Notifications:** actions ALICE asks permission for, its multiple-choice questions and the projects' questions; a
  timeline of events on the computer.
- **More:** today's summary, Do Not Disturb and "Hey Alice" switches, automations, notes, health, model, appearance,
  connection.

Even when the app is closed, you get a notification when ALICE asks for permission, and **Allow / Deny** in the
notification answers it without opening the app. Important warnings from the computer (GPU too hot, disk full) and
projects' questions arrive as notifications too. For this the app stays connected in the background with a quiet
"ALICE connected" notification.

The address can't be used without its long token, which is kept in `~/.alice/phone-token` (`alice phone --new-token`
changes it; then scan again from **More → Connection** in the app). The connection is plain HTTP on the local
network, so open it only on a network you trust. Without the app the same page opens in the phone's browser; there,
voice input uses the phone's recorder.

## Calls

The phone button at the top of the chat screen calls ALICE, and ALICE can call you too ("call me in half an hour", a
reminder, a job it should tell you about when done). On an incoming call, if the app is open, the call screen opens
and rings; if it is closed, a full-screen "ALICE is calling" notification appears on the lock screen (**Answer /
Decline**); unanswered after 45 seconds, it becomes a missed call. During the call the phone's speech recognition
listens (your words are transcribed live) and sends what you said to the computer when you stop; ALICE's answer is
turned into speech on the computer sentence by sentence and played on the phone, so it starts talking before the
whole answer is ready. Tapping the picture in the middle while ALICE is talking makes it stop and listen. The call is
saved as a separate chat with all of ALICE's tools available; if something needs permission, the approval card
appears on the call screen. Saying "görüşürüz" or "kapat" (bye, hang up) makes ALICE say goodbye and hang up. If the
computer can't produce speech (no key or quota), the phone reads the sentence with its own voice. There are Mute,
Type (for places where you can't talk) and Hang up buttons. The phone's browser doesn't allow the microphone over
plain HTTP, so calls work in the Android app.

## Updates

The interface itself comes from the computer, so most changes reach the phone without installing anything. When the
app itself changes, GitHub builds the APK on a change under `android/` and puts it on the `android-apk` branch; ALICE
on the computer fetches that branch every half hour, and the app on the phone asks every few hours (or when opened)
and installs the new version itself. On Android 12 and later it installs without asking; the first time, the
"install apps from this source" permission is asked once. To check by hand: **More → App**.

Android updates an app only with a version signed by the same key. The key lives in GitHub under Settings → Secrets and
variables → Actions: `ALICE_KEYSTORE_B64` (base64 .jks) and `ALICE_KEYSTORE_PASSWORD`, plus `ALICE_KEY_ALIAS` if the
key name isn't `alice`. Without these the APK is built but not published to phones.
