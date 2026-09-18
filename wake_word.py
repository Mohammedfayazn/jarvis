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

# 0.5 openWakeWord ki default hai. Kam karoge toh door se ya dheere bola
# hua bhi pakdega, lekin TV/gaane se galti se jaagne ka khatra badhega.
THRESHOLD = 0.5


class WakeWordListener:
    def __init__(self):
        # Pehle se download ho toh yeh jaldi laut aata hai
        utils.download_models(model_names=[WAKE_MODEL])
        self._model = Model(
            wakeword_models=[WAKE_MODEL], inference_framework="onnx"
        )
        self._pending = bytearray()

    def reset(self) -> None:
        """Naye sire se sunna - pichli neend ka bacha audio na gine."""
        self._model.reset()
        self._pending.clear()

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
        return self.feed(pcm) >= THRESHOLD
