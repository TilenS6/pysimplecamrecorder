"""Record configured RTSP streams and enforce an output-directory disk limit."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any

from ntfy import NtfyNotifier


LOGGER = logging.getLogger("simple_recorder")
DEFAULT_NTFY_SERVER = "https://ntfy.stermecki.biz"
DEFAULT_NTFY_TOPIC = "debug_recorder"


@dataclass(frozen=True)
class Camera:
    name: str
    rtsp_url: str


@dataclass(frozen=True)
class Config:
    output_dir: Path
    segment_duration: int
    delete_at_percent: float
    stop_deleting_at_percent: float
    cameras: tuple[Camera, ...]
    ntfy_server: str = DEFAULT_NTFY_SERVER
    ntfy_topic: str = DEFAULT_NTFY_TOPIC
    cleanup_interval: int = 30


def _number(raw: Any, name: str, minimum: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a number") from error
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def load_config(config_path: Path) -> Config:
    with config_path.open(encoding="utf-8") as config_file:
        raw = json.load(config_file)

    if not isinstance(raw, dict):
        raise ValueError("configuration must be a JSON object")

    output_value = raw.get("output_dir", "./recordings")
    output_dir = Path(output_value)
    if not output_dir.is_absolute():
        output_dir = (config_path.parent / output_dir).resolve()

    segment_duration = int(_number(raw.get("segment_duration", 60), "segment_duration", 1))
    delete_at = _number(
        raw.get("delete_old_segments_at_percent", 95),
        "delete_old_segments_at_percent",
        0,
    )
    stop_deleting_at = _number(
        raw.get("stop_deleting_at_percent", 90),
        "stop_deleting_at_percent",
        0,
    )
    if delete_at > 100 or stop_deleting_at > 100:
        raise ValueError("disk usage thresholds cannot exceed 100")
    if stop_deleting_at >= delete_at:
        raise ValueError(
            "stop_deleting_at_percent must be lower than "
            "delete_old_segments_at_percent"
        )

    raw_cameras = raw.get("cameras", {})
    if not isinstance(raw_cameras, (dict, list)):
        raise ValueError("cameras must be an object or an array")

    if isinstance(raw_cameras, dict):
        camera_entries = raw_cameras.items()
    else:
        camera_entries = []
        for index, camera_config in enumerate(raw_cameras, start=1):
            if not isinstance(camera_config, dict):
                raise ValueError(f"camera entry {index} must be an object")
            camera_entries.append((camera_config.get("name"), camera_config))

    cameras = []
    for name, camera_config in camera_entries:
        if not isinstance(camera_config, dict):
            raise ValueError(f"camera {name!r} must be an object")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("each camera must have a non-empty name")
        url = camera_config.get("rtsp_url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"camera {name!r} has no rtsp_url")
        cameras.append(Camera(str(name), url))

    return Config(
        output_dir=output_dir,
        segment_duration=segment_duration,
        delete_at_percent=delete_at,
        stop_deleting_at_percent=stop_deleting_at,
        cameras=tuple(cameras),
        ntfy_server=str(raw.get("ntfy_server_url", DEFAULT_NTFY_SERVER)),
        ntfy_topic=DEFAULT_NTFY_TOPIC,
        cleanup_interval=int(_number(raw.get("cleanup_interval", 30), "cleanup_interval", 1)),
    )


class StorageCleaner:
    def __init__(self, config: Config, stop_event: threading.Event, notify: NtfyNotifier):
        self.config = config
        self.stop_event = stop_event
        self.notify = notify

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.collect_if_needed()
            except Exception as error:
                LOGGER.exception("Storage cleanup failed")
                notify_error(self.notify, f"Storage cleanup failed: {error}")
            self.stop_event.wait(self.config.cleanup_interval)

    def collect_if_needed(self) -> int:
        used_percent = self._disk_usage_percent()
        print(f"Disk usage: {used_percent:.2f}%")
        if used_percent < self.config.delete_at_percent:
            return 0

        deleted = 0
        for path in self._oldest_files():
            if self._disk_usage_percent() <= self.config.stop_deleting_at_percent:
                break
            try:
                path.unlink()
                deleted += 1
                LOGGER.info("Deleted old recording %s", path)
            except FileNotFoundError:
                continue
            except OSError as error:
                LOGGER.warning("Could not delete recording %s: %s", path, error)
                notify_error(self.notify, f"Could not delete recording {path}: {error}")
        if deleted:
            LOGGER.info("Storage cleanup deleted %d recording(s)", deleted)
        return deleted

    def _disk_usage_percent(self) -> float:
        usage = shutil.disk_usage(self.config.output_dir)
        return (usage.total - usage.free) * 100 / usage.total

    def _oldest_files(self) -> list[Path]:
        root = self.config.output_dir.resolve()
        files = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved == root or root not in resolved.parents:
                continue
            print(resolved.stat().st_mtime - time())
            if time() - resolved.stat().st_mtime > 60*60: # only delete files older than 1 hour
                files.append(resolved)
        return sorted(files, key=lambda path: path.stat().st_mtime)


def notify_error(notifier: NtfyNotifier, message: str) -> None:
    try:
        notifier.notify(message)
    except Exception:
        LOGGER.exception("Could not publish recorder error notification")


class CameraRecorder:
    def __init__(
        self,
        camera: Camera,
        config: Config,
        stop_event: threading.Event,
        notify: NtfyNotifier,
    ):
        self.camera = camera
        self.config = config
        self.stop_event = stop_event
        self.notify = notify
        self.camera_dir = config.output_dir / camera.name

    def run(self) -> None:
        self.camera_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = str(self.camera_dir / "%Y-%m-%d_%H-%M-%S.mkv")
        while not self.stop_event.is_set():
            process: subprocess.Popen[bytes] | None = None
            try:
                process = subprocess.Popen(
                    self._build_command(output_pattern),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                while process.poll() is None and not self.stop_event.wait(1):
                    pass
                if self.stop_event.is_set() and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                    return
                exit_code = process.returncode
                message = f"Recorder for {self.camera.name} stopped (exit code {exit_code})"
                LOGGER.warning(message)
                notify_error(self.notify, message)
            except FileNotFoundError:
                message = "ffmpeg was not found; install ffmpeg and restart the recorder"
                LOGGER.error(message)
                notify_error(self.notify, message)
                return
            except Exception as error:
                LOGGER.exception("Recorder for %s failed", self.camera.name)
                notify_error(self.notify, f"Recorder for {self.camera.name} failed: {error}")
            if not self.stop_event.wait(5):
                continue

    def _build_command(self, output_pattern: str) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-i",
            self.camera.rtsp_url,
            "-map",
            "0",
            "-c",
            "copy",
            "-reset_timestamps",
            "1",
            "-avoid_negative_ts",
            "make_zero",
            "-f",
            "segment",
            "-segment_time",
            str(self.config.segment_duration),
            "-strftime",
            "1",
            output_pattern,
        ]


def run(config: Config) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    stop_event = threading.Event()
    notifier = NtfyNotifier(config.ntfy_server, config.ntfy_topic)
    notifier.notify(f"Recorder started with {len(config.cameras)} camera(s)")
    threads = [
        threading.Thread(
            target=CameraRecorder(camera, config, stop_event, notifier).run,
            name=f"recorder-{camera.name}",
            daemon=True,
        )
        for camera in config.cameras
    ]
    threads.append(
        threading.Thread(
            target=StorageCleaner(config, stop_event, notifier).run,
            name="storage-cleaner",
            daemon=True,
        )
    )

    def stop(_: int, __: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    for thread in threads:
        thread.start()
    LOGGER.info("Started %d camera recorder(s)", len(config.cameras))
    try:
        while not stop_event.wait(1):
            pass
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=15)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", type=Path, default=Path("config.json"))
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
    )
    try:
        config = load_config(args.config.resolve())
        run(config)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        LOGGER.error("Could not start recorder: %s", error)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
