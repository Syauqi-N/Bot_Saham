"""
Diagnostic: intercept and print raw WAHA webhook payload for media messages.
Run this alongside the main bot on a different port to study WAHA media structure.
Usage: python3 diag_waha_media.py
Then temporarily change WAHA webhook URL to http://localhost:5001/webhook
Send an image in WhatsApp → see what WAHA sends.
Press Ctrl+C when done, then restore webhook URL.
"""
from flask import Flask, request, jsonify
import json, logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def hook():
    payload = request.get_json(silent=True) or {}
    data = payload.get("payload", payload)
    media = data.get("media")
    
    print("\n" + "="*70)
    print("EVENT:", payload.get("event"))
    print("chatId:", data.get("chatId"))
    print("body:", str(data.get("body") or "")[:80])
    print("hasMedia:", data.get("hasMedia"))
    
    if media:
        print("\nMEDIA DICT:")
        for k, v in media.items():
            if k == "data":
                vstr = str(v or "")
                print(f"  data: [base64, len={len(vstr)}] {vstr[:60]}...")
            else:
                print(f"  {k}: {v}")
    else:
        print("(no media dict, checking top-level fields)")
        for k in ("mimetype","mimeType","mediaType","url","link","filename","fileName","data","base64"):
            if k in data:
                v = data[k]
                print(f"  top-level {k}: {str(v)[:80]}")
    
    # Save full payload to file for analysis
    with open("/tmp/last_waha_payload.json", "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print("\nFull payload saved to /tmp/last_waha_payload.json")
    print("="*70)
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    print("Diagnostic webhook running on port 5001")
    print("Change WAHA webhook to http://localhost:5001/webhook")
    app.run(port=5001)
