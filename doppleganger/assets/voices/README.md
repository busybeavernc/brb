Voice profiles for spoken guidance while recording a loop (see doppleganger/voice.py).

One folder per voice, named as it should appear in Settings (e.g. `Jenny/`, `Mark/`), holding MP3s named after the
moments in voice.EVENTS: rec_intro.mp3, rec_get_ready.mp3, count_3.mp3 ... loop_ready.mp3. Missing files are skipped.
`voices.json` names the default voice: {"default": "Guidance"}.

Bundled voices: Guidance (default), Everyday, Balance, Anchor - Artlist AI voiceovers, American English,
trimmed and loudness-matched (-16 LUFS). To add a voice, record these lines with the same file names:

| file | line |
|---|---|
| rec_intro | Let's record your loop. Here's how it works. Sit just as you will in your meeting, and pick a spot on the screen to look at. Remember that spot. After a short countdown, hold still for about ten seconds. Then relax and act naturally, as if you're listening in a meeting. Near the end, I'll ask you to return to your starting position and hold still again. The whole recording takes about a minute and a half, unless you've changed the length in Settings. When you're ready, press Enter or Space. |
| rec_get_ready | Get into your starting position, eyes on your spot, and hold still. |
| count_3 / count_2 / count_1 | Three. / Two. / One. (under 1 second each) |
| rec_hold_start | Recording. Hold still. |
| rec_relax | Now relax. Blink, nod and listen naturally, as if you're in the meeting. |
| rec_less_movement | A little less movement. |
| rec_halfway | Halfway there. Keep it natural. |
| rec_return_soon | In a moment, return to your starting position, eyes on your spot. |
| rec_return_now | Now return to your starting position. Eyes back on your spot, same expression. |
| rec_matched | That's it. Hold still. |
| rec_off_match | Back to your starting position, eyes on your spot. |
| rec_done | All done. Preparing your loop. |
| loop_ready | Your new loop is ready. |
| rec_cancelled | Recording cancelled. |
