import os
import time
import json
import logging
import threading
import gspread
import requests
import asyncio
import aiohttp
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any
from datetime import datetime
from google.oauth2.service_account import Credentials

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

# ==================== CHARGEMENT VARIABLES ENVIRONNEMENT ====================
load_dotenv()

# Configuration depuis les variables d'environnement
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
PORT = int(os.getenv("PORT", 8000))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", 2.0))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Vérification des clés API
if not API_KEY or not API_SECRET:
    raise Exception("❌ Clés API manquantes! Configure BINANCE_API_KEY et BINANCE_API_SECRET dans .env")

# Configuration Google Sheets depuis variables d'environnement
GOOGLE_SHEETS_CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")

if not GOOGLE_SHEETS_CREDENTIALS_JSON or not SPREADSHEET_ID:
    raise Exception("❌ Configuration Google Sheets manquante! Configure GOOGLE_SHEETS_CREDENTIALS_JSON et SPREADSHEET_ID dans .env")

try:
    SERVICE_ACCOUNT_JSON = json.loads(GOOGLE_SHEETS_CREDENTIALS_JSON)
except json.JSONDecodeError as e:
    raise Exception(f"❌ Format JSON invalide pour GOOGLE_SHEETS_CREDENTIALS_JSON: {e}")

# Configuration du logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

app = FastAPI()

# ==================== KEEP ALIVE MANAGER ====================
class KeepAliveManager:
    def __init__(self):
        self.last_activity = time.time()
        self.active_connections = 0
        
    def record_activity(self):
        self.last_activity = time.time()
        
    def get_status(self):
        return {
            "last_activity": self.last_activity,
            "minutes_since_activity": (time.time() - self.last_activity) / 60,
            "active_connections": self.active_connections
        }

keep_alive_manager = KeepAliveManager()

# ==================== GOOGLE SHEETS HANDLER ====================
class GoogleSheetsHandler:
    def __init__(self):
        self.client = None
        self.spreadsheet = None
        self.history_sheet = None
        self.state_sheet = None
        self.init_connection()
    
    def init_connection(self):
        """Initialise la connexion à Google Sheets"""
        try:
            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_JSON, scopes=scope)
            self.client = gspread.authorize(creds)
            self.spreadsheet = self.client.open_by_key(SPREADSHEET_ID)
            
            self.init_history_sheet()
            self.init_state_sheet()
            
            logging.info("✅ Google Sheets complètement initialisé")
            
        except Exception as e:
            logging.error(f"❌ Erreur connexion Google Sheets: {e}")
            self.client = None
    
    def init_history_sheet(self):
        """Initialise la feuille d'historique"""
        try:
            # Utiliser la première feuille sans changer son titre
            self.history_sheet = self.spreadsheet.sheet1
            
            # Vérifier/créer les en-têtes
            if not self.history_sheet.get('A1'):
                headers = [
                    "ID", "Date Heure", "Type", "Symbole", "Direction", "Niveau",
                    "Prix Entrée", "Quantité", "Capital", "Effet Levier", 
                    "Prix TP", "Prix SL", "Prix Fermeture", "Type Fermeture",
                    "Profit/Loss (USDT)", "Statut", "Order ID", "TP Order ID", "SL Order ID",
                    "Niveau Renforcement Suivant", "Durée Position", "Timestamp",
                    "TP Status", "SL Status"  # NOUVEAU: Statut des ordres
                ]
                self.history_sheet.append_row(headers)
                logging.info("📊 Feuille Historique initialisée")
                
        except Exception as e:
            logging.error(f"❌ Erreur initialisation historique: {e}")
    
    def init_state_sheet(self):
        """Initialise la feuille d'état"""
        try:
            # Créer ou récupérer la feuille State
            try:
                self.state_sheet = self.spreadsheet.worksheet("State")
            except gspread.WorksheetNotFound:
                self.state_sheet = self.spreadsheet.add_worksheet(title="State", rows=100, cols=5)
                # En-têtes pour State
                self.state_sheet.append_row(["timestamp", "state_json"])
                logging.info("🔧 Feuille State créée")
                
        except Exception as e:
            logging.error(f"❌ Erreur initialisation state: {e}")
    
    # ==================== GESTION HISTORIQUE ====================
    def add_trading_record(self, entry_type, data):
        """Ajoute un record à l'historique trading"""
        if not self.history_sheet:
            logging.error("❌ Feuille historique non initialisée")
            return False
            
        try:
            # Calculer durée si fermeture
            duration = ""
            if entry_type == "POSITION_CLOSED":
                open_timestamp = data.get("open_timestamp")
                if open_timestamp:
                    try:
                        open_time = datetime.fromisoformat(open_timestamp.replace('Z', '+00:00'))
                        close_time = datetime.now()
                        duration_seconds = (close_time - open_time).total_seconds()
                        hours = int(duration_seconds // 3600)
                        minutes = int((duration_seconds % 3600) // 60)
                        seconds = int(duration_seconds % 60)
                        duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                    except Exception as e:
                        logging.warning(f"⚠️ Erreur calcul durée: {e}")
            
            # Nouvel ID
            existing_records = self.history_sheet.get_all_records()
            new_id = len(existing_records) + 1
            
            # NOUVEAU: Récupérer les statuts des ordres
            tp_status = data.get("tp_status", "ACTIVE")
            sl_status = data.get("sl_status", "ACTIVE")
            
            # Nouvelle ligne
            new_row = [
                new_id,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                entry_type,
                data.get("symbol", ""),
                data.get("direction", ""),
                data.get("level", 1),
                data.get("entry_price", 0),
                data.get("quantity", 0),
                data.get("capital", 0),
                data.get("leverage", 1),
                data.get("tp_price", 0),
                data.get("sl_price", 0),
                data.get("close_price", 0),
                data.get("close_type", ""),
                data.get("profit_loss", 0),
                "ACTIVE" if entry_type in ["POSITION_OPENED", "REINFORCEMENT_OPENED"] else "CLOSED",
                data.get("order_id", ""),
                data.get("tp_order_id", ""),
                data.get("sl_order_id", ""),
                data.get("next_reinforcement_level", 1),
                duration,
                datetime.now().isoformat(),
                tp_status,  # NOUVEAU
                sl_status   # NOUVEAU
            ]
            
            self.history_sheet.append_row(new_row)
            logging.info(f"📝 Record ajouté: {entry_type} - {data.get('symbol', '')}")
            return True
            
        except Exception as e:
            logging.error(f"❌ Erreur ajout record: {e}")
            return False
    
    # ==================== GESTION ÉTAT ====================
    def save_state(self, state_data):
        """Sauvegarde l'état de l'application"""
        if not self.state_sheet:
            logging.error("❌ Feuille state non initialisée")
            return False
            
        try:
            # Sauvegarder avec timestamp
            self.state_sheet.append_row([
                datetime.now().isoformat(),
                json.dumps(state_data, indent=2)
            ])
            
            # Garder seulement les 10 dernières sauvegardes
            records = self.state_sheet.get_all_records()
            if len(records) > 10:
                self.state_sheet.delete_rows(2, len(records) - 9)  # Garde 10 lignes
            
            logging.info("💾 État sauvegardé dans Google Sheets")
            return True
            
        except Exception as e:
            logging.error(f"❌ Erreur sauvegarde état: {e}")
            return False
    
    def load_state(self):
        """Charge le dernier état de l'application"""
        if not self.state_sheet:
            logging.error("❌ Feuille state non initialisée")
            return {"positions": {}, "processed_alerts": {}}
            
        try:
            records = self.state_sheet.get_all_records()
            if records:
                # Prendre le DERNIER enregistrement
                last_record = records[-1]
                state_json = last_record["state_json"]
                return json.loads(state_json)
            else:
                return {"positions": {}, "processed_alerts": {}}
                
        except Exception as e:
            logging.error(f"❌ Erreur chargement état: {e}")
            return {"positions": {}, "processed_alerts": {}}
    
    def get_sheets_info(self):
        """Retourne les infos des feuilles"""
        try:
            history_records = len(self.history_sheet.get_all_records()) if self.history_sheet else 0
            state_records = len(self.state_sheet.get_all_records()) if self.state_sheet else 0
            
            return {
                "history_records": history_records,
                "state_records": state_records,
                "spreadsheet_id": SPREADSHEET_ID,
                "status": "connected"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

# Instance globale Google Sheets
gsheets = GoogleSheetsHandler()

# ==================== INITIALISATION BINANCE ====================
if USE_TESTNET:
    client = Client(API_KEY, API_SECRET, testnet=True)
    logging.info("🔧 Mode TESTNET activé")
else:
    client = Client(API_KEY, API_SECRET)
    logging.info("🚀 Mode LIVE activé - ATTENTION!")

# Ta stratégie de niveaux
LEVELS = [
    {"capital": 1.0,  "leverage": 50, "tp_pct": 0.003, "sl_pct": 0.003},
    {"capital": 2.0,  "leverage": 50, "tp_pct": 0.003, "sl_pct": 0.003},
    {"capital": 4.5,  "leverage": 50, "tp_pct": 0.003, "sl_pct": 0.003},
    {"capital": 9.5,  "leverage": 50, "tp_pct": 0.003, "sl_pct": 0.003},
    {"capital": 16.0, "leverage": 65, "tp_pct": 0.003, "sl_pct": 0.003},
]

# ==================== GESTION D'ÉTAT AVEC VERROUS ====================
state_lock = threading.Lock()
symbol_locks: Dict[str, threading.Lock] = {}

def get_symbol_lock(symbol: str):
    with state_lock:
        if symbol not in symbol_locks:
            symbol_locks[symbol] = threading.Lock()
        return symbol_locks[symbol]

def load_state():
    """Charge l'état depuis Google Sheets"""
    return gsheets.load_state()

def save_state(state):
    """Sauvegarde l'état dans Google Sheets"""
    success = gsheets.save_state(state)
    if not success:
        logging.error("❌ Échec sauvegarde état Google Sheets")

def add_to_history(entry_type, data):
    """Ajoute à l'historique Google Sheets"""
    success = gsheets.add_trading_record(entry_type, data)
    if not success:
        logging.error(f"❌ Échec sauvegarde historique: {entry_type}")

# ==================== FONCTIONS AMÉLIORÉES PNL ET ORDRES ====================
def calculate_pnl(position, close_type, close_price=None):
    """Calcule le profit/perte d'une position AVEC levier"""
    try:
        entry_price = position.get("entry_price", 0)
        quantity = position.get("quantity", 0)
        leverage = position.get("leverage", 1)
        
        if close_type == "TP":
            level_config = LEVELS[position.get("current_level", 1)-1]
            if position.get("signal").upper() == "BUY":
                close_price = entry_price * (1 + level_config["tp_pct"])
            else:
                close_price = entry_price * (1 - level_config["tp_pct"])
        elif close_type == "SL":
            level_config = LEVELS[position.get("current_level", 1)-1]
            if position.get("signal").upper() == "BUY":
                close_price = entry_price * (1 - level_config["sl_pct"])
            else:
                close_price = entry_price * (1 + level_config["sl_pct"])
        
        if close_price is None and close_type == "MANUAL":
            close_price = position.get("close_price", entry_price)
        
        # CORRECTION : Ajouter l'effet de levier
        if position.get("signal").upper() == "BUY":
            pnl = (close_price - entry_price) * quantity * leverage
        else:
            pnl = (entry_price - close_price) * quantity * leverage
            
        return round(pnl, 4)
    except Exception as e:
        logging.error(f"❌ Erreur calcul PnL: {e}")
        return 0

def safe_cancel_order(symbol: str, order_id: int, max_retries=3):
    """Annule un ordre de manière robuste avec retry"""
    for attempt in range(max_retries):
        try:
            # Vérifier d'abord si l'ordre existe encore
            status, order_info = get_order_status(symbol, order_id)
            
            if status is None:
                logging.info(f"✅ Ordre {order_id} déjà inexistant")
                return "ALREADY_CANCELED"
                
            if status in ["FILLED", "CANCELED", "EXPIRED"]:
                logging.info(f"✅ Ordre {order_id} déjà en état terminal: {status}")
                return status
                
            # Annuler l'ordre
            client.futures_cancel_order(symbol=symbol, orderId=order_id)
            logging.info(f"✅ Ordre {order_id} annulé avec succès (tentative {attempt+1})")
            return "CANCELED"
            
        except BinanceAPIException as e:
            if "Unknown order" in str(e):
                logging.info(f"✅ Ordre {order_id} déjà annulé/inconnu")
                return "UNKNOWN"
            logging.warning(f"⚠️ Erreur annulation ordre {order_id} (tentative {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)  # Attendre avant retry
        except Exception as e:
            logging.warning(f"⚠️ Erreur générale annulation {order_id} (tentative {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
    
    logging.error(f"💥 Échec annulation ordre {order_id} après {max_retries} tentatives")
    return "FAILED"

def enhanced_order_status_check(symbol: str, order_id: int):
    """Vérification robuste du statut d'un ordre"""
    try:
        order = client.futures_get_order(symbol=symbol, orderId=order_id)
        status = order.get("status")
        
        # Vérifier aussi via les ordres ouverts
        open_orders = client.futures_get_open_orders(symbol=symbol)
        open_order_ids = [str(o['orderId']) for o in open_orders]
        
        is_still_open = str(order_id) in open_order_ids
        
        logging.info(f"🔍 Statut ordre {order_id}: {status}, ouvert: {is_still_open}")
        return status, order, is_still_open
        
    except BinanceAPIException as e:
        if "Unknown order" in str(e):
            return "UNKNOWN", None, False
        raise e

# ==================== CALCULS DE QUANTITÉ ====================
SYMBOL_INFO_CACHE = {}

def fetch_symbol_info(symbol: str):
    if symbol in SYMBOL_INFO_CACHE:
        return SYMBOL_INFO_CACHE[symbol]
    info = client.futures_exchange_info()
    for s in info['symbols']:
        if s['symbol'] == symbol:
            SYMBOL_INFO_CACHE[symbol] = s
            return s
    raise Exception(f"Symbole {symbol} non trouvé")

def get_step_size(symbol: str):
    s = fetch_symbol_info(symbol)
    for f in s['filters']:
        if f['filterType'] == 'LOT_SIZE':
            return float(f['stepSize'])
    return 0.0001

def get_price_precision(symbol: str):
    """Récupère la précision de prix pour un symbole"""
    try:
        symbol_info = fetch_symbol_info(symbol)
        for f in symbol_info['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                tick_size = float(f['tickSize'])
                # Calcul du nombre de décimales
                if tick_size < 1:
                    return len(str(tick_size).split('.')[1].rstrip('0'))
                else:
                    return 0
        return 2  # Valeur par défaut
    except Exception as e:
        logging.warning(f"⚠️ Impossible de récupérer la précision prix: {e}")
        return 2

def get_quantity_precision(symbol):
    """Récupère la précision de quantité pour un symbole"""
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        # Calcul du nombre de décimales
                        if step_size < 1:
                            return len(str(step_size).split('.')[1].rstrip('0'))
                        return 0
        return 3  # Valeur par défaut
    except Exception as e:
        logging.warning(f"⚠️ Impossible de récupérer la précision: {e}")
        return 3

def round_qty(qty: float, step: float):
    step_dec = Decimal(str(step))
    q = Decimal(str(qty))
    rounded = (q // step_dec) * step_dec
    return float(rounded.quantize(step_dec, rounding=ROUND_DOWN))

def calculate_quantity(capital, leverage, price, symbol):
    """Calcule la quantité avec la bonne précision"""
    notional = capital * leverage
    raw_quantity = notional / price
    
    step = get_step_size(symbol)
    quantity = round_qty(raw_quantity, step)
    
    logging.info(f"📊 Calcul quantité: {capital} × {leverage} = {notional} / {price} = {raw_quantity} → {quantity}")
    return quantity

# ==================== GESTION DES ORDRES ====================
def wait_for_order_execution(symbol, order_id, max_attempts=10):
    """Attend que l'ordre soit exécuté et retourne le prix moyen"""
    for i in range(max_attempts):
        try:
            order_status = client.futures_get_order(symbol=symbol, orderId=order_id)
            status = order_status['status']
            avg_price = float(order_status['avgPrice'])
            executed_qty = float(order_status['executedQty'])
            
            logging.info(f"📊 Statut ordre {i+1}/{max_attempts}: {status}, Prix: {avg_price}, Qty exécutée: {executed_qty}")
            
            if status == 'FILLED' and avg_price > 0:
                logging.info(f"🎉 Ordre exécuté! Prix moyen: {avg_price}")
                return avg_price
            elif status in ['CANCELED', 'EXPIRED', 'REJECTED']:
                raise Exception(f"Ordre {status}")
                
        except Exception as e:
            logging.warning(f"⚠️ Erreur vérification ordre: {e}")
        
        time.sleep(1)
    
    # Fallback: utiliser le prix actuel
    ticker = client.futures_symbol_ticker(symbol=symbol)
    current_price = float(ticker['price'])
    logging.info(f"⏰ Timeout, utilisation prix actuel: {current_price}")
    return current_price

def get_order_status(symbol: str, order_id: int):
    """Récupère le statut d'un ordre"""
    try:
        order = client.futures_get_order(symbol=symbol, orderId=order_id)
        return order.get("status"), order
    except Exception as e:
        logging.debug(f"❌ Échec récupération statut ordre {order_id}: {e}")
        return None, None

def get_position_amount(symbol: str):
    """Vérification simplifiée de la position"""
    try:
        # Méthode alternative: vérifier via les ordres ouverts
        open_orders = client.futures_get_open_orders(symbol=symbol)
        has_tp_sl = any(order['type'] in ['STOP_MARKET', 'TAKE_PROFIT_MARKET'] for order in open_orders)
        
        if has_tp_sl:
            logging.info(f"🔍 Position {symbol} active (TP/SL trouvés)")
            return 1.0  # Retourne une valeur non nulle
        else:
            logging.info(f"🔍 Position {symbol} - Aucun TP/SL trouvé")
            return 0.0
            
    except Exception as e:
        logging.warning(f"⚠️ Erreur vérification position {symbol}: {e}")
        return 1.0  # En cas d'erreur, suppose que la position est active

# ==================== PLACEMENT DES ORDRES AVEC closePosition ====================
def place_tp_sl_orders_with_retry(symbol, signal, entry_price, level_config, max_retries=3):
    """Place les ordres Take Profit et Stop Loss avec retry en cas d'échec"""
    tp_pct = level_config["tp_pct"]
    sl_pct = level_config["sl_pct"]
    
    if signal.upper() == "BUY":
        tp_price = entry_price * (1 + tp_pct)
        sl_price = entry_price * (1 - sl_pct)
        tp_side = "SELL"
        sl_side = "SELL"
    else:
        tp_price = entry_price * (1 - tp_pct)
        sl_price = entry_price * (1 + sl_pct)
        tp_side = "BUY"
        sl_side = "BUY"
    
    # CORRECTION : Utiliser la bonne précision automatiquement
    price_precision = get_price_precision(symbol)
    tp_price = round(tp_price, price_precision)
    sl_price = round(sl_price, price_precision)
    
    logging.info(f"🎯 TP: {tp_price} (précision: {price_precision}), SL: {sl_price}")
    
    # Ordre Take Profit avec closePosition
    tp_order_id = None
    sl_order_id = None
    
    # Placement TP avec retry
    for attempt in range(max_retries):
        try:
            tp_order = client.futures_create_order(
                symbol=symbol,
                side=tp_side,
                type="TAKE_PROFIT_MARKET",
                stopPrice=tp_price,
                closePosition=True,
                timeInForce="GTC"
            )
            tp_order_id = tp_order.get("orderId")
            logging.info(f"✅ TP placé: {tp_order_id}")
            break
        except Exception as e:
            logging.error(f"❌ Erreur placement TP (tentative {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                logging.error(f"💥 Échec placement TP après {max_retries} tentatives")
    
    # Placement SL avec retry
    for attempt in range(max_retries):
        try:
            sl_order = client.futures_create_order(
                symbol=symbol,
                side=sl_side,
                type="STOP_MARKET",
                stopPrice=sl_price,
                closePosition=True,
                timeInForce="GTC"
            )
            sl_order_id = sl_order.get("orderId")
            logging.info(f"✅ SL placé: {sl_order_id}")
            break
        except Exception as e:
            logging.error(f"❌ Erreur placement SL (tentative {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                logging.error(f"💥 Échec placement SL après {max_retries} tentatives")
    
    return tp_order_id, sl_order_id

def place_binance_order(symbol, signal, quantity, level_config):
    """Place un ordre sur Binance avec TP/SL en utilisant closePosition=True"""
    try:
        leverage = level_config["leverage"]
        
        # 1. Définir le levier
        logging.info(f"🔧 Mise à jour levier: {symbol} → {leverage}")
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
        
        # 2. Déterminer le côté de l'ordre
        side = "BUY" if signal.upper() == "BUY" else "SELL"
        
        # 3. Placer l'ordre MARKET
        logging.info(f"🎯 Placement ordre: {side} {quantity} {symbol}")
        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type='MARKET',
            quantity=quantity
        )
        
        logging.info(f"✅ Ordre créé: {order['orderId']}")
        
        # 4. Attendre l'exécution et obtenir le prix
        entry_price = wait_for_order_execution(symbol, order['orderId'])
        
        # 5. Placer les ordres TP/SL avec closePosition=True ET retry
        tp_order_id, sl_order_id = place_tp_sl_orders_with_retry(symbol, signal, entry_price, level_config)
        
        return order, entry_price, tp_order_id, sl_order_id
        
    except BinanceAPIException as e:
        logging.error(f"❌ Erreur Binance: {e}")
        raise
    except Exception as e:
        logging.error(f"❌ Erreur inattendue: {e}")
        raise

# ==================== FONCTIONS DE SANTÉ ET ANTI-SLEEP ====================
async def perform_health_checks():
    """Vérifications de santé complètes"""
    checks = {
        "binance": False,
        "gsheets": False,
        "monitoring": False,
        "memory": False
    }
    
    try:
        # Test Binance
        client.ping()
        checks["binance"] = True
    except Exception as e:
        logging.warning(f"⚠️ Health check Binance échoué: {e}")
    
    try:
        # Test Google Sheets
        gsheets_info = gsheets.get_sheets_info()
        checks["gsheets"] = gsheets_info.get("status") == "connected"
    except Exception as e:
        logging.warning(f"⚠️ Health check Google Sheets échoué: {e}")
    
    # Test monitoring thread
    checks["monitoring"] = monitor_thread.is_alive() if monitor_thread else False
    
    # Test mémoire
    try:
        import psutil
        process = psutil.Process()
        memory_usage = process.memory_info().rss / 1024 / 1024  # MB
        checks["memory"] = memory_usage < 500  # Alert si > 500MB
        checks["memory_usage_mb"] = round(memory_usage, 2)
    except:
        checks["memory"] = True  # Skip si psutil pas disponible
    
    return checks

async def internal_self_ping():
    """Auto-ping interne pour maintenir l'activité"""
    try:
        # Se ping soi-même toutes les 2 minutes en plus des pings externes
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://localhost:{PORT}/webhook2",
                json={"signal": "PING", "internal": True},
                timeout=10
            ):
                logging.debug("🔄 Auto-ping interne effectué")
    except Exception as e:
        logging.debug(f"🔁 Auto-ping échoué (normal au démarrage): {e}")

# ==================== MONITORING AVEC DÉLAI DE GRÂCE AMÉLIORÉ ====================
def check_orphaned_position(symbol, position, state, time_diff):
    """Vérifie et nettoie les positions orphelines"""
    try:
        # Vérifier si la position existe vraiment sur Binance
        open_orders = client.futures_get_open_orders(symbol=symbol)
        has_active_orders = any(order['type'] in ['STOP_MARKET', 'TAKE_PROFIT_MARKET'] for order in open_orders)
        
        # Vérifier la position futures
        positions_info = client.futures_position_information(symbol=symbol)
        position_amount = float(positions_info[0]['positionAmt']) if positions_info else 0
        
        position_active = has_active_orders or abs(position_amount) > 0
        
        if not position_active and position.get("is_active", True) and time_diff > 120:
            # Position orpheline depuis plus de 2 minutes - nettoyer
            logging.warning(f"🧹 Nettoyage position orpheline: {symbol}")
            
            # Récupérer le prix actuel
            ticker = client.futures_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            
            # Enregistrer fermeture avec statut des ordres
            history_data = {
                "symbol": symbol,
                "direction": position.get("signal"),
                "level": position.get("current_level", 1),
                "entry_price": position.get("entry_price"),
                "quantity": position.get("quantity"),
                "close_price": current_price,
                "close_type": "ORPHANED_CLEANUP",
                "profit_loss": calculate_pnl(position, "MANUAL", current_price),
                "next_reinforcement_level": 1,
                "open_timestamp": position.get("timestamp"),
                "tp_status": "CANCELED_ORPHANED",  # NOUVEAU
                "sl_status": "CANCELED_ORPHANED"   # NOUVEAU
            }
            add_to_history("POSITION_CLOSED", history_data)
            
            # Nettoyer l'état
            position["is_active"] = False
            
            # Annuler les ordres restants
            tp_order_id = position.get("tp_order_id")
            sl_order_id = position.get("sl_order_id")
            if tp_order_id:
                safe_cancel_order(symbol, tp_order_id)
            if sl_order_id:
                safe_cancel_order(symbol, sl_order_id)
                
            save_state(state)
            
    except Exception as e:
        logging.warning(f"⚠️ Erreur vérification position orpheline {symbol}: {e}")

def monitor_loop():
    """Boucle de surveillance améliorée avec gestion robuste des ordres"""
    logging.info("🔍 Démarrage du monitoring automatique amélioré")
    
    while True:
        try:
            state = load_state()
            positions = state.get("positions", {})
            
            for symbol, position in list(positions.items()):
                if not position.get("is_active", True):
                    continue
                
                # Vérifier le délai de grâce (30 secondes)
                position_timestamp = position.get("timestamp", "")
                if position_timestamp:
                    try:
                        position_time = datetime.fromisoformat(position_timestamp.replace('Z', '+00:00'))
                        time_diff = (datetime.now().replace(tzinfo=None) - position_time.replace(tzinfo=None)).total_seconds()
                        
                        if time_diff < 30:
                            continue
                    except Exception:
                        continue
                
                # Verrou pour éviter les conflits
                lock = get_symbol_lock(symbol)
                if not lock.acquire(blocking=False):
                    continue
                
                try:
                    current_level = position.get("current_level", 1)
                    tp_order_id = position.get("tp_order_id")
                    sl_order_id = position.get("sl_order_id")
                    signal = position.get("signal")
                    entry_price = position.get("entry_price")
                    
                    # VÉRIFICATION ROBUSTE DES ORDRES TP/SL
                    tp_triggered = False
                    sl_triggered = False
                    tp_status = "ACTIVE"
                    sl_status = "ACTIVE"
                    
                    if tp_order_id:
                        tp_status, tp_info, tp_open = enhanced_order_status_check(symbol, tp_order_id)
                        if tp_status in ("FILLED", "TRIGGERED") or not tp_open:
                            logging.info(f"🎯 TP exécuté/désactivé pour {symbol}")
                            tp_triggered = True
                            tp_status = "FILLED"
                    
                    if sl_order_id and not tp_triggered:
                        sl_status, sl_info, sl_open = enhanced_order_status_check(symbol, sl_order_id)
                        if sl_status in ("FILLED", "TRIGGERED") or not sl_open:
                            logging.info(f"🛑 SL exécuté/désactivé pour {symbol}")
                            sl_triggered = True
                            sl_status = "FILLED"
                    
                    # ACTION SELON L'ORDRE DÉCLENCHÉ
                    if tp_triggered:
                        # Annuler le SL de manière robuste
                        sl_cancel_status = "ACTIVE"
                        if sl_order_id:
                            sl_cancel_status = safe_cancel_order(symbol, sl_order_id)
                        
                        # Enregistrer la fermeture AVEC STATUT DES ORDRES
                        history_data = {
                            "symbol": symbol,
                            "direction": signal,
                            "level": current_level,
                            "entry_price": entry_price,
                            "quantity": position.get("quantity"),
                            "close_type": "TAKE_PROFIT",
                            "profit_loss": calculate_pnl(position, "TP"),
                            "next_reinforcement_level": 1,
                            "open_timestamp": position.get("timestamp"),
                            "tp_status": tp_status,      # NOUVEAU
                            "sl_status": sl_cancel_status # NOUVEAU
                        }
                        add_to_history("POSITION_CLOSED", history_data)
                        
                        # Fermer la position
                        position["is_active"] = False
                        save_state(state)
                        continue
                    
                    if sl_triggered:
                        # Annuler le TP de manière robuste
                        tp_cancel_status = "ACTIVE"
                        if tp_order_id:
                            tp_cancel_status = safe_cancel_order(symbol, tp_order_id)
                        
                        # Enregistrer la fermeture AVEC STATUT DES ORDRES
                        history_data = {
                            "symbol": symbol,
                            "direction": signal,
                            "level": current_level,
                            "entry_price": entry_price,
                            "quantity": position.get("quantity"),
                            "close_type": "STOP_LOSS",
                            "profit_loss": calculate_pnl(position, "SL"),
                            "next_reinforcement_level": current_level + 1 if current_level < len(LEVELS) else 1,
                            "open_timestamp": position.get("timestamp"),
                            "tp_status": tp_cancel_status, # NOUVEAU
                            "sl_status": sl_status         # NOUVEAU
                        }
                        add_to_history("POSITION_CLOSED", history_data)
                        
                        # Gérer le renforcement
                        handle_reinforcement(symbol, signal, current_level, state, position)
                        continue
                    
                    # VÉRIFICATION DE SÉCURITÉ : Nettoyage des positions orphelines
                    if not tp_triggered and not sl_triggered:
                        check_orphaned_position(symbol, position, state, time_diff)
                        
                finally:
                    lock.release()
                    
        except Exception as e:
            logging.error(f"❌ Erreur dans monitor_loop: {e}")
            time.sleep(10)  # Attendre plus longtemps en cas d'erreur
        
        time.sleep(3)  # Vérifier plus fréquemment

def handle_reinforcement(symbol, signal, current_level, state, position):
    """Prépare le renforcement pour le prochain signal (quelle que soit la direction)"""
    next_level = current_level + 1
    
    if next_level > len(LEVELS):
        logging.info(f"💥 Niveau maximum atteint pour {symbol} - Séquence terminée")
        position["is_active"] = False
        save_state(state)
        return
    
    # Préparer le renforcement sans direction spécifique
    logging.info(f"⏳ Renforcement préparé: {symbol} prochain signal → niveau {next_level}")
    
    # Marquer la position comme inactive mais garder l'info du niveau suivant
    position.update({
        "is_active": False,
        "pending_reinforcement": True,
        "next_level": next_level
    })
    
    save_state(state)

# ==================== DÉMARRAGE RÉSILIENT DU MONITORING ====================
def start_monitoring():
    """Démarrage robuste du monitoring"""
    global monitor_thread
    
    # Vérifier si le thread existe déjà et est actif
    if monitor_thread and monitor_thread.is_alive():
        logging.info("✅ Thread monitoring déjà actif")
        return
    
    # Créer un nouveau thread
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    logging.info("🚀 Thread monitoring démarré")
    
    # Planifier une vérification de santé dans 30 secondes
    threading.Timer(30, check_monitoring_health).start()

def check_monitoring_health():
    """Vérifie la santé du thread monitoring et le redémarre si nécessaire"""
    if not monitor_thread or not monitor_thread.is_alive():
        logging.warning("🔄 Thread monitoring mort - redémarrage...")
        start_monitoring()
    else:
        # Replanifier la vérification
        threading.Timer(60, check_monitoring_health).start()

# Démarrer le monitoring
monitor_thread = None
start_monitoring()

# ==================== FONCTION DE TRAITEMENT DES SIGNALS ====================
async def process_trading_signal(signal, symbol, price, data, webhook_source="principal"):
    """Traite les signaux de trading (commun aux deux webhooks)"""
    if not signal or price == 0:
        raise HTTPException(status_code=400, detail="Signal ou prix manquant")
    
    # Verrou pour ce symbole
    lock = get_symbol_lock(symbol)
    if not lock.acquire(timeout=10):
        raise HTTPException(status_code=429, detail="Symbole occupé")
    
    try:
        state = load_state()
        positions = state.get("positions", {})
        
        # VÉRIFIER SI RENFORCEMENT EN ATTENTE (quelle que soit la direction)
        if symbol in positions:
            position = positions[symbol]
            if position.get("pending_reinforcement", False):
                next_level = position.get("next_level", 1)
                
                # 🔥 OUVRIR DANS LA DIRECTION DU NOUVEAU SIGNAL, AU NIVEAU SUIVANT
                logging.info(f"🎯 Renforcement activé: {symbol} niveau {next_level} - Direction: {signal}")
                
                # Ouvrir la position au niveau suivant avec la NOUVELLE direction
                level_config = LEVELS[next_level - 1]
                capital = level_config["capital"]
                leverage = level_config["leverage"]
                quantity = calculate_quantity(capital, leverage, price, symbol)
                
                if quantity <= 0:
                    raise HTTPException(status_code=400, detail="Quantité invalide")
                
                # Placer l'ordre de renforcement avec la NOUVELLE direction
                order_result, entry_price, tp_order_id, sl_order_id = place_binance_order(
                    symbol, signal, quantity, level_config
                )
                
                # Ajouter à l'historique
                history_data = {
                    "symbol": symbol,
                    "direction": signal,
                    "level": next_level,
                    "entry_price": entry_price,
                    "quantity": quantity,
                    "capital": capital,
                    "leverage": leverage,
                    "tp_price": entry_price * (1 + level_config["tp_pct"]) if signal.upper() == "BUY" else entry_price * (1 - level_config["tp_pct"]),
                    "sl_price": entry_price * (1 - level_config["sl_pct"]) if signal.upper() == "BUY" else entry_price * (1 + level_config["sl_pct"]),
                    "order_id": order_result['orderId'],
                    "tp_order_id": tp_order_id,
                    "sl_order_id": sl_order_id,
                    "previous_level": next_level - 1,
                    "next_reinforcement_level": next_level + 1 if next_level < len(LEVELS) else 1,
                    "tp_status": "ACTIVE",  # NOUVEAU
                    "sl_status": "ACTIVE"   # NOUVEAU
                }
                add_to_history("REINFORCEMENT_OPENED", history_data)
                
                # Mettre à jour l'état
                position.update({
                    "is_active": True,
                    "pending_reinforcement": False,
                    "current_level": next_level,
                    "signal": signal,  # 🔥 Nouvelle direction
                    "quantity": quantity,
                    "entry_price": entry_price,
                    "capital": capital,
                    "leverage": leverage,
                    "order_id": order_result['orderId'],
                    "tp_order_id": tp_order_id,
                    "sl_order_id": sl_order_id,
                    "timestamp": datetime.now().isoformat()
                })
                save_state(state)
                
                return {
                    "status": "success", 
                    "message": f"Renforcement {signal} (Niveau {next_level})",
                    "webhook": webhook_source,
                    "details": {
                        "symbol": symbol,
                        "quantity": quantity,
                        "entry_price": entry_price,
                        "capital": capital,
                        "leverage": leverage,
                        "order_id": order_result['orderId'],
                        "current_level": next_level
                    }
                }
        
        # VÉRIFICATION DES DOUBLONS
        alert_id = f"{symbol}_{signal}_{data.get('time', '')}"
        processed = state.setdefault("processed_alerts", {})
        if alert_id in processed:
            return {"status": "ignored", "reason": "duplicate_alert", "webhook": webhook_source}
        processed[alert_id] = int(time.time())
        
        # VÉRIFIER SI POSITION ACTIVE
        if symbol in state.get("positions", {}):
            position = state["positions"][symbol]
            if position.get("is_active", True):
                position_amount = get_position_amount(symbol)
                if position_amount != 0:
                    return {"status": "ignored", "reason": "position_already_open", "webhook": webhook_source}
                else:
                    # Nettoyer l'état si position fermée
                    del state["positions"][symbol]
        
        # OUVERTURE NOUVELLE POSITION (niveau 1)
        level_config = LEVELS[0]
        capital = level_config["capital"]
        leverage = level_config["leverage"]
        quantity = calculate_quantity(capital, leverage, price, symbol)
        
        if quantity <= 0:
            raise HTTPException(status_code=400, detail="Quantité invalide")
        
        # Placer l'ordre
        order_result, entry_price, tp_order_id, sl_order_id = place_binance_order(
            symbol, signal, quantity, level_config
        )
        
        # Ajouter à l'historique
        history_data = {
            "symbol": symbol,
            "direction": signal,
            "level": 1,
            "entry_price": entry_price,
            "quantity": quantity,
            "capital": capital,
            "leverage": leverage,
            "tp_price": entry_price * (1 + level_config["tp_pct"]) if signal.upper() == "BUY" else entry_price * (1 - level_config["tp_pct"]),
            "sl_price": entry_price * (1 - level_config["sl_pct"]) if signal.upper() == "BUY" else entry_price * (1 + level_config["sl_pct"]),
            "order_id": order_result['orderId'],
            "tp_order_id": tp_order_id,
            "sl_order_id": sl_order_id,
            "next_reinforcement_level": 2,
            "tp_status": "ACTIVE",  # NOUVEAU
            "sl_status": "ACTIVE"   # NOUVEAU
        }
        add_to_history("POSITION_OPENED", history_data)
        
        # Sauvegarder l'état
        state["positions"][symbol] = {
            "signal": signal,
            "current_level": 1,
            "is_active": True,
            "quantity": quantity,
            "entry_price": entry_price,
            "capital": capital,
            "leverage": leverage,
            "order_id": order_result['orderId'],
            "tp_order_id": tp_order_id,
            "sl_order_id": sl_order_id,
            "alert_id": alert_id,
            "timestamp": datetime.now().isoformat(),
            "pending_reinforcement": False,
            "next_level": 1  # 🔥 Initialiser le niveau suivant
        }
        save_state(state)
        
        return {
            "status": "success", 
            "message": f"Position {signal} ouverte (Niveau 1)",
            "webhook": webhook_source,
            "details": {
                "symbol": symbol,
                "quantity": quantity,
                "entry_price": entry_price,
                "capital": capital,
                "leverage": leverage,
                "order_id": order_result['orderId'],
                "current_level": 1
            }
        }
        
    finally:
        lock.release()

# ==================== ENDPOINTS FASTAPI ====================
@app.get("/health")
def health():
    return {"status":"ok", "timestamp": datetime.now().isoformat()}

@app.post("/webhook")
async def webhook(request: Request):
    """Webhook principal pour VOTRE INDICATEUR TRADING EXISTANT"""
    try:
        data = await request.json()
        logging.info(f"📥 Webhook PRINCIPAL reçu: {data}")
        
        signal = data.get("signal", "").upper()
        symbol = data.get("symbol", "ETHUSDC")
        price = float(data.get("price", 0))
        
        # TRAITEMENT NORMAL DES SIGNALS TRADING
        return await process_trading_signal(signal, symbol, price, data, "principal")
            
    except Exception as e:
        logging.error(f"❌ Erreur webhook principal: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook2")
async def webhook2(request: Request):
    """Webhook secondaire amélioré avec keep-alive robuste"""
    keep_alive_manager.record_activity()
    keep_alive_manager.active_connections += 1
    
    try:
        data = await request.json()
        
        signal = data.get("signal", "").upper()
        
        # ANTI-SLEEP AMÉLIORÉ
        if signal == "PING":
            logging.info("🔁 Keep-alive ping reçu sur webhook2")
            
            # Vérifications de santé
            health_status = await perform_health_checks()
            
            response = {
                "status": "ping", 
                "timestamp": datetime.now().isoformat(),
                "message": "Bot actif via webhook2",
                "webhook": "anti-sleep",
                "health_checks": health_status,
                "keep_alive": keep_alive_manager.get_status()
            }
            
            # Auto-test interne pour maintenir l'activité
            asyncio.create_task(internal_self_ping())
            
            return response
        
        # TRAITEMENT NORMAL POUR LE DEUXIÈME INDICATEUR
        symbol = data.get("symbol", "ETHUSDC")
        price = float(data.get("price", 0))
        
        return await process_trading_signal(signal, symbol, price, data, "secondaire")
        
    except Exception as e:
        logging.error(f"❌ Erreur webhook2: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        keep_alive_manager.active_connections -= 1

@app.post("/")
async def root_post(request: Request):
    """Accepte les POST sur la racine"""
    try:
        logging.info("🔄 Requête reçue sur la racine")
        return await webhook(request)
    except Exception as e:
        logging.error(f"❌ Erreur route racine: {str(e)}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def root():
    return {"message": "Bot Trading Webhook - Double Webhook + Google Sheets"}

@app.get("/state")
async def get_state():
    """Endpoint pour voir l'état actuel"""
    return load_state()

@app.get("/history")
async def get_history(limit: int = 50):
    """Endpoint pour voir l'historique des trades depuis Google Sheets"""
    try:
        if not gsheets.history_sheet:
            return {"history": []}
            
        records = gsheets.history_sheet.get_all_records()
        # Retourner les derniers enregistrements
        return {"history": records[-limit:] if records else []}
    except Exception as e:
        logging.error(f"❌ Erreur chargement historique: {e}")
        return {"history": []}

@app.get("/history/stats")
async def get_history_stats():
    """Statistiques de l'historique depuis Google Sheets"""
    try:
        if not gsheets.history_sheet:
            return {
                "total_trades": 0,
                "total_profit": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0
            }
            
        records = gsheets.history_sheet.get_all_records()
        if not records:
            return {
                "total_trades": 0,
                "total_profit": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0
            }
        
        # Filtrer les positions fermées
        closed_positions = [r for r in records if r.get("Statut") == "CLOSED"]
        
        if not closed_positions:
            return {
                "total_trades": 0,
                "total_profit": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0
            }
        
        total_profit = sum(float(r.get("Profit/Loss (USDT)", 0)) for r in closed_positions)
        winning_trades = len([r for r in closed_positions if float(r.get("Profit/Loss (USDT)", 0)) > 0])
        losing_trades = len([r for r in closed_positions if float(r.get("Profit/Loss (USDT)", 0)) < 0])
        
        stats = {
            "total_trades": len(closed_positions),
            "total_profit": round(total_profit, 2),
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
        }
        
        if stats["total_trades"] > 0:
            stats["win_rate"] = round((stats["winning_trades"] / stats["total_trades"]) * 100, 2)
        else:
            stats["win_rate"] = 0
            
        return stats
        
    except Exception as e:
        logging.error(f"❌ Erreur statistiques: {e}")
        return {
            "total_trades": 0,
            "total_profit": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0
        }

@app.delete("/reset")
async def reset_state():
    """Endpoint pour réinitialiser l'état"""
    state = {"positions": {}, "processed_alerts": {}}
    save_state(state)
    return {"status": "reset", "message": "État réinitialisé"}

@app.get("/gsheets/status")
async def gsheets_status():
    """Statut Google Sheets"""
    return gsheets.get_sheets_info()

@app.post("/gsheets/backup")
async def manual_backup():
    """Sauvegarde manuelle de l'état"""
    state = load_state()
    success = gsheets.save_state(state)
    return {"status": "success" if success else "error", "message": "Backup manuel"}

@app.get("/balance")
async def get_balance():
    """Vérifie le solde du compte"""
    try:
        account_info = client.futures_account()
        assets = account_info.get('assets', [])
        positions = account_info.get('positions', [])
        
        # Trouver le solde USDT
        usdt_balance = next((asset for asset in assets if asset['asset'] == 'USDT'), None)
        
        return {
            "balance": usdt_balance,
            "total_wallet_balance": account_info.get('totalWalletBalance'),
            "available_balance": account_info.get('availableBalance'),
            "account_type": "TESTNET" if USE_TESTNET else "LIVE",
            "assets_count": len(assets),
            "positions_count": len([p for p in positions if float(p['positionAmt']) != 0])
        }
    except BinanceAPIException as e:
        return {"error": f"Binance API Error: {str(e)}", "code": e.code}
    except Exception as e:
        return {"error": f"General error: {str(e)}"}

@app.get("/debug/binance")
async def debug_binance():
    """Endpoint de debug pour Binance"""
    try:
        ping = client.ping()
        server_time = client.get_server_time()
        exchange_info = client.futures_exchange_info()
        
        try:
            account_info = client.futures_account()
            account_status = "OK"
            account_assets = len(account_info.get('assets', []))
        except Exception as acc_e:
            account_status = f"Error: {str(acc_e)}"
            account_assets = 0
        
        return {
            "ping": ping,
            "server_time": server_time,
            "symbols_count": len(exchange_info['symbols']),
            "api_key_set": bool(API_KEY and API_KEY != ""),
            "api_secret_set": bool(API_SECRET and API_SECRET != ""),
            "testnet_mode": USE_TESTNET,
            "account_status": account_status,
            "account_assets_count": account_assets,
            "status": "Connexion Binance OK"
        }
    except Exception as e:
        return {
            "error": str(e),
            "api_key_set": bool(API_KEY and API_KEY != ""),
            "api_secret_set": bool(API_SECRET and API_SECRET != ""),
            "testnet_mode": USE_TESTNET,
            "status": "Erreur connexion Binance"
        }

@app.get("/orders")
async def get_orders(symbol: str = "ETHUSDC"):
    """Vérifie les ordres ouverts"""
    try:
        orders = client.futures_get_open_orders(symbol=symbol)
        return {"symbol": symbol, "open_orders": orders}
    except Exception as e:
        return {"error": str(e)}

@app.get("/check/{symbol}")
async def check_position(symbol: str = "ETHUSDC"):
    """Vérification manuelle par prix (backup)"""
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        current_price = float(ticker['price'])
        
        state = load_state()
        if symbol not in state.get("positions", {}):
            return {"status": "NO_POSITION"}
        
        position = state["positions"][symbol]
        if not position.get("is_active", True):
            return {"status": "POSITION_CLOSED"}
        
        return {
            "symbol": symbol,
            "current_price": current_price,
            "position_active": True,
            "level": position.get("current_level", 1),
            "entry_price": position.get("entry_price"),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {"status": "ERROR", "message": str(e)}

@app.get("/precision/{symbol}")
async def check_precision(symbol: str):
    """Vérifie la précision pour un symbole"""
    try:
        price_precision = get_price_precision(symbol)
        quantity_precision = get_quantity_precision(symbol)
        step_size = get_step_size(symbol)
        
        return {
            "symbol": symbol,
            "price_precision": price_precision,
            "quantity_precision": quantity_precision,
            "step_size": step_size
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/levels")
async def get_levels():
    """Affiche les niveaux de la stratégie"""
    return {
        "strategy": "Renforcement progressif avec monitoring automatique",
        "levels": LEVELS,
        "total_levels": len(LEVELS),
        "total_capital": sum(level["capital"] for level in LEVELS)
    }

@app.get("/status")
async def enhanced_status():
    """Statut complet du service"""
    health_checks = await perform_health_checks()
    
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "Trading Bot V4",
        "version": "2.0.0",
        "environment": "TESTNET" if USE_TESTNET else "LIVE",
        "health_checks": health_checks,
        "keep_alive": keep_alive_manager.get_status(),
        "monitoring_thread": {
            "alive": monitor_thread.is_alive() if monitor_thread else False,
            "daemon": monitor_thread.daemon if monitor_thread else None
        },
        "system": {
            "python_version": "3.x",
            "platform": "Render",
            "port": PORT
        }
    }

@app.get("/performance")
async def get_performance():
    """Statistiques de performance détaillées"""
    try:
        history_stats = await get_history_stats()
        state = load_state()
        
        active_positions = len([p for p in state.get("positions", {}).values() if p.get("is_active", True)])
        pending_reinforcements = len([p for p in state.get("positions", {}).values() if p.get("pending_reinforcement", False)])
        
        return {
            **history_stats,
            "active_positions": active_positions,
            "pending_reinforcements": pending_reinforcements,
            "total_capital_utilized": sum(
                p.get("capital", 0) for p in state.get("positions", {}).values() 
                if p.get("is_active", True)
            )
        }
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    logging.info("🚀 Démarrage du bot avec double webhook et Google Sheets")
    logging.info("🔗 Webhook 1: Trading principal")
    logging.info("🔗 Webhook 2: Anti-sleep + deuxième indicateur")
    uvicorn.run(app, host="0.0.0.0", port=PORT)