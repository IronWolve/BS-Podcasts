"""The playback commands the window dispatches. The window, the tray and MPRIS name them through
this one enum, so a misspelt command fails when it is named rather than when it is dispatched."""
from enum import StrEnum


class PlaybackAction(StrEnum):
    PLAY = "play"
    PAUSE = "pause"
    PLAY_PAUSE = "play_pause"
    STOP = "stop"
    NEXT = "next"
    PREVIOUS = "previous"
    SEEK = "seek"
    SEEK_CURRENT = "seek_current"
    SEEK_EPISODE = "seek_episode"
    SKIP = "skip"
    SKIP_BACK = "skip_back"
    SKIP_FORWARD = "skip_forward"
    LOAD_STREAM = "load_stream"
    SET_SPEED = "set_speed"
    SET_VOLUME = "set_volume"
    SET_TRIM_LEVEL = "set_trim_level"
    SET_SLEEP_TIMER = "set_sleep_timer"
    SET_SLEEP_AT_END = "set_sleep_at_end"
    CANCEL_SLEEP_TIMER = "cancel_sleep_timer"
    UPDATE_ARTWORK = "update_artwork"
    UPDATE_PREVIEW_ARTWORK = "update_preview_artwork"
    COMPLETE_DOWNLOAD = "complete_download"
    DOWNLOAD_FAILED = "download_failed"
