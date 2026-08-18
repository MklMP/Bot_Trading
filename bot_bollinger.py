import logging
import json
import os
import re
import sqlite3
import hashlib
import traceback
import platform
import threading
from contextlib import closing
from datetime import datetime, time, timedelta
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes

import pytz
import requests
import yfinance as yf
import pandas as pd
from PIL import Image, ImageOps

try:
    import pytesseract
    TESSERACT_DISPONIBLE = True
except ImportError:
    TESSERACT_DISPONIBLE = False

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    ReactionTypeEmoji,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ══════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("trading_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)  # silenciar ruido de polling

# ══════════════════════════════════════════════════════════
# CONFIGURACIÓN (SIEMPRE desde variables de entorno — nunca hardcodeado)
# ══════════════════════════════════════════════════════════
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID_RAW = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
ADMIN_CHAT_ID_RAW = 7148286162
OCR_LANG = os.getenv("OCR_LANG", "spa+eng")  # usa "spa+eng" si instalaste el paquete de idioma español de tesseract
DB_PATH = os.getenv("DB_PATH", "semana.db")

if not TOKEN:
    raise SystemExit(
        "Falta TELEGRAM_BOT_TOKEN. Configúralo como variable de entorno "
        "(Render > tu servicio > Environment) y NUNCA lo escribas en el código."
    )
if not CHAT_ID_RAW:
    raise SystemExit("Falta TELEGRAM_CHAT_ID como variable de entorno.")

try:
    CHAT_ID = int(CHAT_ID_RAW)
except ValueError:
    raise SystemExit("TELEGRAM_CHAT_ID debe ser un número entero (puede ser negativo para grupos).")

ADMIN_CHAT_ID = None
if ADMIN_CHAT_ID_RAW:
    try:
        ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)
    except ValueError:
        logger.warning("ADMIN_CHAT_ID inválido, se ignora.")

TICKERS = {
    "NVDA": "NVDA",
    "TSLA": "TSLA",
    "AMD": "AMD",
    "GOOG": "GOOG",
    "AAPL": "AAPL",
    "MSFT": "MSFT",
    "AMZN": "AMZN",
    "META": "META",
    "NAS100": "^NDX",
    "SP500": "^GSPC",
}

INTERVALOS_ESCANEO = ["5m", "15m"]  # Bollinger se evalúa en ambas temporalidades
PERIODO_ESCANEO = "5d"  # suficiente historial para EMA50/RSI/MACD estables en ambos intervalos
HORA_INICIO_BOLLINGER = time(7, 30)
HORA_FIN_BOLLINGER = time(11, 30)
NY_TZ = pytz.timezone("America/New_York")
DIAS_SEMANA = ["monday", "tuesday", "wednesday", "thursday", "friday"]
NOMBRES_DIAS = {
    "monday": "Lunes", "tuesday": "Martes", "wednesday": "Miércoles",
    "thursday": "Jueves", "friday": "Viernes",
}

SALUDOS_LUNES = [
    "Preparados para empezar la semana, ¿no? ¡Espero que sí! 💪",
    "¡Buen lunes! A por todas en el mercado. 🚀",
    "Nueva semana, nuevas oportunidades. ¡Vamos con todo! 📈",
    "Lunes de energía renovada. ¡Que sea una gran semana! ✨",
    "Empezamos la semana con fuerza. ¡A operar! 💼",
    "Feliz lunes, traders. Que la disciplina nos acompañe. 🧠",
]
SALUDOS_VIERNES = [
    "Y terminamos la semana, hagamos un resumen para ver las estadísticas 📊",
    "¡Viernes! Hora de revisar cómo nos fue esta semana. 🔍",
    "Cerramos la semana. Veamos los números. 📈",
    "Último día de trading. Resumen semanal en camino. 🧾",
    "Viernes de balance. ¿Cómo nos fue? 🤔",
    "Se acabó la semana. Aprendamos de los resultados. 📚",
]

# ══════════════════════════════════════════════════════════
# BASE DE DATOS (SQLite local — sin servicios de pago)
# Conserva TODO el historial semanal, no solo la semana en curso.
# ══════════════════════════════════════════════════════════
_db_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with closing(get_conn()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY, value TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS days (
            week_start TEXT, dia TEXT, sentimiento TEXT,
            profit REAL DEFAULT 0, capital REAL DEFAULT 0,
            PRIMARY KEY (week_start, dia)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS day_users (
            week_start TEXT, dia TEXT, usuario TEXT,
            PRIMARY KEY (week_start, dia, usuario)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start TEXT, dia TEXT, fecha TEXT,
            profit REAL, capital REAL, porcentaje REAL,
            usuario TEXT, fuente TEXT
        )""")
    logger.info("Base de datos lista en %s", DB_PATH)


def _lunes_de_esta_semana_ny():
    hoy_ny = datetime.now(NY_TZ).date()
    lunes = hoy_ny - timedelta(days=hoy_ny.weekday())
    return lunes.isoformat()


def get_current_week_start():
    with _db_lock, closing(get_conn()) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='current_week'").fetchone()
        if row:
            return row["value"]
        semana = _lunes_de_esta_semana_ny()
        with conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('current_week', ?)", (semana,))
        return semana


def reset_week():
    nueva_semana = _lunes_de_esta_semana_ny()
    with _db_lock, closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('current_week', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (nueva_semana,),
        )
    logger.info("Semana reiniciada: %s", nueva_semana)
    return nueva_semana


def obtener_dia_actual():
    return datetime.now(NY_TZ).strftime("%A").lower()


def _ensure_day(conn, week_start, dia):
    conn.execute(
        "INSERT OR IGNORE INTO days (week_start, dia, sentimiento, profit, capital) "
        "VALUES (?, ?, NULL, 0, 0)",
        (week_start, dia),
    )


def actualizar_sentimiento_dia(sentimiento):
    semana = get_current_week_start()
    dia = obtener_dia_actual()
    with _db_lock, closing(get_conn()) as conn, conn:
        _ensure_day(conn, semana, dia)
        conn.execute(
            "UPDATE days SET sentimiento=? WHERE week_start=? AND dia=?",
            (sentimiento, semana, dia),
        )


def actualizar_profit_dia(profit, capital=None, porcentaje=None, usuario=None, fuente="desconocido"):
    semana = get_current_week_start()
    dia = obtener_dia_actual()
    with _db_lock, closing(get_conn()) as conn, conn:
        _ensure_day(conn, semana, dia)
        conn.execute(
            "UPDATE days SET profit = profit + ?, capital = capital + ? WHERE week_start=? AND dia=?",
            (profit, capital or 0, semana, dia),
        )
        if usuario:
            conn.execute(
                "INSERT OR IGNORE INTO day_users (week_start, dia, usuario) VALUES (?, ?, ?)",
                (semana, dia, usuario),
            )
        conn.execute(
            "INSERT INTO trades (week_start, dia, fecha, profit, capital, porcentaje, usuario, fuente) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (semana, dia, datetime.now(NY_TZ).isoformat(), profit, capital, porcentaje, usuario, fuente),
        )
    return get_day_data(dia)


def get_day_data(dia, semana=None):
    semana = semana or get_current_week_start()
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM days WHERE week_start=? AND dia=?", (semana, dia)
        ).fetchone()
        usuarios = [
            r["usuario"] for r in conn.execute(
                "SELECT usuario FROM day_users WHERE week_start=? AND dia=?", (semana, dia)
            ).fetchall()
        ]
    if not row:
        return {"sentimiento": None, "profit": 0.0, "capital": 0.0, "usuarios": []}
    return {
        "sentimiento": row["sentimiento"],
        "profit": row["profit"] or 0.0,
        "capital": row["capital"] or 0.0,
        "usuarios": usuarios,
    }


def get_week_summary(semana=None):
    semana = semana or get_current_week_start()
    return {dia: get_day_data(dia, semana) for dia in DIAS_SEMANA}


def get_user_stats(usuario, semanas=4):
    """Estadísticas personales de un usuario en las últimas N semanas."""
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT profit, capital, fecha FROM trades WHERE usuario=? ORDER BY fecha DESC LIMIT 200",
            (usuario,),
        ).fetchall()
    total_profit = sum(r["profit"] or 0 for r in rows)
    total_capital = sum(r["capital"] or 0 for r in rows)
    n = len(rows)
    ganadores = sum(1 for r in rows if (r["profit"] or 0) > 0)
    return {
        "operaciones": n,
        "profit_total": total_profit,
        "capital_total": total_capital,
        "win_rate": (ganadores / n * 100) if n else 0.0,
    }


def get_historial_semanas(n=4):
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT week_start, SUM(profit) as profit, SUM(capital) as capital "
            "FROM days GROUP BY week_start ORDER BY week_start DESC LIMIT ?",
            (n,),
        ).fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════
# DEDUPLICACIÓN (evita contar dos veces la misma operación)
# ══════════════════════════════════════════════════════════
_recientes = {}
_recientes_lock = threading.Lock()


def _hash_resultado(usuario, profit, capital, fuente):
    base = f"{usuario}|{round(profit or 0, 2)}|{round(capital or 0, 2)}|{fuente}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


def es_duplicado(usuario, profit, capital, fuente, ventana_seg=300):
    ahora = datetime.now()
    h = _hash_resultado(usuario, profit, capital, fuente)
    with _recientes_lock:
        global _recientes
        _recientes = {k: ts for k, ts in _recientes.items() if (ahora - ts).total_seconds() < ventana_seg}
        if h in _recientes:
            return True
        _recientes[h] = ahora
    return False


# ══════════════════════════════════════════════════════════
# ESCANEO DE BANDAS DE BOLLINGER
# ══════════════════════════════════════════════════════════
def es_dia_habil():
    """Lunes a viernes en horario de Nueva York. Sábado y domingo el mercado está cerrado."""
    return datetime.now(NY_TZ).weekday() < 5  # 0=lunes ... 4=viernes, 5=sábado, 6=domingo


def dentro_horario_bollinger():
    hora_actual = datetime.now(NY_TZ).time()
    return HORA_INICIO_BOLLINGER <= hora_actual <= HORA_FIN_BOLLINGER


def mercado_activo():
    """True solo si es día hábil Y estamos dentro del horario de escaneo."""
    return es_dia_habil() and dentro_horario_bollinger()


# ══════════════════════════════════════════════════════════
# ANÁLISIS TÉCNICO — EMAs, RSI, MACD y volumen (OBV)
# Actúa como capa de "analista profesional" que confirma o
# descarta la señal de ruptura de Bollinger.
# ══════════════════════════════════════════════════════════
def calcular_indicadores(df):
    """Calcula EMAs (9/21/50), RSI(14), MACD(12,26,9) y OBV sobre el DataFrame."""
    df = df.copy()

    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["Close"].ewm(span=21, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()

    delta = df["Close"].diff()
    ganancia = delta.clip(lower=0)
    perdida = -delta.clip(upper=0)
    avg_gain = ganancia.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = perdida.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    df["RSI"] = 100 - (100 / (1 + rs))

    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"] = df["MACD"] - df["MACD_signal"]

    if "Volume" in df.columns:
        direccion = df["Close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
        df["OBV"] = (direccion * df["Volume"]).fillna(0).cumsum()
    else:
        df["OBV"] = 0.0

    return df


def interpretar_tecnico(df, ventana_obv=10):
    """
    Lee el estado técnico actual como lo haría un analista: tendencia (EMAs),
    momentum (RSI + MACD) y fase de acumulación/distribución (OBV).
    Devuelve un dict con la lectura y un texto listo para mostrar.
    """
    ultima = df.iloc[-1]
    ema9, ema21, ema50 = ultima["EMA9"], ultima["EMA21"], ultima["EMA50"]
    rsi = ultima["RSI"]
    macd_hist = ultima["MACD_hist"]

    if ema9 > ema21 > ema50:
        tendencia, emoji_t = "Alcista", "📈"
    elif ema9 < ema21 < ema50:
        tendencia, emoji_t = "Bajista", "📉"
    else:
        tendencia, emoji_t = "Lateral/Indefinida", "↔️"

    if rsi >= 70:
        momentum = "Sobrecompra"
    elif rsi >= 55:
        momentum = "Momentum positivo"
    elif rsi <= 30:
        momentum = "Sobreventa"
    elif rsi <= 45:
        momentum = "Momentum negativo"
    else:
        momentum = "Neutral"

    macd_dir = "alcista" if macd_hist > 0 else "bajista"
    macd_cruce = ""
    if len(df) >= 2:
        prev_hist = df.iloc[-2]["MACD_hist"]
        if prev_hist <= 0 < macd_hist:
            macd_cruce = " (cruce alcista reciente)"
        elif prev_hist >= 0 > macd_hist:
            macd_cruce = " (cruce bajista reciente)"

    obv_serie = df["OBV"].tail(ventana_obv)
    if len(obv_serie) >= 2 and obv_serie.iloc[-1] != obv_serie.iloc[0]:
        pendiente_obv = obv_serie.iloc[-1] - obv_serie.iloc[0]
        volumen = "Acumulación (entrada de volumen)" if pendiente_obv > 0 else "Distribución (salida de volumen)"
    else:
        volumen = "Volumen estable / sin datos"

    señales_alcistas = sum([
        tendencia == "Alcista",
        momentum in ("Momentum positivo", "Sobrecompra"),
        macd_dir == "alcista",
        volumen.startswith("Acumulación"),
    ])
    señales_bajistas = sum([
        tendencia == "Bajista",
        momentum in ("Momentum negativo", "Sobreventa"),
        macd_dir == "bajista",
        volumen.startswith("Distribución"),
    ])

    if señales_alcistas >= 3:
        fuerza = "🟢 Confluencia alcista fuerte"
    elif señales_bajistas >= 3:
        fuerza = "🔴 Confluencia bajista fuerte"
    elif señales_alcistas > señales_bajistas:
        fuerza = "🟡 Sesgo alcista moderado"
    elif señales_bajistas > señales_alcistas:
        fuerza = "🟡 Sesgo bajista moderado"
    else:
        fuerza = "⚪ Sin confluencia clara (posible lateralización)"

    detalle = (
        f"{emoji_t} Tendencia (EMA 9/21/50): {tendencia}\n"
        f"⚡ Momentum (RSI {rsi:.1f}): {momentum}\n"
        f"📐 MACD: {macd_dir}{macd_cruce}\n"
        f"📦 Volumen (OBV): {volumen}\n"
        f"🧠 Lectura del analista: {fuerza}"
    )
    return {
        "tendencia": tendencia, "momentum": momentum, "rsi": rsi,
        "macd_dir": macd_dir, "volumen": volumen, "fuerza": fuerza, "detalle": detalle,
    }


def generar_estado_mercado():
    """Snapshot compacto del estado técnico de todos los tickers (para el monitor en vivo)."""
    ahora = datetime.now(NY_TZ).strftime("%d/%m/%Y %H:%M:%S")
    lineas = []
    for nombre, ticker_yf in TICKERS.items():
        try:
            t = yf.Ticker(ticker_yf)
            df = t.history(period=PERIODO_ESCANEO, interval="15m")
            if df.empty or len(df) < 25:
                lineas.append(f"⚪ {nombre}: datos insuficientes")
                continue
            cols = ["Close"] + (["Volume"] if "Volume" in df.columns else [])
            df = df[cols].copy()
            df = calcular_indicadores(df)
            tecnico = interpretar_tecnico(df)
            precio = df.iloc[-1]["Close"]
            emoji_fuerza = tecnico["fuerza"].split()[0]
            lineas.append(
                f"{emoji_fuerza} {nombre}: {precio:.2f} | {tecnico['tendencia']} | RSI {tecnico['rsi']:.0f}"
            )
        except Exception:
            logger.exception("Error en monitor para %s", ticker_yf)
            lineas.append(f"⚠️ {nombre}: error al obtener datos")

    return (
        f"📡 Monitor en vivo (15m) — actualizado {ahora} NY\n"
        f"Se refresca cada 1 min en este mismo mensaje mientras el mercado está abierto.\n\n"
        + "\n".join(lineas)
    )


async def _editar_o_enviar_mensaje(bot, bot_data, chat_id, clave, texto):
    """
    Edita SIEMPRE el mismo mensaje (guardado en bot_data[clave]) en vez de
    mandar uno nuevo. Si no existe todavía, o el anterior ya no se puede
    editar (borrado, muy viejo, etc.), crea uno nuevo y lo recuerda para
    la próxima actualización. Así el chat nunca se llena de mensajes
    repetidos: siempre hay UN solo mensaje que se va sustituyendo.
    """
    message_id = bot_data.get(clave)
    if message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=texto)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return  # el contenido no cambió, no hace falta hacer nada
            logger.info("No se pudo editar el mensaje '%s' (%s); se crea uno nuevo.", clave, e)

    nuevo = await bot.send_message(chat_id=chat_id, text=texto)
    bot_data[clave] = nuevo.message_id


async def actualizar_monitor(context: ContextTypes.DEFAULT_TYPE):
    """Actualiza (editando, no reenviando) el dashboard de estado del mercado."""
    chat_id = context.job.chat_id
    if not mercado_activo():
        return  # fuera de horario/fin de semana: no gastamos llamadas ni tocamos el mensaje
    texto = generar_estado_mercado()
    await _editar_o_enviar_mensaje(context.bot, context.bot_data, chat_id, f"monitor_msg_{chat_id}", texto)


def escanear():
    """
    Escanea Bollinger en 5m y 15m para cada ticker, y cuando hay una ruptura
    la enriquece con la lectura técnica (EMAs, RSI, MACD, OBV) para que la
    alerta venga acompañada de contexto, no solo del precio cruzando la banda.
    """
    alertas = []
    ahora = datetime.now(NY_TZ).strftime("%d/%m/%Y %H:%M")

    for nombre, ticker_yf in TICKERS.items():
        try:
            t = yf.Ticker(ticker_yf)
            resultados_ticker = []

            for intervalo in INTERVALOS_ESCANEO:
                df = t.history(period=PERIODO_ESCANEO, interval=intervalo)
                if df.empty or len(df) < 25:
                    logger.warning("Datos insuficientes para %s (%s)", ticker_yf, intervalo)
                    continue

                cols = ["Close"] + (["Volume"] if "Volume" in df.columns else [])
                df = df[cols].copy()
                df["SMA20"] = df["Close"].rolling(window=20).mean()
                df["STD20"] = df["Close"].rolling(window=20).std()
                df["Upper"] = df["SMA20"] + 2 * df["STD20"]
                df["Lower"] = df["SMA20"] - 2 * df["STD20"]
                df = calcular_indicadores(df)

                ultima = df.iloc[-1]
                precio, upper, lower = ultima["Close"], ultima["Upper"], ultima["Lower"]
                if pd.isna(upper) or pd.isna(lower):
                    continue

                if precio > upper:
                    emoji, texto_lado, banda = "🟢", "SOBRE la banda superior", upper
                elif precio < lower:
                    emoji, texto_lado, banda = "🔴", "DEBAJO de la banda inferior", lower
                else:
                    continue

                tecnico = interpretar_tecnico(df)
                resultados_ticker.append(
                    f"{emoji} {nombre} ({ticker_yf}) · {intervalo}\n"
                    f"{texto_lado} — Precio: {precio:.2f} | Banda: {banda:.2f}\n\n"
                    f"{tecnico['detalle']}"
                )

            if resultados_ticker:
                alertas.append("\n\n".join(resultados_ticker))
        except Exception:
            logger.exception("Error escaneando %s", ticker_yf)

    intervalos_txt = "/".join(INTERVALOS_ESCANEO)
    if not alertas:
        return f"✅ {ahora}\nNinguna acción fuera de Bollinger ({intervalos_txt})."
    return f"📊 Escaneo {ahora} ({intervalos_txt})\n\n" + "\n\n───────\n\n".join(alertas)


# ══════════════════════════════════════════════════════════
# SENTIMIENTO Y JSON
# ══════════════════════════════════════════════════════════
PALABRAS_POSITIVAS = ["positivo", "verde", "bien", "cumplió", "cumplio", "ganancia", "profit", "arriba", "excelente"]
PALABRAS_NEGATIVAS = ["negativo", "rojo", "mal", "no cumplió", "no cumplio", "pérdida", "perdida", "abajo", "malo"]


def analizar_sentimiento(texto):
    t = texto.lower()
    if any(p in t for p in PALABRAS_POSITIVAS):
        return "positivo"
    if any(n in t for n in PALABRAS_NEGATIVAS):
        return "negativo"
    return None


def parsear_trade_json(texto):
    try:
        datos = json.loads(texto)
    except json.JSONDecodeError:
        return None
    if "summary" in datos and "total_profit" in datos["summary"]:
        return datos["summary"]["total_profit"]
    return None


def parsear_account_json(texto):
    try:
        datos = json.loads(texto)
    except json.JSONDecodeError:
        return None
    if "account" in datos and "positions" in datos:
        return datos
    return None


def extraer_numero(texto):
    if texto is None:
        return 0.0
    texto = re.sub(r"[\s,]", "", texto)
    try:
        return float(texto)
    except ValueError:
        return 0.0


# ══════════════════════════════════════════════════════════
# PARSERS DE OCR — de más específico a más genérico.
# Solo se responde al usuario cuando alguno de estos detecta
# un resultado de operación real (requisito del bot).
# ══════════════════════════════════════════════════════════
def parsear_ocr_trade(texto_ocr):
    """Capturas de opciones (formato tipo ThinkOrSwim/broker de opciones).

    Antes esta función solo tomaba el PRIMER fill de compra y el PRIMER fill
    de venta (`next(...)`). Si la captura tenía varias operaciones/fills
    parciales (ej. 3 compras a distinto precio + 2 ventas a distinto precio),
    ignoraba el resto y el cálculo quedaba mal. Ahora se agregan TODOS los
    fills de cada lado y se calcula un precio promedio ponderado por
    cantidad — así el resultado es correcto sin importar cuántas líneas
    de ejecución traiga la imagen.
    """
    match_info = re.search(
        r'([A-Z]{2,5})\s+\d+\s+\(Weeklys\)\s+(\d{1,2}\s+[A-Z]{3}\s+\d{2}).*?(\d{3,4})\s*([CP])',
        texto_ocr, re.IGNORECASE,
    )
    if not match_info:
        match_info = re.search(
            r'([A-Z]{2,5}).*?(\d{1,2}\s+[A-Z]{3}\s+\d{2}).*?(\d{3,4})\s*([CP])',
            texto_ocr, re.IGNORECASE,
        )
    if not match_info:
        return None

    symbol = match_info.group(1)
    expiration = match_info.group(2).strip()
    strike = int(match_info.group(3))
    tipo = "Call" if match_info.group(4).upper() == "C" else "Put"

    trades = []
    for line in texto_ocr.splitlines():
        m = re.search(
            r'([+\-]?\d+)\s+(BOT|SOLD)\s+@([\d.]+)\s+(\d{1,2}/\d{1,2}/\d{2}),\s+(\d{1,2}:\d{2}\s*[AP]M)',
            line, re.IGNORECASE,
        )
        if m:
            trades.append({
                "quantity": abs(int(m.group(1))),
                "action": m.group(2).upper(),
                "price": float(m.group(3)),
            })

    if len(trades) < 2:
        return None

    compras = [t for t in trades if t["action"] == "BOT"]
    ventas = [t for t in trades if t["action"] == "SOLD"]
    if not compras or not ventas:
        return None

    qty_compra = sum(t["quantity"] for t in compras)
    qty_venta = sum(t["quantity"] for t in ventas)
    if qty_compra <= 0 or qty_venta <= 0:
        return None

    # Precio promedio ponderado por cantidad de cada lado (compra y venta),
    # que es la forma matemáticamente correcta de "promediar" varios fills.
    precio_compra_prom = sum(t["price"] * t["quantity"] for t in compras) / qty_compra
    precio_venta_prom = sum(t["price"] * t["quantity"] for t in ventas) / qty_venta

    # Solo se puede atribuir profit a los contratos que realmente se cerraron
    # (el menor entre lo comprado y lo vendido); si quedaron contratos sin
    # cerrar del otro lado, se avisa en el detalle en vez de inventar un profit.
    contratos_cerrados = min(qty_compra, qty_venta)
    profit_per_contract = round(precio_venta_prom - precio_compra_prom, 2)
    total_profit = profit_per_contract * contratos_cerrados * 100
    capital = precio_compra_prom * contratos_cerrados * 100
    porcentaje = (total_profit / capital * 100) if capital > 0 else 0.0

    nota_fills = ""
    if len(compras) > 1 or len(ventas) > 1:
        nota_fills += (
            f"\n🧮 {len(compras)} fill(s) de compra prom. ${precio_compra_prom:.2f} "
            f"({qty_compra} contratos) · {len(ventas)} fill(s) de venta prom. "
            f"${precio_venta_prom:.2f} ({qty_venta} contratos)"
        )
    if qty_compra != qty_venta:
        restante = abs(qty_compra - qty_venta)
        lado = "compra" if qty_compra > qty_venta else "venta"
        nota_fills += f"\n⚠️ Quedan {restante} contrato(s) sin cerrar del lado de {lado} (no se contaron en el profit)."

    return {
        "profit": total_profit,
        "capital": capital,
        "porcentaje": round(porcentaje, 2),
        "detalle": (
            f"🔹 Símbolo: {symbol}\n📅 Expiración: {expiration}\n🎯 Strike: {strike} {tipo}\n"
            f"🔢 Contratos cerrados: {contratos_cerrados}\n📈 Compra prom.: ${precio_compra_prom:.2f}\n"
            f"📉 Venta prom.: ${precio_venta_prom:.2f}{nota_fills}"
        ),
    }


def parsear_ocr_account(texto_ocr):
    """Capturas de estado de cuenta / posiciones MT5."""
    lineas = texto_ocr.splitlines()
    datos = {"account": {}, "positions": []}

    patrones_cuenta = {
        "balance": r'Balance\s*:?\s*([\d\s]+\.\d{2})',
        "equity": r'Equidad\s*:?\s*([\d\s]+\.\d{2})',
        "margin": r'(?<!Free\s)Margen\s*:?\s*([\d\s]+\.\d{2})',
        "free_margin": r'Margen\s*libre\s*:?\s*([\d\s]+\.\d{2})',
        "margin_level_percent": r'Nivel\s*de\s*margen\s*\(?%?\)?\s*:?\s*([\d\s]+\.\d{1,2})',
        "profit_total": r'=\s*([+\-]?[\d\s]+\.\d{2})\s*USD',
    }
    for campo, patron in patrones_cuenta.items():
        for linea in lineas:
            m = re.search(patron, linea, re.IGNORECASE)
            if m:
                datos["account"][campo] = extraer_numero(m.group(1))
                break

    if "balance" not in datos["account"] and "equity" not in datos["account"]:
        return None

    i = 0
    while i < len(lineas):
        linea = lineas[i].strip()
        m = re.search(r'#?([A-Z]{2,}[A-Z0-9]*)\s*[,]?\s*(buy|sell|sel|sel!|BOT|SOLD)\s*([\d.]+)?', linea, re.IGNORECASE)
        if m:
            symbol = m.group(1).upper()
            tipo_bruto = m.group(2).lower()
            tipo = "sell" if "sel" in tipo_bruto or "sold" in tipo_bruto else "buy"
            volumen = extraer_numero(m.group(3)) if m.group(3) else 0.0
            if i + 1 < len(lineas):
                sig = lineas[i + 1].strip()
                mp = re.search(r'([\d\s]+\.\d{1,2})\s*[—\-–]\s*([\d\s]+\.\d{1,2})\s*([+\-]?[\d\s]*\.\d{1,2})?', sig)
                if mp:
                    profit = extraer_numero(mp.group(3)) if mp.group(3) else 0.0
                    if profit == 0.0:
                        mpe = re.search(r'([+\-]?[\d\s]+\.\d{1,2})\s*$', sig)
                        if mpe:
                            profit = extraer_numero(mpe.group(1))
                    datos["positions"].append({"symbol": symbol, "type": tipo, "volume": volumen, "profit": profit})
                    i += 2
                    continue
            datos["positions"].append({"symbol": symbol, "type": tipo, "volume": volumen, "profit": 0.0})
        i += 1

    return datos if (datos["account"] or datos["positions"]) else None


# Palabras clave que indican explícitamente un resultado de operación.
_KEYWORD_PROFIT = re.compile(
    r'(?:profit|p[\s/\\]?[l&]|pnl|ganancia|beneficio|resultado|p&l)\s*[:=]?\s*'
    r'([+\-]?\$?\s?[\d][\d.,]*\.\d{2})',
    re.IGNORECASE,
)
# Monto con signo explícito + símbolo o sufijo de moneda (evita capturar horas, fechas, teléfonos).
_SIGNED_MONEY = re.compile(
    r'([+\-]\s?\$?\s?[\d][\d.,]*\.\d{2})\s*(USD|usd|\$)?',
)


def parsear_ocr_generico(texto_ocr):
    """
    Último fallback: cualquier captura donde se detecten uno o varios
    profit/resultado, aunque no encaje con un broker específico. Exige
    contexto (palabra clave o signo + formato monetario) para minimizar
    falsos positivos.

    Antes solo tomaba la PRIMERA coincidencia (`.search`), así que una
    captura con una tabla de varias operaciones (varias líneas de profit)
    perdía todo menos la primera. Ahora se recolectan TODAS las
    coincidencias y se suman, que es el resultado correcto cuando la
    imagen muestra varias operaciones/fills.
    """
    matches_keyword = list(_KEYWORD_PROFIT.finditer(texto_ocr))
    if matches_keyword:
        valores = [extraer_numero(m.group(1).replace("$", "")) for m in matches_keyword]
        total = sum(valores)
        if len(valores) > 1:
            lista = ", ".join(f"${v:.2f}" for v in valores)
            detalle = f"Detectadas {len(valores)} operaciones: {lista} → suma total ${total:.2f}"
        else:
            detalle = f"Detectado: “{matches_keyword[0].group(0).strip()}”"
        return {"profit": total, "capital": None, "porcentaje": None, "detalle": detalle}

    encontrados = []
    for m in _SIGNED_MONEY.finditer(texto_ocr):
        crudo = m.group(1)
        moneda = m.group(2)
        # Exigimos símbolo de moneda o que la línea contenga contexto financiero.
        linea_completa = texto_ocr[max(0, m.start()-40):m.end()+10].lower()
        contexto_ok = bool(moneda) or any(
            kw in linea_completa for kw in ["usd", "$", "balance", "equity", "equidad", "trade", "posici"]
        )
        if contexto_ok:
            encontrados.append(extraer_numero(crudo.replace("$", "")))

    if encontrados:
        total = sum(encontrados)
        if len(encontrados) > 1:
            lista = ", ".join(f"${v:.2f}" for v in encontrados)
            detalle = f"Detectadas {len(encontrados)} operaciones: {lista} → suma total ${total:.2f}"
        else:
            detalle = f"Detectado: ${encontrados[0]:.2f}"
        return {"profit": total, "capital": None, "porcentaje": None, "detalle": detalle}

    return None


def preprocesar_imagen(img: Image.Image) -> Image.Image:
    """Mejora simple de la imagen para OCR: escala de grises + contraste."""
    img = img.convert("L")
    img = ImageOps.autocontrast(img)
    return img


async def journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Abre el mini-app del trading journal. (Antes este código estaba muerto
    dentro de preprocesar_imagen, después de un return, por lo que /journal
    nunca podía funcionar; quedó separado como su propio comando.)"""
    url = "https://trading-journal-1iiz.onrender.com"  # ← Reemplaza por tu URL real
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("📒 Abrir Journal", web_app=WebAppInfo(url=url))]
    ])
    try:
        await update.message.reply_text(
            "Toca el botón para abrir tu journal:",
            reply_markup=teclado,
        )
    except Exception:
        logger.exception("Error enviando journal")

# ══════════════════════════════════════════════════════════
# PROCESAMIENTO DE IMÁGENES
# Solo responde cuando detecta un resultado de operación real.
# ══════════════════════════════════════════════════════════
async def procesar_imagen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not TESSERACT_DISPONIBLE:
        logger.warning("pytesseract no está instalado; se ignora la imagen recibida.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    bio = BytesIO()
    await file.download_to_memory(bio)
    bio.seek(0)

    try:
        img = Image.open(bio)
        img_procesada = preprocesar_imagen(img)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        texto_ocr = pytesseract.image_to_string(img_procesada, lang=OCR_LANG)
    except Exception:
        logger.exception("Fallo al procesar OCR de la imagen")
        return

    if not texto_ocr.strip():
        return  # silencioso: no es una operación, no respondemos

    usuario = update.effective_user.username or update.effective_user.first_name or str(update.effective_user.id)

    # 1) JSON de cuenta/posiciones (por si alguien pega el JSON como imagen de texto plano)
    resultado = None
    fuente = None

    datos_cuenta = parsear_account_json(texto_ocr)
    if datos_cuenta:
        await _responder_cuenta(update, datos_cuenta, usuario)
        return

    # 2) Trade de opciones (alta precisión)
    datos_trade = parsear_ocr_trade(texto_ocr)
    if datos_trade:
        resultado, fuente = datos_trade, "ocr_opciones"

    # 3) Cuenta MT5 (alta precisión)
    if resultado is None:
        datos_cuenta_ocr = parsear_ocr_account(texto_ocr)
        if datos_cuenta_ocr:
            await _responder_cuenta(update, datos_cuenta_ocr, usuario)
            return

    # 4) Fallback genérico: cualquier imagen con un profit/resultado detectable
    if resultado is None:
        generico = parsear_ocr_generico(texto_ocr)
        if generico:
            resultado, fuente = generico, "ocr_generico"

    if resultado is None:
        return  # no es una operación reconocible → silencio total, sin respuesta

    profit = resultado["profit"]
    capital = resultado.get("capital")
    porcentaje = resultado.get("porcentaje")

    if es_duplicado(usuario, profit, capital, fuente):
        await update.message.reply_text("ℹ️ Esta operación ya fue registrada recientemente, no se acumuló de nuevo.")
        return

    dia_data = actualizar_profit_dia(profit, capital=capital, porcentaje=porcentaje, usuario=usuario, fuente=fuente)
    retorno_acum = (dia_data["profit"] / dia_data["capital"] * 100) if dia_data["capital"] else 0.0

    partes = [f"✅ Operación detectada\n\n👤 Usuario: {usuario}"]
    if resultado.get("detalle"):
        partes.append(resultado["detalle"])
    partes.append(f"💰 Resultado: ${profit:.2f}")
    if porcentaje is not None:
        partes.append(f"📊 Retorno de la operación: {porcentaje:.2f}%")
    partes.append(
        f"───────\n📅 Acumulado hoy:\n💵 Ganancia total: ${dia_data['profit']:.2f}\n"
        f"📊 Rentabilidad total: {retorno_acum:.2f}%"
    )
    await update.message.reply_text("\n\n".join(partes))
    try:
        await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
    except Exception:
        logger.debug("No se pudo reaccionar al mensaje", exc_info=True)


async def _responder_cuenta(update, datos, usuario):
    mensaje = construir_mensaje_cuenta(datos, usuario, actualizar=True)
    await update.message.reply_text(mensaje)
    try:
        await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
    except Exception:
        logger.debug("No se pudo reaccionar al mensaje", exc_info=True)


def construir_mensaje_cuenta(datos, usuario=None, actualizar=False):
    acc = datos.get("account", {})
    posiciones = datos.get("positions", [])
    balance = acc.get("balance", 0.0)
    equity = acc.get("equity", 0.0)
    margin = acc.get("margin", 0.0)
    free_margin = acc.get("free_margin", 0.0)
    margin_level = acc.get("margin_level_percent", 0.0)
    profit_total = acc.get("profit_total", 0.0)
    currency = acc.get("currency", "USD")

    if profit_total == 0.0 and posiciones:
        profit_total = sum(p.get("profit", 0.0) for p in posiciones)

    base = balance if balance > 0 else equity
    rentabilidad = (profit_total / base * 100) if base > 0 else 0.0

    lineas_pos = [
        f"• {p.get('symbol','?')} ({p.get('type','')}) {p.get('volume',0.0)} lots\n   Profit: {p.get('profit',0.0):.2f} {currency}"
        for p in posiciones
    ] or ["Sin posiciones abiertas"]

    acumulado_str = ""
    if actualizar:
        dia_data = actualizar_profit_dia(profit_total, capital=balance, usuario=usuario, fuente="ocr_cuenta_mt5")
        retorno_acum = (dia_data["profit"] / dia_data["capital"] * 100) if dia_data["capital"] else 0.0
        acumulado_str = (
            f"\n───────\n📅 Acumulado hoy:\n💵 Ganancia total: ${dia_data['profit']:.2f}\n"
            f"📊 Rentabilidad total: {retorno_acum:.2f}%"
        )

    return (
        f"📊 Estado de cuenta\n\n💰 Balance: {balance:.2f} {currency}\n📈 Equity: {equity:.2f} {currency}\n"
        f"📉 Margen usado: {margin:.2f} {currency}\n🔓 Margen libre: {free_margin:.2f} {currency}\n"
        f"⚖️ Nivel de margen: {margin_level:.2f}%\n💵 Ganancia bruta: {profit_total:.2f} {currency}\n"
        f"📊 Rentabilidad: {rentabilidad:.2f}%{acumulado_str}\n\n📋 Posiciones abiertas\n" + "\n".join(lineas_pos)
    )


# ══════════════════════════════════════════════════════════
# MENSAJES DE TEXTO
# ══════════════════════════════════════════════════════════
async def procesar_mensaje_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    usuario = update.effective_user.username or update.effective_user.first_name or str(update.effective_user.id)

    datos_cuenta = parsear_account_json(texto)
    if datos_cuenta:
        await update.message.reply_text(construir_mensaje_cuenta(datos_cuenta, usuario, actualizar=True))
        return

    profit = parsear_trade_json(texto)
    if profit is not None:
        dia_data = actualizar_profit_dia(profit, usuario=usuario, fuente="json_texto")
        retorno_acum = (dia_data["profit"] / dia_data["capital"] * 100) if dia_data["capital"] else 0.0
        await update.message.reply_text(
            f"💰 Trade registrado. Ganancia: ${profit:.2f}\n"
            f"📅 Acumulado hoy: ${dia_data['profit']:.2f} | Rentabilidad: {retorno_acum:.2f}%"
        )
        return

    sentimiento = analizar_sentimiento(texto)
    if sentimiento:
        actualizar_sentimiento_dia(sentimiento)
        await update.message.reply_text(f"✅ Día registrado como {sentimiento}.")


# ══════════════════════════════════════════════════════════
# RESUMEN SEMANAL
# ══════════════════════════════════════════════════════════
def generar_resumen_semanal():
    resumen_dias = get_week_summary()
    positivos = negativos = 0
    total_profit = 0.0
    lineas = []

    for dia in DIAS_SEMANA:
        info = resumen_dias[dia]
        sent, profit, capital = info["sentimiento"], info["profit"], info["capital"]
        retorno = (profit / capital * 100) if capital > 0 else 0.0
        total_profit += profit

        if sent == "positivo":
            positivos += 1
            estado = "🟢 Positivo"
        elif sent == "negativo":
            negativos += 1
            estado = "🔴 Negativo"
        else:
            estado = "⚪ Sin datos"

        lineas.append(f"{NOMBRES_DIAS[dia]}: {estado} | Profit: ${profit:.2f} | Retorno: {retorno:.2f}%")

    total_dias = positivos + negativos
    pct_aciertos = (positivos / total_dias * 100) if total_dias else 0

    if pct_aciertos >= 70:
        consejo = "Excelente semana. Sigue aplicando tu estrategia con disciplina."
    elif pct_aciertos >= 50:
        consejo = "Buena semana, pero aún puedes mejorar la consistencia."
    elif pct_aciertos >= 30:
        consejo = "Semana regular. Revisa tus reglas de gestión de riesgo y análisis."
    else:
        consejo = "Semana difícil. Considera operar menos y esperar mejores setups."

    return (
        "📅 Resumen semanal\n\n" + "\n".join(lineas) +
        f"\n\n📈 Días positivos: {positivos}\n📉 Días negativos: {negativos}\n"
        f"📊 Porcentaje de aciertos: {pct_aciertos:.1f}%\n💵 Ganancia total: ${total_profit:.2f}\n\n"
        f"💡 Consejo: {consejo}"
    )


# ══════════════════════════════════════════════════════════
# CALENDARIO ECONÓMICO (TradingView — endpoint público, gratuito)
# ══════════════════════════════════════════════════════════
CALENDARIO_TV_URL = "https://economic-calendar.tradingview.com/events"
CALENDARIO_PAISES = "US"  # cambia/agrega códigos (ej. "US,EU") si quieres más regiones


def obtener_calendario_economico():
    """
    Consulta el calendario económico público de TradingView y devuelve los
    eventos de ALTO impacto (ej. FOMC, CPI, NFP) programados para hoy.

    Nota: este es el endpoint no-oficial que usa el propio widget de
    TradingView (economic-calendar.tradingview.com), no hay API key de
    por medio porque TradingView no ofrece una oficial y gratuita para
    esto. Al ser no-oficial, TradingView podría cambiar el formato o
    bloquear la IP del servidor en cualquier momento; por eso todo está
    envuelto en try/except y si falla simplemente se avisa en vez de
    romper el saludo de las 7am. Si en algún momento deja de funcionar,
    revisa la respuesta cruda (agrega un logger.info(resp.text)) para
    ver si TradingView cambió los nombres de los campos.
    """
    try:
        hoy_ny = datetime.now(NY_TZ).date()
        desde = datetime.combine(hoy_ny, time(0, 0)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        hasta = datetime.combine(hoy_ny + timedelta(days=1), time(0, 0)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; TradingJournalBot/1.0)",
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/economic-calendar/",
        }
        params = {"from": desde, "to": hasta, "countries": CALENDARIO_PAISES}

        resp = requests.get(CALENDARIO_TV_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        eventos = resp.json().get("result", [])
    except Exception:
        logger.exception("No se pudo obtener el calendario económico de TradingView")
        return None

    # importance en TradingView: 1 = alto impacto, 0 = medio, -1 = bajo.
    alto_impacto = [e for e in eventos if (e.get("importance") or 0) >= 1]
    if not alto_impacto:
        return "🗞️ Calendario económico de hoy\n\nNo hay eventos de alto impacto programados en EE.UU. para hoy."

    alto_impacto.sort(key=lambda e: e.get("date", ""))
    lineas = []
    for e in alto_impacto:
        try:
            hora_utc = datetime.strptime(e["date"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=pytz.utc)
            hora_ny = hora_utc.astimezone(NY_TZ).strftime("%H:%M")
        except Exception:
            hora_ny = "?"

        titulo = e.get("title") or e.get("indicator") or "Evento económico"
        pronostico = e.get("forecast")
        previo = e.get("previous")
        extras = []
        if pronostico not in (None, ""):
            extras.append(f"pronóstico: {pronostico}")
        if previo not in (None, ""):
            extras.append(f"previo: {previo}")
        extra_txt = f" ({', '.join(extras)})" if extras else ""
        lineas.append(f"🔴 {hora_ny} NY — {titulo}{extra_txt}")

    return "🗞️ Calendario económico de hoy — eventos de alto impacto (EE.UU.)\n\n" + "\n".join(lineas)


# ══════════════════════════════════════════════════════════
# TAREAS PROGRAMADAS
# ══════════════════════════════════════════════════════════
def obtener_saludo(lista):
    semana_actual = datetime.now(NY_TZ).isocalendar()[1]
    return lista[(semana_actual - 1) % len(lista)]


async def saludo_lunes(context: ContextTypes.DEFAULT_TYPE):
    mensaje = obtener_saludo(SALUDOS_LUNES)
    reset_week()
    await context.bot.send_message(chat_id=CHAT_ID, text=mensaje)


async def mensaje_matutino(context: ContextTypes.DEFAULT_TYPE):
    """
    Se ejecuta de lunes a viernes a las 7:00 NY. El calendario económico
    (FOMC, CPI, NFP, etc.) es siempre el PRIMER mensaje del día; los lunes,
    justo después, se manda el saludo semanal y se reinicia la semana.
    """
    if not es_dia_habil():
        return

    calendario = obtener_calendario_economico()
    if calendario:
        await context.bot.send_message(chat_id=CHAT_ID, text=calendario)
    else:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=(
                "🗞️ No pude obtener el calendario económico de TradingView esta mañana. "
                "Revisa manualmente si hay datos de alto impacto (FOMC, CPI, NFP, etc.)."
            ),
        )

    if datetime.now(NY_TZ).weekday() == 0:  # lunes
        await saludo_lunes(context)


async def saludo_viernes(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=CHAT_ID, text=obtener_saludo(SALUDOS_VIERNES))
    await context.bot.send_message(chat_id=CHAT_ID, text=generar_resumen_semanal())
    # backup automático los viernes, así siempre tienes una copia reciente aunque Render reinicie el disco
    await enviar_backup(context.bot, CHAT_ID, motivo="automático de viernes")


async def escaneo_programado(context: ContextTypes.DEFAULT_TYPE):
    # Sábado y domingo el mercado está cerrado: no se escanea ni se toca nada.
    if not mercado_activo():
        return
    mensaje = escanear()
    # Se EDITA siempre el mismo mensaje (no se manda uno nuevo cada 5 min),
    # así el grupo no se llena de mensajes repetidos con la misma info.
    await _editar_o_enviar_mensaje(context.bot, context.bot_data, CHAT_ID, "scan_msg", mensaje)


async def felicitacion_diaria(context: ContextTypes.DEFAULT_TYPE):
    # No hay jornada que cerrar en fin de semana.
    if not es_dia_habil():
        return
    dia = obtener_dia_actual()
    info = get_day_data(dia)
    profit, capital, usuarios = info["profit"], info["capital"], info["usuarios"]
    retorno = (profit / capital * 100) if capital > 0 else 0.0

    if usuarios:
        menciones = ", ".join(f"@{u}" for u in usuarios if u)
        mensaje = (
            f"🎉 ¡Fin de jornada!\n\n👤 Operaron: {menciones}\n💵 Ganancia total: ${profit:.2f}\n"
            f"📊 Rentabilidad diaria: {retorno:.2f}%\n\n¡Felicitaciones por el trabajo de hoy! 👏"
        )
    else:
        mensaje = "🎉 ¡Fin de jornada! No se registraron operaciones hoy."
    await context.bot.send_message(chat_id=CHAT_ID, text=mensaje)


# ══════════════════════════════════════════════════════════
# BACKUP / RESTORE
# ══════════════════════════════════════════════════════════
async def enviar_backup(bot, chat_id, motivo="manual"):
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        with _db_lock:
            # checkpoint para asegurarnos de volcar el WAL al archivo principal antes de copiarlo
            with closing(get_conn()) as conn, conn:
                conn.execute("PRAGMA wal_checkpoint(FULL);")
        with open(DB_PATH, "rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=f"backup_{datetime.now(NY_TZ).strftime('%Y%m%d_%H%M')}.db",
                caption=f"🗄️ Backup {motivo}. Guarda este archivo — con /restore puedes recuperarlo.",
            )
    except FileNotFoundError:
        await bot.send_message(chat_id=chat_id, text="Todavía no hay datos para respaldar.")
    except Exception:
        logger.exception("Error generando backup")
        await bot.send_message(chat_id=chat_id, text="⚠️ No pude generar el backup, revisa los logs.")


async def comando_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await enviar_backup(context.bot, update.effective_chat.id, motivo="manual")


async def comando_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document or (update.message.reply_to_message.document if update.message.reply_to_message else None)
    if not doc:
        await update.message.reply_text("Envía este comando respondiendo (reply) al archivo .db del backup, o adjúntalo junto con /restore.")
        return
    if not doc.file_name.endswith(".db"):
        await update.message.reply_text("El archivo debe ser un backup .db generado con /backup.")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sí, restaurar", callback_data=f"restore_confirm_{doc.file_id}"),
        InlineKeyboardButton("❌ Cancelar", callback_data="restore_cancel"),
    ]])
    await update.message.reply_text(
        "⚠️ Esto reemplazará TODOS los datos actuales por los del backup. ¿Confirmas?",
        reply_markup=keyboard,
    )


async def boton_restore_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    file_id = query.data.replace("restore_confirm_", "")
    try:
        file = await context.bot.get_file(file_id)
        with _db_lock:
            tmp_path = DB_PATH + ".restore_tmp"
            await file.download_to_drive(tmp_path)
            for suffix in ("", "-wal", "-shm"):
                p = DB_PATH + suffix
                if os.path.exists(p):
                    os.remove(p)
            os.rename(tmp_path, DB_PATH)
        await query.edit_message_text("✅ Backup restaurado correctamente.")
    except Exception:
        logger.exception("Error restaurando backup")
        await query.edit_message_text("⚠️ No se pudo restaurar el backup.")


async def boton_restore_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Restauración cancelada.")


# ══════════════════════════════════════════════════════════
# COMANDOS Y MENÚ
# ══════════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mostrar_menu(update, context)


async def mostrar_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Escanear ahora", callback_data="escanear_ahora")],
        [InlineKeyboardButton("📊 Estado del bot", callback_data="estado")],
        [InlineKeyboardButton("📅 Resumen semanal", callback_data="resumen")],
        [InlineKeyboardButton("⏰ Horario de operación", callback_data="horario")],
        [InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")],
    ])
    mensaje = "🤖 Bot de Trading Journal\n\nSelecciona una opción:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text=mensaje, reply_markup=teclado)
    else:
        await update.message.reply_text(text=mensaje, reply_markup=teclado)


async def boton_escanear_ahora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Escaneando...")
    await query.edit_message_text("⏳ Escaneando el mercado, espera unos segundos...")
    await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
    await context.bot.send_message(chat_id=query.message.chat_id, text=escanear())


async def boton_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    estado_dia = "✅ Día hábil" if es_dia_habil() else "⛔ Fin de semana (mercado cerrado)"
    estado_horario = "✅ Dentro del horario NY" if dentro_horario_bollinger() else "⏸️ Fuera del horario NY"
    ocr_estado = "✅ Disponible" if TESSERACT_DISPONIBLE else "❌ No instalado"
    mensaje = (
        f"📊 Estado del bot\n\n🔹 Activo: ✅\n🔹 OCR: {ocr_estado}\n"
        f"🔹 Intervalos de escaneo: {'/'.join(INTERVALOS_ESCANEO)}\n"
        f"🔹 Horario NY: {HORA_INICIO_BOLLINGER.strftime('%H:%M')} - {HORA_FIN_BOLLINGER.strftime('%H:%M')}\n"
        f"🔹 {estado_dia}\n🔹 Estado actual: {estado_horario}\n🔹 Símbolos monitoreados: {len(TICKERS)}\n"
        f"🔹 Semana en curso: {get_current_week_start()}"
    )
    await query.edit_message_text(text=mensaje)


async def boton_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await context.bot.send_message(chat_id=query.message.chat_id, text=generar_resumen_semanal())


async def boton_horario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text=(
            f"⏰ Horario de operación (Nueva York)\n\nDesde: {HORA_INICIO_BOLLINGER.strftime('%H:%M')}\n"
            f"Hasta: {HORA_FIN_BOLLINGER.strftime('%H:%M')}\n\nSolo se enviarán alertas automáticas dentro de ese horario."
        )
    )


async def boton_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text=TEXTO_AYUDA)


TEXTO_AYUDA = (
    "❓ Comandos disponibles\n\n"
    "/start o /menu - Menú principal\n"
    "/escanear - Forzar un escaneo manual (Bollinger 5m/15m + análisis técnico)\n"
    "/tecnico TICKER - Análisis técnico bajo demanda (ej: /tecnico NVDA)\n"
    "/monitor - Activa/desactiva un mensaje que se auto-actualiza cada 1 min con el estado del mercado\n"
    "/noticias - Ver el calendario económico de hoy (FOMC, CPI, NFP, etc.)\n"
    "/resumen - Ver el resumen de la semana en curso\n"
    "/misstats - Tus estadísticas personales\n"
    "/historial - Resultados de las últimas semanas\n"
    "/backup - Descargar una copia de seguridad de los datos\n"
    "/restore - Restaurar un backup (respondiendo al archivo .db)\n\n"
    "Además, registro automáticamente:\n"
    "• Mensajes de texto con sentimiento positivo/negativo\n"
    "• Trades enviados en formato JSON\n"
    "• Capturas de pantalla donde se detecte un resultado de operación (profit/pérdida)\n\n"
    "🗓️ El escaneo automático de Bollinger (5m y 15m) solo corre de lunes a viernes, "
    "dentro del horario configurado, y actualiza SIEMPRE el mismo mensaje (no manda uno nuevo cada vez).\n"
    "🗞️ A las 7am (NY), de lunes a viernes, el primer mensaje del día es el calendario económico de alto impacto."
)


async def comando_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mostrar_menu(update, context)


async def comando_escanear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    await update.message.reply_text("⏳ Escaneando... puede tardar unos segundos.")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    await update.message.reply_text(escanear())


async def comando_tecnico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    ticker_nombre = args[0].upper() if args else "NAS100"
    ticker_yf = TICKERS.get(ticker_nombre)
    if not ticker_yf:
        await update.message.reply_text(
            "Ticker no reconocido. Usa uno de: " + ", ".join(TICKERS.keys()) +
            "\nEjemplo: /tecnico NVDA"
        )
        return

    await update.message.reply_text(f"⏳ Analizando {ticker_nombre}...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        t = yf.Ticker(ticker_yf)
        df = t.history(period=PERIODO_ESCANEO, interval="15m")
        if df.empty or len(df) < 25:
            await update.message.reply_text("No hay suficientes datos para analizar este ticker ahora mismo.")
            return

        cols = ["Close"] + (["Volume"] if "Volume" in df.columns else [])
        df = df[cols].copy()
        df["SMA20"] = df["Close"].rolling(window=20).mean()
        df["STD20"] = df["Close"].rolling(window=20).std()
        df["Upper"] = df["SMA20"] + 2 * df["STD20"]
        df["Lower"] = df["SMA20"] - 2 * df["STD20"]
        df = calcular_indicadores(df)
        tecnico = interpretar_tecnico(df)
        precio = df.iloc[-1]["Close"]

        await update.message.reply_text(
            f"📊 Análisis técnico — {ticker_nombre} (15m)\n\n💲 Precio actual: {precio:.2f}\n\n{tecnico['detalle']}"
        )
    except Exception:
        logger.exception("Error en /tecnico")
        await update.message.reply_text("⚠️ No pude generar el análisis, intenta de nuevo más tarde.")


async def comando_noticias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    calendario = obtener_calendario_economico()
    await update.message.reply_text(
        calendario or "No pude obtener el calendario económico de TradingView en este momento."
    )


async def comando_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    nombre_job = f"monitor_{chat_id}"
    jobs = context.job_queue.get_jobs_by_name(nombre_job)

    if jobs:
        for j in jobs:
            j.schedule_removal()
        context.bot_data.pop(f"monitor_msg_{chat_id}", None)
        await update.message.reply_text("🛑 Monitor en vivo desactivado.")
        return

    if mercado_activo():
        texto_inicial = generar_estado_mercado()
    else:
        texto_inicial = (
            "⏸️ Mercado cerrado ahora mismo.\n"
            "El monitor se actualizará automáticamente (en este mismo mensaje) "
            "cuando el mercado vuelva a abrir, de lunes a viernes."
        )
    msg = await update.message.reply_text(texto_inicial)
    context.bot_data[f"monitor_msg_{chat_id}"] = msg.message_id
    context.job_queue.run_repeating(
        actualizar_monitor, interval=60, first=60, chat_id=chat_id, name=nombre_job
    )
    await update.message.reply_text(
        "✅ Monitor en vivo activado: este mensaje se irá actualizando solo, cada 1 minuto, "
        "en lugar de mandar uno nuevo cada vez. Usa /monitor otra vez para apagarlo."
    )


async def comando_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(generar_resumen_semanal())


async def comando_misstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = update.effective_user.username or update.effective_user.first_name or str(update.effective_user.id)
    s = get_user_stats(usuario)
    await update.message.reply_text(
        f"📈 Tus estadísticas ({usuario})\n\n"
        f"Operaciones registradas: {s['operaciones']}\n"
        f"Profit total: ${s['profit_total']:.2f}\n"
        f"Win rate: {s['win_rate']:.1f}%"
    )


async def comando_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    semanas = get_historial_semanas(6)
    if not semanas:
        await update.message.reply_text("Todavía no hay historial de semanas anteriores.")
        return
    lineas = [f"Semana del {s['week_start']}: ${s['profit'] or 0:.2f}" for s in semanas]
    await update.message.reply_text("📚 Historial semanal\n\n" + "\n".join(lineas))


async def comando_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(TEXTO_AYUDA)


# ══════════════════════════════════════════════════════════
# SERVIDOR HTTP DE SALUD (para el health check de Render)
# ══════════════════════════════════════════════════════════
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ocr":
            try:
                version = pytesseract.get_tesseract_version() if TESSERACT_DISPONIBLE else "no instalado"
                self._responder(200, f"Tesseract: {version}")
            except Exception as e:
                self._responder(500, f"Error: {e}")
        elif self.path == "/db":
            try:
                with closing(get_conn()) as conn:
                    conn.execute("SELECT 1")
                self._responder(200, "DB OK")
            except Exception as e:
                self._responder(500, f"DB error: {e}")
        else:
            self._responder(200, "OK")

    def _responder(self, code, texto):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(texto.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # evita ensuciar los logs con cada healthcheck


def iniciar_servidor_salud():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Servidor de salud escuchando en el puerto %s", port)


# ══════════════════════════════════════════════════════════
# MANEJO DE ERRORES
# ══════════════════════════════════════════════════════════
async def manejador_errores(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Excepción no controlada", exc_info=context.error)
    if ADMIN_CHAT_ID:
        try:
            tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"⚠️ Error en el bot:\n\n{tb[-1500:]}",
            )
        except Exception:
            logger.exception("No se pudo notificar el error al admin")


# ══════════════════════════════════════════════════════════
# ARRANQUE
# ══════════════════════════════════════════════════════════
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Mostrar menú principal"),
        BotCommand("menu", "Mostrar menú principal"),
        BotCommand("escanear", "Forzar escaneo manual"),
        BotCommand("tecnico", "Análisis técnico de un ticker"),
        BotCommand("monitor", "Activar/desactivar monitor en vivo"),
        BotCommand("noticias", "Ver calendario económico de hoy"),
        BotCommand("resumen", "Ver el resumen de la semana"),
        BotCommand("misstats", "Ver tus estadísticas personales"),
        BotCommand("historial", "Ver semanas anteriores"),
        BotCommand("backup", "Descargar copia de seguridad"),
        BotCommand("restore", "Restaurar copia de seguridad"),
        BotCommand("ayuda", "Ver todos los comandos"),
    ])
    logger.info("Comandos registrados correctamente")


def configurar_tesseract():
    global TESSERACT_DISPONIBLE
    if not TESSERACT_DISPONIBLE:
        logger.warning("pytesseract no está instalado — el reconocimiento de imágenes estará DESACTIVADO.")
        return
    if platform.system() == "Windows":
        pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    else:
        pytesseract.pytesseract.tesseract_cmd = os.getenv("TESSERACT_CMD", "/usr/bin/tesseract")
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        logger.warning("No se encontró el binario de Tesseract en el sistema — el OCR estará DESACTIVADO.")
        TESSERACT_DISPONIBLE = False


def main():
    init_db()
    configurar_tesseract()
    iniciar_servidor_salud()

    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )
    app.post_init = post_init

    # Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", comando_menu))
    app.add_handler(CommandHandler("escanear", comando_escanear))
    app.add_handler(CommandHandler("tecnico", comando_tecnico))
    app.add_handler(CommandHandler("monitor", comando_monitor))
    app.add_handler(CommandHandler("noticias", comando_noticias))
    app.add_handler(CommandHandler("resumen", comando_resumen))
    app.add_handler(CommandHandler("misstats", comando_misstats))
    app.add_handler(CommandHandler("historial", comando_historial))
    app.add_handler(CommandHandler("backup", comando_backup))
    app.add_handler(CommandHandler("restore", comando_restore))
    app.add_handler(CommandHandler("ayuda", comando_ayuda))

    # Callbacks de botones
    app.add_handler(CallbackQueryHandler(boton_escanear_ahora, pattern="^escanear_ahora$"))
    app.add_handler(CallbackQueryHandler(boton_estado, pattern="^estado$"))
    app.add_handler(CallbackQueryHandler(boton_resumen, pattern="^resumen$"))
    app.add_handler(CallbackQueryHandler(boton_horario, pattern="^horario$"))
    app.add_handler(CallbackQueryHandler(boton_ayuda, pattern="^ayuda$"))
    app.add_handler(CallbackQueryHandler(boton_restore_confirm, pattern="^restore_confirm_"))
    app.add_handler(CallbackQueryHandler(boton_restore_cancel, pattern="^restore_cancel$"))

    # Mensajes
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensaje_texto))
    app.add_handler(MessageHandler(filters.PHOTO, procesar_imagen))
    app.add_handler(MessageHandler(filters.Document.FileExtension("db"), comando_restore))
    app.add_handler(CommandHandler("journal", journal))
    # Errores
    app.add_error_handler(manejador_errores)

    # Tareas programadas
    # Se escanea cada 5 minutos; escaneo_programado ya filtra día hábil + horario NY.
    app.job_queue.run_repeating(escaneo_programado, interval=300, first=10)
    # Todos los horarios llevan tzinfo=NY_TZ para que "lunes"/"viernes" se evalúen
    # en hora de Nueva York y no en UTC (evita que el saludo caiga en el día equivocado).
    # El mensaje de las 7am corre TODOS los días hábiles: primero el calendario
    # económico (siempre) y, si es lunes, el saludo semanal justo después.
    app.job_queue.run_daily(mensaje_matutino, time=time(7, 0, tzinfo=NY_TZ), days=(0, 1, 2, 3, 4))
    app.job_queue.run_daily(saludo_viernes, time=time(11, 30, tzinfo=NY_TZ), days=(4,))
    app.job_queue.run_daily(
        felicitacion_diaria, time=time(18, 0, tzinfo=NY_TZ), days=(0, 1, 2, 3, 4)
    )

    logger.info("🤖 Bot iniciado y monitoreando el mercado...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
