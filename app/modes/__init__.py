from app.modes.album import (
    AnimationMode,
    CorpseMode,
    MissingMode,
    CompleteMode,
    CoopMode,
    NormalMode,
    PlagiatMode,
    SandwichMode,
)
from app.modes.guess import GuessMode

MODES = {mode.key: mode for mode in (
    GuessMode, NormalMode, SandwichMode, PlagiatMode,
    AnimationMode, CompleteMode, CoopMode, CorpseMode, MissingMode,
)}


def build_mode(key, room):
    return MODES.get(key, GuessMode)(room)
