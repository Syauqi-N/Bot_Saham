import argparse
import json

import requests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:5000/webhook")
    parser.add_argument("--chat-id", default="628123456789@c.us")
    parser.add_argument("--text", default="$BBCA")
    args = parser.parse_args()

    payload = {
        "event": "message",
        "payload": {
            "body": args.text,
            "chatId": args.chat_id,
            "fromMe": False,
        },
    }
    response = requests.post(args.url, json=payload, timeout=10)
    print(response.status_code)
    print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
