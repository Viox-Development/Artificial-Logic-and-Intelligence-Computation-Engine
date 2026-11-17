"""ALICE's voice and ears.

  audio     -> PCM helpers, WAV read/write, playback (cancellable) and microphone
  tone      -> emotion selection based on what is being said (calm, cheerful, serious, urgent, playful)
  tts       -> voice engines: Gemini (cloud), XTTS and Chatterbox (local, clones from emotion library)
  stt       -> speech-to-text: Groq Whisper (cloud), faster-whisper (local)
  speaker   -> speaker that synthesizes sentence by sentence, plays sequentially, and shuts up when interrupted
  listen    -> speech detection (VAD), push-to-talk and "Hey Alice"
  assistant -> listen -> understand -> ALICE -> speak loop
  audition  -> listening test for voice selection
"""
