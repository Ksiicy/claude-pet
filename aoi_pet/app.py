# -*- coding: utf-8 -*-
"""Claude 桌宠入口：单实例、托盘图标、主窗口。"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .claude_painter import ASSET_DIR
from . import presence
from .config import Config
from .window import MENU_QSS, PetWindow

SERVER_NAME = "ClaudeDesktopPet-single-instance"


def make_icon() -> QIcon:
    """直接使用 CrossPet 配套的 Claude 方形头像作为托盘图标。"""
    icon = QIcon()
    avatar = QPixmap(str(ASSET_DIR / "avatar.webp"))
    source = avatar if not avatar.isNull() else QPixmap(str(ASSET_DIR / "idle.webp"))
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(source.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    return icon


def main() -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Claude.DesktopPet")
        except Exception:
            pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Claude桌宠")

    # 单实例：已经在运行就叫醒那个
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    server.listen(SERVER_NAME)

    cfg = Config.load()
    presence.mark_running()
    icon = make_icon()
    app.setWindowIcon(icon)
    pet = PetWindow(cfg)
    pet.setWindowIcon(icon)
    pet.show()

    def on_conn():
        c = server.nextPendingConnection()
        if c:
            c.readyRead.connect(lambda: (c.readAll(), pet.summon()))

    server.newConnection.connect(on_conn)

    tray = QSystemTrayIcon(icon)
    tray.setToolTip("Claude · 桌面书灵")
    menu = QMenu()
    menu.setStyleSheet(MENU_QSS)
    a_show = QAction("显示 / 隐藏", menu)
    a_show.triggered.connect(lambda: pet.hide() if pet.isVisible() else pet.show())
    a_summon = QAction("召唤到屏幕中央", menu)
    a_summon.triggered.connect(pet.summon)
    a_pass = QAction("鼠标穿透", menu)
    a_pass.setCheckable(True)
    a_pass.toggled.connect(lambda on: pet.set_passthrough(on) if on != pet.is_passthrough() else None)
    pet.passthrough_changed.connect(lambda on: (a_pass.blockSignals(True), a_pass.setChecked(on),
                                                a_pass.blockSignals(False)))
    a_quit = QAction("退出", menu)

    def quit_app():
        pet.save_state()
        presence.mark_quit(by_user=True)   # 手动退出后，Claude 不会再自动把她叫出来，直到你自己再打开
        tray.hide()
        app.quit()

    a_quit.triggered.connect(quit_app)
    pet.quit_requested.connect(quit_app)
    for a in (a_show, a_summon, a_pass):
        menu.addAction(a)
    menu.addSeparator()
    menu.addAction(a_quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: (pet.show(), pet.raise_()) if r == QSystemTrayIcon.Trigger else None)
    tray.show()

    return app.exec()
