from app.modes.guess import GuessMode

MODES = {GuessMode.key: GuessMode}


def build_mode(key, room):
    return MODES.get(key, GuessMode)(room)
