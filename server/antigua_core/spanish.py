"""Spanish commands and replies (docs/spanish_support_plan.md, Phase 2).

Rather than a Spanish alternative in every skill parser, a Spanish command is
rewritten into the English command those parsers already understand ("pon un
temporizador de cinco minutos" → "set a timer for 5 minutes"), so all the
tested skill logic is reused. The skill's English reply is then turned back
into Spanish: templates for the frequent ones (instant), and the pipeline asks
the LLM to translate anything else.

A match here is also evidence the turn was Spanish: a two-word "Siguiente
canción" is too short for STT language detection, but comes through as
Spanish text, so the reply should be Spanish too.
"""

import re
import unicodedata


def _norm(text: str) -> str:
    """Lowercase, no accents or punctuation, no politeness around the command."""
    t = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    t = re.sub(r"[^\w\s:%]", " ", t)
    t = " ".join(t.split())
    t = re.sub(r"^(?:(?:alexa|antigua|oye|hey|ey|porfa|por favor|a ver)\s+)+", "", t)
    t = re.sub(r"^(?:(?:me |nos )?(?:puedes|podrias|quieres)\s+|quiero que\s+)", "", t)
    t = re.sub(r"\s+(?:por favor|porfa|porfavor|gracias)$", "", t)
    return t.strip()


_UNITS = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21, "veintiuno": 21,
    "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25,
    "veintiseis": 26, "veintisiete": 27, "veintiocho": 28, "veintinueve": 29,
}
_TENS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70,
         "ochenta": 80, "noventa": 90, "cien": 100}
_NUM = (r"\d{1,3}|(?:" + "|".join(_TENS) + r")(?:\s+y\s+(?:" + "|".join(_UNITS) + r"))?|"
        + "|".join(sorted(_UNITS, key=len, reverse=True)))


def _num(words: str) -> int | None:
    words = words.strip()
    if words.isdigit():
        return int(words)
    m = re.fullmatch(r"(\w+)(?:\s+y\s+(\w+))?", words)
    if not m:
        return None
    if m.group(1) in _TENS:
        return _TENS[m.group(1)] + (_UNITS.get(m.group(2), 0) if m.group(2) else 0)
    return _UNITS.get(m.group(1)) if not m.group(2) else None


# ── things Spanish commands point at ──────────────────────────────────────────

# Spoken speaker → a key of settings.MUSIC_SPEAKERS
_SPEAKERS = [
    (r"(?:la )?(?:sala|tele|television|tv|apple tv)", "living room"),
    (r"(?:la )?cocina", "kitchen"),
    (r"(?:la )?barra(?: de sonido)?|(?:el |la bocina )?jbl", "soundbar"),
    (r"(?:el |la )?(?:cuarto|recamara|homepod|dormitorio)", "bedroom"),
]
_SPEAKER = r"\s+en\s+(?P<spk>" + "|".join(p for p, _ in _SPEAKERS) + r")$"

# Govee fixtures (the English phrases parse_govee_request knows)
_LIGHTS = [
    (r"(?:todas )?(?:las )?luces", "the lights"),
    (r"(?:las )?(?:luces )?del? pasillo", "the hallway lights"),
    (r"(?:el |la )?(?:candelabro|arana|candil|lampara)|(?:las )?luces de la cocina", "the chandelier"),
    (r"(?:la )?barra de (?:luz de )?la tele", "the tv bar"),
    (r"(?:las )?(?:luces )?rosa(?:s|das)?", "the pink lights"),
    (r"(?:el )?neon|(?:la )?manguera(?: de luz)?", "the rope light"),
    (r"(?:la )?tira del monitor", "the monitor strip"),
]
_COLORS = {"rojo": "red", "roja": "red", "verde": "green", "azul": "blue", "morado": "purple",
           "morada": "purple", "violeta": "violet", "rosa": "pink", "rosado": "pink",
           "naranja": "orange", "anaranjado": "orange", "amarillo": "yellow",
           "amarilla": "yellow", "cian": "cyan", "turquesa": "teal", "magenta": "magenta",
           "blanco calido": "warm white", "blanco frio": "cool white", "blanco": "white"}

_TV = r"(?:la )?(?:tele|television|tv)"
_TV_APPS = r"netflix|youtube|disney(?: plus)?|hulu|max|hbo(?: max)?|prime(?: video)?|plex|peacock|paramount(?: plus)?|apple tv|tubi|pluto"


def _speaker_en(spoken: str | None) -> str:
    if not spoken:
        return ""
    for pat, key in _SPEAKERS:
        if re.fullmatch(pat, spoken):
            return f" on the {key}"
    return ""


def _light_en(spoken: str) -> str | None:
    for pat, en in _LIGHTS:
        if re.fullmatch(pat, spoken):
            return en
    return None


def _time_en(hour: str, rest: str | None, period: str | None) -> str | None:
    h = _num(hour)
    if h is None or not 0 <= h <= 23:
        return None
    m = 0
    if rest:
        rest = rest.strip()
        if rest == "y media":
            m = 30
        elif rest == "y cuarto":
            m = 15
        elif rest == "menos cuarto":
            h, m = (h - 1) % 24, 45
        elif rest.startswith(("y ", ":")):
            m = _num(rest[1:].strip() if rest.startswith(":") else rest[2:]) or 0
    suffix = ""
    if period in ("manana", "madrugada"):
        suffix = " am"
    elif period in ("tarde", "noche"):
        suffix = " pm"
    return f"{h}:{m:02d}{suffix}" if m else f"{h}{suffix}"


# ── the command table: normalized Spanish regex → English command ────────────

_DUR = r"(?P<n>" + _NUM + r")\s+(?P<unit>segundos?|minutos?|horas?)"
_HOUR = r"(?P<h>" + _NUM + r")(?P<rest>\s+y\s+media|\s+y\s+cuarto|\s+menos\s+cuarto|\s+y\s+(?:" + _NUM + r")|:\d{2})?"
_PERIOD = r"(?:\s+de\s+la\s+(?P<period>manana|madrugada|tarde|noche))?"
_UNIT_EN = {"segundo": "second", "minuto": "minute", "hora": "hour"}


def _timer(m):
    n = _num(m.group("n"))
    unit = _UNIT_EN[m.group("unit").rstrip("s")]
    return f"set a timer for {n} {unit}{'s' if n != 1 else ''}" if n else None


def _lights_power(m):
    target = _light_en(m.group("what"))
    on = m.group("verb") in ("prende", "enciende", "prendeme", "enciendeme")
    return f"turn {'on' if on else 'off'} {target}" if target else None


def _lights_color(m):
    target = _light_en(m.group("what") or "las luces")
    color = _COLORS.get(m.group("color"))
    return f"set {target} to {color}" if target and color else None


def _music_play(m):
    what = m.group("what").strip()
    # "pon atención a lo que te digo" is conversation, and so is anything long
    if re.match(r"(?:atencion|cuidado|ojo|mucho ojo)\b", what) or len(what.split()) > 6:
        return None
    if re.fullmatch(r"(?:la |algo de |un poco de )?musica", what):
        return "play music"
    what = re.sub(r"^(?:algo de |un poco de |musica de |canciones de |la musica de |las canciones de )", "", what)
    return f"play {what}{_speaker_en(m.group('spk'))}" if what else None


_RULES = [
    # ── time and date
    (r"(?:que hora es|que horas son|(?:me )?(?:dices|das) la hora|dime la hora)", lambda m: "what time is it"),
    (r"(?:que dia es(?: hoy)?|que fecha es(?: hoy)?|a que (?:dia|fecha) estamos|cual es la fecha(?: de hoy)?)",
     lambda m: "what's the date today"),

    # ── weather
    (r"(?:va a llover|llovera)(?P<when> hoy| manana)?",
     lambda m: "will it rain tomorrow" if m.group("when") == " manana" else "will it rain today"),
    (r"(?:que tiempo hace|como esta el (?:clima|tiempo)|(?:como estara |que tal )?el (?:clima|tiempo)(?: de hoy| hoy)?|como esta afuera)",
     lambda m: "what's the weather"),
    (r"(?:el )?(?:clima|tiempo|pronostico) (?:de|para) manana|como (?:va a estar|estara) el (?:clima|tiempo) manana",
     lambda m: "what's the weather tomorrow"),
    (r"(?:que temperatura hace|cuantos grados hace|a cuantos grados estamos)", lambda m: "what's the temperature"),
    (r"hace (?:mucho )?calor(?: afuera)?", lambda m: "how hot is it"),
    (r"hace (?:mucho )?frio(?: afuera)?", lambda m: "how cold is it"),

    # ── timers and alarms
    (r"(?:pon(?:me)?|crea|programa|inicia|empieza)(?: un| el)? (?:temporizador|timer|cronometro)(?: de| por| para)? " + _DUR, _timer),
    (r"(?:temporizador|timer)(?: de| por)? " + _DUR, _timer),
    (r"(?:pon(?:me)?|crea)(?: un| el)? (?:temporizador|timer)(?: de| por| para)? media hora", lambda m: "set a timer for 30 minutes"),
    (r"(?:cancela|quita|borra|deten|para)(?: el| la)? (?:temporizador|timer|cronometro)", lambda m: "cancel the timer"),
    (r"(?:cancela|quita|borra)(?: todos los| todas las)? (?:temporizadores|timers|alarmas)", lambda m: "cancel all timers"),
    (r"(?:cancela|quita|borra)(?: la)? alarma", lambda m: "cancel the alarm"),
    (r"cuanto (?:tiempo )?(?:le )?(?:falta|queda)(?: al (?:temporizador|timer)| en el (?:temporizador|timer)| para el (?:temporizador|timer))?",
     lambda m: "how much time is left on the timer"),
    (r"(?:despiertame|levantame|pon(?:me)?(?: una| la)? alarma|ponme el despertador)(?: manana)?(?: a| para)? (?:las |la )" + _HOUR + _PERIOD,
     lambda m: (lambda t: f"wake me up at {t}" if t else None)(_time_en(m.group("h"), m.group("rest"), m.group("period")))),

    # ── TV (before music: "pon Netflix" is the TV, "pon Bad Bunny" is music)
    (r"(?:prende|enciende|prendeme|pon)(?: la)? " + _TV, lambda m: "turn on the tv"),
    (r"(?:apaga|apagame)(?: la)? " + _TV, lambda m: "turn off the tv"),
    (r"(?:silencia|mutea|quitale el (?:sonido|volumen) a)(?: la)? " + _TV, lambda m: "mute the tv"),
    (r"(?:quita(?:le)? el silencio a|dale sonido a)(?: la)? " + _TV, lambda m: "unmute the tv"),
    (r"(?:subele|sube(?: el volumen)?)(?: a| de)?(?: la)? " + _TV, lambda m: "turn up the tv"),
    (r"(?:bajale|baja(?: el volumen)?)(?: a| de)?(?: la)? " + _TV, lambda m: "turn down the tv"),
    (r"(?:pausa|pon pausa a)(?: la)? " + _TV, lambda m: "pause the tv"),
    (r"(?:pon|abre|ponme) (?P<app>" + _TV_APPS + r")(?: en la (?:tele|television|tv))?", lambda m: f"open {m.group('app')} on the tv"),

    # ── lights
    (r"(?P<verb>prende|enciende|apaga|prendeme|enciendeme|apagame) (?P<what>.+)", _lights_power),
    (r"pon (?P<what>.+?) (?:en |de |a )?(?:color )?(?P<color>" + "|".join(sorted(_COLORS, key=len, reverse=True)) + ")",
     _lights_color),
    (r"pon (?P<what>.+?) al (?P<n>" + _NUM + r")(?: por ciento| %)?",
     lambda m: (lambda t, n: f"set {t} to {n} percent" if t and n is not None else None)(_light_en(m.group("what")), _num(m.group("n")))),

    # ── music controls
    (r"(?:pausa|pausar|pon pausa|pausale|pausala|detente|para|parale|deten)(?: la| a la| esta)?(?: musica| cancion)?", lambda m: "pause"),
    (r"(?:quita|para|deten|apaga)(?: la)? musica", lambda m: "pause"),
    (r"(?:continua|continuar|sigue|reanuda|quita la pausa|dale play|ponle play)(?: con)?(?: la)?(?: musica| cancion)?", lambda m: "resume"),
    (r"(?:siguiente|la siguiente|proxima|la proxima|saltala|saltate(?: esta)?|salta(?: esta)?|cambiala|otra|pasala|adelanta)(?: cancion| rola)?",
     lambda m: "next song"),
    (r"(?:anterior|la anterior|regresa|regresala|la de antes)(?: cancion| rola)?", lambda m: "previous song"),
    # Not a bare "otra vez": that's as likely to mean "say that again".
    (r"(?:ponla|tocala|repitela) (?:otra vez|de nuevo|desde el principio)", lambda m: "restart the song"),
    (r"(?:subele|sube(?:le)? el volumen|mas fuerte|mas alto|subele al volumen|zubele)(?P<music> a la musica| la musica)?",
     lambda m: "turn the music up" if m.group("music") else "turn it up"),
    (r"(?:bajale|bachale|baja(?:le)? el volumen|mas bajo|mas bajito|mas quedito)(?P<music> a la musica| la musica)?",
     lambda m: "turn the music down" if m.group("music") else "turn it down"),
    (r"(?:pon|sube|baja)(?: la musica| el volumen(?: de la musica)?) (?:al|a) (?P<n>" + _NUM + r")(?: por ciento| %)?",
     lambda m: (lambda n: f"set the music volume to {n}" if n is not None else None)(_num(m.group("n")))),
    (r"(?:que (?:cancion|rola) es esta|que esta sonando|quien canta esta(?: cancion)?)", lambda m: "what's playing"),
    (r"(?:pon|ponme|reproduce|toca)(?: el)? (?:ultimo|nuevo|mas nuevo) (?:album|disco) de (?P<who>.+)",
     lambda m: f"play the newest album by {m.group('who')}"),
    (r"(?:pon|ponme|reproduce|toca)(?: el)? (?:album|disco) (?P<what>.+?)(?: de (?P<who>.+))?",
     lambda m: f"play the album {m.group('what')}" + (f" by {m.group('who')}" if m.group("who") else "")),
    (r"(?:pon|ponme|reproduce|toca)(?: la)? cancion (?P<what>.+?)(?: de (?P<who>.+))?",
     lambda m: f"play the song {m.group('what')}" + (f" by {m.group('who')}" if m.group("who") else "")),
    (r"(?:pon|ponme|ponnos|reproduce|toca|quiero (?:escuchar|oir)) (?P<what>.+?)(?:" + _SPEAKER + r")?", _music_play),
]
_RULES = [(re.compile(p), f) for p, f in _RULES]


def to_english_command(text: str) -> str | None:
    """The English command for a Spanish one, or None if it isn't one we know.
    Accents and politeness are ignored; "Súbele" and "subele" are the same."""
    t = _norm(text)
    if not t:
        return None
    for rx, build in _RULES:
        m = rx.fullmatch(t)
        if m:
            english = build(m)
            if english:   # a rule can match and still not apply ("apaga la musica" isn't a light)
                return english
    return None


# ── replies ───────────────────────────────────────────────────────────────────

_DAYS = {"Monday": "lunes", "Tuesday": "martes", "Wednesday": "miércoles", "Thursday": "jueves",
         "Friday": "viernes", "Saturday": "sábado", "Sunday": "domingo"}
_MONTHS = {"January": "enero", "February": "febrero", "March": "marzo", "April": "abril",
           "May": "mayo", "June": "junio", "July": "julio", "August": "agosto",
           "September": "septiembre", "October": "octubre", "November": "noviembre",
           "December": "diciembre"}
_PLACES = {"the soundbar": "la barra de sonido", "the bedroom HomePod": "el HomePod del cuarto",
           "the living room": "la sala", "the kitchen": "la cocina", "the bedroom": "el cuarto"}
_TARGETS = {"the lights": "las luces", "all the lights": "todas las luces",
            "the hallway lights": "las luces del pasillo", "the kitchen chandelier": "el candelabro",
            "the TV bar": "la barra de la tele", "the pink lights": "las luces rosas",
            "the rope light": "el neón", "the monitor strip": "la tira del monitor"}
_COLORS_ES = {"red": "rojo", "green": "verde", "blue": "azul", "purple": "morado", "violet": "violeta",
              "pink": "rosa", "orange": "naranja", "yellow": "amarillo", "cyan": "cian",
              "teal": "turquesa", "magenta": "magenta", "white": "blanco",
              "warm white": "blanco cálido", "cool white": "blanco frío"}
_TV_ES = "la tele"
_FIXED = {
    "I can't play music right now.": "Ahora no puedo poner música.",
    "I can't control the TV right now.": "Ahora no puedo controlar la tele.",
    "I can't control the lights right now.": "Ahora no puedo controlar las luces.",
    "I couldn't reach the music server.": "No pude conectarme con la música.",
    "What would you like to hear?": "¿Qué quieres escuchar?",
    "You don't have any timers or alarms set.": "No tienes temporizadores ni alarmas.",
    "Hmm, I didn't quite catch that. Could you say it again?": "Mmm, no te entendí bien. ¿Me lo repites?",
    "Sorry, I missed that. Want to try once more?": "Perdón, no te escuché. ¿Otra vez?",
    "I didn't hear that clearly. Could you repeat it?": "No te oí bien. ¿Me lo repites?",
}


def _period_es(hour: int, ampm: str) -> str:
    if ampm == "AM":
        return "de la madrugada" if hour < 6 or hour == 12 else "de la mañana"
    return "del mediodía" if hour == 12 else "de la tarde" if hour < 8 else "de la noche"


def _clock_es(h: str, mm: str | None, ampm: str) -> str:
    hour = int(h)
    art = "la" if hour == 1 else "las"
    return f"{art} {hour}{':' + mm if mm else ''} {_period_es(hour, ampm)}"


def _duration_es(d: str) -> str:
    d = re.sub(r"\b(\d+) hours?\b", lambda m: f"{m.group(1)} hora{'s' if m.group(1) != '1' else ''}", d)
    d = re.sub(r"\b(\d+) minutes?\b", lambda m: f"{m.group(1)} minuto{'s' if m.group(1) != '1' else ''}", d)
    d = re.sub(r"\b(\d+) seconds?\b", lambda m: f"{m.group(1)} segundo{'s' if m.group(1) != '1' else ''}", d)
    parts = d.replace(" and ", " ").split(" ")
    words = [" ".join(parts[i:i + 2]) for i in range(0, len(parts), 2)]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " y " + words[-1]


def _playing(m):
    what, place = m.group("what"), m.group("place")
    what = re.sub(r" by ([^,]+)$", r", de \1", what)
    return f"Poniendo {what}{' en ' + _PLACES.get(place, place) if place else ''}."


_REPLIES = [
    (r"It's (?P<h>\d{1,2})(?::(?P<mm>\d{2}))? (?P<ampm>AM|PM)\.",
     lambda m: f"{'Es' if m.group('h') == '1' else 'Son'} {_clock_es(m.group('h'), m.group('mm'), m.group('ampm'))}."),
    (r"Today is (?P<day>\w+), (?P<month>\w+) (?P<d>\d{1,2})\.",
     lambda m: f"Hoy es {_DAYS.get(m.group('day'), m.group('day'))} {m.group('d')} de {_MONTHS.get(m.group('month'), m.group('month'))}."),
    (r"Timer set for (?P<d>.+)\.", lambda m: f"Temporizador de {_duration_es(m.group('d'))}."),
    (r"Alarm set for (?P<h>\d{1,2})(?::(?P<mm>\d{2}))? (?P<ampm>AM|PM)(?P<when> tomorrow| today)?\.",
     lambda m: f"Alarma para {'mañana ' if m.group('when') == ' tomorrow' else 'hoy ' if m.group('when') else ''}a "
               f"{_clock_es(m.group('h'), m.group('mm'), m.group('ampm'))}."),
    (r"Playing (?P<what>.+?)(?: on (?P<place>the soundbar|the bedroom HomePod|the living room|the kitchen|the bedroom))?\.", _playing),
    (r"Music volume (?P<n>\d+)\.", lambda m: f"Volumen de la música al {m.group('n')}."),
    (r"Cancelled all (?P<n>\d+)\.", lambda m: f"Cancelé los {m.group('n')}."),
    (r"Cancelled (?P<n>\d+)\.", lambda m: f"Cancelé {m.group('n')}."),
    (r"Cancelled the (?:[\w -]+ )?timer\.", lambda m: "Cancelé el temporizador."),
    (r"Cancelled the (?:[\w -]+ )?alarm\.", lambda m: "Cancelé la alarma."),
    (r"Turning on the Living Room TV", lambda m: f"Prendiendo {_TV_ES}."),
    (r"Turning off the Living Room TV", lambda m: f"Apagando {_TV_ES}."),
    (r"Muting the Living Room TV", lambda m: f"Silenciando {_TV_ES}."),
    (r"Unmuting the Living Room TV", lambda m: f"Quitando el silencio a {_TV_ES}."),
    (r"Turning up the Living Room TV volume", lambda m: f"Subiendo el volumen de {_TV_ES}."),
    (r"Turning down the Living Room TV volume", lambda m: f"Bajando el volumen de {_TV_ES}."),
    (r"Pausing the Living Room TV", lambda m: f"Pausando {_TV_ES}."),
    (r"Resuming the Living Room TV", lambda m: f"Reanudando {_TV_ES}."),
    (r"Opening (?P<app>.+?)(?: on the Living Room TV)?\.?", lambda m: f"Abriendo {m.group('app')}."),
    (r"Turning (?P<on>on|off) (?P<what>.+?)\.?",
     lambda m: f"{'Prendiendo' if m.group('on') == 'on' else 'Apagando'} {_TARGETS[m.group('what')]}."
     if m.group("what") in _TARGETS else None),
    (r"Setting (?P<what>.+?) to (?P<n>\d+) percent\.?",
     lambda m: f"Poniendo {_TARGETS[m.group('what')]} al {m.group('n')} por ciento."
     if m.group("what") in _TARGETS else None),
    (r"Setting (?P<what>.+?) to (?P<color>[a-z ]+)\.?",
     lambda m: f"Poniendo {_TARGETS[m.group('what')]} en {_COLORS_ES[m.group('color')]}."
     if m.group("what") in _TARGETS and m.group("color") in _COLORS_ES else None),
]
_REPLIES = [(re.compile(p), f) for p, f in _REPLIES]


def reply_in_spanish(reply: str) -> str | None:
    """A skill's English reply in Spanish, or None when no template covers it
    (the pipeline then has the LLM translate it)."""
    reply = reply.strip()
    if reply in _FIXED:
        return _FIXED[reply]
    for rx, build in _REPLIES:
        m = rx.fullmatch(reply)
        if m:
            return build(m)
    return None
