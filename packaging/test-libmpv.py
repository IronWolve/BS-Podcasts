"""Silent smoke: load the LGPL libmpv on Windows, decode a local wav, no audio out."""
import os, sys
dll_dir = sys.argv[1]
os.add_dll_directory(dll_dir)
os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
import mpv
print("libmpv loaded:", mpv._mpv_client_api_version())
m = mpv.MPV(video=False, ao="null", audio_display="no", terminal=False)
print("mpv version:", m.mpv_version)
wav = sys.argv[2]
m.play(wav)
m.wait_until_playing(timeout=15)
print("duration:", m.duration, "codec:", m.audio_codec_name)
m.terminate()
print("DLL-TEST-OK")
