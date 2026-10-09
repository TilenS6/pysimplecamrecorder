import requests
from pathlib import Path

class NtfyNotifier:
    def __init__(self, server_url: str, topic: str):
        self.server_url = server_url.rstrip("/")
        self.topic = topic.strip("/")

    @property
    def endpoint(self) -> str:
        return f"{self.server_url}/{self.topic}"

    def notify(self, message: str):
        requests.post(
            self.endpoint,
            data=message,
            timeout=5,
        )

    def notify_image(self, message: str, image: bytes | str | Path):
        image_data = image if isinstance(image, bytes) else Path(image).read_bytes()
        response = requests.post(
            self.endpoint,
            data=image_data,
            headers={
                "Content-Type": "image/jpeg",
                "X-Filename": "event.jpg",
                "X-Title": message,
            },
            timeout=10,
        )

        if not response.ok:
            raise RuntimeError(
                f"ntfy request failed ({response.status_code}): {response.text}"
            )


#notify("Bla")
#notify_image("Motion detected", "./event.jpg")
