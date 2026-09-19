"""What Jarvis's voice loop does when someone asks for another mode.

Run it - no microphone, no API key, nothing is sent anywhere:

    python examples/mode_switch_demo.py

Every line below is one thing said out loud. For each one it shows whether
it switches mode, and what the next Live session would be built with. In
the real loop (main.py) the same thing happens twice over:

* The model calls the `set_mode` tool -> `MODE.switch(...)`.
* Whatever the model does, main.py also runs `mode_manager.detect()` on
  the spoken line, so a clear "kids mode" still works if the model missed
  the tool. Switching to the mode already active does nothing, so the two
  paths can't fight each other.

A switch sets an event; the session loop closes the session, drops the
resume handle and reconnects with the new profile - new prompt, new pause
threshold, new tool list, fresh conversation.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mode_manager  # noqa: E402

SAID = [
    "Jarvis, WhatsApp pe kuch aaya?",
    "Jarvis, speak to my daughter",
    "Jarvis, send a WhatsApp to Afnan saying I'll be late",   # child can't
    "apple",
    "Jarvis, back to me",
    "adult mode",                                             # already adult
]


def main():
    reconnects = 0
    # In main.py this callback sets the event the session loop waits on
    mode = mode_manager.ModeManager(on_switch=lambda profile: None)

    for said in SAID:
        print(f'\n🗣️  "{said}"')
        asked = mode_manager.detect(said)
        if asked is None:
            profile = mode.profile
            print(f"    no mode change - still {profile.label}")
            if not profile.allows("send_whatsapp") and "whatsapp" in said.lower():
                print("    (in Child Mode the WhatsApp tools aren't in the session at all,")
                print("     so Jarvis can only say it can't do that)")
            continue

        result = mode.switch(asked)
        if not result["changed"]:
            print(f"    {result['message']}  (no reconnect)")
            continue

        reconnects += 1
        profile = mode.profile
        print(f"    -> {profile.label}: reconnecting with a fresh conversation")
        print(f"       pause threshold : {profile.pause_seconds:.1f}s "
              f"({profile.silence_duration_ms} ms of silence ends a turn)")
        print(f"       speaking pace   : ~{profile.words_per_minute} wpm (prompt guidance)")
        print(f"       recorder pause  : {profile.record_pause:.1f}s floor for word practice")
        print(f"       tools           : "
              f"{'all of them' if profile.tool_names is None else str(len(profile.tool_names)) + ' (coach + Quran only)'}")
        print(f"       opens with      : {profile.greeting}")

    print(f"\n{reconnects} reconnects in total.")


if __name__ == "__main__":
    main()
