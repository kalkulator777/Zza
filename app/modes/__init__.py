from app.modes.album import NormalMode, PlagiatMode, SandwichMode
from app.modes.guess import GuessMode

MODES = {mode.key: mode for mode in (GuessMode, NormalMode, SandwichMode, PlagiatMode)}


def build_mode(key, room):
    return MODES.get(key, GuessMode)(room)
