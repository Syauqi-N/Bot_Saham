import argparse
import json

import requests


def send_payload(url: str, payload: dict) -> None:
    response = requests.post(url, json=payload, timeout=10)
    print(response.status_code)
    print(json.dumps(response.json(), indent=2))


def build_message_payload(
    chat_id: str,
    text: str,
    from_me: bool = False,
    media_url: str = "",
    media_mimetype: str = "image/jpeg",
    media_filename: str = "",
    media_data_base64: str = "",
) -> dict:
    message_payload = {
        "body": text,
        "chatId": chat_id,
        "fromMe": bool(from_me),
    }
    if media_url or media_data_base64:
        message_payload["hasMedia"] = True
        message_payload["media"] = {
            "url": media_url or None,
            "mimetype": media_mimetype or None,
            "filename": media_filename or None,
            "data": media_data_base64 or None,
        }
    return {
        "event": "message",
        "payload": message_payload,
    }


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
    parser.add_argument(
        "--logbook-demo",
        action="store_true",
        help="Kirim urutan simulasi: !logbook -> materi contoh -> !ok",
    )
    args = parser.parse_args()

    if args.logbook_demo:
        demo_messages = [
            "!logbook",
            "Mengembangkan modul API internal dan melakukan testing endpoint backend.",
            "!ok",
        ]
        for text in demo_messages:
            print(f"==> {text}")
            payload = build_message_payload(
                chat_id=args.chat_id,
                text=text,
                from_me=args.from_me,
            )
            send_payload(args.url, payload)
        return

    payload = build_message_payload(
        chat_id=args.chat_id,
        text=args.text,
        from_me=args.from_me,
        media_url=args.media_url,
        media_mimetype=args.media_mimetype,
        media_filename=args.media_filename,
        media_data_base64=args.media_data_base64,
    )
    send_payload(args.url, payload)


if __name__ == "__main__":
    main()
