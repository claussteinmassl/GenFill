"""Photoshop (Spectrum dark) look for the GenFill panel."""

FONT_STACK = '"Adobe Clean", "Segoe UI", "SF Pro Text", "Helvetica Neue", Arial, sans-serif'

BG = "#2b2b2b"            # panel
BAR = "#1f1f1f"           # contextual task bar
BAR_BORDER = "#444444"
FIELD_TEXT = "#ececec"
MUTED = "#9a9a9a"
ACCENT = "#3b63fb"        # Spectrum accent blue (Generate)
ACCENT_HOVER = "#4b72ff"
ACCENT_DOWN = "#2f54e0"
SELECT = "#4b9cf5"        # selected variation outline
ERROR = "#ff6b5e"

STYLESHEET = """
QWidget#GenFillPanel {{ background: transparent; font-family: {font}; }}
QLabel#gfTitle {{ color: #f0f0f0; font-size: 13px; font-weight: 600; }}
QLabel#gfSection {{ color: {muted}; font-size: 11px; font-weight: 600;
                    letter-spacing: 0.5px; text-transform: uppercase; }}
QLabel#gfMuted {{ color: {muted}; font-size: 11px; }}
QLabel#gfError {{ color: {error}; font-size: 11px; }}
QFrame#gfBar {{ background: {bar}; border: 1px solid {bar_border}; border-radius: 10px; }}
QPlainTextEdit#gfPrompt {{ background: transparent; border: none; color: {text};
                     font-size: 13px; padding: 4px 2px; selection-background-color: {accent}; }}
QPushButton#gfGenerate {{ background: {accent}; color: white; border: none; border-radius: 15px;
                         font-size: 12px; font-weight: 600; padding: 6px 16px 6px 12px; }}
QPushButton#gfGenerate:hover {{ background: {accent_hover}; }}
QPushButton#gfGenerate:pressed {{ background: {accent_down}; }}
QPushButton#gfGenerate:disabled {{ background: #4a4a4a; color: #9a9a9a; }}
QPushButton#gfGenerate[busy="true"] {{ background: #4a4a4a; color: #f0f0f0; }}
QToolButton#gfNav {{ background: transparent; color: #d8d8d8; border: none; border-radius: 6px;
                    font-size: 15px; padding: 0px 4px; }}
QToolButton#gfNav:hover {{ background: #3a3a3a; }}
QToolButton#gfNav:disabled {{ color: #5a5a5a; }}
QLabel#gfCounter {{ color: #d8d8d8; font-size: 12px; min-width: 34px; }}
QToolButton#gfIcon {{ background: transparent; color: #cfcfcf; border: none; border-radius: 6px;
                     padding: 3px 6px; font-size: 12px; }}
QToolButton#gfIcon:hover {{ background: #3a3a3a; }}
QToolButton#gfIcon:disabled {{ color: #5a5a5a; }}
QToolButton#gfTag {{ background: {bar}; color: #cfcfcf; border: 1px solid {bar_border};
                    border-radius: 4px; padding: 1px 5px; font-size: 11px;
                    font-family: Menlo, Consolas, monospace; }}
QToolButton#gfTag:hover {{ border-color: {accent}; }}
QToolButton#gfTag:disabled {{ color: #5a5a5a; border-color: #3a3a3a; }}
QLineEdit#gfBoxPrompt {{ background: {bar}; color: {text}; border: 1px solid {bar_border};
                        border-radius: 5px; padding: 2px 6px; font-size: 12px;
                        selection-background-color: {accent}; }}
QLineEdit#gfBoxPrompt:focus {{ border-color: {accent}; }}
QLineEdit#gfBoxPrompt:disabled {{ color: #6a6a6a; border-color: #3a3a3a; }}
QCheckBox#gfShowBoxes {{ color: {muted}; font-size: 11px; }}
QComboBox#gfModel {{ background: #1f1f1f; color: {text}; border: 1px solid {bar_border};
                    border-radius: 6px; padding: 3px 8px; font-size: 12px; min-height: 20px; }}
QComboBox#gfModel:hover {{ border-color: #5a5a5a; }}
QComboBox#gfModel QAbstractItemView {{ background: #2b2b2b; color: {text};
                                      selection-background-color: {accent}; }}
QComboBox#gfOption {{ background: #1f1f1f; color: {text}; border: 1px solid {bar_border};
                     border-radius: 6px; padding: 2px 6px; font-size: 11px; min-height: 18px; }}
QComboBox#gfOption:hover {{ border-color: #5a5a5a; }}
QComboBox#gfOption QAbstractItemView {{ background: #2b2b2b; color: {text};
                                       selection-background-color: {accent}; }}
QProgressBar#gfProgress {{ background: #3a3a3a; border: none; border-radius: 2px; max-height: 4px; }}
QProgressBar#gfProgress::chunk {{ background: {accent}; border-radius: 2px; }}
QScrollArea#gfScroll {{ background: transparent; border: none; }}
QWidget#gfGrid {{ background: transparent; }}
QFrame#gfEmpty {{ background: #262626; border: 1px dashed #4a4a4a; border-radius: 8px; }}
""".format(font=FONT_STACK, muted=MUTED, error=ERROR, bar=BAR, bar_border=BAR_BORDER,
           text=FIELD_TEXT, accent=ACCENT, accent_hover=ACCENT_HOVER, accent_down=ACCENT_DOWN)
