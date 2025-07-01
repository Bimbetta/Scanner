import os
import io
import logging
from typing import Optional, Dict, Any
import asyncio
from PIL import Image
import cv2
import numpy as np
from pyzbar import pyzbar
from pyzbar.pyzbar import ZBarSymbol
import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import requests
from io import BytesIO

# Configuration du logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class CodeDecoder:
    """Classe pour décoder différents types de codes"""
    
    @staticmethod
    def get_barcode_type_name(symbol_type) -> str:
        """Convertit le type de symbole pyzbar en nom lisible"""
        type_mapping = {
            ZBarSymbol.EAN8: "EAN-8",
            ZBarSymbol.EAN13: "EAN-13", 
            ZBarSymbol.UPCA: "UPC-A",
            ZBarSymbol.UPCE: "UPC-E",
            ZBarSymbol.CODE39: "Code 39",
            ZBarSymbol.CODE93: "Code 93",
            ZBarSymbol.CODE128: "Code 128",
            ZBarSymbol.CODABAR: "Codabar",
            ZBarSymbol.DATABAR: "DataBar",
            ZBarSymbol.DATABAR_EXP: "DataBar Expanded",
            ZBarSymbol.I25: "Interleaved 2 of 5",
            ZBarSymbol.QRCODE: "QR Code",
            ZBarSymbol.PDF417: "PDF417",
            ZBarSymbol.DATAMATRIX: "Data Matrix",
            ZBarSymbol.AZTEC: "Aztec Code"
        }
        return type_mapping.get(symbol_type, f"Type inconnu ({symbol_type})")
    
    @staticmethod
    def preprocess_image(image: np.ndarray) -> list:
        """Préprocesse l'image pour améliorer la détection"""
        processed_images = []
        
        # Image originale
        processed_images.append(image)
        
        # Conversion en niveaux de gris
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image
        processed_images.append(gray)
        
        # Amélioration du contraste
        enhanced = cv2.equalizeHist(gray)
        processed_images.append(enhanced)
        
        # Seuillage adaptatif
        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )
        processed_images.append(thresh)
        
        # Seuillage d'Otsu
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        processed_images.append(otsu)
        
        return processed_images
    
    @staticmethod
    def decode_codes(image_data: bytes) -> Dict[str, Any]:
        """Décode tous les types de codes dans une image"""
        # Conversion en array numpy
        image_array = np.frombuffer(image_data, np.uint8)
        image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        
        if image is None:
            return {"error": "Impossible de lire l'image"}
        
        results = {
            "codes_found": [],
            "image_info": {
                "dimensions": f"{image.shape[1]}x{image.shape[0]}",
                "channels": image.shape[2] if len(image.shape) == 3 else 1
            }
        }
        
        # Essai sur différentes versions préprocessées de l'image
        processed_images = CodeDecoder.preprocess_image(image)
        
        all_decoded = []
        for i, proc_img in enumerate(processed_images):
            try:
                decoded_objects = pyzbar.decode(proc_img)
                for obj in decoded_objects:
                    # Éviter les doublons
                    if not any(existing['data'] == obj.data.decode('utf-8', errors='ignore') 
                             and existing['type'] == CodeDecoder.get_barcode_type_name(obj.type) 
                             for existing in all_decoded):
                        all_decoded.append({
                            'data': obj.data.decode('utf-8', errors='ignore'),
                            'type': CodeDecoder.get_barcode_type_name(obj.type),
                            'raw_type': str(obj.type),
                            'quality': obj.quality if hasattr(obj, 'quality') else 'N/A',
                            'rect': {
                                'x': obj.rect.left,
                                'y': obj.rect.top,
                                'width': obj.rect.width,
                                'height': obj.rect.height
                            },
                            'polygon': [(point.x, point.y) for point in obj.polygon],
                            'preprocessing_step': i
                        })
            except Exception as e:
                logger.warning(f"Erreur lors du décodage étape {i}: {e}")
        
        results["codes_found"] = all_decoded
        results["total_codes"] = len(all_decoded)
        
        return results

class TelegramBot:
    """Bot Telegram principal"""
    
    def __init__(self, token: str):
        self.token = token
        self.decoder = CodeDecoder()
        
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Commande /start"""
        welcome_text = """
🔍 **Bot Décodeur de Codes Ultra-Précis**

Je peux décoder avec une précision maximale :
• QR Codes
• Codes-barres (EAN-8, EAN-13, UPC-A, UPC-E, Code 39, 93, 128, etc.)
• Codes Aztec
• Data Matrix
• PDF417
• Et bien d'autres !

📸 **Comment utiliser :**
1. Envoyez-moi une photo contenant des codes
2. Je vous donnerai toutes les informations détaillées

🎯 **Fonctionnalités avancées :**
• Détection du type exact de code-barres
• Informations sur la qualité et position
• Préprocessing intelligent pour améliorer la détection
• Support de multiples codes dans une même image

Envoyez une image pour commencer !
        """
        
        keyboard = [
            [InlineKeyboardButton("📖 Guide des codes", callback_data='guide')],
            [InlineKeyboardButton("ℹ️ À propos", callback_data='about')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            welcome_text, 
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Traite les photos envoyées"""
        try:
            # Message de traitement
            processing_msg = await update.message.reply_text("🔍 Analyse en cours...")
            
            # Récupération de la photo en haute qualité
            photo = update.message.photo[-1]  # Plus haute résolution
            file = await context.bot.get_file(photo.file_id)
            
            # Téléchargement de l'image
            image_data = await file.download_as_bytearray()
            
            # Décodage
            results = self.decoder.decode_codes(image_data)
            
            if "error" in results:
                await processing_msg.edit_text(f"❌ Erreur: {results['error']}")
                return
            
            # Formatage de la réponse
            response = self.format_results(results)
            
            # Suppression du message de traitement et envoi du résultat
            await processing_msg.delete()
            await update.message.reply_text(response, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Erreur lors du traitement de l'image: {e}")
            await update.message.reply_text(
                "❌ Erreur lors du traitement de l'image. Veuillez réessayer."
            )
    
    def format_results(self, results: Dict[str, Any]) -> str:
        """Formate les résultats pour l'affichage"""
        if results["total_codes"] == 0:
            return """
❌ **Aucun code détecté**

💡 **Conseils pour améliorer la détection :**
• Assurez-vous que l'image est nette
• Évitez les reflets et ombres
• Cadrez bien le code
• Vérifiez que le contraste est suffisant
            """
        
        response = f"✅ **{results['total_codes']} code(s) détecté(s)**\n\n"
        response += f"📐 **Info image :** {results['image_info']['dimensions']}\n\n"
        
        for i, code in enumerate(results["codes_found"], 1):
            response += f"**📊 Code #{i} - {code['type']}**\n"
            response += f"```\n{code['data']}\n```\n"
            
            # Informations détaillées
            response += f"• **Position :** ({code['rect']['x']}, {code['rect']['y']})\n"
            response += f"• **Taille :** {code['rect']['width']}×{code['rect']['height']}px\n"
            
            if code['quality'] != 'N/A':
                response += f"• **Qualité :** {code['quality']}\n"
            
            # Analyse du contenu pour certains types
            if code['type'] in ['EAN-13', 'EAN-8', 'UPC-A', 'UPC-E']:
                response += self.analyze_product_code(code['data'])
            elif code['type'] == 'QR Code':
                response += self.analyze_qr_content(code['data'])
            
            response += "\n"
        
        return response
    
    def analyze_product_code(self, data: str) -> str:
        """Analyse les codes produits"""
        analysis = ""
        if len(data) == 13:  # EAN-13
            country_code = data[:3]
            analysis += f"• **Code pays :** {country_code}\n"
            analysis += f"• **Code fabricant :** {data[3:8]}\n"
            analysis += f"• **Code produit :** {data[8:12]}\n"
            analysis += f"• **Chiffre de contrôle :** {data[12]}\n"
        elif len(data) == 8:  # EAN-8
            analysis += f"• **Code pays :** {data[:2]}\n"
            analysis += f"• **Code produit :** {data[2:7]}\n"
            analysis += f"• **Chiffre de contrôle :** {data[7]}\n"
        
        return analysis
    
    def analyze_qr_content(self, data: str) -> str:
        """Analyse le contenu des QR codes"""
        analysis = ""
        
        if data.startswith('http'):
            analysis += f"• **Type :** URL\n"
        elif data.startswith('mailto:'):
            analysis += f"• **Type :** Email\n"
        elif data.startswith('tel:'):
            analysis += f"• **Type :** Téléphone\n"
        elif data.startswith('WIFI:'):
            analysis += f"• **Type :** Configuration WiFi\n"
            # Parse WiFi data
            if 'S:' in data:
                ssid = data.split('S:')[1].split(';')[0]
                analysis += f"• **SSID :** {ssid}\n"
        elif data.startswith('BEGIN:VCARD'):
            analysis += f"• **Type :** Carte de visite (vCard)\n"
        else:
            analysis += f"• **Type :** Texte\n"
        
        analysis += f"• **Longueur :** {len(data)} caractères\n"
        
        return analysis
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gère les callbacks des boutons inline"""
        query = update.callback_query
        await query.answer()
        
        if query.data == 'guide':
            guide_text = """
📖 **Guide des Types de Codes**

**🏷️ Codes-barres 1D :**
• **EAN-13/8** : Produits commerciaux
• **UPC-A/E** : Produits américains  
• **Code 39** : Industrie, inventaire
• **Code 128** : Logistique, transport
• **Codabar** : Bibliothèques, banques de sang

**📱 Codes 2D :**
• **QR Code** : URLs, texte, WiFi, vCards
• **Data Matrix** : Marquage industriel
• **Aztec Code** : Billets, transport
• **PDF417** : Documents d'identité

**🎯 Conseils de qualité :**
• Éclairage uniforme sans reflets
• Image nette et bien cadrée
• Contraste suffisant
• Éviter les déformations
            """
            await query.edit_message_text(guide_text, parse_mode='Markdown')
            
        elif query.data == 'about':
            about_text = """
ℹ️ **À propos de ce bot**

**🔧 Technologies utilisées :**
• pyzbar : Décodage multi-format
• OpenCV : Traitement d'image avancé
• PIL : Manipulation d'images
• Telegram Bot API

**⚡ Fonctionnalités :**
• Détection ultra-précise
• Préprocessing intelligent
• Support multi-codes
• Analyse détaillée du contenu

**👨‍💻 Développé avec :**
Python 3.9+ et amour du code propre !

Version 1.0 - 2025
            """
            await query.edit_message_text(about_text, parse_mode='Markdown')
    
    def run(self):
        """Lance le bot"""
        # Création de l'application
        application = Application.builder().token(self.token).build()
        
        # Ajout des handlers
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        application.add_handler(CallbackQueryHandler(self.handle_callback))
        
        # Démarrage
        logger.info("Bot démarré !")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Configuration
    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not BOT_TOKEN:
        print("❌ Erreur: Variable d'environnement TELEGRAM_BOT_TOKEN manquante")
        exit(1)
    
    # Lancement du bot
    bot = TelegramBot(BOT_TOKEN)
    bot.run()
