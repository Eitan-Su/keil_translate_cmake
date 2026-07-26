# -*- coding: utf-8 -*-
import os
import re
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QDir,
    QObject,
    QPropertyAnimation,
    QSettings,
    QStringListModel,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QFont, QIcon
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from get_keil_mdk_info import find_keil_mdk_root
from keil_to_cmake import (
    DEFAULT_GENERATOR_BY_HOST,
    GenerationOptions,
    KeilProjectToCMake,
    SUPPORTED_COMPILERS,
    SUPPORTED_DEBUG_BACKENDS,
    SUPPORTED_DEBUG_PROBES,
    SUPPORTED_HOST_SYSTEMS,
    get_supported_generators,
)


def _config_file_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.ini"
    return Path(__file__).resolve().parent / "config.ini"


def _resource_path(relative_path: str) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_path / relative_path


CHECKBOX_CHECK_ICON = _resource_path("assets/checkbox-check.svg").as_posix()


APP_STYLE = """
QWidget {
    color: #17243a;
    font-family: "Segoe UI Variable", "Microsoft YaHei UI";
    font-size: 12px;
}
QMainWindow { background: #edf2f7; }
QWidget#pageSurface { background: #edf2f7; }
QLabel, QCheckBox, QSvgWidget { background: transparent; border: none; }
QFrame#navRail { background: #ffffff; border-right: 1px solid #b9c6d3; }
QLabel#railMark {
    color: #ffffff; background: #126fe8; border-radius: 12px;
    font-family: "Bahnschrift"; font-size: 21px; font-weight: 800;
}
QLabel#railCaption { color: #60748a; font-size: 9px; font-weight: 700; min-height: 14px; }
QPushButton#navButton {
    min-height: 46px; max-height: 46px;
    color: #50657b; background: transparent; border: none; border-radius: 9px;
    padding: 0 12px; text-align: left; font-size: 11px; font-weight: 700;
}
QPushButton#navButton:hover { color: #126fe8; background: #edf5ff; }
QPushButton#navButtonActive {
    min-height: 46px; max-height: 46px;
    color: #ffffff; background: #126fe8; border: none; border-radius: 9px;
    padding: 0 12px; text-align: left; font-size: 11px; font-weight: 800;
}
QFrame#topBar { background: #ffffff; border-bottom: 1px solid #b8c5d2; }
QFrame#statusBar { background: #ffffff; border-top: 1px solid #b8c5d2; }
QLabel#brand { color: #10233c; font-family: "Bahnschrift"; font-size: 22px; font-weight: 800; }
QLabel#eyebrow { color: #1688d0; font-family: "Bahnschrift"; font-size: 10px; font-weight: 800; }
QLabel#muted { color: #52677d; min-height: 18px; }
QLabel#section { color: #142842; font-family: "Bahnschrift"; font-size: 15px; font-weight: 700; min-height: 22px; }
QLabel#fieldLabel { color: #4f6378; font-size: 10px; font-weight: 700; min-height: 16px; }
QLabel#statusPill {
    color: #08785f; background: #e5f7f1; border: 1px solid #b9e6d9;
    border-radius: 10px; padding: 3px 10px; font-size: 10px; font-weight: 800;
}
QLabel#statusPill[state="running"] {
    color: #0b6c91; background: #e4f5fb; border-color: #b6deec;
}
QLabel#statusPill[state="success"] {
    color: #08785f; background: #e5f7f1; border-color: #b9e6d9;
}
QLabel#statusPill[state="failed"] {
    color: #9b3b3b; background: #fff0ef; border-color: #efc7c3;
}
QFrame#pipeline {
    background: #f8fbfe; border: 1px solid #aebfd0; border-radius: 10px;
}
QFrame#stage { background: transparent; border: none; }
QLabel#stageIndex {
    color: #ffffff; background: #1688d0; border-radius: 10px;
    min-width: 20px; max-width: 20px; min-height: 20px; max-height: 20px;
    font-family: "Bahnschrift"; font-size: 10px; font-weight: 800;
}
QLabel#stageIndex[state="pending"] { color: #668096; background: #dbe7f0; }
QLabel#stageIndex[state="running"] { color: #ffffff; background: #18a8c9; }
QLabel#stageIndex[state="complete"] { color: #ffffff; background: #1688d0; }
QLabel#stageIndex[state="failed"] { color: #ffffff; background: #c94b4b; }
QLabel#stageName { color: #203954; font-size: 11px; font-weight: 700; }
QLabel#stageName[state="pending"] { color: #71879a; }
QLabel#stageName[state="running"] { color: #087da8; }
QLabel#stageName[state="complete"] { color: #203954; }
QLabel#stageName[state="failed"] { color: #a63f3f; }
QLabel#stageName { min-height: 18px; }
QLabel#stageDetail { color: #5d7389; font-size: 9px; min-height: 14px; }
QFrame#inspector { background: #ffffff; border-left: 1px solid #aebdcb; }
QScrollArea#inspectorScroll { background: #ffffff; border: none; }
QFrame#metric { background: #f8fafc; border: 1px solid #aebdcb; border-radius: 8px; min-height: 52px; }
QLabel#metricValue { color: #142b48; font-family: "Bahnschrift"; font-size: 14px; font-weight: 800; min-height: 20px; }
QLineEdit, QComboBox {
    min-height: 35px; color: #1d3048; background: #ffffff;
    border: 1px solid #8296aa; border-radius: 6px; padding: 0 10px;
    selection-background-color: #b9def5;
}
QLineEdit:hover, QComboBox:hover { border-color: #587a99; background: #fbfdff; }
QLineEdit:focus, QComboBox:focus { border: 2px solid #0878c9; padding: 0 9px; }
QComboBox::drop-down { border: none; width: 26px; }
QAbstractItemView {
    color: #1d3048; background: #ffffff; border: 1px solid #8296aa;
    selection-color: #075f9d; selection-background-color: #dceefa;
    outline: none; padding: 4px;
}
QAbstractItemView::item { min-height: 28px; padding: 4px 8px; }
QAbstractItemView::item:hover { color: #075f9d; background: #dceefa; }
QAbstractItemView::item:selected { color: #ffffff; background: #1387d4; }
QPushButton {
    min-height: 35px; color: #294158; background: #ffffff;
    border: 1px solid #8fa2b5; border-radius: 6px; padding: 0 14px; font-weight: 650;
}
QPushButton:hover { color: #075f9d; background: #eaf5fc; border-color: #4f87ad; }
QPushButton:pressed { background: #e6f1f8; }
QPushButton#primary {
    min-height: 40px; color: #ffffff; background: #1387d4;
    border: 1px solid #0f79bf; border-radius: 7px; padding: 0 22px;
    font-family: "Bahnschrift"; font-size: 13px; font-weight: 800;
}
QPushButton#primary:hover { background: #0879c3; }
QPushButton#primary:disabled { color: #dceaf5; background: #8bb8d6; border-color: #8bb8d6; }
QCheckBox { spacing: 8px; color: #425a72; }
QCheckBox::indicator {
    width: 16px; height: 16px; background: #ffffff;
    border: 1px solid #7f93a7; border-radius: 3px;
}
QCheckBox::indicator:checked {
    background: #1387d4; border-color: #1387d4;
    image: url("__CHECKBOX_CHECK_ICON__");
}
QCheckBox::indicator:checked:disabled {
    background: #8bb8d6; border-color: #8bb8d6;
}
QFrame#console { background: #ffffff; border-top: 1px solid #aebdcb; }
QLabel#consoleTitle { color: #22364d; font-family: "Bahnschrift"; font-size: 12px; font-weight: 700; min-height: 20px; }
QPushButton#consoleButton { color: #63758a; background: transparent; border: none; min-height: 28px; padding: 0 8px; }
QPushButton#consoleButton:hover { color: #126fe8; background: #edf5ff; }
QTextBrowser {
    color: #293f56; background: #f7fafc; border: 1px solid #aebdcb; border-radius: 4px;
    padding: 8px 16px; font-family: "Cascadia Mono", "Consolas"; font-size: 11px;
}
QProgressBar#mainProgress {
    min-height: 18px; max-height: 18px; color: #27445d; background: #dfe9f2;
    border: none; text-align: center; font-family: "Bahnschrift"; font-size: 10px;
}
QProgressBar#mainProgress::chunk { background: #19a6d8; }
QProgressBar#memoryBar {
    min-height: 7px; max-height: 7px; background: #e4edf4; border: none; border-radius: 3px;
}
QProgressBar#memoryBar::chunk { background: #18a8c9; border-radius: 3px; }
QScrollArea { border: none; background: #edf2f7; }
QScrollBar:vertical {
    width: 10px; margin: 2px; background: transparent; border: none;
}
QScrollBar::handle:vertical {
    min-height: 30px; background: #a8bac9; border-radius: 4px;
}
QScrollBar::handle:vertical:hover { background: #1688d0; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0; background: transparent; border: none;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal {
    height: 10px; margin: 2px; background: transparent; border: none;
}
QScrollBar::handle:horizontal {
    min-width: 30px; background: #a8bac9; border-radius: 4px;
}
QScrollBar::handle:horizontal:hover { background: #1688d0; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0; background: transparent; border: none;
}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
}
QStackedWidget { background: #edf2f7; }
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal {
    width: 6px; border-left: 1px solid #c5d2dd;
}
QSplitter::handle:horizontal:hover {
    border-left: 1px solid #1688d0;
}
QSplitter::handle:vertical {
    height: 6px; border-top: 1px solid #c5d2dd;
}
QSplitter::handle:vertical:hover {
    border-top: 1px solid #1688d0;
}

/* Warm, editorial surfaces keep the workspace quiet while the action stays visible. */
QMainWindow { background: #f7f5f0; }
QWidget#pageSurface { background: #f7f5f0; }
QFrame#navRail {
    background: #fffdfa; border-right: 1px solid #e8e1da;
}
QFrame#topBar {
    background: #fffdfa; border-bottom: 1px solid #e8e1da;
}
QFrame#statusBar {
    background: #fffdfa; border-top: 1px solid #e8e1da;
}
QLabel#brand {
    color: #211f1d; font-family: "Aptos Display", "Bahnschrift";
    font-size: 18px; font-weight: 800; letter-spacing: 1px;
}
QLabel#topEyebrow {
    color: #a58f86; font-family: "Aptos", "Segoe UI Variable";
    font-size: 9px; font-weight: 700; letter-spacing: 2px;
}
QLabel#railCaption { color: #a58f86; }
QLabel#muted { color: #847b75; }
QLabel#eyebrow {
    color: #bd4f6a; font-family: "Aptos", "Segoe UI Variable";
    font-size: 10px; font-weight: 800; letter-spacing: 1.6px;
}
QLabel#section {
    color: #211f1d; font-family: "Aptos Display", "Bahnschrift";
    font-size: 16px; font-weight: 750;
}
QLabel#fieldLabel {
    color: #9a8e86; font-family: "Aptos", "Segoe UI Variable";
    font-size: 9px; font-weight: 800; letter-spacing: 1.1px;
}
QPushButton#navButton,
QPushButton#navButtonActive {
    min-height: 44px; max-height: 44px; border-radius: 12px;
    padding: 0 13px; font-family: "Aptos", "Segoe UI Variable";
    font-size: 11px; font-weight: 750;
}
QPushButton#navButton {
    color: #746b66; background: transparent; border: 1px solid transparent;
}
QPushButton#navButton:hover {
    color: #bd4f6a; background: #fbefef; border-color: #f3dfe0;
}
QPushButton#navButtonActive {
    color: #ffffff; background: #d75874; border: 1px solid #d75874;
}
QPushButton#navButtonActive:hover { background: #c94c68; border-color: #c94c68; }
QLabel#statusPill {
    color: #3e7b68; background: #e9f4ee; border: 1px solid #cfe7da;
    border-radius: 14px; padding: 3px 12px; font-size: 10px; font-weight: 800;
}
QLabel#statusPill[state="running"] {
    color: #a66243; background: #fff0e7; border-color: #f4d7c6;
}
QLabel#statusPill[state="success"] {
    color: #3e7b68; background: #e9f4ee; border-color: #cfe7da;
}
QLabel#statusPill[state="failed"] {
    color: #a34d56; background: #fcebed; border-color: #f0cfd3;
}
QFrame#pipeline {
    background: #fffdfa; border: 1px solid #e9e1da; border-radius: 16px;
}
QLabel#stageIndex {
    color: #ffffff; background: #d75874; border-radius: 11px;
    min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;
    font-family: "Aptos", "Segoe UI Variable"; font-size: 10px; font-weight: 800;
}
QLabel#stageIndex[state="pending"] { color: #a18f87; background: #eee8e2; }
QLabel#stageIndex[state="running"] { color: #ffffff; background: #df8a67; }
QLabel#stageIndex[state="complete"] { color: #ffffff; background: #d75874; }
QLabel#stageIndex[state="failed"] { color: #ffffff; background: #b95757; }
QLabel#stageName { color: #312c29; font-size: 11px; font-weight: 750; }
QLabel#stageName[state="pending"] { color: #a18f87; }
QLabel#stageName[state="running"] { color: #b9684e; }
QLabel#stageName[state="complete"] { color: #312c29; }
QLabel#stageName[state="failed"] { color: #a34d56; }
QLabel#stageDetail { color: #958a82; font-size: 9px; }
QFrame#inspector {
    background: #fffdfa; border-left: 1px solid #e8e1da;
}
QScrollArea#inspectorScroll { background: #fffdfa; }
QFrame#metric {
    background: #fcfaf7; border: 1px solid #eee6de; border-radius: 12px;
    min-height: 52px;
}
QLabel#metricValue {
    color: #211f1d; font-family: "Aptos Display", "Bahnschrift";
    font-size: 14px; font-weight: 800;
}
QLineEdit, QComboBox {
    min-height: 38px; color: #302b28; background: #fffefa;
    border: 1px solid #ded5cd; border-radius: 10px; padding: 0 12px;
    selection-background-color: #f1cbd2;
}
QLineEdit:hover, QComboBox:hover { border-color: #cdbdb3; background: #ffffff; }
QLineEdit:focus, QComboBox:focus {
    border: 2px solid #d75874; padding: 0 11px; background: #ffffff;
}
QComboBox::drop-down { border: none; width: 28px; }
QAbstractItemView {
    color: #302b28; background: #fffefa; border: 1px solid #ded5cd;
    selection-color: #ffffff; selection-background-color: #d75874;
    outline: none; padding: 5px; border-radius: 8px;
}
QAbstractItemView::item { min-height: 30px; padding: 5px 9px; }
QAbstractItemView::item:hover { color: #9d4058; background: #fbedef; }
QAbstractItemView::item:selected { color: #ffffff; background: #d75874; }
QPushButton {
    min-height: 38px; color: #514742; background: #fffefa;
    border: 1px solid #ded5cd; border-radius: 10px; padding: 0 15px;
    font-family: "Aptos", "Segoe UI Variable"; font-weight: 700;
}
QPushButton:hover { color: #a83f5b; background: #fbedef; border-color: #e2b7c0; }
QPushButton:pressed { background: #f6e2e5; }
QPushButton#primary {
    min-height: 42px; color: #ffffff;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #df6a80, stop:1 #c94e69);
    border: 1px solid #c94e69; border-radius: 12px; padding: 0 22px;
    font-family: "Aptos Display", "Bahnschrift"; font-size: 13px; font-weight: 800;
}
QPushButton#primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #d95c76, stop:1 #ba3f5c);
    border-color: #ba3f5c;
}
QPushButton#primary:disabled { color: #f8e8eb; background: #dba3b0; border-color: #dba3b0; }
QCheckBox { spacing: 8px; color: #665b56; font-weight: 650; }
QCheckBox::indicator {
    width: 17px; height: 17px; background: #fffefa;
    border: 1px solid #d4c8c0; border-radius: 5px;
}
QCheckBox::indicator:checked { background: #d75874; border-color: #d75874; }
QCheckBox::indicator:checked:disabled { background: #dba3b0; border-color: #dba3b0; }
QFrame#console { background: #fffdfa; border-top: 1px solid #e8e1da; }
QLabel#consoleTitle {
    color: #312c29; font-family: "Aptos Display", "Bahnschrift";
    font-size: 13px; font-weight: 750;
}
QPushButton#consoleButton {
    color: #958a82; background: transparent; border: none; min-height: 30px;
    padding: 0 9px; border-radius: 8px;
}
QPushButton#consoleButton:hover { color: #a83f5b; background: #fbedef; }
QTextBrowser {
    color: #3b3531; background: #fbfaf7; border: 1px solid #e8e0d8;
    border-radius: 10px; padding: 9px 16px;
    font-family: "Cascadia Mono", "Consolas"; font-size: 11px;
}
QProgressBar#mainProgress {
    min-height: 12px; max-height: 12px; color: #8d716a; background: #f1e5e2;
    border: none; text-align: center; font-size: 9px; font-weight: 700;
}
QProgressBar#mainProgress::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #df8a67, stop:1 #d75874);
}
QProgressBar#memoryBar {
    min-height: 6px; max-height: 6px; background: #f0e8e3;
    border: none; border-radius: 3px;
}
QProgressBar#memoryBar::chunk { background: #d88a72; border-radius: 3px; }
QScrollArea, QStackedWidget { border: none; background: #f7f5f0; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: #d3c5bd; border-radius: 5px;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: #d75874; }
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 6px; border-left: 1px solid #eee6de; }
QSplitter::handle:horizontal:hover { border-left: 1px solid #d75874; }
QSplitter::handle:vertical { height: 6px; border-top: 1px solid #eee6de; }
QSplitter::handle:vertical:hover { border-top: 1px solid #d75874; }
QToolTip {
    color: #fffefa; background: #312c29; border: 1px solid #312c29;
    padding: 5px 8px; border-radius: 6px;
}
QWidget#topBrand, QSvgWidget#railMark { background: transparent; }
QPushButton#secondaryButton {
    min-height: 34px; color: #746b66; background: #fffefa;
    border-color: #e2d8d0; border-radius: 10px; padding: 0 13px;
}
QPushButton#secondaryButton:hover {
    color: #a83f5b; background: #fbedef; border-color: #e2b7c0;
}
QPushButton#fieldButton {
    min-height: 38px; min-width: 72px; color: #746b66;
    background: #fffefa; border-color: #ded5cd; border-radius: 10px;
}
QPushButton#fieldButton:hover {
    color: #a83f5b; background: #fbedef; border-color: #e2b7c0;
}
/* Use installed system faces and point sizes so glyphs stay stable under DPI scaling. */
QWidget {
    color: #211f1d;
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 10pt;
}
QLabel#brand {
    font-family: "Segoe UI Semibold", "Segoe UI", "Microsoft YaHei UI";
    font-size: 16pt; font-weight: 700; letter-spacing: 0px;
}
QLabel#topEyebrow {
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 8pt; font-weight: 600; letter-spacing: 1px;
}
QLabel#railCaption {
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 8pt; font-weight: 600;
}
QLabel#eyebrow {
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 9pt; font-weight: 700; letter-spacing: 1px;
}
QLabel#section {
    font-family: "Segoe UI Semibold", "Segoe UI", "Microsoft YaHei UI";
    font-size: 14pt; font-weight: 600; letter-spacing: 0px;
}
QLabel#fieldLabel {
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 9pt; font-weight: 600; letter-spacing: 0px;
}
QLabel#muted { font-size: 10pt; }
QPushButton#navButton,
QPushButton#navButtonActive {
    font-family: "Segoe UI Semibold", "Segoe UI", "Microsoft YaHei UI";
    font-size: 10pt; font-weight: 600;
}
QLabel#statusPill { font-size: 9pt; font-weight: 700; }
QLabel#stageIndex {
    font-family: "Segoe UI Semibold", "Segoe UI", "Microsoft YaHei UI";
    font-size: 9pt; font-weight: 700;
}
QLabel#stageName { font-size: 10pt; font-weight: 600; }
QLabel#stageDetail { font-size: 9pt; }
QLabel#metricValue {
    font-family: "Segoe UI Semibold", "Segoe UI", "Microsoft YaHei UI";
    font-size: 11pt; font-weight: 700;
}
QLineEdit, QComboBox { font-size: 10pt; }
QPushButton { font-size: 10pt; font-weight: 600; }
QPushButton#primary {
    font-family: "Segoe UI Semibold", "Segoe UI", "Microsoft YaHei UI";
    font-size: 11pt; font-weight: 700;
}
QCheckBox { font-size: 10pt; font-weight: 600; }
QLabel#consoleTitle {
    font-family: "Segoe UI Semibold", "Segoe UI", "Microsoft YaHei UI";
    font-size: 10pt; font-weight: 600;
}
QTextBrowser {
    font-family: "Cascadia Mono", "Consolas", "Courier New";
    font-size: 9pt;
}
QProgressBar#mainProgress { font-size: 9pt; }
""".replace("__CHECKBOX_CHECK_ICON__", CHECKBOX_CHECK_ICON)


class PathHistoryEdit(QLineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._history = []
        self._history_model = QStringListModel(self)
        self._history_completer = QCompleter(self._history_model, self)
        self._history_completer.setCaseSensitivity(
            Qt.CaseSensitivity.CaseInsensitive
        )
        self._history_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._history_completer.setCompletionMode(
            QCompleter.CompletionMode.PopupCompletion
        )
        self._history_completer.popup().setMouseTracking(True)
        self.setCompleter(self._history_completer)

    def set_history(self, values):
        self._history = list(values)
        self._history_model.setStringList(self._history)

    def history(self):
        return list(self._history)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if not self._history:
            return
        self._history_completer.setCompletionPrefix("")
        self._history_completer.complete()


class ConversionWorker(QObject):
    log = Signal(str)
    progress = Signal(int, str)
    memory_usage = Signal(int, int, int, int)
    finished = Signal(bool, str, str, float)

    def __init__(
        self,
        project_file: str,
        output_dir: str,
        uv4_path: str,
        target_name: str,
        options: GenerationOptions,
        validate_build: bool,
    ):
        super().__init__()
        self.project_file = project_file
        self.output_dir = output_dir
        self.uv4_path = uv4_path
        self.target_name = target_name
        self.options = options
        self.validate_build = validate_build

    @staticmethod
    def _map_progress(value, start, end):
        value = max(0, min(100, int(value)))
        return start + round((end - start) * value / 100)

    def _generation_progress(self, value, message):
        end = 45 if self.validate_build else 99
        self.progress.emit(self._map_progress(value, 20, end), message)

    def _validation_progress(self, value, message):
        self.progress.emit(self._map_progress(value, 45, 99), message)

    @staticmethod
    def _memory_bytes(value, unit):
        factors = {"B": 1, "KB": 1024, "MB": 1024 * 1024}
        return int(float(value) * factors.get(unit.upper(), 1))

    @classmethod
    def _parse_memory_regions(cls, output, parser=None):
        values = []
        pattern = re.compile(
            r"^\s*([A-Za-z_][A-Za-z0-9_.-]*):\s+([0-9.]+)\s+(B|KB|MB)\s+"
            r"([0-9.]+)\s+(B|KB|MB)",
            re.MULTILINE | re.IGNORECASE,
        )
        for match in pattern.finditer(output or ""):
            values.append(
                (
                    match.group(1),
                    cls._memory_bytes(match.group(2), match.group(3)),
                    cls._memory_bytes(match.group(4), match.group(5)),
                )
            )
        if values:
            return values

        armclang_size = re.search(
            r"Program\s+Size:\s*Code\s*=\s*(\d+)\s+"
            r"RO-data\s*=\s*(\d+)\s+RW-data\s*=\s*(\d+)\s+"
            r"ZI-data\s*=\s*(\d+)",
            output or "",
            re.IGNORECASE,
        )
        if armclang_size:
            code, ro_data, rw_data, zi_data = map(
                int, armclang_size.groups()
            )
            flash_total = int(getattr(parser, "flash_length", 0) or 0)
            ram_total = int(getattr(parser, "ram_length", 0) or 0)
            values.extend(
                [
                    ("FLASH", code + ro_data + rw_data, flash_total),
                    ("RAM", rw_data + zi_data, ram_total),
                ]
            )
        return values

    @classmethod
    def _parse_memory_usage(cls, output, parser):
        values = {
            name.upper(): (used, total)
            for name, used, total in cls._parse_memory_regions(output, parser)
        }

        flash_used, flash_total = values.get(
            "FLASH", (0, int(parser.flash_length or 0))
        )
        ram_used, ram_total = values.get("RAM", (0, int(parser.ram_length or 0)))
        return flash_used, flash_total, ram_used, ram_total

    @staticmethod
    def _format_size(value):
        value = int(value)
        for unit in ("B", "KB", "MB"):
            if value < 1024 or unit == "MB":
                if unit == "B":
                    return f"{value} {unit}"
                return f"{value:.2f} {unit}"
            value /= 1024
        return f"{value:.2f} MB"

    def _build_success_message(self, parser, memory_regions):
        build_dir = Path(self.output_dir) / str(self.options.build_dir)
        artifact_name = parser._artifact_name()
        artifact_suffixes = (
            (".elf", ".hex", ".bin")
            if self.options.compiler == "gcc"
            else (".axf",)
        )
        firmware_files = [
            build_dir / f"{artifact_name}{suffix}"
            for suffix in artifact_suffixes
        ]
        firmware_lines = [
            f"{path.name}: {self._format_size(path.stat().st_size)}"
            for path in firmware_files
            if path.is_file()
        ]

        memory_lines = []
        for name, used, total in memory_regions:
            percent = used * 100 / total if total else 0
            memory_lines.append(
                f"{name}: {self._format_size(used)} / "
                f"{self._format_size(total)} ({percent:.1f}%)"
            )

        probes = [
            SUPPORTED_DEBUG_PROBES.get(probe, probe)
            for probe in parser._selected_probes(self.options)
        ]
        backends = [
            SUPPORTED_DEBUG_BACKENDS.get(backend, backend)
            for backend in parser._selected_debug_backends(self.options)
        ]
        sections = ["构建验证通过。"]
        if firmware_lines:
            sections.append("固件文件\n" + "\n".join(firmware_lines))
        if memory_lines:
            sections.append("内存占用\n" + "\n".join(memory_lines))
        sections.append(
            "调试配置\n"
            f"探针: {', '.join(probes) if probes else '未启用'}\n"
            f"后端: {', '.join(backends) if backends else '未启用'}\n"
            f"VS Code 配置: {'生成' if self.options.export_vsc_settings else '未生成'}\n"
            f"CMake 生成器: {self.options.generator}"
        )
        sections.append(f"输出目录\n{Path(self.output_dir).resolve()}")
        return "\n\n".join(sections)

    @Slot()
    def run(self):
        validation_started = None
        try:
            success_message = "CMake 工程生成完成。"
            self.progress.emit(2, "初始化转换任务")
            self.log.emit(
                f"构建配置: 编译器={self.options.compiler} | "
                f"生成器={self.options.generator} | 构建目录={self.options.build_dir}"
            )
            self.progress.emit(5, "解析 Keil 工程")
            self.log.emit("解析 Keil 工程...")
            parser = KeilProjectToCMake(
                self.project_file,
                self.uv4_path or None,
                target_name=self.target_name or None,
            )
            if not parser.parse():
                self.finished.emit(False, parser.last_error or "解析失败", "", -1.0)
                return

            self.progress.emit(20, "工程解析完成")
            self.log.emit(
                f"目标: {parser.target_name} | 设备: {parser.device_name} | 源文件: {len(parser.source_files)}"
            )
            self.log.emit("生成 CMake、工具链和 VS Code 配置...")
            if not parser.generate_cmake(
                self.output_dir,
                self.options.export_vsc_settings,
                self.options,
                progress_callback=self._generation_progress,
            ):
                self.finished.emit(False, parser.last_error or "生成失败", "", -1.0)
                return

            for warning in parser.warnings:
                self.log.emit(f"警告: {warning}")

            if self.validate_build:
                validation_started = time.monotonic()
                self.log.emit("执行 CMake 配置与编译验证...")
                report = parser.validate_generated_build(
                    self.output_dir,
                    self.options,
                    timeout_seconds=600,
                    progress_callback=self._validation_progress,
                )
                if report.status != "success":
                    categories = ", ".join(
                        diagnostic.category for diagnostic in report.diagnostics
                    )
                    result = report.build or report.configure
                    raw_detail = "\n".join(
                        part
                        for part in (result.error, result.stderr, result.stdout)
                        if part
                    )
                    detail_lines = [
                        line.strip() for line in raw_detail.splitlines() if line.strip()
                    ]
                    important_lines = [
                        line
                        for line in detail_lines
                        if any(
                            marker in line.lower()
                            for marker in (
                                "error",
                                "fatal",
                                "undefined reference",
                                "not set",
                                "not found",
                                "failed",
                            )
                        )
                    ]
                    summary_lines = (important_lines or detail_lines)[-8:]
                    summary = "\n".join(summary_lines)
                    if summary:
                        self.log.emit("构建错误摘要:\n" + summary)
                    message = f"构建验证失败: {categories}"
                    if summary:
                        message += "\n\n" + summary
                    validation_seconds = time.monotonic() - validation_started
                    self.finished.emit(
                        False, message, self.output_dir, validation_seconds
                    )
                    return
                build_output = "\n".join(
                    part
                    for part in (report.build.stdout, report.build.stderr)
                    if part
                )
                memory_regions = self._parse_memory_regions(build_output, parser)
                self.memory_usage.emit(*self._parse_memory_usage(build_output, parser))
                success_message = self._build_success_message(
                    parser, memory_regions
                )
                artifact_labels = (
                    "ELF、HEX、BIN" if self.options.compiler == "gcc" else "AXF"
                )
                self.log.emit(
                    f"构建验证通过，{artifact_labels} 产物生成完成。"
                )

            self.progress.emit(100, "转换完成")
            validation_seconds = (
                time.monotonic() - validation_started
                if validation_started is not None
                else -1.0
            )
            self.finished.emit(
                True, success_message, self.output_dir, validation_seconds
            )
        except Exception as exc:
            validation_seconds = (
                time.monotonic() - validation_started
                if validation_started is not None
                else -1.0
            )
            self.finished.emit(
                False, str(exc), self.output_dir, validation_seconds
            )


class MainWindow(QMainWindow):
    DEFAULT_WINDOW_SIZE = (929, 480)
    MINIMUM_WINDOW_SIZE = (520, 440)
    COMPACT_LAYOUT_WIDTH = 800
    DEFAULT_NAV_WIDTH = 84
    MINIMUM_NAV_WIDTH = 72
    MAXIMUM_NAV_WIDTH = 180
    DEFAULT_INSPECTOR_WIDTH = 270
    MINIMUM_INSPECTOR_WIDTH = 240
    MAXIMUM_INSPECTOR_WIDTH = 340

    def __init__(self):
        super().__init__()
        self.worker_thread = None
        self.worker = None
        self.stage_badges = []
        self.stage_names = []
        self.stage_details = []
        self.current_stage = 0
        self.validation_started = None
        self.validation_timer = QTimer(self)
        self.validation_timer.setInterval(100)
        self.validation_timer.timeout.connect(self._refresh_validation_elapsed)
        self._entrance_animation = None
        self.setWindowTitle("Keil Forge · CMake Converter")
        self.setMinimumSize(*self.MINIMUM_WINDOW_SIZE)
        self.resize(*self.DEFAULT_WINDOW_SIZE)
        self._build_ui()
        self._connect_signals()
        self._load_config()

    def _build_ui(self):
        root = QWidget()
        root.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.nav_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.nav_splitter.setChildrenCollapsible(False)
        self.nav_splitter.setHandleWidth(6)

        rail = QFrame(objectName="navRail")
        rail.setMinimumWidth(72)
        rail.setMaximumWidth(180)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(14, 18, 14, 14)
        rail_layout.setSpacing(8)

        rail_mark = QSvgWidget(str(_resource_path("assets/keilforge-mark.svg")))
        rail_mark.setObjectName("railMark")
        rail_mark.setFixedSize(48, 48)
        rail_layout.addWidget(rail_mark, 0, Qt.AlignmentFlag.AlignHCenter)
        rail_layout.addSpacing(18)

        self.nav_buttons = []
        self.nav_labels = (
            ("工程", "工程转换", "工程转换"),
            ("构建", "构建配置", "构建配置"),
            ("调试", "调试烧录", "调试与烧录"),
            ("日志", "运行日志", "运行日志"),
        )
        for index, (_, text, tooltip) in enumerate(self.nav_labels):
            button = QPushButton(
                text,
                objectName="navButtonActive" if index == 0 else "navButton",
            )
            button.setToolTip(tooltip)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            rail_layout.addWidget(button)
            self.nav_buttons.append(button)
        rail_layout.addStretch(1)
        rail_layout.addWidget(QLabel("v2.0", objectName="railCaption"), 0, Qt.AlignmentFlag.AlignHCenter)

        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        top_bar = QFrame(objectName="topBar")
        top_bar.setFixedHeight(66)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(18, 0, 18, 0)
        top_layout.setSpacing(12)

        self.top_brand = QWidget(objectName="topBrand")
        brand_layout = QVBoxLayout(self.top_brand)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(0)
        brand_layout.addWidget(QLabel("KEIL FORGE", objectName="brand"))
        brand_layout.addWidget(QLabel("PROJECT CONVERTER", objectName="topEyebrow"))
        top_layout.addWidget(self.top_brand)
        top_layout.addStretch(1)
        self.status_pill = QLabel("就绪", objectName="statusPill")
        self.status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_pill.setFixedHeight(34)
        self.status_pill.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        top_layout.addWidget(self.status_pill)
        self.open_output_button = QPushButton("打开输出目录", objectName="secondaryButton")
        self.open_output_button.setFixedHeight(38)
        self.open_output_button.setEnabled(False)
        top_layout.addWidget(self.open_output_button)
        self.validate_check = QCheckBox(
            "生成后执行编译验证", objectName="validationCheck"
        )
        self.validate_check.setChecked(True)
        self.validate_check.setToolTip(
            "勾选后将在 CMake 工程生成完成后执行配置、编译和固件产物检查。"
        )
        top_layout.addWidget(self.validate_check)
        self.convert_button = QPushButton("转换并验证", objectName="primary")
        self.convert_button.setFixedWidth(136)
        top_layout.addWidget(self.convert_button)
        shell_layout.addWidget(top_bar)

        self.progress = QProgressBar(objectName="mainProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("0% · 就绪")
        shell_layout.addWidget(self.progress)

        self.page_stack = QStackedWidget()

        project_page = QWidget(objectName="pageSurface")
        project_page_layout = QVBoxLayout(project_page)
        project_page_layout.setContentsMargins(0, 0, 0, 0)
        project_page_layout.setSpacing(0)
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setChildrenCollapsible(True)
        self.workspace_splitter.setHandleWidth(6)
        content_scroll = QScrollArea()
        content_scroll.setWidgetResizable(True)
        content_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget(objectName="pageSurface")
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 20, 24, 22)
        content_layout.setSpacing(14)

        heading = QHBoxLayout()
        heading_box = QVBoxLayout()
        heading_box.setSpacing(2)
        heading_box.addWidget(QLabel("转换流程", objectName="section"))
        heading_box.addWidget(
            QLabel("选择 Keil 工程并生成可验证的跨平台构建资产。", objectName="muted")
        )
        heading.addLayout(heading_box)
        heading.addStretch(1)
        content_layout.addLayout(heading)

        pipeline = QFrame(objectName="pipeline")
        pipeline_layout = QHBoxLayout(pipeline)
        pipeline_layout.setContentsMargins(18, 14, 18, 14)
        pipeline_layout.setSpacing(9)
        stages = (
            ("1", "INPUT", "uVision 工程"),
            ("2", "RESOLVE", "启动与链接资产"),
            ("3", "GENERATE", "CMake 与工具链"),
            ("4", "VALIDATE", "GCC 编译验证"),
        )
        for index, (number, name, detail) in enumerate(stages):
            stage = QFrame(objectName="stage")
            stage_layout = QHBoxLayout(stage)
            stage_layout.setContentsMargins(2, 0, 2, 0)
            stage_layout.setSpacing(8)
            badge = QLabel(number, objectName="stageIndex")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setProperty("state", "complete" if index == 0 else "pending")
            copy = QVBoxLayout()
            copy.setSpacing(0)
            name_label = QLabel(name, objectName="stageName")
            name_label.setProperty("state", "complete" if index == 0 else "pending")
            name_label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            copy.addWidget(name_label)
            detail_label = QLabel(detail, objectName="stageDetail")
            detail_label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            copy.addWidget(detail_label)
            stage_layout.addWidget(badge)
            stage_layout.addLayout(copy)
            pipeline_layout.addWidget(stage, 1)
            self.stage_badges.append(badge)
            self.stage_names.append(name_label)
            self.stage_details.append(detail_label)
            if index < len(stages) - 1:
                arrow = QLabel("›", objectName="muted")
                arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                pipeline_layout.addWidget(arrow)
        content_layout.addWidget(pipeline)

        content_layout.addWidget(QLabel("工程资产", objectName="section"))

        file_grid = QGridLayout()
        file_grid.setHorizontalSpacing(10)
        file_grid.setVerticalSpacing(7)
        file_grid.setColumnStretch(0, 1)
        file_grid.setColumnStretch(1, 0)
        self.project_edit, self.project_button = self._path_field(
            file_grid, 0, "KEIL PROJECT", "选择 .uvprojx 工程"
        )
        self.output_edit, self.output_button = self._path_field(
            file_grid, 1, "OUTPUT DIRECTORY", "生成文件保存目录"
        )
        self.uv4_edit, self.uv4_button = self._path_field(
            file_grid, 2, "KEIL MDK", "用于 CMSIS-Pack 与 RTE 解析，可留空自动检测"
        )
        content_layout.addLayout(file_grid)

        self.compiler_combo = self._combo(SUPPORTED_COMPILERS)
        self.host_combo = self._combo(SUPPORTED_HOST_SYSTEMS)
        self.generator_combo = QComboBox()
        self.generator_combo.setEditable(True)
        self.probe_combo = self._combo(SUPPORTED_DEBUG_PROBES)
        self.backend_combo = self._combo(SUPPORTED_DEBUG_BACKENDS)
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("留空时使用第一个 Target")
        self.build_dir_edit = QLineEdit()
        self.build_dir_edit.setPlaceholderText("build-gcc")

        output_summary = QFrame(objectName="pipeline")
        output_summary_layout = QVBoxLayout(output_summary)
        output_summary_layout.setContentsMargins(16, 12, 16, 12)
        output_summary_layout.setSpacing(4)
        output_summary_layout.addWidget(QLabel("生成内容", objectName="section"))
        summary_detail = QLabel(
            "CMakeLists.txt · GCC 工具链 · VS Code 调试任务 · ELF · HEX · BIN",
            objectName="muted",
        )
        summary_detail.setWordWrap(True)
        summary_detail.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        output_summary_layout.addWidget(summary_detail)
        content_layout.addWidget(output_summary)
        content_layout.addStretch(1)
        content_scroll.setWidget(content)

        inspector = QFrame(objectName="inspector")
        inspector_layout = QVBoxLayout(inspector)
        inspector_layout.setContentsMargins(16, 16, 16, 16)
        inspector_layout.setSpacing(10)
        inspector_layout.addWidget(QLabel("工程检查", objectName="eyebrow"))
        inspector_layout.addWidget(QLabel("转换上下文", objectName="section"))
        self.inspector_project = self._metric("工程", "未选择", inspector_layout)
        self.inspector_compiler = self._metric("编译器", "GNU Arm GCC", inspector_layout)
        self.inspector_target = self._metric("目标", "自动识别", inspector_layout)
        self.inspector_probe = self._metric("调试探针", "全部", inspector_layout)
        self.inspector_status = self._metric("构建状态", "等待转换", inspector_layout)
        self.inspector_validation_time = self._metric(
            "验证耗时", "等待验证", inspector_layout
        )

        inspector_layout.addSpacing(4)
        inspector_layout.addWidget(QLabel("内存估算", objectName="fieldLabel"))
        flash_row = QHBoxLayout()
        flash_row.addWidget(QLabel("FLASH", objectName="stageName"))
        flash_row.addStretch(1)
        self.flash_usage_label = QLabel("等待构建", objectName="muted")
        flash_row.addWidget(self.flash_usage_label)
        inspector_layout.addLayout(flash_row)
        self.flash_bar = QProgressBar(objectName="memoryBar")
        self.flash_bar.setRange(0, 100)
        self.flash_bar.setValue(0)
        self.flash_bar.setTextVisible(False)
        inspector_layout.addWidget(self.flash_bar)
        ram_row = QHBoxLayout()
        ram_row.addWidget(QLabel("RAM", objectName="stageName"))
        ram_row.addStretch(1)
        self.ram_usage_label = QLabel("等待构建", objectName="muted")
        ram_row.addWidget(self.ram_usage_label)
        inspector_layout.addLayout(ram_row)
        self.ram_bar = QProgressBar(objectName="memoryBar")
        self.ram_bar.setRange(0, 100)
        self.ram_bar.setValue(0)
        self.ram_bar.setTextVisible(False)
        inspector_layout.addWidget(self.ram_bar)
        inspector_layout.addStretch(1)

        self.inspector_scroll = QScrollArea(objectName="inspectorScroll")
        self.inspector_scroll.setWidgetResizable(True)
        self.inspector_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.inspector_scroll.setMinimumWidth(240)
        self.inspector_scroll.setMaximumWidth(340)
        self.inspector_scroll.setWidget(inspector)

        self.workspace_splitter.addWidget(content_scroll)
        self.workspace_splitter.addWidget(self.inspector_scroll)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 0)
        self.workspace_splitter.setSizes([800, 270])
        project_page_layout.addWidget(self.workspace_splitter)
        self.page_stack.addWidget(project_page)

        config_page = QScrollArea()
        config_page.setWidgetResizable(True)
        config_content = QWidget(objectName="pageSurface")
        config_layout = QVBoxLayout(config_content)
        config_layout.setContentsMargins(32, 28, 32, 32)
        config_layout.setSpacing(16)
        config_layout.addWidget(QLabel("构建配置", objectName="section"))
        config_layout.addWidget(
            QLabel("设置目标编译器、宿主平台和 CMake 构建方式。", objectName="muted")
        )
        profile_grid = QGridLayout()
        profile_grid.setHorizontalSpacing(16)
        profile_grid.setVerticalSpacing(14)

        fields = [
            ("COMPILER", self.compiler_combo),
            ("HOST", self.host_combo),
            ("CMAKE GENERATOR", self.generator_combo),
            ("TARGET NAME", self.target_edit),
            ("BUILD DIRECTORY", self.build_dir_edit),
        ]
        for index, (label, widget) in enumerate(fields):
            row, column = divmod(index, 2)
            box = QVBoxLayout()
            box.setSpacing(4)
            box.addWidget(QLabel(label, objectName="fieldLabel"))
            box.addWidget(widget)
            profile_grid.addLayout(box, row, column)
        config_layout.addLayout(profile_grid)

        toggles = QHBoxLayout()
        self.vscode_check = QCheckBox("生成 VS Code 配置与调试任务")
        self.vscode_check.setChecked(True)
        toggles.addWidget(self.vscode_check)
        toggles.addStretch(1)
        config_layout.addLayout(toggles)
        config_layout.addStretch(1)
        config_page.setWidget(config_content)
        self.page_stack.addWidget(config_page)

        debug_page = QScrollArea()
        debug_page.setWidgetResizable(True)
        debug_content = QWidget(objectName="pageSurface")
        debug_layout = QVBoxLayout(debug_content)
        debug_layout.setContentsMargins(32, 28, 32, 32)
        debug_layout.setSpacing(16)
        debug_layout.addWidget(QLabel("调试与烧录", objectName="section"))
        debug_layout.addWidget(
            QLabel("生成 VS Code、OpenOCD、J-Link 和探针相关任务。", objectName="muted")
        )
        debug_grid = QGridLayout()
        debug_grid.setHorizontalSpacing(16)
        debug_grid.setVerticalSpacing(14)
        for column, (label, widget) in enumerate(
            (("DEBUG PROBE", self.probe_combo), ("DEBUG BACKEND", self.backend_combo))
        ):
            box = QVBoxLayout()
            box.setSpacing(4)
            box.addWidget(QLabel(label, objectName="fieldLabel"))
            box.addWidget(widget)
            debug_grid.addLayout(box, 0, column)
        debug_layout.addLayout(debug_grid)
        debug_assets = QFrame(objectName="pipeline")
        debug_assets_layout = QVBoxLayout(debug_assets)
        debug_assets_layout.setContentsMargins(18, 16, 18, 16)
        debug_assets_layout.setSpacing(7)
        debug_assets_layout.addWidget(QLabel("生成的调试资产", objectName="section"))
        debug_assets_layout.addWidget(QLabel(".vscode/launch.json", objectName="muted"))
        debug_assets_layout.addWidget(QLabel(".vscode/tasks.json", objectName="muted"))
        debug_assets_layout.addWidget(QLabel("OpenOCD · J-Link · ST-Link 烧录任务", objectName="muted"))
        debug_layout.addWidget(debug_assets)
        debug_actions = QHBoxLayout()
        self.debug_open_output_button = QPushButton("打开输出目录")
        self.debug_open_vscode_button = QPushButton("打开 .vscode")
        debug_actions.addWidget(self.debug_open_output_button)
        debug_actions.addWidget(self.debug_open_vscode_button)
        debug_actions.addStretch(1)
        debug_layout.addLayout(debug_actions)
        debug_layout.addStretch(1)
        debug_page.setWidget(debug_content)
        self.page_stack.addWidget(debug_page)

        console = QFrame(objectName="console")
        console_layout = QVBoxLayout(console)
        console_layout.setContentsMargins(0, 0, 0, 0)
        console_layout.setSpacing(0)
        console_header = QHBoxLayout()
        console_header.setContentsMargins(18, 8, 12, 6)
        console_header.addWidget(QLabel("构建控制台", objectName="consoleTitle"))
        console_header.addSpacing(12)
        console_header.addWidget(QLabel("输出", objectName="railCaption"))
        console_header.addStretch(1)
        self.clear_log_button = QPushButton("清空", objectName="consoleButton")
        console_header.addWidget(self.clear_log_button)
        console_layout.addLayout(console_header)
        self.log_browser = QTextBrowser()
        self.log_browser.setMinimumHeight(180)
        console_layout.addWidget(self.log_browser, 1)
        self.page_stack.addWidget(console)
        shell_layout.addWidget(self.page_stack, 1)

        status_bar = QFrame(objectName="statusBar")
        status_layout = QHBoxLayout(status_bar)
        status_layout.setContentsMargins(18, 5, 18, 5)
        status_layout.addWidget(QLabel("就绪", objectName="railCaption"))
        status_layout.addStretch(1)
        status_layout.addWidget(QLabel("Keil → CMake → GCC", objectName="railCaption"))
        shell_layout.addWidget(status_bar)

        self.nav_splitter.addWidget(rail)
        self.nav_splitter.addWidget(shell)
        self.nav_splitter.setStretchFactor(0, 0)
        self.nav_splitter.setStretchFactor(1, 1)
        self.nav_splitter.setSizes([84, 476])
        root_layout.addWidget(self.nav_splitter)
        self.setCentralWidget(root)
        self._fade_effect = QGraphicsOpacityEffect(root)
        root.setGraphicsEffect(self._fade_effect)
        self._fade_effect.setOpacity(0.0)

    @staticmethod
    def _metric(label, value, layout):
        frame = QFrame(objectName="metric")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(12, 9, 12, 9)
        frame_layout.setSpacing(2)
        frame_layout.addWidget(QLabel(label.upper(), objectName="fieldLabel"))
        value_label = QLabel(value, objectName="metricValue")
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        frame_layout.addWidget(value_label)
        layout.addWidget(frame)
        return value_label

    def _path_field(self, grid, row, label, placeholder):
        label_widget = QLabel(label, objectName="fieldLabel")
        edit = PathHistoryEdit()
        edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        edit.setMinimumWidth(0)
        edit.setPlaceholderText(placeholder)
        button = QPushButton("浏览", objectName="fieldButton")
        button.setFixedWidth(78)
        grid.addWidget(label_widget, row * 2, 0, 1, 2)
        grid.addWidget(edit, row * 2 + 1, 0)
        grid.addWidget(button, row * 2 + 1, 1)
        return edit, button

    @staticmethod
    def _combo(values):
        combo = QComboBox()
        for key, label in values.items():
            combo.addItem(label, key)
        return combo

    def _connect_signals(self):
        self.nav_splitter.splitterMoved.connect(self._update_nav_labels)
        for index, button in enumerate(self.nav_buttons):
            button.clicked.connect(
                lambda checked=False, page_index=index: self._show_page(page_index)
            )
        self.project_button.clicked.connect(self._browse_project)
        self.output_button.clicked.connect(self._browse_output)
        self.uv4_button.clicked.connect(self._browse_uv4)
        self.convert_button.clicked.connect(self._start_conversion)
        self.validate_check.toggled.connect(self._sync_validation_action)
        self.open_output_button.clicked.connect(self._open_output)
        self.clear_log_button.clicked.connect(self.log_browser.clear)
        self.debug_open_output_button.clicked.connect(self._open_output)
        self.debug_open_vscode_button.clicked.connect(self._open_vscode)
        self.host_combo.currentIndexChanged.connect(self._update_generators)
        self.host_combo.currentIndexChanged.connect(self._sync_backends)
        self.probe_combo.currentIndexChanged.connect(self._sync_backends)
        self.project_edit.textChanged.connect(self._project_path_changed)
        self.compiler_combo.currentIndexChanged.connect(self._refresh_inspector)
        self.compiler_combo.currentIndexChanged.connect(self._sync_backends)
        self.target_edit.textChanged.connect(self._refresh_inspector)
        self.probe_combo.currentIndexChanged.connect(self._refresh_inspector)

    def _update_nav_labels(self, *args):
        expanded = self.nav_splitter.sizes()[0] >= 130
        for button, (compact, full, _) in zip(self.nav_buttons, self.nav_labels):
            button.setText(full if expanded else compact)

    @classmethod
    def _is_compact_width(cls, width):
        return int(width) < cls.COMPACT_LAYOUT_WIDTH

    @classmethod
    def _normalized_inspector_width(cls, width):
        try:
            width = int(width)
        except (TypeError, ValueError):
            return cls.DEFAULT_INSPECTOR_WIDTH
        if width <= 0:
            return cls.DEFAULT_INSPECTOR_WIDTH
        return max(
            cls.MINIMUM_INSPECTOR_WIDTH,
            min(cls.MAXIMUM_INSPECTOR_WIDTH, width),
        )

    @classmethod
    def _normalized_nav_width(cls, width):
        try:
            width = int(width)
        except (TypeError, ValueError):
            return cls.DEFAULT_NAV_WIDTH
        return max(cls.MINIMUM_NAV_WIDTH, min(cls.MAXIMUM_NAV_WIDTH, width))

    @staticmethod
    def _conversion_action_text(validate_build):
        return "转换并验证" if validate_build else "仅转换"

    def _sync_validation_action(self, validate_build):
        self.convert_button.setText(
            self._conversion_action_text(validate_build)
        )
        self.convert_button.setToolTip(
            "生成 CMake 工程并执行编译验证。"
            if validate_build
            else "仅生成 CMake 工程。"
        )

    def _update_compact_layout(self):
        if not hasattr(self, "inspector_scroll"):
            return
        compact = self._is_compact_width(self.width())
        pipeline_compact = self.width() < 1020
        self.top_brand.setVisible(not compact)
        self.open_output_button.setVisible(True)
        self.open_output_button.setText("目录" if compact else "打开输出目录")
        self.validate_check.setText(
            "编译验证" if compact else "生成后执行编译验证"
        )
        self.convert_button.setFixedWidth(108 if compact else 136)
        for detail in self.stage_details:
            detail.setVisible(not pipeline_compact)
        self.inspector_scroll.setVisible(not compact)
        if compact:
            self.nav_splitter.setSizes(
                [
                    self.DEFAULT_NAV_WIDTH,
                    max(1, self.width() - self.DEFAULT_NAV_WIDTH),
                ]
            )
        self._update_nav_labels()

    def _show_page(self, index):
        if not 0 <= index < self.page_stack.count():
            return
        self.page_stack.setCurrentIndex(index)
        for button_index, button in enumerate(self.nav_buttons):
            button.setObjectName(
                "navButtonActive" if button_index == index else "navButton"
            )
            self._refresh_dynamic_style(button)

    def _browse_project(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Keil 工程",
            QDir.homePath(),
            "Keil Projects (*.uvprojx *.uvproj)",
        )
        if not file_path:
            return
        self.project_edit.setText(QDir.toNativeSeparators(file_path))
        self._remember_path(self.project_edit, file_path)

    @staticmethod
    def _default_output_directory(project_path):
        project_path = project_path.strip()
        if not project_path:
            return ""
        return str(Path(project_path).parent / "cmake-generated")

    def _project_path_changed(self, project_path):
        output = self._default_output_directory(project_path)
        self.output_edit.setText(QDir.toNativeSeparators(output))
        self._refresh_inspector()

    def _browse_output(self):
        directory = QFileDialog.getExistingDirectory(
            self, "选择输出目录", self.output_edit.text().strip() or QDir.homePath()
        )
        if directory:
            self.output_edit.setText(QDir.toNativeSeparators(directory))
            self._remember_path(self.output_edit, directory)

    def _browse_uv4(self):
        directory = QFileDialog.getExistingDirectory(
            self, "选择 Keil MDK 目录", self.uv4_edit.text().strip() or QDir.homePath()
        )
        if directory:
            self.uv4_edit.setText(QDir.toNativeSeparators(directory))

    def _set_combo(self, combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    @staticmethod
    def _generator_target(host, preferred, current, host_changed):
        if host_changed:
            return DEFAULT_GENERATOR_BY_HOST.get(host, "Ninja")
        return preferred or current or DEFAULT_GENERATOR_BY_HOST.get(host, "Ninja")

    def _update_generators(self, preferred=""):
        host_changed = not isinstance(preferred, str)
        current = self.generator_combo.currentText().strip()
        if host_changed:
            preferred = ""
        host = self.host_combo.currentData() or "windows"
        values = get_supported_generators(host)
        self.generator_combo.blockSignals(True)
        self.generator_combo.clear()
        self.generator_combo.addItems(values)
        target = self._generator_target(host, preferred, current, host_changed)
        index = self.generator_combo.findText(target)
        if index >= 0:
            self.generator_combo.setCurrentIndex(index)
        else:
            self.generator_combo.setEditText(target)
        self.generator_combo.blockSignals(False)

    def _sync_backends(self):
        probe = self.probe_combo.currentData() or "default"
        host = self.host_combo.currentData() or "windows"
        compiler = self.compiler_combo.currentData() or "gcc"
        current = self.backend_combo.currentData() or "default"
        self.backend_combo.blockSignals(True)
        self.backend_combo.clear()
        for key, label in SUPPORTED_DEBUG_BACKENDS.items():
            if probe not in {"default", "all", "jlink"} and key == "jlink":
                continue
            if probe not in {"default", "all", "daplink", "ulink"} and key == "pyocd":
                continue
            if (host != "windows" or compiler != "armclang") and key == "keil":
                continue
            self.backend_combo.addItem(label, key)
        self._set_combo(self.backend_combo, current)
        self.backend_combo.blockSignals(False)

    def _refresh_inspector(self):
        project_path = self.project_edit.text().strip()
        self.inspector_project.setText(Path(project_path).name if project_path else "未选择")
        self.inspector_project.setToolTip(project_path)
        self.inspector_compiler.setText(self.compiler_combo.currentText() or "未选择")
        self.inspector_target.setText(self.target_edit.text().strip() or "自动识别")
        self.inspector_probe.setText(self.probe_combo.currentText() or "未选择")

    @staticmethod
    def _format_memory_size(value):
        if value >= 1024 * 1024:
            return f"{value / (1024 * 1024):.2f} MiB"
        if value >= 1024:
            return f"{value / 1024:.1f} KiB"
        return f"{value} B"

    @Slot(int, int, int, int)
    def _update_memory_usage(self, flash_used, flash_total, ram_used, ram_total):
        def update(label, bar, used, total):
            percent = round(used * 100 / total, 1) if total else 0.0
            label.setText(
                f"{self._format_memory_size(used)} / "
                f"{self._format_memory_size(total)} · {percent:.1f}%"
            )
            bar.setValue(max(0, min(100, round(percent))))

        update(self.flash_usage_label, self.flash_bar, flash_used, flash_total)
        update(self.ram_usage_label, self.ram_bar, ram_used, ram_total)

    @staticmethod
    def _refresh_dynamic_style(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _set_status(self, text, state):
        self.status_pill.setText(text)
        self.status_pill.setProperty("state", state)
        self._refresh_dynamic_style(self.status_pill)

    def _set_stage_state(self, stage_index, state):
        if not 0 <= stage_index < len(self.stage_badges):
            return
        for widget in (self.stage_badges[stage_index], self.stage_names[stage_index]):
            widget.setProperty("state", state)
            self._refresh_dynamic_style(widget)

    def _reset_stages(self):
        self.current_stage = 0
        for index in range(len(self.stage_badges)):
            self._set_stage_state(index, "running" if index == 0 else "pending")

    def _advance_stage(self, stage_index):
        stage_index = max(0, min(stage_index, len(self.stage_badges) - 1))
        for index in range(stage_index):
            self._set_stage_state(index, "complete")
        self._set_stage_state(stage_index, "running")
        for index in range(stage_index + 1, len(self.stage_badges)):
            self._set_stage_state(index, "pending")
        self.current_stage = stage_index

    def _handle_worker_log(self, message):
        if "解析 Keil 工程" in message:
            self._advance_stage(0)
        elif "目标:" in message:
            self._advance_stage(1)
        elif "生成 CMake" in message:
            self._advance_stage(2)
        elif "编译验证" in message or "构建验证" in message:
            self._advance_stage(3)
            if message.startswith("执行 CMake 配置与编译验证"):
                self._start_validation_timer()
        self._log(message)

    @staticmethod
    def _format_duration(seconds):
        seconds = max(0.0, float(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours >= 1:
            return f"{int(hours)} 小时 {int(minutes):02d} 分 {seconds:04.1f} 秒"
        if minutes >= 1:
            return f"{int(minutes)} 分 {seconds:04.1f} 秒"
        return f"{seconds:.1f} 秒"

    @classmethod
    def _with_validation_duration(cls, message, validation_seconds):
        timing_text = (
            cls._format_duration(validation_seconds)
            if validation_seconds >= 0
            else "未执行"
        )
        return f"{message}\n\n验证耗时\n{timing_text}"

    def _start_validation_timer(self):
        if self.validation_started is not None:
            return
        self.validation_started = time.monotonic()
        self.inspector_validation_time.setText("0.0 秒")
        self.validation_timer.start()

    def _refresh_validation_elapsed(self):
        if self.validation_started is None:
            return
        elapsed = time.monotonic() - self.validation_started
        self.inspector_validation_time.setText(self._format_duration(elapsed))

    def _stop_validation_timer(self, validation_seconds):
        self.validation_timer.stop()
        self.validation_started = None
        if validation_seconds >= 0:
            self.inspector_validation_time.setText(
                self._format_duration(validation_seconds)
            )
        else:
            self.inspector_validation_time.setText("未执行")

    @Slot(int, str)
    def _update_conversion_progress(self, value, message):
        value = max(0, min(100, value))
        self.progress.setRange(0, 100)
        self.progress.setValue(value)
        self.progress.setFormat(f"{value}% · {message}")

    def _options(self):
        return GenerationOptions(
            compiler=self.compiler_combo.currentData() or "gcc",
            generator=self.generator_combo.currentText().strip() or "Ninja",
            host_os=self.host_combo.currentData() or "windows",
            debugger=self.backend_combo.currentData() or "all",
            debug_probe=self.probe_combo.currentData() or "all",
            debug_backend=self.backend_combo.currentData() or "all",
            build_dir=self.build_dir_edit.text().strip() or None,
            export_vsc_settings=self.vscode_check.isChecked(),
        ).normalized()

    def _start_conversion(self):
        project_file = self.project_edit.text().strip()
        output_dir = self.output_edit.text().strip()
        if not project_file or not Path(project_file).is_file():
            QMessageBox.critical(self, "工程无效", "请选择有效的 Keil 工程文件。")
            return
        if not output_dir:
            QMessageBox.critical(self, "输出目录无效", "请选择输出目录。")
            return

        try:
            options = self._options()
        except Exception as exc:
            QMessageBox.critical(self, "配置错误", str(exc))
            return

        self._remember_path(self.project_edit, project_file)
        self._remember_path(self.output_edit, output_dir)
        self._save_config()
        self.convert_button.setEnabled(False)
        self.validate_check.setEnabled(False)
        self.open_output_button.setEnabled(False)
        self._set_status("转换中", "running")
        self.inspector_status.setText("正在转换")
        self.validation_timer.stop()
        self.validation_started = None
        self.inspector_validation_time.setText(
            "等待开始" if self.validate_check.isChecked() else "未执行"
        )
        memory_state = "正在构建" if self.validate_check.isChecked() else "未执行验证"
        self.flash_usage_label.setText(memory_state)
        self.ram_usage_label.setText(memory_state)
        self.flash_bar.setValue(0)
        self.ram_bar.setValue(0)
        self._reset_stages()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("0% · 准备转换")
        self.log_browser.clear()
        self._log(f"输入: {project_file}")
        self._log(f"输出: {output_dir}")
        self._log(
            f"编译器: {self.compiler_combo.currentText()} ({options.compiler})"
        )
        self._log(f"CMake 生成器: {options.generator}")

        self.worker_thread = QThread(self)
        self.worker = ConversionWorker(
            project_file,
            output_dir,
            self.uv4_edit.text().strip(),
            self.target_edit.text().strip(),
            options,
            self.validate_check.isChecked(),
        )
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self._handle_worker_log)
        self.worker.progress.connect(self._update_conversion_progress)
        self.worker.memory_usage.connect(self._update_memory_usage)
        self.worker.finished.connect(self._conversion_finished)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.start()

    @staticmethod
    def _completion_status(success, validation_seconds):
        validation_completed = validation_seconds >= 0
        if success:
            text = "构建通过" if validation_completed else "转换完成"
        else:
            text = "构建失败" if validation_completed else "转换失败"
        return text, validation_completed

    @Slot(bool, str, str, float)
    def _conversion_finished(self, success, message, output_dir, validation_seconds):
        self._stop_validation_timer(validation_seconds)
        message = self._with_validation_duration(message, validation_seconds)
        self.progress.setRange(0, 100)
        if success:
            self.progress.setValue(100)
            self.progress.setFormat("100% · 转换完成")
        else:
            self.progress.setFormat(f"{self.progress.value()}% · 转换失败")
        self.convert_button.setEnabled(True)
        self.validate_check.setEnabled(True)
        self.open_output_button.setEnabled(bool(output_dir))
        self._set_status(
            "成功" if success else "失败",
            "success" if success else "failed",
        )
        inspector_text, validation_completed = self._completion_status(
            success, validation_seconds
        )
        self.inspector_status.setText(inspector_text)
        if success:
            for index in range(len(self.stage_badges)):
                state = (
                    "pending"
                    if index == len(self.stage_badges) - 1
                    and not validation_completed
                    else "complete"
                )
                self._set_stage_state(index, state)
        else:
            self._set_stage_state(self.current_stage, "failed")
            if self.flash_usage_label.text() == "正在构建":
                self.flash_usage_label.setText("构建失败")
                self.ram_usage_label.setText("构建失败")
        self._log(message)
        if success:
            QMessageBox.information(self, "转换完成", message)
        else:
            self._show_page(3)
            QMessageBox.critical(self, "转换失败", message)

    def _open_output(self):
        output = self.output_edit.text().strip()
        if output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(output))

    def _open_vscode(self):
        output = Path(self.output_edit.text().strip())
        vscode_dir = output / ".vscode"
        if vscode_dir.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(vscode_dir)))
            return
        QMessageBox.information(
            self,
            "调试配置尚未生成",
            "完成转换后可打开输出目录中的 .vscode 配置。",
        )

    def _log(self, message):
        self.log_browser.append(message)

    @staticmethod
    def _history_values(value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value).strip()
        return [text] if text else []

    @staticmethod
    def _merge_path_history(path, history, limit=12):
        values = []
        candidates = [path, *history]
        seen = set()
        for candidate in candidates:
            value = str(candidate).strip()
            if not value:
                continue
            key = os.path.normcase(os.path.normpath(value))
            if key in seen:
                continue
            seen.add(key)
            values.append(value)
            if len(values) >= limit:
                break
        return values

    def _remember_path(self, edit, path):
        edit.set_history(self._merge_path_history(path, edit.history()))

    def _load_config(self):
        settings = QSettings(str(_config_file_path()), QSettings.Format.IniFormat)
        project_path = str(settings.value("uvproj_filepath", ""))
        output_directory = str(settings.value("output_directory", ""))
        project_history = self._history_values(
            settings.value("uvproj_history", [])
        )
        output_history = self._history_values(
            settings.value("output_history", [])
        )
        self.project_edit.set_history(
            self._merge_path_history(project_path, project_history)
        )
        self.output_edit.set_history(
            self._merge_path_history(output_directory, output_history)
        )
        self.project_edit.setText(project_path)
        self.output_edit.setText(output_directory)
        self.uv4_edit.setText(
            str(settings.value("uv4_path", find_keil_mdk_root() or ""))
        )
        self.target_edit.setText(str(settings.value("target_name", "")))
        self.build_dir_edit.setText(str(settings.value("build_dir", "")))
        self._set_combo(self.compiler_combo, str(settings.value("compiler", "gcc")))
        self._set_combo(self.host_combo, str(settings.value("host_os", "windows")))
        self._update_generators(str(settings.value("generator", "Ninja")))
        self._set_combo(self.probe_combo, str(settings.value("debug_probe", "all")))
        self._sync_backends()
        self._set_combo(self.backend_combo, str(settings.value("debug_backend", "all")))
        self.vscode_check.setChecked(
            str(settings.value("export_vsc_settings", "true")).lower()
            in {"1", "true", "yes"}
        )
        self.validate_check.setChecked(
            str(settings.value("validate_build", "true")).lower()
            in {"1", "true", "yes"}
        )
        nav_width = self._normalized_nav_width(
            settings.value("nav_width", self.DEFAULT_NAV_WIDTH)
        )
        inspector_width = self._normalized_inspector_width(
            settings.value("inspector_width", self.DEFAULT_INSPECTOR_WIDTH)
        )
        if self._is_compact_width(self.width()):
            nav_width = min(nav_width, self.DEFAULT_NAV_WIDTH)
        self.nav_splitter.setSizes([nav_width, max(1, self.width() - nav_width)])
        self.workspace_splitter.setSizes(
            [max(1, self.width() - nav_width - inspector_width), inspector_width]
        )
        self._update_nav_labels()
        self._update_compact_layout()
        self._refresh_inspector()

    def showEvent(self, event):
        super().showEvent(event)
        if self._entrance_animation is not None:
            return
        self._entrance_animation = QPropertyAnimation(
            self._fade_effect, b"opacity", self
        )
        self._entrance_animation.setDuration(320)
        self._entrance_animation.setStartValue(0.0)
        self._entrance_animation.setEndValue(1.0)
        self._entrance_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._entrance_animation.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_compact_layout()

    def _save_config(self):
        settings = QSettings(str(_config_file_path()), QSettings.Format.IniFormat)
        values = {
            "uvproj_filepath": self.project_edit.text().strip(),
            "output_directory": self.output_edit.text().strip(),
            "uvproj_history": self.project_edit.history(),
            "output_history": self.output_edit.history(),
            "uv4_path": self.uv4_edit.text().strip(),
            "target_name": self.target_edit.text().strip(),
            "build_dir": self.build_dir_edit.text().strip(),
            "compiler": self.compiler_combo.currentData(),
            "host_os": self.host_combo.currentData(),
            "generator": self.generator_combo.currentText().strip(),
            "debug_probe": self.probe_combo.currentData(),
            "debug_backend": self.backend_combo.currentData(),
            "export_vsc_settings": "true" if self.vscode_check.isChecked() else "false",
            "validate_build": "true" if self.validate_check.isChecked() else "false",
            "nav_width": self._normalized_nav_width(
                self.nav_splitter.sizes()[0]
            ),
            "inspector_width": self._normalized_inspector_width(
                self.workspace_splitter.sizes()[1]
            ),
        }
        for key, value in values.items():
            settings.setValue(key, value)
        settings.sync()

    def closeEvent(self, event):
        self._save_config()
        if self.worker_thread and self.worker_thread.isRunning():
            QMessageBox.information(self, "任务运行中", "转换完成后再关闭窗口。")
            event.ignore()
            return
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(_resource_path("assets/keilforge-app-icon.svg"))))
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
