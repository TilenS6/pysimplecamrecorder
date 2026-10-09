# Simple recorder

Run the recorder from this directory:

```bash
python3 simple_recorder.py config.json
```

The `cameras` setting may be either an object keyed by camera name (the
standalone format) or an array of camera objects containing a `name` field
(the format used by the main application).

Each camera is recorded by its own `ffmpeg` process. RTSP input is always
forced to TCP, and segments are written below `output_dir/<camera name>`.
`ffmpeg` is restarted after an unexpected exit.

The storage cleaner measures the filesystem containing `output_dir`. When
usage reaches `delete_old_segments_at_percent`, it deletes the oldest regular
files (of which are older than 1h) below `output_dir` until usage reaches `stop_deleting_at_percent`.
Nothing outside `output_dir` is eligible for deletion.

Errors are published through the unchanged `ntfy.py` notifier. The default
server is `https://ntfy.stermecki.biz` and the default topic is
`debug_recorder`. The server can be changed with `ntfy_server_url`; the topic
is intentionally fixed to `debug_recorder`. `cleanup_interval` is also
configurable.

Stop the application with `Ctrl+C` or `SIGTERM`.
