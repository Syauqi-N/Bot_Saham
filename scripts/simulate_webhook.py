import argparse
import json

import requests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:5000/webhook")
    parser.add_argument("--chat-id", default="628123456789@c.us")
    parser.add_argument("--text", default="$BBCA")
    parser.add_argument("--media-url", default="")
    parser.add_argument("--media-mimetype", default="image/jpeg")
    parser.add_argument("--media-filename", default="")
    parser.add_argument("--media-data-base64", default="")
    parser.add_argument("--from-me", action="store_true")
    args = parser.parse_args()

    message_payload = {
        "body": args.text,
        "chatId": args.chat_id,
        "fromMe": bool(args.from_me),
    }
    if args.media_url or args.media_data_base64:
        message_payload["hasMedia"] = True
        message_payload["media"] = {
            "url": args.media_url or None,
            "mimetype": args.media_mimetype or None,
            "filename": args.media_filename or None,
            "data": args.media_data_base64 or None,
        }

    payload = {
        "event": "message",
        "payload": message_payload,
    }
    response = requests.post(args.url, json=payload, timeout=10)
    print(response.status_code)
    print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
