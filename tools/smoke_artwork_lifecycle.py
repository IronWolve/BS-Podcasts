"""Cache replacement, failed-file recovery and bounded decode retirement."""
import time
from threading import Event
from pathlib import Path
from unittest.mock import patch
from smoke_gui_remediation import create_application, fixture
from bs_podcasts.ui import pixmaps
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QWidget


def main():
    app = create_application(["artwork-lifecycle"])
    path = str(Path(fixture.name)/"later.png")
    assert pixmaps._source(path,32,32,sync=True) is None
    image=QImage(32,32,QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    image.save(path)
    pixmaps.invalidate_artwork(path)
    first=pixmaps._source(path,32,32,sync=True)
    assert first is not None and first.toImage().pixelColor(0,0).red()>200
    image.fill(QColor("blue"))
    image.save(path)
    pixmaps.invalidate_artwork(path)
    second=pixmaps._source(path,32,32,sync=True)
    assert second.toImage().pixelColor(0,0).blue()>200
    entered,release=Event(),Event()
    def slow(*args):
        entered.set()
        assert release.wait(2)
        return image
    canvas=QWidget()
    with patch.object(pixmaps,"_read_image",slow), patch.object(pixmaps,"MAX_PENDING_DECODES",2):
        pixmaps._schedule_decode("held",path,32,32,canvas)
        assert entered.wait(1)
        pixmaps._schedule_decode("queued",path,33,33,canvas)
        pixmaps._schedule_decode("excess",path,34,34,canvas)
        assert pixmaps.pending_decodes()==2
        start=time.monotonic()
        assert pixmaps.shutdown_decodes(0)==1
        assert time.monotonic()-start<.2
        release.set()
        assert pixmaps.shutdown_decodes(1)==0
    canvas.close()
    app.processEvents()
    print("A15: PASS missing/replaced images, bounded queue and shutdown")


if __name__=="__main__":
    main()
