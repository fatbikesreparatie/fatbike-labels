import os, csv, json, hmac, hashlib, base64, requests, uuid
from flask import Flask, request, jsonify, render_template_string
from datetime import datetime

app = Flask(__name__)

SHOPIFY_SHOP   = os.environ.get("SHOPIFY_SHOP", "fatbikesreparatie")
SHOPIFY_TOKEN  = os.environ.get("SHOPIFY_TOKEN", "")
SHOPIFY_SECRET = os.environ.get("SHOPIFY_SECRET", "")
DHL_USER_ID    = os.environ.get("DHL_USER_ID", "")
DHL_API_KEY    = os.environ.get("DHL_API_KEY", "")
DHL_ACCOUNT    = os.environ.get("DHL_ACCOUNT", "06405237")
DHL_API_BASE   = "https://api-gw.dhlparcel.nl"

# In-memory opslag voor dashboard
orders_log = []

# --- Productafmetingen laden ---
PRODUCTS = {}
def load_products():
    global PRODUCTS
    path = os.path.join(os.path.dirname(__file__), "dhl_afmetingen_v2.csv")
    if not os.path.exists(path):
        print("WAARSCHUWING: dhl_afmetingen_v2.csv niet gevonden!")
        return
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            PRODUCTS[row["SKU"].strip()] = {
                "title":    row["Title"],
                "l":        float(row["Lengte_cm"]),
                "b":        float(row["Breedte_cm"]),
                "h":        float(row["Hoogte_cm"]),
                "kg":       float(row["Gewicht_kg"]),
                "dhl_type": row["DHL_type"].strip()
            }
    print(f"Geladen: {len(PRODUCTS)} producten")

load_products()

# --- DHL type berekenen voor bundel ---
def bepaal_dhl_bundel(line_items):
    totaal_kg = 0
    totaal_vol = 0
    max_l = 0
    onbekend = []
    for item in line_items:
        sku = str(item.get("sku", "")).strip()
        qty = int(item.get("quantity", 1))
        if sku in PRODUCTS:
            p = PRODUCTS[sku]
            totaal_kg  += p["kg"] * qty
            totaal_vol += (p["l"] * p["b"] * p["h"] / 1000) * qty
            max_l       = max(max_l, p["l"])
        else:
            onbekend.append(item.get("title", sku)[:30])

    if max_l <= 38 and totaal_kg <= 0.5 and totaal_vol <= 0.32:
        dhl_type = "ENVELOPE"
    elif max_l <= 38 and totaal_kg <= 1.0 and totaal_vol <= 1.07:
        dhl_type = "MAILBOX"
    elif totaal_kg <= 5 and totaal_vol <= 10:
        dhl_type = "PARCEL"
    elif totaal_kg <= 10 and totaal_vol <= 24:
        dhl_type = "PARCEL"
    elif totaal_kg <= 20 and totaal_vol <= 240:
        dhl_type = "PARCEL"
    else:
        dhl_type = "PARCEL"

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
def maak_dhl_label(order, dhl_type, totaal_kg):
    token = dhl_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    addr = order.get("shipping_address", {})
    shipment_id = str(uuid.uuid4())

    payload = {
        "shipmentId": shipment_id,
        "orderReference": str(order["order_number"]),
        "receiver": {
            "name": {
                "firstName":   addr.get("first_name", ""),
                "lastName":    addr.get("last_name", ""),
                "companyName": addr.get("company", "") or ""
            },
            "address": {
                "countryCode": addr.get("country_code", "NL"),
                "postalCode":  addr.get("zip", "").replace(" ", ""),
                "city":        addr.get("city", ""),
                "street":      addr.get("address1", ""),
                "number":      addr.get("address2", "") or ""
            },
            "email": order.get("email", "")
        },
        "shipper": {
            "deliveringPartyAccount": DHL_ACCOUNT
        },
        "pieces": [{
            "parcelType": dhl_type,
            "quantity":   1,
            "weight":     max(round(totaal_kg, 3), 0.01)
        }],
        "options": [{"key": "DOOR_DELIVERY"}],
        "returnLabel": False
    }

    resp = requests.post(
        f"{DHL_API_BASE}/shipments",
        json=payload,
        headers=headers,
        timeout=15
    )
    
    if not resp.ok:
        print(f"DHL fout {resp.status_code}: {resp.text}")
        resp.raise_for_status()

    data = resp.json()
    tracking  = None
    label_url = None
    for piece in data.get("pieces", []):
        tracking  = piece.get("barcode") or piece.get("labelBarcode")
        label_url = piece.get("labelUrl") or (piece.get("label") or {}).get("url")

    return tracking, label_url

# --- Shopify verificatie ---
def verify_shopify(data, hmac_header):
    if not SHOPIFY_SECRET:
        return True
    digest = hmac.new(SHOPIFY_SECRET.encode(), data, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), hmac_header or "")

# --- Webhook endpoint ---
@app.route("/webhook/order-paid", methods=["POST"])
def order_paid():
    raw = request.get_data()
    if not verify_shopify(raw, request.headers.get("X-Shopify-Hmac-SHA256", "")):
        return jsonify({"error": "Unauthorized"}), 401

    order    = request.get_json(force=True)
    order_nr = order.get("order_number", "?")
    items    = order.get("line_items", [])

    try:
        dhl_type, totaal_kg, totaal_vol, onbekend = bepaal_dhl_bundel(items)
        tracking, label_url = maak_dhl_label(order, dhl_type, totaal_kg)

        entry = {
            "order_nr":   order_nr,
            "naam":       f"{order.get('shipping_address',{}).get('first_name','')} {order.get('shipping_address',{}).get('last_name','')}".strip(),
            "dhl_type":   dhl_type,
            "kg":         totaal_kg,
            "tracking":   tracking,
            "label_url":  label_url,
            "items":      [{"title": i.get("title",""), "qty": i.get("quantity",1), "sku": i.get("sku","")} for i in items],
            "onbekend":   onbekend,
            "tijd":       datetime.now().strftime("%d-%m-%Y %H:%M"),
            "status":     "label aangemaakt"
        }
        orders_log.insert(0, entry)
        print(f"[OK] Order #{order_nr} | {dhl_type} | {totaal_kg}kg | {tracking}")
        return jsonify({"ok": True, "tracking": tracking}), 200

    except Exception as e:
        entry = {
            "order_nr":  order_nr,
            "naam":      f"{order.get('shipping_address',{}).get('first_name','')} {order.get('shipping_address',{}).get('last_name','')}".strip(),
            "dhl_type":  "?",
            "kg":        0,
            "tracking":  None,
            "label_url": None,
            "items":     [{"title": i.get("title",""), "qty": i.get("quantity",1), "sku": i.get("sku","")} for i in items],
            "onbekend":  [],
            "tijd":      datetime.now().strftime("%d-%m-%Y %H:%M"),
            "status":    f"FOUT: {str(e)}"
        }
        orders_log.insert(0, entry)
        print(f"[FOUT] Order #{order_nr}: {e}")
        return jsonify({"error": str(e)}), 500

# --- Dashboard ---
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fatbike Labels Dashboard</title>
<style>
  body { font-family: Arial, sans-serif; margin: 0; background: #f4f4f4; }
  header { background: #FFCC00; padding: 16px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { margin: 0; font-size: 20px; color: #D40511; }
  .stats { display: flex; gap: 16px; padding: 16px 24px; }
  .stat { background: white; border-radius: 8px; padding: 16px 24px; flex: 1; text-align: center; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
  .stat .n { font-size: 32px; font-weight: bold; color: #D40511; }
  .stat .l { font-size: 13px; color: #666; margin-top: 4px; }
  table { width: calc(100% - 48px); margin: 0 24px 24px; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
  th { background: #D40511; color: white; padding: 10px 14px; text-align: left; font-size: 13px; }
  td { padding: 10px 14px; border-bottom: 1px solid #eee; font-size: 13px; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: bold; }
  .PARCEL { background: #dbeafe; color: #1e40af; }
  .MAILBOX { background: #dcfce7; color: #166534; }
  .ENVELOPE { background: #f3f4f6; color: #374151; }
  .ok { background: #dcfce7; color: #166534; }
  .fout { background: #fee2e2; color: #991b1b; }
  .btn { display: inline-block; background: #D40511; color: white; padding: 5px 12px; border-radius: 4px; text-decoration: none; font-size: 12px; }
  .btn:hover { background: #b00; }
  .products { font-size: 12px; color: #555; }
  .warn { color: #b45309; font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>🚚 Fatbike Labels Dashboard</h1>
  <span style="margin-left:auto;font-size:13px;">{{ now }}</span>
</header>

<div class="stats">
  <div class="stat"><div class="n">{{ orders|length }}</div><div class="l">Orders vandaag</div></div>
  <div class="stat"><div class="n">{{ orders|selectattr('status','equalto','label aangemaakt')|list|length }}</div><div class="l">Labels aangemaakt</div></div>
  <div class="stat"><div class="n">{{ orders|selectattr('status','ne','label aangemaakt')|list|length }}</div><div class="l">Fouten</div></div>
</div>

{% if orders %}
<table>
  <tr>
    <th>Order</th>
    <th>Klant</th>
    <th>Producten</th>
    <th>DHL type</th>
    <th>Gewicht</th>
    <th>Tijd</th>
    <th>Status</th>
    <th>Label</th>
  </tr>
  {% for o in orders %}
  <tr>
    <td><strong>#{{ o.order_nr }}</strong></td>
    <td>{{ o.naam }}</td>
    <td class="products">
      {% for i in o.items %}<div>{{ i.qty }}× {{ i.title[:40] }}</div>{% endfor %}
      {% if o.onbekend %}<div class="warn">⚠ Onbekend: {{ o.onbekend|join(', ') }}</div>{% endif %}
    </td>
    <td><span class="badge {{ o.dhl_type }}">{{ o.dhl_type }}</span></td>
    <td>{{ o.kg }} kg</td>
    <td>{{ o.tijd }}</td>
    <td><span class="badge {{ 'ok' if o.status == 'label aangemaakt' else 'fout' }}">{{ o.status[:30] }}</span></td>
    <td>
      {% if o.label_url %}
      <a class="btn" href="{{ o.label_url }}" target="_blank">🖨 Print</a>
      {% elif o.tracking %}
      <small>{{ o.tracking }}</small>
      {% else %}—{% endif %}
    </td>
  </tr>
  {% endfor %}
</table>
{% else %}
<div style="text-align:center;padding:60px;color:#999;">Nog geen orders ontvangen. Wacht op de eerste betaling...</div>
{% endif %}
</body>
</html>
"""

@app.route("/dashboard")
def dashboard():
    return render_template_string(
        DASHBOARD_HTML,
        orders=orders_log,
        now=datetime.now().strftime("%d-%m-%Y %H:%M")
    )

@app.route("/")
def health():
    return jsonify({"status": "ok", "producten": len(PRODUCTS)}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
