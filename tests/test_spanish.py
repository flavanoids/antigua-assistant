#!/usr/bin/env python3
"""Spanish commands and replies (antigua_core/spanish.py).

Each Spanish command must become the English command the skills already
understand, and that English must route where it should; each skill reply
template must come back as natural Spanish. No network.

Run: python3 tests/test_spanish.py   (also works under pytest)
"""

import sys
from pathlib import Path

import _isolated  # noqa: E402,F401  — temp data dir; must precede antigua_core
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from antigua_core import settings  # noqa: E402

settings.configure({"music": {"speakers": {
    "kitchen": "Kitchen Speaker", "living room": "Living Room", "soundbar": "Soundbar", "bedroom": "Bedroom (2)"}}})

from antigua_core.classify import classify  # noqa: E402
from antigua_core.spanish import reply_in_spanish, to_english_command  # noqa: E402
from antigua_core.tts_text import clean_for_tts  # noqa: E402

# Spanish (as STT writes it) -> (English command, route)
COMMANDS = [
    # music
    ("Pausa la música.", "pause", "music"),
    ("Para la música.", "pause", "music"),
    ("Apaga la música", "pause", "music"),
    ("Pausa.", "pause", "music"),
    ("Continúa.", "resume", "music"),
    ("Sigue la música.", "resume", "music"),
    ("Siguiente.", "next song", "music"),
    ("Siguiente canción.", "next song", "music"),
    ("La anterior", "previous song", "music"),
    ("Ponla otra vez", "restart the song", "music"),
    ("Súbele a la música.", "turn the music up", "music"),
    ("Súbele.", "turn it up", "volume"),
    ("Bájale.", "turn it down", "volume"),
    ("Bachale.", "turn it down", "volume"),      # STT's usual spelling of bájale
    ("Pon la música al cuarenta", "set the music volume to 40", "music"),
    ("Pon Bad Bunny.", "play bad bunny", "music"),
    ("Pon música de Chappell Roan.", "play chappell roan", "music"),
    ("Ponme algo de Bad Bunny en la barra de sonido", "play bad bunny on the soundbar", "music"),
    ("Pon música de Toro y Moi en la sala", "play toro y moi on the living room", "music"),
    ("Alexa, pon música por favor", "play music", "music"),
    ("Pon el último álbum de Bad Bunny", "play the newest album by bad bunny", "music"),
    ("Pon la canción Tití Me Preguntó de Bad Bunny", "play the song titi me pregunto by bad bunny", "music"),
    ("¿Qué canción es esta?", "what's playing", "music"),
    # TV
    ("Prende la tele.", "turn on the tv", "tv"),
    ("Pon la tele", "turn on the tv", "tv"),
    ("Apaga la tele.", "turn off the tv", "tv"),
    ("Silencia la tele", "mute the tv", "tv"),
    ("Pon Netflix", "open netflix on the tv", "tv"),
    ("Abre YouTube en la tele", "open youtube on the tv", "tv"),
    # lights
    ("Apaga las luces.", "turn off the lights", "govee"),
    ("Prende las luces del pasillo", "turn on the hallway lights", "govee"),
    ("Pon las luces en azul", "set the lights to blue", "govee"),
    ("Pon el candelabro rojo", "set the chandelier to red", "govee"),
    ("Pon las luces al cincuenta por ciento", "set the lights to 50 percent", "govee"),
    # time, timers, alarms
    ("¿Qué hora es?", "what time is it", "time_date"),
    ("¿Qué día es hoy?", "what's the date today", "time_date"),
    ("Pon un temporizador de cinco minutos.", "set a timer for 5 minutes", "timer_set"),
    ("Pon un temporizador de 5 minutos.", "set a timer for 5 minutes", "timer_set"),
    ("Temporizador de treinta y cinco minutos", "set a timer for 35 minutes", "timer_set"),
    ("Pon un temporizador de media hora", "set a timer for 30 minutes", "timer_set"),
    ("Pon un temporizador de una hora", "set a timer for 1 hour", "timer_set"),
    ("Cancela el temporizador.", "cancel the timer", "timer_cancel"),
    ("¿Cuánto le falta al temporizador?", "how much time is left on the timer", "timer_status"),
    ("Despiértame a las seis y media.", "wake me up at 6:30", "alarm_set"),
    ("Despiértame a las siete de la mañana", "wake me up at 7 am", "alarm_set"),
    ("Ponme una alarma a las 6:30 de la mañana", "wake me up at 6:30 am", "alarm_set"),
    ("Ponme una alarma a las ocho menos cuarto de la noche", "wake me up at 7:45 pm", "alarm_set"),
    # weather
    ("¿Va a llover hoy?", "will it rain today", "weather"),
    ("¿Va a llover mañana?", "will it rain tomorrow", "weather"),
    ("¿Cómo está el clima?", "what's the weather", "weather"),
    ("¿Qué temperatura hace?", "what's the temperature", "weather"),
    ("Hace mucho calor", "how hot is it", "weather"),
]

# Not commands: conversation, and English that must stay English
NOT_COMMANDS = [
    "Cuéntame un chiste", "¿Cómo estuvo tu día?", "Pon atención a esto que te voy a decir",
    "Otra vez", "play", "pause", "stop", "next song", "turn on the tv", "",
]

REPLIES = [
    ("It's 11:13 PM.", "Son las 11:13 de la noche."),
    ("It's 1:05 PM.", "Es la 1:05 de la tarde."),
    ("It's 12 PM.", "Son las 12 del mediodía."),
    ("It's 7:45 AM.", "Son las 7:45 de la mañana."),
    ("Today is Friday, September 25.", "Hoy es viernes 25 de septiembre."),
    ("Timer set for 5 minutes.", "Temporizador de 5 minutos."),
    ("Timer set for 1 minute 30 seconds.", "Temporizador de 1 minuto y 30 segundos."),
    ("Timer set for 2 hours.", "Temporizador de 2 horas."),
    ("Alarm set for 6:30 AM tomorrow.", "Alarma para mañana a las 6:30 de la mañana."),
    ("Alarm set for 8 PM tomorrow.", "Alarma para mañana a las 8 de la noche."),
    ("Cancelled all 4.", "Cancelé los 4."),
    ("Cancelled the timer.", "Cancelé el temporizador."),
    ("Cancelled the alarm.", "Cancelé la alarma."),
    ("You don't have any timers or alarms set.", "No tienes temporizadores ni alarmas."),
    ("Playing Bad Bunny Essentials.", "Poniendo Bad Bunny Essentials."),
    ("Playing Un Verano Sin Ti by Bad Bunny on the soundbar.",
     "Poniendo Un Verano Sin Ti, de Bad Bunny en la barra de sonido."),
    ("Music volume 40.", "Volumen de la música al 40."),
    ("Turning on the Living Room TV", "Prendiendo la tele."),
    ("Muting the Living Room TV", "Silenciando la tele."),
    ("Opening Netflix", "Abriendo Netflix."),
    ("Turning off the lights", "Apagando las luces."),
    ("Turning on the hallway lights", "Prendiendo las luces del pasillo."),
    ("Setting the lights to blue", "Poniendo las luces en azul."),
    ("Setting the lights to 50 percent", "Poniendo las luces al 50 por ciento."),
    ("I can't play music right now.", "Ahora no puedo poner música."),
]
# No template: the pipeline has the LLM translate these
UNTEMPLATED = ["It's 89 and clear right now, with a low tonight around 75.",
               "Turning on the mystery lamp", "Setting the lights to chartreuse"]

# Spanish text as espeak es-419 should receive it (clean_for_tts(text, "es"))
SPOKEN = [
    ("Es la 1:05 de la tarde.", "Es la una y cinco de la tarde."),
    ("Son las 11:13 de la noche.", "Son las once y trece de la noche."),
    ("Alarma para mañana a las 6:30 de la mañana.", "Alarma para mañana a las seis y media de la mañana."),
    ("Son las 7:15.", "Son las siete y cuarto."),
    ("a las 7:45 PM", "a las siete y cuarenta y cinco de la noche"),
    ("a las 6:00 AM", "a las seis de la mañana"),
    ("Temporizador de 1 minuto y 21 segundos.", "Temporizador de un minuto y veintiún segundos."),
    ("Falta 1 hora y 1 día.", "Falta una hora y un día."),
    ("Tienes 11 minutos.", "Tienes 11 minutos."),
    ("Hoy es 1 de octubre.", "Hoy es primero de octubre."),
    ("El 31 de marzo.", "El 31 de marzo."),
    ("Hace 88°F, se siente como 98 °.", "Hace 88 grados, se siente como 98 grados."),
    ("Vientos de 12 mph.", "Vientos de 12 millas por hora."),
    ("Cuesta $4.99, o $1.", "Cuesta cuatro dólares con noventa y nueve centavos, o un dólar."),
    ("Hay 1,500 personas y 2.5 kilómetros.", "Hay 1500 personas y dos punto cinco kilómetros."),
    ("Poniendo DeBÍ TiRAR MáS FOToS, de Bad Bunny.", "Poniendo debí tirar más fotos, de Bad Bunny."),
    ("La NASA y la TV.", "La NASA y la TV."),
    ("**Hola**, soy Antigua...", "Hola, soy Antigua."),
]


def run():
    for es, english, route in COMMANDS:
        got = to_english_command(es)
        assert got == english, f"{es!r}: got {got!r}, want {english!r}"
        assert classify(got) == route, f"{es!r} -> {got!r} routes to {classify(got)!r}, want {route!r}"
    for text in NOT_COMMANDS:
        assert to_english_command(text) is None, f"{text!r} became {to_english_command(text)!r}"
    for english, es in REPLIES:
        got = reply_in_spanish(english)
        assert got == es, f"{english!r}: got {got!r}, want {es!r}"
    for english in UNTEMPLATED:
        assert reply_in_spanish(english) is None, english
    for text, want in SPOKEN:
        got = clean_for_tts(text, "es")
        assert got == want, f"{text!r}: spoken as {got!r}, want {want!r}"
    print(f"PASS — {len(COMMANDS)} Spanish commands, {len(NOT_COMMANDS)} non-commands, "
          f"{len(REPLIES)} replies, {len(SPOKEN)} spoken forms")


if __name__ == "__main__":
    run()
