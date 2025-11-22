import os
import time
import json
import logging
import threading
import gspread
import requests
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
                    "Niveau Renforcement Suivant", "Durée Position", "Timestamp"
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
                datetime.now().isoformat()
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
    # Configuration spécifique pour Testnet
    client = Client(
        API_KEY, 
        API_SECRET, 
        testnet=True,
        requests_params={'timeout': 10}
    )
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

def calculate_pnl(position, close_type, close_price=None):
    """Calcule le profit/perte d'une position"""
    try:
        entry_price = position.get("entry_price", 0)
        quantity = position.get("quantity", 0)
        
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
        
        # Si close_price est fourni (fermeture manuelle), l'utiliser
        if close_price is None and close_type == "MANUAL":
            close_price = position.get("close_price", entry_price)
        
        if position.get("signal").upper() == "BUY":
            pnl = (close_price - entry_price) * quantity
        else:
            pnl = (entry_price - close_price) * quantity
            
        return round(pnl, 4)
    except Exception as e:
        logging.error(f"❌ Erreur calcul PnL: {e}")
        return 0

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

def cancel_order(symbol: str, order_id: int):
    """Annule un ordre avec vérification simple"""
    try:
        client.futures_cancel_order(symbol=symbol, orderId=order_id)
        logging.info(f"✅ Ordre annulé: {order_id} sur {symbol}")
        return True
    except Exception as e:
        # Si l'ordre n'existe plus, c'est bon aussi
        if "Order does not exist" in str(e):
            logging.info(f"ℹ️ Ordre {order_id} déjà annulé")
            return True
        logging.warning(f"⚠️ Échec annulation ordre {order_id}: {e}")
        return False

def cancel_all_orders_for_symbol(symbol: str):
    """Annule TOUS les ordres pour un symbole (sécurité)"""
    try:
        orders = client.futures_get_open_orders(symbol=symbol)
        cancelled_count = 0
        for order in orders:
            try:
                client.futures_cancel_order(symbol=symbol, orderId=order['orderId'])
                logging.info(f"✅ Ordre annulé: {order['orderId']} ({order['type']})")
                cancelled_count += 1
                time.sleep(0.1)  # Petite pause pour éviter rate limit
            except Exception as e:
                logging.warning(f"⚠️ Échec annulation ordre {order['orderId']}: {e}")
        
        logging.info(f"🔧 Nettoyage {symbol}: {cancelled_count} ordres annulés")
        return cancelled_count
        
    except Exception as e:
        logging.error(f"❌ Erreur nettoyage ordres {symbol}: {e}")
        return 0

def get_order_status(symbol: str, order_id: int):
    """Récupère le statut d'un ordre"""
    try:
        order = client.futures_get_order(symbol=symbol, orderId=order_id)
        return order.get("status"), order
    except Exception as e:
        logging.debug(f"❌ Échec récupération statut ordre {order_id}: {e}")
        return None, None

def get_position_amount(symbol: str):
    """Vérification ULTRA-ROBUSTE de la position via méthodes sécurisées"""
    try:
        # Méthode PRINCIPALE: Vérifier via les ordres TP/SL ouverts (le plus fiable)
        try:
            open_orders = client.futures_get_open_orders(symbol=symbol)
            tp_sl_orders = [o for o in open_orders if o['type'] in ['STOP_MARKET', 'TAKE_PROFIT_MARKET']]
            
            if tp_sl_orders:
                logging.info(f"🔍 Position {symbol} active - {len(tp_sl_orders)} ordres TP/SL trouvés")
                return 1.0  # Position active si TP/SL existent
            else:
                logging.info(f"🔍 Position {symbol} - Aucun ordre TP/SL trouvé")
                return 0.0
                
        except Exception as e1:
            logging.warning(f"⚠️ Méthode ordres ouverts échouée: {e1}")
        
        # Méthode FALLBACK: Vérifier via le compte (si disponible)
        try:
            account_info = client.futures_account()
            positions = account_info.get('positions', [])
            position = next((p for p in positions if p['symbol'] == symbol), None)
            if position and float(position['positionAmt']) != 0:
                position_amt = abs(float(position['positionAmt']))
                logging.info(f"🔍 Position {symbol} via futures_account: {position_amt}")
                return position_amt
        except Exception as e2:
            logging.warning(f"⚠️ Méthode compte échouée: {e2}")
        
        # Si tout échoue, considérer aucune position
        return 0.0
        
    except Exception as e:
        logging.error(f"❌ Erreur critique vérification position {symbol}: {e}")
        return 0.0  # Conservative: assume no position on error

def safe_position_check(symbol: str):
    """Vérification sécurisée de la position pour le monitoring"""
    try:
        # Vérifier d'abord les ordres TP/SL
        open_orders = client.futures_get_open_orders(symbol=symbol)
        tp_sl_orders = [o for o in open_orders if o['type'] in ['STOP_MARKET', 'TAKE_PROFIT_MARKET']]
        
        # Si aucun ordre TP/SL, position considérée fermée
        if not tp_sl_orders:
            return 0.0
            
        # Vérifier aussi la position réelle si possible
        return get_position_amount(symbol)
        
    except Exception as e:
        logging.warning(f"⚠️ Safe position check failed for {symbol}: {e}")
        return 1.0  # Conservative: assume position exists

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

# ==================== MONITORING RENFORCÉ ====================
def monitor_loop():
    """Boucle de surveillance ULTRA-ROBUSTE avec détection des fermetures manuelles"""
    logging.info("🔍 Démarrage du monitoring ULTRA-ROBUSTE")
    
    while True:
        try:
            state = load_state()
            positions = state.get("positions", {})
            
            for symbol, position in list(positions.items()):
                if not position.get("is_active", True):
                    continue
                
                # Vérifier l'âge de la position (délai de grâce réduit)
                position_timestamp = position.get("timestamp", "")
                time_diff = 0
                if position_timestamp:
                    try:
                        position_time = datetime.fromisoformat(position_timestamp.replace('Z', '+00:00'))
                        time_diff = (datetime.now().replace(tzinfo=None) - position_time.replace(tzinfo=None)).total_seconds()
                        
                        if time_diff < 15:  # Délai de grâce réduit à 15 secondes
                            logging.debug(f"⏳ Position {symbol} trop récente ({time_diff:.1f}s)")
                            continue
                    except Exception as e:
                        logging.warning(f"⚠️ Erreur calcul délai position: {e}")
                        continue
                
                lock = get_symbol_lock(symbol)
                if not lock.acquire(blocking=False):
                    continue
                
                try:
                    current_level = position.get("current_level", 1)
                    tp_order_id = position.get("tp_order_id")
                    sl_order_id = position.get("sl_order_id")
                    signal = position.get("signal")
                    entry_price = position.get("entry_price")
                    
                    # ==================== VÉRIFICATION ULTRA-ROBUSTE ====================
                    
                    # 1. Vérifier les ordres TP/SL individuels
                    tp_active = False
                    sl_active = False
                    
                    if tp_order_id:
                        tp_status, _ = get_order_status(symbol, tp_order_id)
                        tp_active = tp_status not in ["FILLED", "CANCELED", "EXPIRED"]
                    
                    if sl_order_id:
                        sl_status, _ = get_order_status(symbol, sl_order_id)
                        sl_active = sl_status not in ["FILLED", "CANCELED", "EXPIRED"]
                    
                    # 2. Vérifier la position globale
                    position_active = safe_position_check(symbol)
                    
                    # 3. Vérifier les ordres ouverts globaux
                    try:
                        all_open_orders = client.futures_get_open_orders(symbol=symbol)
                        has_any_tp_sl = any(order['type'] in ['STOP_MARKET', 'TAKE_PROFIT_MARKET'] for order in all_open_orders)
                    except:
                        has_any_tp_sl = True  # Conservative en cas d'erreur
                    
                    logging.info(f"🔍 Vérification {symbol}: Position={position_active}, TP={tp_active}, SL={sl_active}, Any_TP_SL={has_any_tp_sl}")
                    
                    # ==================== DÉTECTION FERMETURE MANUELLE ====================
                    
                    # SCÉNARIO 1: Aucun ordre TP/SL actif ET aucune position détectée → FERMETURE MANUELLE
                    if not tp_active and not sl_active and not has_any_tp_sl and position_active == 0:
                        logging.info(f"🎯 DÉTECTION: Position {symbol} fermée manuellement")
                        
                        # Récupérer le prix actuel
                        ticker = client.futures_symbol_ticker(symbol=symbol)
                        current_price = float(ticker['price'])
                        
                        # Calculer PnL
                        pnl = calculate_pnl(position, "MANUAL", current_price)
                        
                        # Ajouter à l'historique
                        history_data = {
                            "symbol": symbol,
                            "direction": signal,
                            "level": current_level,
                            "entry_price": entry_price,
                            "quantity": position.get("quantity"),
                            "close_price": current_price,
                            "close_type": "MANUAL_CLOSE",
                            "profit_loss": pnl,
                            "next_reinforcement_level": 1,
                            "open_timestamp": position.get("timestamp")
                        }
                        add_to_history("POSITION_CLOSED", history_data)
                        
                        # Nettoyer l'état
                        position["is_active"] = False
                        save_state(state)
                        continue
                    
                    # SCÉNARIO 2: Position inactive mais ordres encore présents → NETTOYAGE
                    if position_active == 0 and (tp_active or sl_active or has_any_tp_sl):
                        logging.info(f"🧹 NETTOYAGE: Position {symbol} fermée mais ordres restants")
                        
                        # Annuler tous les ordres
                        cancel_all_orders_for_symbol(symbol)
                        
                        # Récupérer le prix actuel
                        ticker = client.futures_symbol_ticker(symbol=symbol)
                        current_price = float(ticker['price'])
                        
                        # Calculer PnL
                        pnl = calculate_pnl(position, "MANUAL", current_price)
                        
                        # Ajouter à l'historique
                        history_data = {
                            "symbol": symbol,
                            "direction": signal,
                            "level": current_level,
                            "entry_price": entry_price,
                            "quantity": position.get("quantity"),
                            "close_price": current_price,
                            "close_type": "AUTO_CLEANUP",
                            "profit_loss": pnl,
                            "next_reinforcement_level": 1,
                            "open_timestamp": position.get("timestamp")
                        }
                        add_to_history("POSITION_CLOSED", history_data)
                        
                        # Nettoyer l'état
                        position["is_active"] = False
                        save_state(state)
                        continue
                    
                    # ==================== VÉRIFICATION ORDRES TP/SL ====================
                             
                    if tp_active:
                        status, order_info = get_order_status(symbol, tp_order_id)
                        if status in ("FILLED", "TRIGGERED"):
                            logging.info(f"🎯 TP exécuté pour {symbol}")
                            
                            # 🔥 FORCER L'ANNULATION DU SL
                            if sl_order_id:
                                cancel_order(symbol, sl_order_id)
                                logging.info(f"✅ SL annulé: {sl_order_id}")
                            
                            # Historique
                            history_data = {
                                "symbol": symbol,
                                "direction": signal,
                                "level": current_level,
                                "entry_price": entry_price,
                                "quantity": position.get("quantity"),
                                "close_type": "TAKE_PROFIT",
                                "profit_loss": calculate_pnl(position, "TP"),
                                "next_reinforcement_level": 1,
                                "open_timestamp": position.get("timestamp")
                            }
                            add_to_history("POSITION_CLOSED", history_data)
                            
                            # Mettre à jour état
                            position["is_active"] = False
                            save_state(state)
                            continue
                    
        
                        # Vérifier SL
                    if sl_active:
                        status, order_info = get_order_status(symbol, sl_order_id)
                        if status in ("FILLED", "TRIGGERED"):
                            logging.info(f"🛑 SL exécuté pour {symbol}")
                            
                            # 🔥 FORCER L'ANNULATION DU TP
                            if tp_order_id:
                                cancel_order(symbol, tp_order_id)
                                logging.info(f"✅ TP annulé: {tp_order_id}")
                            
                            # Historique
                            history_data = {
                                "symbol": symbol,
                                "direction": signal,
                                "level": current_level,
                                "entry_price": entry_price,
                                "quantity": position.get("quantity"),
                                "close_type": "STOP_LOSS",
                                "profit_loss": calculate_pnl(position, "SL"),
                                "next_reinforcement_level": current_level + 1 if current_level < len(LEVELS) else 1,
                                "open_timestamp": position.get("timestamp")
                            }
                            add_to_history("POSITION_CLOSED", history_data)
                            
                            # Gérer renforcement
                            handle_reinforcement(symbol, signal, current_level, state, position)
                            continue
                            
                except Exception as e:
                    logging.error(f"❌ Erreur dans monitoring {symbol}: {e}")
                finally:
                    lock.release()
                    
        except Exception as e:
            logging.error(f"❌ Erreur globale dans monitor_loop: {e}")
            time.sleep(10)  # Pause plus longue en cas d'erreur
        
        time.sleep(3)  # Vérification plus fréquente

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

# Démarrer le monitoring
monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
monitor_thread.start()

# ==================== FONCTION DE TRAITEMENT DES SIGNALS ====================
async def process_trading_signal(signal, symbol, price, data, webhook_source="principal"):
    """Traite les signaux de trading avec VÉRIFICATIONS DE SÉCURITÉ RENFORCÉES"""
    if not signal or price == 0:
        raise HTTPException(status_code=400, detail="Signal ou prix manquant")
    
    # ==================== VÉRIFICATIONS DE SÉCURITÉ PRÉALABLES ====================
    
    # 1. VÉRIFICATION INITIALE: Pas de position active réelle sur Binance
    try:
        real_position_amount = get_position_amount(symbol)
        if real_position_amount > 0:
            logging.warning(f"⚠️ Position {symbol} déjà active sur Binance - Ignorer signal")
            return {
                "status": "ignored", 
                "reason": "position_already_active_on_binance", 
                "webhook": webhook_source,
                "details": f"Position détectée: {real_position_amount}"
            }
    except Exception as e:
        logging.warning(f"⚠️ Erreur vérification position {symbol}: {e}. Continuer avec prudence...")
        # Continuer mais avec prudence en cas d'erreur d'API
    
    # 2. Verrou pour ce symbole (éviter les conflits)
    lock = get_symbol_lock(symbol)
    if not lock.acquire(timeout=15):  # Timeout augmenté à 15s
        raise HTTPException(status_code=429, detail="Symbole occupé - timeout verrou")
    
    try:
        state = load_state()
        positions = state.get("positions", {})
        processed_alerts = state.get("processed_alerts", {})
        
        # ==================== NETTOYAGE PRÉALABLE DE L'ÉTAT ====================
        
        if symbol in positions:
            current_pos = positions[symbol]
            
            # Si position marquée active mais aucune position réelle sur Binance → NETTOYAGE
            if current_pos.get("is_active", True) and real_position_amount == 0:
                logging.info(f"🔧 Nettoyage position orpheline dans l'état: {symbol}")
                
                # Annuler tous les ordres potentiellement restants
                cancelled_count = cancel_all_orders_for_symbol(symbol)
                
                # Marquer comme inactive
                current_pos["is_active"] = False
                
                # Ajouter à l'historique pour tracking
                history_data = {
                    "symbol": symbol,
                    "direction": current_pos.get("signal", ""),
                    "level": current_pos.get("current_level", 1),
                    "entry_price": current_pos.get("entry_price", 0),
                    "quantity": current_pos.get("quantity", 0),
                    "close_type": "AUTO_CLEANUP_PRE_OPEN",
                    "profit_loss": 0,
                    "next_reinforcement_level": 1,
                    "open_timestamp": current_pos.get("timestamp")
                }
                add_to_history("POSITION_CLOSED", history_data)
                
                logging.info(f"🧹 Position orpheline {symbol} nettoyée - {cancelled_count} ordres annulés")
        
        # ==================== GESTION DU RENFORCEMENT ====================
        
        # VÉRIFIER SI RENFORCEMENT EN ATTENTE (quelle que soit la direction)
        if symbol in positions:
            position = positions[symbol]
            if position.get("pending_reinforcement", False):
                next_level = position.get("next_level", 1)
                
                # VÉRIFIER que le niveau suivant est valide
                if next_level > len(LEVELS):
                    logging.warning(f"💥 Niveau maximum atteint pour {symbol} - Réinitialisation")
                    position.update({
                        "is_active": False,
                        "pending_reinforcement": False,
                        "next_level": 1
                    })
                    save_state(state)
                else:
                    # 🔥 OUVRIR DANS LA DIRECTION DU NOUVEAU SIGNAL, AU NIVEAU SUIVANT
                    logging.info(f"🎯 Renforcement activé: {symbol} niveau {next_level} - Direction: {signal}")
                    
                    # Configuration du niveau
                    level_config = LEVELS[next_level - 1]
                    capital = level_config["capital"]
                    leverage = level_config["leverage"]
                    quantity = calculate_quantity(capital, leverage, price, symbol)
                    
                    if quantity <= 0:
                        raise HTTPException(status_code=400, detail="Quantité invalide pour renforcement")
                    
                    # DOUBLE VÉRIFICATION: s'assurer qu'aucune position n'est active
                    final_check = get_position_amount(symbol)
                    if final_check > 0:
                        logging.error(f"🚨 CONFLIT: Position {symbol} active pendant renforcement - Abandon")
                        return {
                            "status": "error", 
                            "reason": "position_conflict_during_reinforcement",
                            "webhook": webhook_source
                        }
                    
                    # Placer l'ordre de renforcement avec la NOUVELLE direction
                    order_result, entry_price, tp_order_id, sl_order_id = place_binance_order(
                        symbol, signal, quantity, level_config
                    )
                    
                    # VÉRIFICATION que les ordres TP/SL ont été créés
                    if not tp_order_id or not sl_order_id:
                        logging.error(f"🚨 Échec création TP/SL pour renforcement {symbol}")
                        # Annuler l'ordre principal si TP/SL échouent
                        try:
                            cancel_order(symbol, order_result['orderId'])
                        except:
                            pass
                        raise HTTPException(status_code=500, detail="Échec création ordres TP/SL")
                    
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
                        "next_reinforcement_level": next_level + 1 if next_level < len(LEVELS) else 1
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
                            "current_level": next_level,
                            "type": "reinforcement"
                        }
                    }
        
        # ==================== VÉRIFICATION DES DOUBLONS ====================
        
        alert_id = f"{symbol}_{signal}_{data.get('time', '')}"
        if alert_id in processed_alerts:
            # Nettoyer les alertes trop anciennes (> 1 heure)
            current_time = int(time.time())
            old_alerts = [aid for aid, timestamp in processed_alerts.items() if current_time - timestamp > 3600]
            for old_alert in old_alerts:
                del processed_alerts[old_alert]
            
            if alert_id in processed_alerts:  # Vérifier à nouveau après nettoyage
                return {
                    "status": "ignored", 
                    "reason": "duplicate_alert", 
                    "webhook": webhook_source,
                    "alert_id": alert_id
                }
        
        processed_alerts[alert_id] = int(time.time())
        
        # ==================== VÉRIFICATION POSITION ACTIVE DANS L'ÉTAT ====================
        
        if symbol in positions:
            position = positions[symbol]
            if position.get("is_active", True):
                # Vérification finale de la position réelle
                position_amount = get_position_amount(symbol)
                if position_amount != 0:
                    return {
                        "status": "ignored", 
                        "reason": "position_already_open_in_state", 
                        "webhook": webhook_source,
                        "details": f"Position dans l'état: {position_amount}"
                    }
                else:
                    # Nettoyer l'état si position fermée
                    logging.info(f"🧹 Nettoyage position inactive dans l'état: {symbol}")
                    position["is_active"] = False
                    # Ne pas supprimer pour garder l'historique du renforcement
        
        # ==================== OUVERTURE NOUVELLE POSITION (NIVEAU 1) ====================
        
        level_config = LEVELS[0]
        capital = level_config["capital"]
        leverage = level_config["leverage"]
        quantity = calculate_quantity(capital, leverage, price, symbol)
        
        if quantity <= 0:
            raise HTTPException(status_code=400, detail="Quantité invalide pour niveau 1")
        
        # DERNIÈRE VÉRIFICATION de sécurité
        final_position_check = get_position_amount(symbol)
        if final_position_check > 0:
            logging.error(f"🚨 CONFLIT FINAL: Position {symbol} active avant ouverture - Abandon")
            return {
                "status": "error", 
                "reason": "position_conflict_final_check",
                "webhook": webhook_source
            }
        
        # Placer l'ordre
        order_result, entry_price, tp_order_id, sl_order_id = place_binance_order(
            symbol, signal, quantity, level_config
        )
        
        # VÉRIFICATION que les ordres TP/SL ont été créés
        if not tp_order_id or not sl_order_id:
            logging.error(f"🚨 Échec création TP/SL pour position {symbol}")
            # Annuler l'ordre principal si TP/SL échouent
            try:
                cancel_order(symbol, order_result['orderId'])
            except:
                pass
            raise HTTPException(status_code=500, detail="Échec création ordres TP/SL pour nouvelle position")
        
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
            "next_reinforcement_level": 2
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
        
        logging.info(f"✅ NOUVELLE POSITION OUVERTE: {symbol} {signal} Niveau 1")
        
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
                "current_level": 1,
                "type": "new_position"
            }
        }
        
    except BinanceAPIException as e:
        logging.error(f"❌ Erreur Binance API dans process_trading_signal: {e}")
        raise HTTPException(status_code=500, detail=f"Erreur Binance: {str(e)}")
    except Exception as e:
        logging.error(f"❌ Erreur inattendue dans process_trading_signal: {e}")
        raise HTTPException(status_code=500, detail=f"Erreur traitement signal: {str(e)}")
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
    """Webhook secondaire pour ANTI-SLEEP + DEUXIÈME INDICATEUR"""
    try:
        data = await request.json()
        
        signal = data.get("signal", "").upper()
        
        # ==================== ANTI-SLEEP RAPIDE ====================
        if signal == "PING":
            logging.info("🔁 Keep-alive ping reçu sur webhook2")
            return {
                "status": "ping", 
                "timestamp": datetime.now().isoformat(),
                "message": "Bot actif via webhook2",
                "webhook": "anti-sleep"
            }
        # ==================== FIN ANTI-SLEEP ====================
        
        logging.info(f"📥 Webhook SECONDAIRE reçu: {data}")
        
        # TRAITEMENT NORMAL POUR LE DEUXIÈME INDICATEUR
        symbol = data.get("symbol", "ETHUSDC")
        price = float(data.get("price", 0))
        
        return await process_trading_signal(signal, symbol, price, data, "secondaire")
        
    except Exception as e:
        logging.error(f"❌ Erreur webhook2: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

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

@app.get("/debug/api-test")
async def debug_api_test():
    """Test complet de l'API Binance"""
    try:
        tests = {}
        
        # Test 1: Ping
        try:
            ping_result = client.ping()
            tests["ping"] = "OK"
        except Exception as e:
            tests["ping"] = f"FAILED: {e}"
        
        # Test 2: Server Time
        try:
            server_time = client.get_server_time()
            tests["server_time"] = "OK"
        except Exception as e:
            tests["server_time"] = f"FAILED: {e}"
        
        # Test 3: Exchange Info
        try:
            exchange_info = client.futures_exchange_info()
            tests["exchange_info"] = f"OK - {len(exchange_info['symbols'])} symbols"
        except Exception as e:
            tests["exchange_info"] = f"FAILED: {e}"
        
        # Test 4: Open Orders
        try:
            open_orders = client.futures_get_open_orders()
            tests["open_orders"] = f"OK - {len(open_orders)} orders"
        except Exception as e:
            tests["open_orders"] = f"FAILED: {e}"
        
        # Test 5: Position Information
        try:
            positions = client.futures_position_information()
            tests["position_info"] = f"OK - {len(positions)} positions"
        except Exception as e:
            tests["position_info"] = f"FAILED: {e}"
        
        # Test 6: Account (problématique)
        try:
            account_info = client.futures_account()
            tests["account_info"] = f"OK - {len(account_info.get('assets', []))} assets"
        except Exception as e:
            tests["account_info"] = f"FAILED: {e}"
        
        return {
            "api_status": "TEST_COMPLETED",
            "testnet_mode": USE_TESTNET,
            "tests": tests
        }
        
    except Exception as e:
        return {"error": str(e)}

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

@app.post("/cleanup/{symbol}")
async def cleanup_symbol(symbol: str):
    """Nettoyage manuel d'un symbole"""
    try:
        # Nettoyer les ordres
        cancelled = cancel_all_orders_for_symbol(symbol)
        
        # Nettoyer l'état
        state = load_state()
        if symbol in state.get("positions", {}):
            state["positions"][symbol]["is_active"] = False
            save_state(state)
        
        return {
            "status": "success", 
            "symbol": symbol, 
            "orders_cancelled": cancelled,
            "message": f"Nettoyage terminé pour {symbol}"
        }
    except Exception as e:
        logging.error(f"❌ Erreur nettoyage {symbol}: {e}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    logging.info("🚀 Démarrage du bot avec double webhook et Google Sheets")
    logging.info("🔗 Webhook 1: Trading principal")
    logging.info("🔗 Webhook 2: Anti-sleep + deuxième indicateur")
    uvicorn.run(app, host="0.0.0.0", port=PORT)