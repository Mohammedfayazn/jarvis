"""StorytellingModule: picture scenes to talk about.

Each scene is drawn with large emoji on the HUD (safe, offline, instantly
recognisable to a 4 year old). The child is asked "What do you see?", then
guided from single words to a whole sentence; each scene carries a model
sentence in all three languages for Jarvis to build up to.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Scene:
    key: str
    title: dict            # language -> title
    picture: tuple[str, ...]
    words: tuple[str, ...]          # vocabulary keys that appear
    questions: dict                 # language -> guiding questions, simple first
    model_sentence: dict            # language -> the complete sentence to build to


ASK = {"en": "What do you see?", "nl": "Wat zie je?", "hi": "तुम्हें क्या दिख रहा है?"}

SCENES: tuple[Scene, ...] = (
    Scene("park", {"en": "In the park", "nl": "In het park", "hi": "पार्क में"},
          ("🌳", "☀️", "👧", "🐶", "⚽"), ("tree", "sun", "dog", "ball"),
          {"en": ("Who is there?", "What is the dog doing?", "What is the girl playing with?"),
           "nl": ("Wie is daar?", "Wat doet de hond?", "Waarmee speelt het meisje?"),
           "hi": ("वहाँ कौन है?", "कुत्ता क्या कर रहा है?", "लड़की किससे खेल रही है?")},
          {"en": "The girl is playing with the dog in the park.",
           "nl": "Het meisje speelt met de hond in het park.",
           "hi": "लड़की पार्क में कुत्ते के साथ खेल रही है।"}),
    Scene("breakfast", {"en": "Breakfast time", "nl": "Ontbijt", "hi": "नाश्ते का समय"},
          ("👩", "👧", "🍞", "🥛", "🍎"), ("mother", "bread", "milk", "apple"),
          {"en": ("Who is eating?", "What is there to eat?", "What is in the glass?"),
           "nl": ("Wie eet er?", "Wat is er te eten?", "Wat zit er in het glas?"),
           "hi": ("कौन खा रहा है?", "खाने में क्या है?", "गिलास में क्या है?")},
          {"en": "Mum and the girl are eating bread and drinking milk.",
           "nl": "Mama en het meisje eten brood en drinken melk.",
           "hi": "माँ और बच्ची ब्रेड खा रही हैं और दूध पी रही हैं।"}),
    Scene("rain", {"en": "A rainy day", "nl": "Een regenachtige dag", "hi": "बारिश का दिन"},
          ("🌧️", "🏠", "🐱", "☂️"), ("rain", "cat"),
          {"en": ("What is the weather?", "Where is the cat?", "What do you need in the rain?"),
           "nl": ("Wat voor weer is het?", "Waar is de kat?", "Wat heb je nodig als het regent?"),
           "hi": ("मौसम कैसा है?", "बिल्ली कहाँ है?", "बारिश में क्या चाहिए?")},
          {"en": "It is raining, so the cat stays inside the house.",
           "nl": "Het regent, dus de kat blijft in het huis.",
           "hi": "बारिश हो रही है, इसलिए बिल्ली घर के अंदर है।"}),
    Scene("beach", {"en": "At the beach", "nl": "Op het strand", "hi": "समुद्र किनारे"},
          ("☀️", "🌊", "🏖️", "🐟", "⛵"), ("sun", "sea", "fish"),
          {"en": ("What is in the sky?", "What is in the water?", "What is the boat doing?"),
           "nl": ("Wat is er in de lucht?", "Wat zit er in het water?", "Wat doet de boot?"),
           "hi": ("आसमान में क्या है?", "पानी में क्या है?", "नाव क्या कर रही है?")},
          {"en": "The sun is shining and a boat is sailing on the sea.",
           "nl": "De zon schijnt en er vaart een boot op de zee.",
           "hi": "सूरज चमक रहा है और समुद्र में एक नाव चल रही है।"}),
    Scene("farm", {"en": "On the farm", "nl": "Op de boerderij", "hi": "खेत पर"},
          ("🐄", "🐴", "🐔", "🌾", "🚜"), ("cow", "horse"),
          {"en": ("Which animals do you see?", "What does the cow say?", "What colour is the horse?"),
           "nl": ("Welke dieren zie je?", "Wat zegt de koe?", "Welke kleur heeft het paard?"),
           "hi": ("कौन से जानवर दिख रहे हैं?", "गाय क्या बोलती है?", "घोड़ा किस रंग का है?")},
          {"en": "The cow and the horse are on the farm.",
           "nl": "De koe en het paard staan op de boerderij.",
           "hi": "गाय और घोड़ा खेत पर हैं।"}),
    Scene("school", {"en": "Going to school", "nl": "Naar school", "hi": "स्कूल चलो"},
          ("🏫", "🧒", "🎒", "📖", "✏️"), ("bag", "book", "pencil"),
          {"en": ("Where are they going?", "What is in the bag?", "What do you do at school?"),
           "nl": ("Waar gaan ze naartoe?", "Wat zit er in de tas?", "Wat doe je op school?"),
           "hi": ("वे कहाँ जा रहे हैं?", "बस्ते में क्या है?", "स्कूल में तुम क्या करती हो?")},
          {"en": "The children go to school with a book and a pencil in their bags.",
           "nl": "De kinderen gaan naar school met een boek en een potlood in hun tas.",
           "hi": "बच्चे बस्ते में किताब और पेंसिल लेकर स्कूल जाते हैं।"}),
    Scene("night", {"en": "Night time", "nl": "Het is nacht", "hi": "रात का समय"},
          ("🌙", "⭐", "🛏️", "🧸"), ("moon", "star"),
          {"en": ("What is in the sky?", "What time is it?", "Who sleeps with you?"),
           "nl": ("Wat is er in de lucht?", "Hoe laat is het?", "Wie slaapt er bij jou?"),
           "hi": ("आसमान में क्या है?", "कौन सा समय है?", "तुम्हारे साथ कौन सोता है?")},
          {"en": "The moon and the stars are shining, and it is time to sleep.",
           "nl": "De maan en de sterren schijnen, en het is tijd om te slapen.",
           "hi": "चाँद और तारे चमक रहे हैं, और सोने का समय है।"}),
    Scene("birthday", {"en": "A birthday party", "nl": "Een verjaardagsfeest", "hi": "जन्मदिन की पार्टी"},
          ("🎂", "🎈", "🎁", "👧", "🎉"), (),
          {"en": ("What is on the table?", "Whose birthday is it?", "What is in the present?"),
           "nl": ("Wat staat er op tafel?", "Wie is er jarig?", "Wat zit er in het cadeau?"),
           "hi": ("मेज़ पर क्या है?", "किसका जन्मदिन है?", "तोहफ़े में क्या है?")},
          {"en": "The girl has a birthday cake, balloons and a present.",
           "nl": "Het meisje heeft een taart, ballonnen en een cadeau.",
           "hi": "लड़की के पास जन्मदिन का केक, गुब्बारे और एक तोहफ़ा है।"}),
)
SCENES_BY_KEY = {s.key: s for s in SCENES}


class StorytellingModule:
    def __init__(self, tracker):
        self.tracker = tracker

    def next_scene(self, child, language: str, key: str | None = None) -> Scene:
        if key and key in SCENES_BY_KEY:
            return SCENES_BY_KEY[key]
        done = self.tracker.lessons(child)
        for scene in SCENES:
            if f"story:{scene.key}:{language}" not in done:
                return scene
        return SCENES[len(done) % len(SCENES)]

    @staticmethod
    def display(scene: Scene, language: str) -> dict:
        return {"mode": "scene", "title": scene.title[language], "picture": list(scene.picture),
                "question": ASK[language]}

    @staticmethod
    def guide(scene: Scene, language: str, name: str) -> str:
        qs = " / ".join(scene.questions[language])
        return (f"Story time - {scene.title[language]}. Ask: \"{ASK[language]}\" Let {name} name "
                f"things first, then guide with: {qs}. Build up, one step at a time, to the "
                f"whole sentence: \"{scene.model_sentence[language]}\" - and praise every step.")
