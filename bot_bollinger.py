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
from telegram.ext import Application, CommandHandler, ContextTypes
import pytz
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
    "GOOG": "GOOGL",
    "AAPL": "AAPL",
    "MSFT": "MSFT",
    "AMZN": "AMZN",
    "META": "META",
    "NAS100": "^NDX",
}

INTERVALO = "5m"
PERIODO = "1d"
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
def dentro_horario_bollinger():
    hora_actual = datetime.now(NY_TZ).time()
    return HORA_INICIO_BOLLINGER <= hora_actual <= HORA_FIN_BOLLINGER


def escanear():
    alertas = []
    ahora = datetime.now(NY_TZ).strftime("%d/%m/%Y %H:%M")

    for nombre, ticker_yf in TICKERS.items():
        try:
            t = yf.Ticker(ticker_yf)
            df = t.history(period=PERIODO, interval=INTERVALO)
            if df.empty or len(df) < 20:
                logger.warning("Datos insuficientes para %s", ticker_yf)
                continue

            df = df[["Close"]].copy()
            df["SMA20"] = df["Close"].rolling(window=20).mean()
            df["STD20"] = df["Close"].rolling(window=20).std()
            df["Upper"] = df["SMA20"] + 2 * df["STD20"]
            df["Lower"] = df["SMA20"] - 2 * df["STD20"]

            ultima = df.iloc[-1]
            precio, upper, lower = ultima["Close"], ultima["Upper"], ultima["Lower"]
            if pd.isna(upper) or pd.isna(lower):
                continue

            if precio > upper:
                alertas.append(f"🟢 {nombre} ({ticker_yf}) SOBRE la banda superior\nPrecio: {precio:.2f} | Banda: {upper:.2f}")
            elif precio < lower:
                alertas.append(f"🔴 {nombre} ({ticker_yf}) DEBAJO de la banda inferior\nPrecio: {precio:.2f} | Banda: {lower:.2f}")
        except Exception:
            logger.exception("Error escaneando %s", ticker_yf)

    if not alertas:
        return f"✅ {ahora}\nNinguna acción fuera de Bollinger ({INTERVALO})."
    return f"📊 Escaneo {ahora} ({INTERVALO})\n\n" + "\n\n".join(alertas)


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
    """Capturas de opciones (formato tipo ThinkOrSwim/broker de opciones)."""
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

    buy = next((t for t in trades if t["action"] == "BOT"), None)
    sell = next((t for t in trades if t["action"] == "SOLD"), None)
    if not buy or not sell:
        return None

    profit_per_contract = round(sell["price"] - buy["price"], 2)
    total_profit = profit_per_contract * buy["quantity"] * 100
    capital = buy["price"] * buy["quantity"] * 100
    porcentaje = (total_profit / capital * 100) if capital > 0 else 0.0

    return {
        "profit": total_profit,
        "capital": capital,
        "porcentaje": round(porcentaje, 2),
        "detalle": (
            f"🔹 Símbolo: {symbol}\n📅 Expiración: {expiration}\n🎯 Strike: {strike} {tipo}\n"
            f"🔢 Contratos: {buy['quantity']}\n📈 Compra: ${buy['price']:.2f}\n📉 Venta: ${sell['price']:.2f}"
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
    Último fallback: cualquier captura donde se detecte un profit/resultado,
    aunque no encaje con un broker específico. Exige contexto (palabra clave
    o signo + formato monetario) para minimizar falsos positivos.
    """
    m = _KEYWORD_PROFIT.search(texto_ocr)
    if m:
        valor = extraer_numero(m.group(1).replace("$", ""))
        return {"profit": valor, "capital": None, "porcentaje": None, "detalle": f"Detectado: “{m.group(0).strip()}”"}

    for m in _SIGNED_MONEY.finditer(texto_ocr):
        crudo = m.group(1)
        moneda = m.group(2)
        # Exigimos símbolo de moneda o que la línea contenga contexto financiero.
        linea_completa = texto_ocr[max(0, m.start()-40):m.end()+10].lower()
        contexto_ok = bool(moneda) or any(
            kw in linea_completa for kw in ["usd", "$", "balance", "equity", "equidad", "trade", "posici"]
        )
        if contexto_ok:
            valor = extraer_numero(crudo.replace("$", ""))
            return {"profit": valor, "capital": None, "porcentaje": None, "detalle": f"Detectado: “{crudo.strip()}”"}
    return None


def preprocesar_imagen(img: Image.Image) -> Image.Image:
    """Mejora simple de la imagen para OCR: escala de grises + contraste."""
    img = img.convert("L")
    img = ImageOps.autocontrast(img)
    return img

async def journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("📒 Abrir Journal", web_app=WebAppInfo(url="https://trading-journal-1iiz.onrender.com"))]
    ])
    await update.message.reply_text(
        "Toca el botón para abrir tu journal de trading:",
        reply_markup=teclado
    )
# ══════════════════════════════════════════════════════════
# PROCESAMIENTO DE IMÁGENES
# Solo responde cuando detecta un resultado de operación real.
# ══════════════════════════════════════════════════════════
async def procesar_imagen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not TESSERACT_DISPONIBLE:
        logger.warning("pytesseract no está instalado; se ignora la imagen recibida.")
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    bio = BytesIO()
    await file.download_to_memory(bio)
    bio.seek(0)

    try:
        img = Image.open(bio)
        img_procesada = preprocesar_imagen(img)
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
# TAREAS PROGRAMADAS
# ══════════════════════════════════════════════════════════
def obtener_saludo(lista):
    semana_actual = datetime.now(NY_TZ).isocalendar()[1]
    return lista[(semana_actual - 1) % len(lista)]


async def saludo_lunes(context: ContextTypes.DEFAULT_TYPE):
    mensaje = obtener_saludo(SALUDOS_LUNES)
    reset_week()
    await context.bot.send_message(chat_id=CHAT_ID, text=mensaje)


async def saludo_viernes(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=CHAT_ID, text=obtener_saludo(SALUDOS_VIERNES))
    await context.bot.send_message(chat_id=CHAT_ID, text=generar_resumen_semanal())
    # backup automático los viernes, así siempre tienes una copia reciente aunque Render reinicie el disco
    await enviar_backup(context.bot, CHAT_ID, motivo="automático de viernes")


async def escaneo_programado(context: ContextTypes.DEFAULT_TYPE):
    if not dentro_horario_bollinger():
        return
    mensaje = escanear()
    if "Ninguna" not in mensaje:
        await context.bot.send_message(chat_id=CHAT_ID, text=mensaje)


async def felicitacion_diaria(context: ContextTypes.DEFAULT_TYPE):
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


def programar_felicitacion(job_queue):
    ahora_ny = datetime.now(NY_TZ)
    proximo = ahora_ny.replace(hour=18, minute=0, second=0, microsecond=0)
    if ahora_ny >= proximo:
        proximo += timedelta(days=1)
    job_queue.run_once(felicitacion_diaria, (proximo - ahora_ny).total_seconds())


# ══════════════════════════════════════════════════════════
# BACKUP / RESTORE
# ══════════════════════════════════════════════════════════
async def enviar_backup(bot, chat_id, motivo="manual"):
    try:
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
    await context.bot.send_message(chat_id=query.message.chat_id, text=escanear())


async def boton_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    estado_horario = "✅ Dentro del horario NY" if dentro_horario_bollinger() else "⏸️ Fuera del horario NY"
    ocr_estado = "✅ Disponible" if TESSERACT_DISPONIBLE else "❌ No instalado"
    mensaje = (
        f"📊 Estado del bot\n\n🔹 Activo: ✅\n🔹 OCR: {ocr_estado}\n🔹 Intervalo de velas: {INTERVALO}\n"
        f"🔹 Horario NY: {HORA_INICIO_BOLLINGER.strftime('%H:%M')} - {HORA_FIN_BOLLINGER.strftime('%H:%M')}\n"
        f"🔹 Estado actual: {estado_horario}\n🔹 Símbolos monitoreados: {len(TICKERS)}\n"
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
    "/escanear - Forzar un escaneo manual\n"
    "/resumen - Ver el resumen de la semana en curso\n"
    "/misstats - Tus estadísticas personales\n"
    "/historial - Resultados de las últimas semanas\n"
    "/backup - Descargar una copia de seguridad de los datos\n"
    "/restore - Restaurar un backup (respondiendo al archivo .db)\n\n"
    "Además, registro automáticamente:\n"
    "• Mensajes de texto con sentimiento positivo/negativo\n"
    "• Trades enviados en formato JSON\n"
    "• Capturas de pantalla donde se detecte un resultado de operación (profit/pérdida)"
)


async def comando_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mostrar_menu(update, context)


async def comando_escanear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Escaneando... puede tardar unos segundos.")
    await update.message.reply_text(escanear())


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
    intervalo_seg = 300 if INTERVALO == "5m" else 900
    app.job_queue.run_repeating(escaneo_programado, interval=intervalo_seg, first=10)
    app.job_queue.run_daily(saludo_lunes, time=time(7, 0), days=(0,))
    app.job_queue.run_daily(saludo_viernes, time=time(11, 30), days=(4,))
    programar_felicitacion(app.job_queue)

    logger.info("🤖 Bot iniciado y monitoreando el mercado...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
