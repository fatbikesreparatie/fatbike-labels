import os, csv, json, hmac, hashlib, base64, requests
from flask import Flask, request, jsonify
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from datetime import datetime
import io

app = Flask(__name__)

# --- Config via omgevingsvariabelen ---
SHOPIFY_SHOP       = os.environ.get("SHOPIFY_SHOP", "fatbikesreparatie")
SHOPIFY_TOKEN      = os.environ.get("SHOPIFY_TOKEN", "")
SHOPIFY_SECRET     = os.environ.get("SHOPIFY_SECRET", "")
DHL_USER_ID        = os.environ.get("DHL_USER_ID", "3e375d5f-14ef-48ae-b7af-757426090ca4")
DHL_API_KEY        = os.environ.get("DHL_API_KEY", "4c2a04d3-c336-4b3e-a32e-691bdc79710c")
DHL_ACCOUNT        = os.environ.get("DHL_ACCOUNT", "06405237")
NOTIFY_EMAIL       = os.environ.get("NOTIFY_EMAIL", "info@fatbikesreparatie.nl")

DHL_API_BASE = "https://api-gw.dhlparcel.nl"

# --- Laad productafmetingen ---
PRODUCTS = {}
def load_products():
    global PRODUCTS
    path = os.path.join(os.path.dirname(__file__), "dhl_afmetingen_v2.csv")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            sku = row["SKU"].strip()
            PRODUCTS[sku] = {
                "title":  row["Title"],
                "l": float(row["Lengte_cm"]),
                "b": float(row["Breedte_cm"]),
                "h": float(row["Hoogte_cm"]),
                "kg": float(row["Gewicht_kg"]),
                "dhl_type": row["DHL_type"].strip()
            }

load_products()

# --- DHL type bepalen voor een bundel van meerdere producten ---
def bepaal_dhl_type_bundel(line_items):
    """
    Berekent het DHL pakkettype voor een order met meerdere producten.
    Strategie: neem het zwaarste/grootste type dat nodig is.
    """
    totaal_kg = 0
    totaal_vol = 0
    max_l = 0
    onbekend = []

    volgorde = ["envelop", "brievenbuspakket", "pakket-S", "pakket-M", "pakket-L", "pakket-XL", "pakket-XXL"]

    for item in line_items:
        sku = str(item.get("sku", "")).strip()
        qty = int(item.get("quantity", 1))

        if sku in PRODUCTS:
            p = PRODUCTS[sku]
            totaal_kg  += p["kg"] * qty
            totaal_vol += (p["l"] * p["b"] * p["h"] / 1000) * qty
            max_l       = max(max_l, p["l"])
        else:
            onbekend.append(item.get("title", sku))

    # Bepaal type op basis van gecombineerde maten
    if max_l <= 38 and totaal_vol <= (38*26.5*3.2/1000) and totaal_kg <= 0.5:
        dhl_type = "envelop"
    elif max_l <= 38 and totaal_kg <= 1.0 and totaal_vol <= (38*26.5*3.2/1000):
        dhl_type = "brievenbuspakket"
    elif totaal_kg <= 5 and totaal_vol <= 10:
        dhl_type = "pakket-S"
    elif totaal_kg <= 10 and totaal_vol <= 24:
        dhl_type = "pakket-M"
    elif totaal_kg <= 15 and totaal_vol <= 60:
        dhl_type = "pakket-L"
    elif totaal_kg <= 20 and totaal_vol <= 240:
        dhl_type = "pakket-XL"
    else:
        dhl_type = "pakket-XXL"

    return dhl_type, round(totaal_kg, 3), round(totaal_vol, 2), onbekend

# --- DHL authenticatie ---
def dhl_token():
    resp = requests.post(
        f"{DHL_API_BASE}/authenticate/api-key",
        json={"userId": DHL_USER_ID, "key": DHL_API_KEY},
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()["accessToken"]

# --- DHL label aanmaken ---
DHL_PRODUCT_MAP = {
    "envelop":          "ENVELOPE",
    "brievenbuspakket": "MAILBOX",
    "pakket-S":         "PARCEL",
    "pakket-M":         "PARCEL",
    "pakket-L":         "PARCEL",
    "pakket-XL":        "PARCEL",
    "pakket-XXL":       "PARCEL",
}

def maak_dhl_label(order, dhl_type, totaal_kg):
    token = dhl_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    addr = order.get("shipping_address", {})

    payload = {
        "orderReference": str(order["order_number"]),
        "receiver": {
            "name": {
                "firstName":   addr.get("first_name", ""),
                "lastName":    addr.get("last_name", ""),
                "companyName": addr.get("company", "")
            },
            "address": {
                "countryCode":   addr.get("country_code", "NL"),
                "postalCode":    addr.get("zip", ""),
                "city":          addr.get("city", ""),
                "street":        addr.get("address1", ""),
                "number":        addr.get("address2", "")
            },
            "email": order.get("email", ""),
            "phoneNumber": addr.get("phone", "")
        },
        "shipper": {
            "deliveringPartyAccount": DHL_ACCOUNT
        },
        "pieces": [{
            "parcelType":  DHL_PRODUCT_MAP.get(dhl_type, "PARCEL"),
            "quantity":    1,
            "weight":      max(totaal_kg, 0.01)
        }],
        "options": [{"key": "DOOR_DELIVERY"}],
        "returnLabel": False
    }

    resp = requests.post(f"{DHL_API_BASE}/shipments", json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    label_url = None
    tracking  = None
    for piece in data.get("pieces", []):
        tracking  = piece.get("barcode")
        label_url = piece.get("labelUrl") or piece.get("label", {}).get("url")

    return tracking, label_url

# --- Picklijst PDF genereren ---
def maak_picklijst_pdf(order, dhl_type, totaal_kg, onbekend):
    buf = io.BytesIO()
    c   = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, h-50, f"Picklijst - Order #{order['order_number']}")

    c.setFont("Helvetica", 10)
    c.drawString(40, h-70, f"Datum: {datetime.now().strftime('%d-%m-%Y %H:%M')}")
    c.drawString(40, h-85, f"DHL type: {dhl_type}  |  Gewicht: {totaal_kg} kg")

    addr = order.get("shipping_address", {})
    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, h-115, "Bezorgadres:")
    c.setFont("Helvetica", 11)
    c.drawString(40, h-130, f"{addr.get('first_name','')} {addr.get('last_name','')}")
    c.drawString(40, h-145, f"{addr.get('address1','')} {addr.get('address2','')}")
    c.drawString(40, h-160, f"{addr.get('zip','')} {addr.get('city','')}")
    c.drawString(40, h-175, addr.get("country", "Nederland"))

    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, h-205, "Producten:")
    c.line(40, h-210, w-40, h-210)

    y = h - 225
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40,  y, "Artikel")
    c.drawString(380, y, "SKU")
    c.drawString(460, y, "Aantal")
    y -= 15

    c.setFont("Helvetica", 10)
    for item in order.get("line_items", []):
        if y < 80:
            c.showPage()
            y = h - 50
        title = item.get("title","")[:55]
        c.drawString(40,  y, title)
        c.drawString(380, y, str(item.get("sku","")))
        c.drawString(460, y, str(item.get("quantity",1)))
        y -= 18

    if onbekend:
        y -= 10
        c.setFont("Helvetica-Bold", 9)
        c.setFillColorRGB(0.8, 0, 0)
        c.drawString(40, y, f"Let op: onbekende SKU's → {', '.join(onbekend[:5])}")

    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 8)
    c.drawString(40, 30, "Fatbike Reparatie — automatisch gegenereerd")
    c.save()
    buf.seek(0)
    return buf

# --- Shopify webhook verificatie ---
def verify_shopify(data, hmac_header):
    if not SHOPIFY_SECRET:
        return True
    digest = hmac.new(SHOPIFY_SECRET.encode("utf-8"), data, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed, hmac_header or "")

# --- Webhook endpoint ---
@app.route("/webhook/order-paid", methods=["POST"])
def order_paid():
    raw  = request.get_data()
    sig  = request.headers.get("X-Shopify-Hmac-SHA256", "")

    if not verify_shopify(raw, sig):
        return jsonify({"error": "Unauthorized"}), 401

    order = request.get_json(force=True)
    order_nr = order.get("order_number", "?")

    try:
        line_items = order.get("line_items", [])
        dhl_type, totaal_kg, totaal_vol, onbekend = bepaal_dhl_type_bundel(line_items)

        # DHL label aanmaken
        tracking, label_url = maak_dhl_label(order, dhl_type, totaal_kg)

        # Picklijst PDF
        pdf = maak_picklijst_pdf(order, dhl_type, totaal_kg, onbekend)

        print(f"[OK] Order #{order_nr} | {dhl_type} | {totaal_kg}kg | tracking: {tracking}")
        return jsonify({
            "order":     order_nr,
            "dhl_type":  dhl_type,
            "tracking":  tracking,
            "label_url": label_url,
            "gewicht_kg": totaal_kg
        }), 200

    except Exception as e:
        print(f"[FOUT] Order #{order_nr}: {e}")
        return jsonify({"error": str(e)}), 500

# --- Gezondheidscheck ---
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "producten_geladen": len(PRODUCTS)}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
