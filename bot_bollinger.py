import logging
import json
import os
import re
import hashlib
from datetime import datetime, time, timedelta
from io import BytesIO
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import pytz
import yfinance as yf
import pandas as pd
from PIL import Image
import pytesseract
from telegram import ReactionTypeEmoji

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ══════════════════════════════════════════════════════════
# CONFIGURACIÓN GENERAL (variables de entorno)
# ══════════════════════════════════════════════════════════
# TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

TOKEN = "8998906448:AAET3Ura7gGHhlklrONcY7-fQeUeLk-OXQs"
CHAT_ID = -1004309635781


if not TOKEN:
    raise ValueError("Falta la variable de entorno TELEGRAM_BOT_TOKEN")
if CHAT_ID == 0:
    raise ValueError("Falta la variable de entorno TELEGRAM_CHAT_ID")

TICKERS = {
    "NVDA": "NVDA",
    "TSLA": "TSLA",
    "AMD": "AMD",
    "GOOG": "GOOGL",
    "AAPL": "AAPL",
    "MSFT": "MSFT",
    "AMZN": "AMZN",
    "META": "META",
    "NAS100": "^NDX"
}

INTERVALO = "5m"
PERIODO = "1d"

HORA_INICIO_BOLLINGER = time(7, 30)
HORA_FIN_BOLLINGER = time(11, 30)

NY_TZ = pytz.timezone("America/New_York")

# ══════════════════════════════════════════════════════════
# SALUDOS ROTATIVOS
# ══════════════════════════════════════════════════════════
SALUDOS_LUNES = [
    "Preparados para empezar la semana, ¿no? ¡Espero que sí! 💪",
    "¡Buen lunes! A por todas en el mercado. 🚀",
    "Nueva semana, nuevas oportunidades. ¡Vamos con todo! 📈",
    "Lunes de energía renovada. ¡Que sea una gran semana! ✨",
    "Empezamos la semana con fuerza. ¡A operar! 💼",
    "Feliz lunes, traders. Que la disciplina nos acompañe. 🧠"
]

SALUDOS_VIERNES = [
    "Y terminamos la semana, hagamos un resumen para ver las estadísticas 📊",
    "¡Viernes! Hora de revisar cómo nos fue esta semana. 🔍",
    "Cerramos la semana. Veamos los números. 📈",
    "Último día de trading. Resumen semanal en camino. 🧾",
    "Viernes de balance. ¿Cómo nos fue? 🤔",
    "Se acabó la semana. Aprendamos de los resultados. 📚"
]

# ══════════════════════════════════════════════════════════
# ALMACENAMIENTO SEMANAL
# ══════════════════════════════════════════════════════════
ARCHIVO_SEMANA = "semana.json"
DIAS_SEMANA = ["monday", "tuesday", "wednesday", "thursday", "friday"]

def cargar_datos_semana():
    if os.path.exists(ARCHIVO_SEMANA):
        with open(ARCHIVO_SEMANA, "r", encoding="utf-8") as f:
            datos = json.load(f)

        # Reparar estructura si faltan claves
        for dia in DIAS_SEMANA:
            if dia not in datos["days"]:
                datos["days"][dia] = {
                    "sentimiento": None,
                    "profit": 0.0,
                    "capital": 0.0,
                    "usuarios": []
                }
            else:
                datos["days"][dia].setdefault("sentimiento", None)
                datos["days"][dia].setdefault("profit", 0.0)
                datos["days"][dia].setdefault("capital", 0.0)
                datos["days"][dia].setdefault("usuarios", [])
        if "trades" not in datos:
            datos["trades"] = []

        guardar_datos_semana(datos)
        return datos
    else:
        datos = {
            "week_start": None,
            "days": {
                dia: {
                    "sentimiento": None,
                    "profit": 0.0,
                    "capital": 0.0,
                    "usuarios": []
                }
                for dia in DIAS_SEMANA
            },
            "trades": []
        }
        guardar_datos_semana(datos)
        return datos

def guardar_datos_semana(datos):
    with open(ARCHIVO_SEMANA, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)

def obtener_dia_actual():
    ahora_ny = datetime.now(NY_TZ)
    return ahora_ny.strftime("%A").lower()

def actualizar_sentimiento_dia(sentimiento):
    datos = cargar_datos_semana()
    dia = obtener_dia_actual()
    datos["days"][dia]["sentimiento"] = sentimiento
    guardar_datos_semana(datos)

def actualizar_profit_dia(profit, capital=None, porcentaje=None, usuario=None):
    datos = cargar_datos_semana()
    dia = obtener_dia_actual()
    datos["days"][dia]["profit"] += profit
    if capital is not None:
        datos["days"][dia]["capital"] += capital
    if usuario and usuario not in datos["days"][dia]["usuarios"]:
        datos["days"][dia]["usuarios"].append(usuario)
    datos["trades"].append({
        "fecha": datetime.now(NY_TZ).isoformat(),
        "dia": dia,
        "profit": profit,
        "capital": capital,
        "usuario": usuario
    })
    guardar_datos_semana(datos)

# ══════════════════════════════════════════════════════════
# DEDUPLICACIÓN DE IMÁGENES
# ══════════════════════════════════════════════════════════
# Guarda hash de textos OCR ya procesados con timestamp
_ocr_recientes = {}

def _hash_texto(texto):
    return hashlib.md5(texto.encode("utf-8")).hexdigest()

def es_duplicado(texto_ocr, ventana_seg=300):
    """Devuelve True si el texto OCR fue procesado en los últimos 'ventana_seg' segundos."""
    ahora = datetime.now()
    # Limpiar entradas antiguas
    global _ocr_recientes
    _ocr_recientes = {
        h: ts for h, ts in _ocr_recientes.items()
        if (ahora - ts).total_seconds() < ventana_seg
    }
    h = _hash_texto(texto_ocr)
    if h in _ocr_recientes:
        return True
    _ocr_recientes[h] = ahora
    return False

# ══════════════════════════════════════════════════════════
# ESCANEO BOLLINGER
# ══════════════════════════════════════════════════════════
def dentro_horario_bollinger():
    ahora_ny = datetime.now(NY_TZ)
    hora_actual = ahora_ny.time()
    return HORA_INICIO_BOLLINGER <= hora_actual <= HORA_FIN_BOLLINGER

def escanear():
    alertas = []
    ahora = datetime.now(NY_TZ).strftime("%d/%m/%Y %H:%M")

    for nombre, ticker_yf in TICKERS.items():
        try:
            t = yf.Ticker(ticker_yf)
            df = t.history(period=PERIODO, interval=INTERVALO)

            if df.empty:
                continue

            df = df[["Close"]].copy()
            df["SMA20"] = df["Close"].rolling(window=20).mean()
            df["STD20"] = df["Close"].rolling(window=20).std()
            df["Upper"] = df["SMA20"] + 2 * df["STD20"]
            df["Lower"] = df["SMA20"] - 2 * df["STD20"]

            ultima = df.iloc[-1]
            precio = ultima["Close"]
            upper = ultima["Upper"]
            lower = ultima["Lower"]

            if pd.isna(upper) or pd.isna(lower):
                continue

            if precio > upper:
                alertas.append(
                    f"🟢 {nombre} ({ticker_yf}) está SOBRE la banda superior\n"
                    f"Precio: {precio:.2f} | Banda superior: {upper:.2f}"
                )
            elif precio < lower:
                alertas.append(
                    f"🔴 {nombre} ({ticker_yf}) está DEBAJO de la banda inferior\n"
                    f"Precio: {precio:.2f} | Banda inferior: {lower:.2f}"
                )

        except Exception as e:
            print(f"Error con {ticker_yf}: {e}")

    if not alertas:
        return f"✅ {ahora}\nNinguna acción fuera de Bollinger ({INTERVALO})."
    return f"📊 Escaneo {ahora} ({INTERVALO})\n\n" + "\n\n".join(alertas)

# ══════════════════════════════════════════════════════════
# ANÁLISIS DE SENTIMIENTO Y PARSING DE JSON
# ══════════════════════════════════════════════════════════
PALABRAS_POSITIVAS = ["positivo", "verde", "bien", "cumplió", "cumplio", "ganancia", "profit", "+", "arriba", "excelente"]
PALABRAS_NEGATIVAS = ["negativo", "rojo", "mal", "no cumplió", "no cumplio", "pérdida", "perdida", "-", "abajo", "malo"]

def analizar_sentimiento(texto):
    texto_lower = texto.lower()
    if any(p in texto_lower for p in PALABRAS_POSITIVAS):
        return "positivo"
    elif any(n in texto_lower for n in PALABRAS_NEGATIVAS):
        return "negativo"
    return None

def parsear_trade_json(texto):
    try:
        datos = json.loads(texto)
        if "trades" in datos and "summary" in datos:
            total_profit = datos["summary"].get("total_profit", 0)
            return total_profit
        elif "summary" in datos and "total_profit" in datos["summary"]:
            return datos["summary"]["total_profit"]
        return None
    except json.JSONDecodeError:
        return None

def parsear_account_json(texto):
    try:
        datos = json.loads(texto)
        if "account" in datos and "positions" in datos:
            return datos
        return None
    except json.JSONDecodeError:
        return None

def extraer_numero(texto):
    """Limpia un string numérico: quita espacios, comas y convierte a float."""
    texto = re.sub(r'[\s,]', '', texto)
    try:
        return float(texto)
    except:
        return 0.0

def parsear_ocr_trade(texto_ocr):
    """
    Intenta extraer información de trade desde texto OCR.
    Devuelve dict con trades y summary, o None si no lo detecta.
    """
    match_info = re.search(
        r'([A-Z]{2,5})\s+\d+\s+\(Weeklys\)\s+(\d{1,2}\s+[A-Z]{3}\s+\d{2}).*?(\d{3,4})\s*([CP])',
        texto_ocr, re.IGNORECASE
    )
    if not match_info:
        match_info = re.search(
            r'([A-Z]{2,5}).*?(\d{1,2}\s+[A-Z]{3}\s+\d{2}).*?(\d{3,4})\s*([CP])',
            texto_ocr, re.IGNORECASE
        )
    if not match_info:
        return None

    symbol = match_info.group(1)
    expiration = match_info.group(2).strip()
    strike = int(match_info.group(3))
    tipo = "Call" if match_info.group(4).upper() == "C" else "Put"

    trades = []
    for line in texto_ocr.splitlines():
        match_trade = re.search(
            r'([+\-]?\d+)\s+(BOT|SOLD)\s+@([\d.]+)\s+(\d{1,2}/\d{1,2}/\d{2}),\s+(\d{1,2}:\d{2}\s*[AP]M)',
            line, re.IGNORECASE
        )
        if match_trade:
            quantity = int(match_trade.group(1))
            action = match_trade.group(2).upper()
            price = float(match_trade.group(3))
            date_str = match_trade.group(4)
            time_str = match_trade.group(5)
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", "%m/%d/%y %I:%M %p")
                iso_time = dt.isoformat()
            except:
                iso_time = ""
            trades.append({
                "symbol": f"{symbol} 100 (Weeklys)",
                "expiration": expiration,
                "strike": strike,
                "type": tipo,
                "action": action,
                "quantity": abs(quantity),
                "price": price,
                "status": "FILLED",
                "time": iso_time
            })

    if len(trades) < 2:
        return None

    buy = None
    sell = None
    for t in trades:
        if t["action"] == "BOT":
            buy = t
        elif t["action"] == "SOLD":
            sell = t
    if not buy or not sell:
        return None

    profit_per_contract = round(sell["price"] - buy["price"], 2)
    total_profit = profit_per_contract * buy["quantity"] * 100
    capital_invertido = buy["price"] * buy["quantity"] * 100

    if capital_invertido > 0:
        porcentaje_retorno = (total_profit / capital_invertido) * 100
    else:
        porcentaje_retorno = 0.0

    return {
        "trades": trades,
        "summary": {
            "profit_per_contract": profit_per_contract,
            "total_profit": total_profit,
            "capital": capital_invertido,
            "currency": "USD",
            "porcentaje_retorno": round(porcentaje_retorno, 2)
        }
    }

def parsear_ocr_account(texto_ocr):
    """
    Parser especializado para capturas de MetaTrader 5 (móvil).
    Soporta números con espacios de miles y posiciones con símbolos genéricos.
    """
    lineas = texto_ocr.splitlines()
    datos = {
        "account": {},
        "positions": []
    }

    # ---- Campos de cuenta ----
    patrones_cuenta = {
        "balance": r'Balance\s*:?\s*([\d\s]+\.\d{2})',
        "equity": r'Equidad\s*:?\s*([\d\s]+\.\d{2})',
        "margin": r'(?<!Free\s)Margen\s*:?\s*([\d\s]+\.\d{2})',
        "free_margin": r'Margen\s*libre\s*:?\s*([\d\s]+\.\d{2})',
        "margin_level_percent": r'Nivel\s*de\s*margen\s*\(?%?\)?\s*:?\s*([\d\s]+\.\d{1,2})',
        "profit_total": r'=\s*([+\-]?[\d\s]+\.\d{2})\s*USD'
    }

    for campo, patron in patrones_cuenta.items():
        for linea in lineas:
            match = re.search(patron, linea, re.IGNORECASE)
            if match:
                datos["account"][campo] = extraer_numero(match.group(1))
                break

    if "balance" not in datos["account"] and "equity" not in datos["account"]:
        return None

    # ---- Posiciones ----
    i = 0
    while i < len(lineas):
        linea = lineas[i].strip()
        # Detectar línea de símbolo: permite letras, dígitos, #, etc.
        match_simbolo = re.search(
            r'#?([A-Z]{2,}[A-Z0-9]*)\s*[,]?\s*(buy|sell|sel|sel!|BOT|SOLD)\s*([\d.]+)?',
            linea, re.IGNORECASE
        )
        if match_simbolo:
            symbol = match_simbolo.group(1).upper()
            tipo_bruto = match_simbolo.group(2).lower()
            if "sel" in tipo_bruto:
                tipo = "sell"
            elif "bot" in tipo_bruto:
                tipo = "buy"
            elif "sold" in tipo_bruto:
                tipo = "sell"
            else:
                tipo = tipo_bruto
            volumen = extraer_numero(match_simbolo.group(3)) if match_simbolo.group(3) else 0.0

            # Buscar en la siguiente línea los precios y profit
            if i + 1 < len(lineas):
                linea_siguiente = lineas[i + 1].strip()
                match_precios = re.search(
                    r'([\d\s]+\.\d{1,2})\s*[—\-–]\s*([\d\s]+\.\d{1,2})\s*([+\-]?[\d\s]*\.\d{1,2})?',
                    linea_siguiente, re.IGNORECASE
                )
                if match_precios:
                    entry = extraer_numero(match_precios.group(1))
                    current = extraer_numero(match_precios.group(2))
                    profit_str = match_precios.group(3)
                    profit = extraer_numero(profit_str) if profit_str else 0.0

                    if profit == 0.0:
                        match_profit_extra = re.search(r'([+\-]?[\d\s]+\.\d{1,2})\s*$', linea_siguiente)
                        if match_profit_extra:
                            profit = extraer_numero(match_profit_extra.group(1))

                    datos["positions"].append({
                        "symbol": symbol,
                        "type": tipo,
                        "volume": volumen,
                        "entry_price": entry,
                        "current_price": current,
                        "profit": profit
                    })
                    i += 2
                    continue
            # Si no se pudo emparejar precios, agregar con datos mínimos
            datos["positions"].append({
                "symbol": symbol,
                "type": tipo,
                "volume": volumen,
                "entry_price": 0.0,
                "current_price": 0.0,
                "profit": 0.0
            })
        i += 1

    return datos if (datos["account"] or datos["positions"]) else None

# ══════════════════════════════════════════════════════════
# OCR Y MANEJO DE IMÁGENES
# ══════════════════════════════════════════════════════════
async def procesar_imagen(update, context):
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    bio = BytesIO()
    await file.download_to_memory(bio)
    bio.seek(0)

    img = Image.open(bio)
    texto_ocr = pytesseract.image_to_string(img)

    if not texto_ocr.strip():
        await update.message.reply_text("No pude leer texto en la imagen.")
        return

    if es_duplicado(texto_ocr):
        await update.message.reply_text("ℹ️ Esta imagen ya fue procesada recientemente. No se acumuló de nuevo.")
        return

    usuario = update.effective_user.username or update.effective_user.first_name or str(update.effective_user.id)

    # 1) JSON de cuenta/posiciones
    datos_cuenta = parsear_account_json(texto_ocr)
    if datos_cuenta:
        mensaje = construir_mensaje_cuenta(datos_cuenta, usuario, actualizar=True)
        await update.message.reply_text(mensaje)
        try:
            await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
        except Exception as e:
            print(f"No se pudo reaccionar: {e}")
        return

    # 2) JSON de trade previo
    profit_json = parsear_trade_json(texto_ocr)
    if profit_json is not None:
        actualizar_profit_dia(profit_json, usuario=usuario)
        datos = cargar_datos_semana()
        dia = obtener_dia_actual()
        profit_acum = datos["days"][dia]["profit"]
        capital_acum = datos["days"][dia]["capital"]
        retorno_acum = (profit_acum / capital_acum * 100) if capital_acum > 0 else 0.0
        mensaje = (f"💰 Trade JSON registrado. Ganancia: ${profit_json:.2f}\n"
                   f"📅 Acumulado hoy: ${profit_acum:.2f} | Rentabilidad: {retorno_acum:.2f}%")
        await update.message.reply_text(mensaje)
        try:
           await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
        except Exception as e:
            print(f"No se pudo reaccionar: {e}")
        return

    # 3) Parser OCR para trade de opciones
    datos_trade = parsear_ocr_trade(texto_ocr)
    if datos_trade:
        total_profit = datos_trade["summary"]["total_profit"]
        capital = datos_trade["summary"].get("capital", 0.0)
        porcentaje = datos_trade["summary"].get("porcentaje_retorno", 0.0)

        actualizar_profit_dia(total_profit, capital=capital, porcentaje=porcentaje, usuario=usuario)

        datos = cargar_datos_semana()
        dia = obtener_dia_actual()
        profit_acum = datos["days"][dia]["profit"]
        capital_acum = datos["days"][dia]["capital"]
        retorno_acum = (profit_acum / capital_acum * 100) if capital_acum > 0 else 0.0

        t0 = datos_trade["trades"][0]
        t1 = datos_trade["trades"][1] if len(datos_trade["trades"]) > 1 else t0

        detalle = (
            f"✅ Trade detectado en imagen\n\n"
            f"👤 Usuario: {usuario}\n"
            f"🔹 Símbolo: {t0['symbol']}\n"
            f"📅 Expiración: {t0['expiration']}\n"
            f"🎯 Strike: {t0['strike']} {t0['type']}\n"
            f"🔢 Cantidad: {t0['quantity']} contratos\n\n"
            f"📈 Compra: ${t0['price']:.2f}\n"
            f"📉 Venta: ${t1['price']:.2f}\n"
            f"💰 Ganancia bruta: ${total_profit:.2f}\n"
            f"📊 Retorno de la operación: {porcentaje:.2f}%\n\n"
            f"───────\n"
            f"📅 Acumulado hoy:\n"
            f"💵 Ganancia total: ${profit_acum:.2f}\n"
            f"📊 Rentabilidad total: {retorno_acum:.2f}%"
        )
        await update.message.reply_text(detalle)
        try:
            await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
        except Exception as e:
            print(f"No se pudo reaccionar: {e}")
        return

    # 4) Parser OCR para cuenta MT5
    datos_cuenta_ocr = parsear_ocr_account(texto_ocr)
    if datos_cuenta_ocr:
        mensaje = construir_mensaje_cuenta(datos_cuenta_ocr, usuario, actualizar=True)
        await update.message.reply_text(mensaje)
        try:
            await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
        except Exception as e:
            print(f"No se pudo reaccionar: {e}")
        return

# ══════════════════════════════════════════════════════════
# MENSAJES DE TEXTO
# ══════════════════════════════════════════════════════════
async def procesar_mensaje_texto(update, context):
    texto = update.message.text.strip()
    usuario = update.effective_user.username or update.effective_user.first_name or str(update.effective_user.id)

    # 1) JSON de cuenta/posiciones
    datos_cuenta = parsear_account_json(texto)
    if datos_cuenta:
        mensaje = construir_mensaje_cuenta(datos_cuenta, usuario, actualizar=True)
        await update.message.reply_text(mensaje)
        return

    # 2) JSON de trade previo
    profit = parsear_trade_json(texto)
    if profit is not None:
        actualizar_profit_dia(profit, usuario=usuario)
        datos = cargar_datos_semana()
        dia = obtener_dia_actual()
        profit_acum = datos["days"][dia]["profit"]
        capital_acum = datos["days"][dia]["capital"]
        retorno_acum = (profit_acum / capital_acum * 100) if capital_acum > 0 else 0.0
        await update.message.reply_text(
            f"💰 Trade registrado. Ganancia: ${profit:.2f}\n"
            f"📅 Acumulado hoy: ${profit_acum:.2f} | Rentabilidad: {retorno_acum:.2f}%"
        )
        return

    # 3) Análisis de sentimiento
    sentimiento = analizar_sentimiento(texto)
    if sentimiento:
        actualizar_sentimiento_dia(sentimiento)
        await update.message.reply_text(f"✅ Día registrado como {sentimiento}.")
    else:
        pass

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

    # Si no hay profit_total en account, sumamos posiciones
    if profit_total == 0.0 and posiciones:
        profit_total = sum(p.get("profit", 0.0) for p in posiciones)

    # Rentabilidad sobre balance (o equity si balance es 0)
    base = balance if balance > 0 else equity
    rentabilidad = (profit_total / base * 100) if base > 0 else 0.0

    lineas_pos = []
    for p in posiciones:
        simbolo = p.get("symbol", "?")
        tipo = p.get("type", "")
        volumen = p.get("volume", 0.0)
        entry = p.get("entry_price", 0.0)
        actual = p.get("current_price", 0.0)
        profit = p.get("profit", 0.0)
        lineas_pos.append(
            f"• {simbolo} ({tipo}) {volumen} lots\n"
            f"   Entrada: {entry} | Actual: {actual}\n"
            f"   Profit: {profit:.2f} {currency}"
        )

    pos_str = "\n".join(lineas_pos) if lineas_pos else "Sin posiciones abiertas"

    # Acumular en el día si se solicita
    acumulado_str = ""
    if actualizar:
        # Usamos profit_total como ganancia del día y balance como capital
        actualizar_profit_dia(profit_total, capital=balance, usuario=usuario)
        datos = cargar_datos_semana()
        dia = obtener_dia_actual()
        profit_acum = datos["days"][dia]["profit"]
        capital_acum = datos["days"][dia]["capital"]
        retorno_acum = (profit_acum / capital_acum * 100) if capital_acum > 0 else 0.0
        acumulado_str = (
            f"\n───────\n"
            f"📅 Acumulado hoy:\n"
            f"💵 Ganancia total: ${profit_acum:.2f}\n"
            f"📊 Rentabilidad total: {retorno_acum:.2f}%"
        )

    mensaje = (
        f"📊 **Estado de cuenta**\n\n"
        f"💰 Balance: {balance:.2f} {currency}\n"
        f"📈 Equity: {equity:.2f} {currency}\n"
        f"📉 Margen usado: {margin:.2f} {currency}\n"
        f"🔓 Margen libre: {free_margin:.2f} {currency}\n"
        f"⚖️ Nivel de margen: {margin_level:.2f}%\n"
        f"💵 Ganancia bruta: {profit_total:.2f} {currency}\n"
        f"📊 Rentabilidad: {rentabilidad:.2f}%"
        f"{acumulado_str}\n\n"
        f"📋 **Posiciones abiertas**\n"
        f"{pos_str}"
    )
    return mensaje

# ══════════════════════════════════════════════════════════
# RESUMEN SEMANAL
# ══════════════════════════════════════════════════════════
def generar_resumen_semanal():
    datos = cargar_datos_semana()
    positivos = 0
    negativos = 0
    total_profit = 0.0
    lineas = []

    for dia in DIAS_SEMANA:
        info = datos["days"].get(dia, {"sentimiento": None, "profit": 0.0, "capital": 0.0})
        sent = info.get("sentimiento")
        profit = info.get("profit", 0.0)
        capital = info.get("capital", 0.0)
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

        lineas.append(
            f"{dia.capitalize()}: {estado} | Profit: ${profit:.2f} | Retorno: {retorno:.2f}%"
        )

    total_dias = positivos + negativos
    porcentaje_aciertos = (positivos / total_dias * 100) if total_dias > 0 else 0

    if porcentaje_aciertos >= 70:
        consejo = "Excelente semana. Sigue aplicando tu estrategia con disciplina."
    elif porcentaje_aciertos >= 50:
        consejo = "Buena semana, pero aún puedes mejorar la consistencia."
    elif porcentaje_aciertos >= 30:
        consejo = "Semana regular. Revisa tus reglas de gestión de riesgo y análisis."
    else:
        consejo = "Semana difícil. Considera operar menos y esperar mejores setups."

    tabla = "\n".join(lineas)
    resumen = (
        "📅 Resumen semanal\n\n"
        f"{tabla}\n\n"
        f"📈 Días positivos: {positivos}\n"
        f"📉 Días negativos: {negativos}\n"
        f"📊 Porcentaje de aciertos: {porcentaje_aciertos:.1f}%\n"
        f"💵 Ganancia total: ${total_profit:.2f}\n\n"
        f"💡 Consejo: {consejo}"
    )
    return resumen

# ══════════════════════════════════════════════════════════
# TAREAS PROGRAMADAS
# ══════════════════════════════════════════════════════════
def obtener_saludo(lista):
    semana_actual = datetime.now(NY_TZ).isocalendar()[1]
    indice = (semana_actual - 1) % len(lista)
    return lista[indice]

async def saludo_lunes(context):
    mensaje = obtener_saludo(SALUDOS_LUNES)
    nuevo_datos = {
        "week_start": datetime.now(NY_TZ).strftime("%Y-%m-%d"),
        "days": {dia: {"sentimiento": None, "profit": 0.0, "capital": 0.0, "usuarios": []} for dia in DIAS_SEMANA},
        "trades": []
    }
    guardar_datos_semana(nuevo_datos)
    await context.bot.send_message(chat_id=CHAT_ID, text=mensaje)

async def saludo_viernes(context):
    mensaje = obtener_saludo(SALUDOS_VIERNES)
    resumen = generar_resumen_semanal()
    await context.bot.send_message(chat_id=CHAT_ID, text=mensaje)
    await context.bot.send_message(chat_id=CHAT_ID, text=resumen)

async def escaneo_programado(context):
    if not dentro_horario_bollinger():
        print("⏸️ Fuera de horario NY, omitiendo escaneo automático.")
        return
    mensaje = escanear()
    if "Ninguna" not in mensaje:
        await context.bot.send_message(chat_id=CHAT_ID, text=mensaje)

async def felicitacion_diaria(context):
    datos = cargar_datos_semana()
    dia = obtener_dia_actual()
    info = datos["days"].get(dia, {})
    profit = info.get("profit", 0.0)
    capital = info.get("capital", 0.0)
    usuarios = info.get("usuarios", [])

    retorno = (profit / capital * 100) if capital > 0 else 0.0

    if usuarios:
        menciones = ", ".join([f"@{u}" for u in usuarios if u])
        mensaje = (
            f"🎉 ¡Fin de jornada!\n\n"
            f"👤 Operaron: {menciones}\n"
            f"💵 Ganancia total: ${profit:.2f}\n"
            f"📊 Rentabilidad diaria: {retorno:.2f}%\n\n"
            f"¡Felicitaciones por el trabajo de hoy! 👏"
        )
    else:
        mensaje = "🎉 ¡Fin de jornada! No se registraron operaciones hoy."

    await context.bot.send_message(chat_id=CHAT_ID, text=mensaje)

def programar_felicitacion(job_queue):
    ahora_ny = datetime.now(NY_TZ)
    proximo = ahora_ny.replace(hour=18, minute=0, second=0, microsecond=0)
    if ahora_ny >= proximo:
        proximo += timedelta(days=1)
    segundos = (proximo - ahora_ny).total_seconds()
    job_queue.run_once(felicitacion_diaria, segundos)

# ══════════════════════════════════════════════════════════
# SERVIDOR HTTP PARA HEALTH CHECK
# ══════════════════════════════════════════════════════════
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

# ══════════════════════════════════════════════════════════
# COMANDOS Y MENÚ
# ══════════════════════════════════════════════════════════
async def start(update, context):
    await mostrar_menu(update, context)

async def mostrar_menu(update, context):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Escanear ahora", callback_data="escanear_ahora")],
        [InlineKeyboardButton("📊 Estado del bot", callback_data="estado")],
        [InlineKeyboardButton("⏰ Horario de operación", callback_data="horario")],
        [InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")],
    ])
    mensaje = "🤖 Bot de Bandas de Bollinger\n\nSelecciona una opción:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text=mensaje, reply_markup=teclado)
    else:
        await update.message.reply_text(text=mensaje, reply_markup=teclado)

async def boton_escanear_ahora(update, context):
    query = update.callback_query
    await query.answer("Escaneando...")
    await query.edit_message_text("⏳ Escaneando el mercado, espera unos segundos...")
    mensaje = escanear()
    await context.bot.send_message(chat_id=query.message.chat_id, text=mensaje)

async def boton_estado(update, context):
    query = update.callback_query
    await query.answer()
    estado_horario = "✅ Dentro del horario NY" if dentro_horario_bollinger() else "⏸️ Fuera del horario NY"
    mensaje = (
        f"📊 Estado del bot\n\n"
        f"🔹 Activo: ✅\n"
        f"🔹 Intervalo de velas: {INTERVALO}\n"
        f"🔹 Horario NY: {HORA_INICIO_BOLLINGER.strftime('%H:%M')} - {HORA_FIN_BOLLINGER.strftime('%H:%M')}\n"
        f"🔹 Estado actual: {estado_horario}\n"
        f"🔹 Símbolos monitoreados: {len(TICKERS)}\n"
    )
    await query.edit_message_text(text=mensaje)

async def boton_horario(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text=(
            f"⏰ Horario de operación (Nueva York)\n\n"
            f"Desde: {HORA_INICIO_BOLLINGER.strftime('%H:%M')}\n"
            f"Hasta: {HORA_FIN_BOLLINGER.strftime('%H:%M')}\n\n"
            "Solo se enviarán alertas automáticas dentro de ese horario."
        )
    )

async def boton_ayuda(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text=(
            "❓ Comandos disponibles\n\n"
            "/start - Mostrar menú principal\n"
            "/menu - Mostrar menú principal\n"
            "/escanear - Forzar un escaneo manual\n\n"
            "Además, registro automáticamente tus mensajes con sentimiento positivo/negativo y los trades que envíes en formato JSON o imagen."
        )
    )

async def comando_menu(update, context):
    await mostrar_menu(update, context)

async def comando_escanear(update, context):
    await update.message.reply_text("⏳ Escaneando... puede tardar unos segundos.")
    mensaje = escanear()
    await update.message.reply_text(mensaje)

# ══════════════════════════════════════════════════════════
# REGISTRO DE COMANDOS Y CONFIGURACIÓN
# ══════════════════════════════════════════════════════════
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Mostrar menú principal"),
        BotCommand("menu", "Mostrar menú principal"),
        BotCommand("escanear", "Forzar escaneo manual"),
    ])

async def manejador_errores(update, context):
    print(f"⚠️ Error controlado: {context.error}")

def main():
    logging.basicConfig(level=logging.INFO)

    # Iniciar servidor HTTP para health check (Render)
    start_health_server()

    # Si Tesseract no está en PATH local, descomenta y ajusta:
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

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

    # Callbacks
    app.add_handler(CallbackQueryHandler(boton_escanear_ahora, pattern="^escanear_ahora$"))
    app.add_handler(CallbackQueryHandler(boton_estado, pattern="^estado$"))
    app.add_handler(CallbackQueryHandler(boton_horario, pattern="^horario$"))
    app.add_handler(CallbackQueryHandler(boton_ayuda, pattern="^ayuda$"))

    # Mensajes
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensaje_texto))
    app.add_handler(MessageHandler(filters.PHOTO, procesar_imagen))

    # Errores
    app.add_error_handler(manejador_errores)

    # Tareas programadas
    intervalo_seg = 300 if INTERVALO == "5m" else 900
    app.job_queue.run_repeating(escaneo_programado, interval=intervalo_seg, first=10)

    app.job_queue.run_daily(saludo_lunes, time=time(7, 0), days=(0,))
    app.job_queue.run_daily(saludo_viernes, time=time(11, 30), days=(4,))

    # Felicitación diaria a las 18:00 NY
    programar_felicitacion(app.job_queue)

    print("🤖 Bot iniciado y monitoreando el mercado...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()