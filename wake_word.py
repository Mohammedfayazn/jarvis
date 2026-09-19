"""'Hey Jarvis' sunne wala - poori tarah offline.

Jarvis so raha ho toh Gemini se connection band rehta hai: kamre ki koi baat
Google tak nahi jati. Mic ka audio sirf is chhote local model (openWakeWord)
tak jata hai, jo sirf yeh batata hai ki "Hey Jarvis" bola gaya ya nahi.

Model pehli baar chalne par ek hi baar download hota hai (~4 MB) aur phir
venv me pada rehta hai.
"""

import numpy as np
from openwakeword import utils
from openwakeword.model import Model

WAKE_MODEL = "hey_jarvis"

# Model 80ms (1280 samples @ 16kHz) ke tukdon par sabse achha chalta hai
FRAME_SAMPLES = 1280
FRAME_BYTES = FRAME_SAMPLES * 2   # int16

# openWakeWord ki default 0.5 thi - laptop mic se door baithe ho toh
# chilla kar bolna padta tha. Test (Windows TTS + shor): 0.4 par "Hey Jarvis"
# thoda zyada pakda gaya, aur galti se jaagna 270 me 4 baar. Isse neeche
# jaane par galat jaagna tezi se badhta hai - asal hal paas wala mic hai.
THRESHOLD = 0.4

# Isse upar magar THRESHOLD se neeche = "lagbhag suna". Log me likhte hain
# taaki asli istemaal ke score dekh kar threshold theek kar sakein.
NEAR_MISS = 0.2
NEAR_MISS_GAP = 2.0   # ek hi baat ke kai frames - ek hi line


class WakeWordListener:
    def __init__(self):
        # Pehle se download ho toh yeh jaldi laut aata hai
        utils.download_models(model_names=[WAKE_MODEL])
        self._model = Model(
            wakeword_models=[WAKE_MODEL], inference_framework="onnx"
        )
        self._pending = bytearray()
        self._near_peak = 0.0
        self._near_at = 0.0
        self._clock = 0.0   # kitne second ka audio suna (asli waqt nahi)

    def reset(self) -> None:
        """Naye sire se sunna - pichli neend ka bacha audio na gine."""
        self._model.reset()
        self._pending.clear()
        self._near_peak = 0.0

    def feed(self, pcm: bytes) -> float:
        """Mic ka PCM do; is tukde tak ka sabse ooncha score lautata hai.

        Mic 20ms ke chunk deta hai, model 80ms maangta hai - beech ka hisaab
        yahin jama hota hai.
        """
        self._pending.extend(pcm)
        best = 0.0
        while len(self._pending) >= FRAME_BYTES:
            frame = np.frombuffer(self._pending[:FRAME_BYTES], dtype=np.int16)
            del self._pending[:FRAME_BYTES]
            scores = self._model.predict(frame)
            best = max(best, float(max(scores.values())))
        return best

    def heard(self, pcm: bytes) -> bool:
        score = self.feed(pcm)
        if score >= THRESHOLD:
            return True
        self._clock += len(pcm) / 32000   # 16 kHz int16
        now = self._clock
        if self._near_peak and now - self._near_at > NEAR_MISS_GAP:
            print(f"(wake word lagbhag suna: {self._near_peak:.2f}, "
                  f"chahiye {THRESHOLD})")
            self._near_peak = 0.0
        if score >= NEAR_MISS:
            self._near_peak = max(self._near_peak, score)
            self._near_at = now
        return False
