"""Shared desktop styling for the workspace and project dialogs."""

WORKSPACE_STYLE = """
QWidget {
    background: #202225; color: #e5e7eb;
    font-family: 'SF Pro Text', 'Segoe UI', 'DejaVu Sans'; font-size: 12px;
}
QLabel { background: transparent; }
QFrame#topBar { background: #282b2f; border-bottom: 1px solid #41454b; }
QLabel#appTitle { font-size: 15px; font-weight: 600; }
QLabel#appSubtitle { color: #a2a8b1; font-size: 10px; }
QLabel#projectTitle { font-weight: 600; }
QLabel#profileLabel { color: #a2a8b1; font-size: 11px; }
QFrame#inspectorPanel, QFrame#previewPanel, QFrame#timingPanel,
QFrame#librarySection, QFrame#actionBar { background: #282b2f; border: 0; border-radius: 0; }
QFrame#libraryPanel { border: 0; background: transparent; }
QFrame#librarySection { border-bottom: 1px solid #41454b; }
QFrame#actionBar { border-top: 1px solid #41454b; }
QFrame#formSeparator { background: #41454b; min-height: 1px; max-height: 1px; border: 0; }
QGroupBox { background: transparent; border: 0; border-top: 1px solid #41454b;
    margin-top: 12px; padding: 14px 2px 6px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 2px; padding: 0 4px; color: #c3c8d0; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {
    background: #1c1e21; border: 1px solid #494e56; border-radius: 3px; padding: 5px;
    selection-background-color: #365a78; selection-color: #ffffff;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #79b6df; }
QComboBox::drop-down { width: 22px; border: 0; border-left: 1px solid #494e56; }
QComboBox::down-arrow { width: 9px; height: 7px; }
QComboBox QAbstractItemView { background: #282b2f; border: 1px solid #626973;
    padding: 3px; selection-background-color: #365a78; }
QPushButton { background: #34383e; color: #e5e7eb; border: 1px solid #50565f;
    border-radius: 3px; padding: 6px 10px; font-weight: 500; }
QPushButton:hover { background: #424850; border-color: #77818e; }
QPushButton:pressed { background: #1c1e21; }
QPushButton:focus { border-color: #79b6df; }
QPushButton:checked { background: #365a78; border-color: #79b6df; }
QPushButton#primaryButton { background: #b6d7ed; color: #14232f; border-color: #b6d7ed; font-weight: 600; }
QPushButton#primaryButton:hover { background: #d2e8f6; border-color: #d2e8f6; }
QPushButton#primaryButton:pressed { background: #91bdda; }
QPushButton#quietButton, QPushButton#topButton { background: transparent; }
QPushButton#iconButton { min-width: 28px; padding: 5px; }
QPushButton#layoutChoice { text-align: left; padding: 10px 12px; }
QPushButton#cancelButton, QPushButton#dangerButton { color: #f0b1aa; border-color: #885751; }
QPushButton:disabled, QPushButton#primaryButton:disabled,
QPushButton#secondaryButton:disabled, QPushButton#quietButton:disabled,
QPushButton#workflowButton:disabled, QPushButton#dangerButton:disabled {
    background: #282b2f; color: #777f8a; border-color: #3b4047;
}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled { color: #777f8a; border-color: #3b4047; }
QHeaderView::section { background: #282b2f; color: #b2bac5; border: 0;
    border-bottom: 1px solid #41454b; padding: 6px 4px; font-size: 10px; font-weight: 600; }
QTableWidget, QTreeWidget { background: #24272b; border: 0; padding: 2px; }
QTableWidget::item { padding: 4px; border: 0; }
QTreeWidget::item { min-height: 26px; padding: 3px 5px; border: 0; }
QTreeWidget::item:hover, QTableWidget::item:hover { background: #343a42; }
QTreeWidget::item:selected, QTableWidget::item:selected { background: #365a78; color: #ffffff; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: #282b2f; color: #a2a8b1; border: 0;
    border-bottom: 2px solid transparent; padding: 8px 9px; font-weight: 500; }
QTabBar::tab:hover { color: #ffffff; background: #34383e; }
QTabBar::tab:selected { color: #ffffff; border-bottom: 2px solid #91bdda; }
QLabel#previewLimit, QLabel#sourceStatus { color: #b2bac5; font-size: 11px; }
QLabel#playheadTime, QLabel#selectedMediaFiles { font-family: 'SF Mono', 'Consolas', 'DejaVu Sans Mono'; font-size: 11px; }
QLabel#autosaveStatus { color: #9cceb2; padding: 0 8px; font-size: 10px; }
QLabel[muted='true'] { color: #b2bac5; }
QLabel[sectionTitle='true'], QLabel[inspectorTitle='true'] { color: #d9dfe7; font-size: 12px; font-weight: 600; }
QFrame#selectedMediaCard { background: #24272b; border: 1px solid #494e56; border-radius: 3px; }
QFrame#selectedMediaCard[state='ready'] { border-color: #769d86; }
QFrame#selectedMediaCard[state='warning'] { border-color: #c9a16d; }
QLabel#selectedMediaState[state='ready'] { color: #9cceb2; }
QLabel#selectedMediaState[state='warning'] { color: #e3bd87; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: #24272b; width: 10px; margin: 0; }
QScrollBar:horizontal { background: #24272b; height: 10px; margin: 0; }
QScrollBar::handle { background: #59616c; border-radius: 3px; margin: 2px; }
QScrollBar::handle:vertical { min-height: 28px; }
QScrollBar::handle:horizontal { min-width: 28px; }
QScrollBar::handle:hover { background: #8a95a3; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSplitter::handle { background: #202225; }
QSplitter::handle:hover { background: #79b6df; }
QProgressBar { background: #1c1e21; border: 1px solid #494e56; border-radius: 3px; text-align: center; }
QProgressBar::chunk { background: #527d9d; border-radius: 2px; }
QStatusBar { background: #282b2f; border-top: 1px solid #41454b; color: #b2bac5; }
QMenuBar, QMenu { background: #282b2f; color: #e5e7eb; }
QMenu::item { padding: 6px 22px; }
QMenu::item:selected { background: #365a78; }
QMenu::item:disabled { color: #777f8a; }
QToolTip { background: #34383e; color: #ffffff; border: 1px solid #77818e; padding: 5px; }
"""
