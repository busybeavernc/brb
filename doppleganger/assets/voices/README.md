Voice profiles for spoken guidance while recording a loop (see doppleganger/voice.py).

One folder per voice, named as it should appear in Settings (e.g. `Jenny/`, `Mark/`), holding MP3s named after the
moments in voice.EVENTS: rec_intro.mp3, rec_get_ready.mp3, count_3.mp3 ... loop_ready.mp3. Missing files are skipped.
`voices.json` names the default voice: {"default": "Jenny"}.
