"""Graphical first-run setup and settings. No secrets are bundled or exported."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import DiscoverySettings, ServerConfig, load_settings
from .credentials import write_credential
from .desktop_config import (
    VERSION,
    DesktopSettings,
    credential_target,
    mcp_entry,
    profile_from_settings,
    read_profile,
    register_codex,
)
from .remote import connect, valid_child


class Worker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, operation):
        super().__init__()
        self.operation = operation

    def run(self):
        try:
            self.completed.emit(self.operation())
        except Exception as error:
            # Server replies and credentials must not leak into UI diagnostics.
            self.failed.emit(f"操作失败（{type(error).__name__}）。请检查账号、网络和日志目录权限。")


def test_remote(config_path, alias):
    server = load_settings(config_path).servers[alias]
    with connect(server) as remote:
        names = []
        sample = None
        for index, item in enumerate(remote.listing(server.root)):
            if index >= 1000:
                break
            name = item["name"]
            if ".log" in name.lower():
                names.append(name)
                if sample is None:
                    sample = valid_child(server.root, name, server.root)
            if len(names) == 5:
                break
        if sample and server.protocol == "sftp":
            with remote.sftp.open(remote.checked(sample), "rb") as stream:
                stream.read(1)
        return ("登录和目录访问成功。\n" +
                ("已验证一个日志文件可读，未保存日志内容。\n" + "\n".join(names)
                 if sample else "该目录没有匹配的日志文件，请检查日志目录。"))


def preview_logs(config_path, alias):
    from .service import LogService
    service = LogService(load_settings(config_path))
    try:
        result = service.list_remote(alias, limit=1000)
        files = list(result["files"])
        cursor = result["next_cursor"]
        while cursor:
            page = service.list_remote(alias, limit=1000, cursor=cursor)
            files.extend(page["files"])
            cursor = page["next_cursor"]
        return {**result, "files": files, "has_more": False, "next_cursor": None,
                "truncated": result["incomplete"], "preview": True}
    finally:
        service.close()


def test_mcp(config_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    entry = mcp_entry(config_path)

    async def run():
        params = StdioServerParameters(command=entry["command"], args=entry["args"], env=dict(os.environ))
        # A windowed EXE has no inherited stderr console handle.
        with open(os.devnull, "w") as diagnostics:
            async with stdio_client(params, errlog=diagnostics) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return f"MCP 启动成功，提供 {len(result.tools)} 个工具。此项未连接日志服务器。"
    return asyncio.run(asyncio.wait_for(run(), timeout=30))


class Window(QMainWindow):
    def __init__(self, home=None):
        super().__init__()
        self.settings = DesktopSettings(home)
        self.worker = None
        self.editing_alias = None
        self.loaded_entry = None
        self.setWindowTitle(f"LogTools · 日志连接管理 {VERSION}")
        self.resize(1080, 900)
        self.setMinimumSize(920, 680)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        heading = QLabel("LogTools")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        layout.addWidget(QLabel("新增或导入项目，连接日志，让 AI 帮你排查问题。"))
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.make_servers()
        self.make_client()
        self.make_storage()
        self.make_fragments()
        self.status = QLabel("准备就绪。可手动新增项目，也可导入服务器配置文件。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.tabs.currentChanged.connect(lambda index: self.status.setText("先列出候选文件并勾选目标日期，再检索保存片段；查看范围与遗漏提示。") if index == 3 else None)
        self.setCentralWidget(container)
        self.refresh()

    def button(self, label, callback, primary=False):
        button = QPushButton(label)
        if primary:
            button.setObjectName("primary")
        button.clicked.connect(lambda: self.guard(callback))
        return button

    def guard(self, callback):
        try:
            callback()
        except Exception as error:
            text = str(error) if isinstance(error, ValueError) and type(error).__name__ != "ValidationError" else f"操作未完成（{type(error).__name__}）。请检查文件及输入内容。"
            QMessageBox.warning(self, "操作未完成", text)

    def make_servers(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.button("新增项目", self.new_project, True))
        toolbar.addWidget(self.button("导入服务器配置", self.import_file))
        toolbar.addWidget(self.button("导出共享配置", self.export_file))
        toolbar.addWidget(self.button("迁移旧版配置", self.migrate))
        toolbar.addStretch()
        layout.addLayout(toolbar)
        tip = QLabel("选中项目即可编辑名称、服务器和端口；新增项目也可手动填写。密码与私钥内容不会导出。")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        split = QSplitter()
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["项目", "服务器", "日志目录"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.selected)
        split.addWidget(self.table)
        panel = QFrame()
        form = QFormLayout(panel)
        form.setContentsMargins(20, 10, 8, 10)
        self.alias = QLineEdit()
        self.alias.setPlaceholderText("英文字母、数字、下划线或连字符")
        self.address = QLineEdit()
        self.address.setPlaceholderText("IP 或主机名，不含 sftp://")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.username = QLineEdit()
        self.root = QLineEdit()
        self.auth = QComboBox()
        self.auth.addItems(["密码认证", "私钥认证"])
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("留空保留已有密码或口令")
        self.key = QLineEdit()
        keyrow = QHBoxLayout()
        keyrow.addWidget(self.key)
        self.key_button = self.button("选择", self.choose_key)
        keyrow.addWidget(self.key_button)
        for label, widget in [("项目", self.alias), ("服务器", self.address), ("端口", self.port),
                              ("账号", self.username), ("日志目录", self.root), ("认证方式", self.auth),
                              ("密码 / 私钥口令", self.password)]:
            form.addRow(label, widget)
        form.addRow("本机私钥", keyrow)
        self.auth.currentIndexChanged.connect(self.update_auth_fields)
        self.update_auth_fields()
        note = QLabel("凭据保存在当前 Windows 用户的凭据管理器中。\n连接不校验服务器主机公钥。")
        note.setWordWrap(True)
        form.addRow(note)
        form.addRow(self.button("保存项目设置", self.save_server, True))
        form.addRow(self.button("测试连接与日志读取", self.start_remote))
        form.addRow(self.button("预览日志文件", self.start_preview))
        editor_tabs = QTabWidget()
        panel.setMinimumHeight(550)
        connection_scroll = QScrollArea()
        connection_scroll.setWidgetResizable(True)
        connection_scroll.setWidget(panel)
        editor_tabs.addTab(connection_scroll, "连接设置")
        scan_panel = QWidget()
        scan_form = QFormLayout(scan_panel)
        self.discovery_enabled = QCheckBox("自动发现历史日志")
        self.history_dirs = QLineEdit()
        self.history_dirs.setPlaceholderText("error, warn, info, debug；留空只扫描根目录")
        self.discovery_depth = QSpinBox()
        self.discovery_depth.setRange(0, 10)
        self.discovery_pattern = QLineEdit()
        self.exclude_dirs = QLineEdit()
        self.discovery_entries = QSpinBox()
        self.discovery_entries.setRange(1, 10000)
        scan_form.addRow(self.discovery_enabled)
        scan_form.addRow("历史目录（逗号分隔）", self.history_dirs)
        scan_form.addRow("最大深度（根目录为 0）", self.discovery_depth)
        scan_form.addRow("日志文件匹配", self.discovery_pattern)
        scan_form.addRow("排除目录（逗号分隔）", self.exclude_dirs)
        scan_form.addRow("最多检查目录项", self.discovery_entries)
        scan_note = QLabel("历史目录填写根目录下的文件夹名。扫描只读取文件信息，不下载正文。\n默认跳过 target 和 .git，不跟随符号链接。文件日期仅用于筛选，排查以日志内容时间为准。")
        scan_note.setWordWrap(True)
        scan_form.addRow(scan_note)
        scan_form.addRow(self.button("保存项目设置", self.save_server, True))
        scan_form.addRow(self.button("预览日志文件", self.start_preview))
        scan_panel.setMinimumHeight(440)
        scan_scroll = QScrollArea()
        scan_scroll.setWidgetResizable(True)
        scan_scroll.setWidget(scan_panel)
        editor_tabs.addTab(scan_scroll, "历史日志扫描")
        split.addWidget(editor_tabs)
        split.setSizes([550, 440])
        layout.addWidget(split, 1)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(120)
        self.output.setPlaceholderText("测试结果会显示在这里。日志正文不会显示或保存。")
        layout.addWidget(self.output)
        self.tabs.addTab(page, "1  服务器与项目")

    def make_client(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel("配置完成后，将本机 MCP 服务接入 AI 客户端。图形界面关闭后仍可使用。")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.client_text = QTextEdit()
        self.client_text.setReadOnly(True)
        self.client_text.setPlainText(json.dumps({"mcpServers": {"log-tools": mcp_entry(self.settings.path)}}, ensure_ascii=False, indent=2))
        layout.addWidget(self.client_text)
        row = QHBoxLayout()
        row.addWidget(self.button("验证 MCP 服务", lambda: self.background(lambda: test_mcp(self.settings.path))))
        row.addWidget(self.button("导出客户端配置", self.export_client))
        row.addWidget(self.button("接入本机 Codex", self.codex, True))
        layout.addLayout(row)
        layout.addWidget(QLabel("接入后请重新启动客户端。已打开的对话可能需要重新加载工具。"))
        self.tabs.addTab(page, "2  AI 客户端接入")

    def make_storage(self):
        page = QWidget()
        layout = QFormLayout(page)
        layout.addRow("版本", QLabel(VERSION))
        layout.addRow("配置位置", QLabel(str(self.settings.path)))
        self.cache = QLineEdit()
        self.cache.setReadOnly(True)
        row = QHBoxLayout()
        row.addWidget(self.cache)
        row.addWidget(self.button("更改", self.choose_cache))
        layout.addRow("下载缓存", row)
        note = QLabel("升级只更新程序。服务器配置、凭据和下载数据会保留。\n更改缓存目录不会搬迁或删除旧日志；运行中的客户端需重启后使用新目录。\n卸载默认保留用户数据，清理日志请在确认不再需要后自行操作。")
        note.setWordWrap(True)
        layout.addRow(note)
        self.tabs.addTab(page, "3  安装与数据")

    def make_fragments(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel("通过现有 SFTP 读取范围，在本机内存检索，只保存命中上下文。Linux 无需改造。历史/完整模式可能传输全部候选内容；尾部模式只覆盖有限范围。")
        note.setWordWrap(True)
        layout.addWidget(note)
        fields = QWidget()
        fields_layout = QHBoxLayout(fields)
        left, right = QFormLayout(), QFormLayout()
        fields_layout.addLayout(left, 1)
        fields_layout.addLayout(right, 1)
        self.fragment_server = QComboBox()
        self.fragment_server.currentIndexChanged.connect(self.clear_fragment_selection)
        self.fragment_mode = QComboBox()
        for name, value in (("最新尾部（有限范围）", "tail"), ("历史时段（选定日期文件）", "history"), ("完整流式（选定文件）", "full")):
            self.fragment_mode.addItem(name, value)
        self.fragment_query, self.fragment_trace, self.fragment_start, self.fragment_end = [QLineEdit() for _ in range(4)]
        self.fragment_query.setPlaceholderText("例如 NullPointerException；按字面匹配")
        self.fragment_trace.setPlaceholderText("可选 traceId")
        self.fragment_start.setPlaceholderText("2026-10-28 09:00:00；历史模式至少填一项时间")
        self.fragment_end.setPlaceholderText("2026-10-28 18:00:00")
        self.fragment_level = QComboBox()
        for name, value in (("ERROR", "ERROR"), ("全部级别", ""), ("WARN", "WARN"), ("INFO", "INFO"), ("DEBUG", "DEBUG"), ("FATAL", "FATAL")):
            self.fragment_level.addItem(name, value)
        for name, widget in (("SFTP 项目", self.fragment_server), ("查询模式", self.fragment_mode),
                             ("关键词", self.fragment_query), ("traceId", self.fragment_trace),
                             ("开始时间", self.fragment_start), ("结束时间", self.fragment_end), ("级别", self.fragment_level)):
            left.addRow(name, widget)
        def spin(low, high, value):
            widget = QSpinBox()
            widget.setRange(low, high)
            widget.setValue(value)
            return widget
        self.fragment_tail = spin(1, 32, 8)
        self.fragment_network = spin(1, 2048, 64)
        self.fragment_file = spin(1, 2048, 32)
        self.fragment_save = spin(1, 16, 2)
        self.fragment_seconds = spin(1, 120, 30)
        self.fragment_before = spin(0, 50, 20)
        self.fragment_after = spin(0, 100, 60)
        for name, widget in (("尾部窗口（MiB）", self.fragment_tail), ("本次网络上限（MiB）", self.fragment_network),
                             ("每文件读取上限（MiB）", self.fragment_file), ("片段保存上限（MiB）", self.fragment_save),
                             ("扫描限时（秒）", self.fragment_seconds), ("前文行数", self.fragment_before), ("后文行数", self.fragment_after)):
            right.addRow(name, widget)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(fields)
        layout.addWidget(scroll, 2)
        row = QHBoxLayout()
        row.addWidget(self.button("列出候选文件", self.start_fragment_listing))
        row.addWidget(self.button("全选候选文件", lambda: self.check_fragment_files(True)))
        row.addWidget(self.button("清空勾选", lambda: self.check_fragment_files(False)))
        row.addStretch()
        row.addWidget(self.button("检索并保存片段", self.start_fragment_collection, True))
        row.addWidget(self.button("继续上次扫描", lambda: self.start_fragment_collection(True)))
        layout.addLayout(row)
        self.fragment_tree = QTreeWidget()
        self.fragment_tree.setHeaderLabels(["勾选待查日志（按文件名选择日期/分卷）", "大小（字节）", "修改时间（UTC）"])
        self.fragment_tree.setColumnWidth(0, 490)
        layout.addWidget(self.fragment_tree, 2)
        self.fragment_result = QTextEdit()
        self.fragment_result.setReadOnly(True)
        self.fragment_result.setPlaceholderText("检索完成后显示网络读取量、本地保存量、未扫描范围、数据集 ID 和少量片段。继续扫描沿用上次条件，可能包含重叠上下文。")
        layout.addWidget(self.fragment_result, 1)
        self.last_fragment_result = self.last_fragment_alias = None
        self.tabs.addTab(page, "4  片段检索")

    def clear_fragment_selection(self):
        if hasattr(self, "fragment_tree"):
            self.fragment_tree.clear()
            self.last_fragment_result = self.last_fragment_alias = None

    def check_fragment_files(self, checked):
        for index in range(self.fragment_tree.topLevelItemCount()):
            self.fragment_tree.topLevelItem(index).setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def start_fragment_listing(self):
        alias = self.fragment_server.currentText()
        if not alias:
            raise ValueError("请先保存一个 SFTP 项目。")
        self.background(lambda: {**preview_logs(self.settings.path, alias), "fragment_listing": True})

    def fragment_arguments(self, resume=False):
        alias = self.fragment_server.currentText()
        if not alias:
            raise ValueError("请先保存一个 SFTP 项目。")
        arguments = {"max_network_bytes": self.fragment_network.value() * 1024**2,
                     "max_file_bytes": self.fragment_file.value() * 1024**2,
                     "max_output_bytes": self.fragment_save.value() * 1024**2,
                     "timeout_seconds": self.fragment_seconds.value()}
        if resume:
            if not self.last_fragment_result or self.last_fragment_alias != alias or not self.last_fragment_result.get("resume_dataset_id"):
                raise ValueError("当前项目没有可继续的扫描；请查看结果中的遗漏/压缩日志提示。")
            arguments["resume_dataset_id"] = self.last_fragment_result["resume_dataset_id"]
        else:
            paths = [self.fragment_tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole)
                     for index in range(self.fragment_tree.topLevelItemCount())
                     if self.fragment_tree.topLevelItem(index).checkState(0) == Qt.CheckState.Checked]
            if not 1 <= len(paths) <= 200:
                raise ValueError("请勾选 1–200 个候选日志；历史查询请按文件名选择目标日期。")
            start, end = self.fragment_start.text().strip() or None, self.fragment_end.text().strip() or None
            from .query import parse_time
            settings = load_settings(self.settings.path)
            times = [parse_time(value, settings.timezone_offset) if value else None for value in (start, end)]
            if times[0] and times[1] and times[0] > times[1]:
                raise ValueError("开始时间不能晚于结束时间。")
            if self.fragment_mode.currentData() == "history" and not (start or end):
                raise ValueError("历史时段模式至少填写开始或结束时间。")
            arguments.update(paths=paths, mode=self.fragment_mode.currentData(), query=self.fragment_query.text(),
                             trace_id=self.fragment_trace.text().strip() or None, start_time=start, end_time=end,
                             level=self.fragment_level.currentData() or None, before=self.fragment_before.value(),
                             after=self.fragment_after.value(), tail_bytes=self.fragment_tail.value() * 1024**2)
        return alias, arguments

    def start_fragment_collection(self, resume=False):
        alias, arguments = self.fragment_arguments(resume)
        def run():
            from .service import LogService
            service = LogService(load_settings(self.settings.path))
            try:
                return {**service.collect_fragments(alias, **arguments), "fragment_collection": True, "server": alias}
            finally:
                service.close()
        self.background(run)

    def refresh(self, selected_alias=None):
        document = self.settings.read()
        current_fragment = self.fragment_server.currentText()
        self.fragment_server.blockSignals(True)
        self.fragment_server.clear()
        self.fragment_server.addItems([name for name, value in document["servers"].items() if value.get("protocol", "sftp") == "sftp"])
        if current_fragment in document["servers"]:
            self.fragment_server.setCurrentText(current_fragment)
        self.fragment_server.blockSignals(False)
        self.clear_fragment_selection()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        selected_row = 0
        for alias, server in document["servers"].items():
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, text in enumerate((alias, server["host"], server["root"])):
                self.table.setItem(row, col, QTableWidgetItem(text))
            if alias == selected_alias:
                selected_row = row
        self.table.blockSignals(False)
        self.cache.setText(str(load_settings(self.settings.path).cache_dir))
        if self.table.rowCount():
            self.table.selectRow(selected_row)
        else:
            self.new_project()

    def new_project(self):
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.editing_alias = None
        self.loaded_entry = None
        self.alias.clear()
        self.address.clear()
        self.port.setValue(22)
        self.username.clear()
        self.root.clear()
        self.load_discovery({})
        self.root.setPlaceholderText("SFTP 内的绝对目录，例如 /sdscmcontract")
        self.auth.setCurrentIndex(0)
        self.password.clear()
        self.key.clear()
        self.output.clear()
        self.alias.setFocus()
        self.status.setText("正在新增项目。填写名称、服务器、端口、账号和日志目录，然后保存。")

    def load_discovery(self, value):
        settings = DiscoverySettings.model_validate(value)
        self.discovery_enabled.setChecked(settings.enabled)
        self.history_dirs.setText(", ".join(settings.history_dirs))
        self.discovery_depth.setValue(settings.max_depth)
        self.discovery_pattern.setText(settings.pattern)
        self.exclude_dirs.setText(", ".join(settings.exclude_dirs))
        self.discovery_entries.setValue(settings.max_entries)

    def discovery_values(self):
        def names(field):
            return list(dict.fromkeys(value.strip() for value in field.text().replace("，", ",").split(",") if value.strip()))
        return DiscoverySettings(enabled=self.discovery_enabled.isChecked(),
                                 history_dirs=names(self.history_dirs),
                                 max_depth=self.discovery_depth.value(),
                                 pattern=self.discovery_pattern.text().strip(),
                                 exclude_dirs=names(self.exclude_dirs),
                                 max_entries=self.discovery_entries.value()).model_dump()

    def update_auth_fields(self):
        using_key = self.auth.currentIndex() == 1
        self.key.setEnabled(using_key)
        self.key_button.setEnabled(using_key)
        self.key.setPlaceholderText("选择本机私钥文件" if using_key else "密码认证无需填写")
        self.password.setPlaceholderText("私钥有口令时填写；留空保留已有口令" if using_key else "留空保留该服务器账号的已有密码")

    def selected(self):
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            return
        alias = self.table.item(row, 0).text()
        server = self.settings.read()["servers"][alias]
        self.editing_alias = alias
        self.loaded_entry = dict(server)
        self.alias.setText(alias)
        self.address.setText(server["host"])
        self.port.setValue(server.get("port") or 22)
        self.username.setText(server["username"])
        self.root.setText(server["root"])
        self.load_discovery(server.get("discovery", {}))
        self.auth.setCurrentIndex(1 if server.get("private_key") is not None else 0)
        self.key.setText(server.get("private_key") or "")
        self.password.clear()
        self.output.clear()
        self.status.setText(f"正在编辑项目 {alias}，修改后点击“保存项目设置”。")

    def import_profile(self, profile):
        existing = set(self.settings.read()["servers"]) & set(profile.servers)
        replace = False
        if existing:
            replace = QMessageBox.question(self, "替换同名项目", "以下项目已存在，是否替换连接配置？\n" + ", ".join(sorted(existing))) == QMessageBox.StandardButton.Yes
            if not replace:
                return False
        count = self.settings.import_profile(profile, replace=replace)
        self.refresh()
        self.status.setText(f"已导入 {count} 个日志项目。请设置本机凭据，再测试连接。")
        return True

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入服务器配置", "", "LogTools 配置 (*.json)")
        if path:
            self.import_profile(read_profile(path))

    def export_file(self):
        document = self.settings.export_profile()
        path, _ = QFileDialog.getSaveFileName(self, "导出服务器与日志项目（不含凭据）", "servers.logtools.json", "JSON (*.json)")
        if path:
            with Path(path).open("x", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, indent=2)
            self.status.setText("共享配置已导出，包含服务器地址和日志项目，不包含密码与私钥。请交给获授权的接收方。")

    def migrate(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择旧版 config.json", "", "JSON (*.json)")
        if not path:
            return
        settings = load_settings(path)
        if not self.import_profile(profile_from_settings(settings)):
            return
        for alias, old in settings.servers.items():
            entry = self.settings.read()["servers"][alias]
            for field in ("password_env", "private_key", "key_passphrase_env", "password_credential", "key_passphrase_credential", "timeout_seconds", "encoding"):
                value = getattr(old, field)
                if value:
                    entry[field] = value
            self.settings.update_server(alias, entry)
        with self.settings.lock():
            original = self.settings.path.read_bytes()
            document = json.loads(original)
            document.update(settings.model_dump(mode="json", exclude={"servers"}))
            self.settings.save(document, original)
        self.refresh()
        self.status.setText("旧配置已迁移，继续使用原日志缓存和本机凭据引用。原文件没有修改。")

    def choose_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择本机私钥")
        if path:
            self.key.setText(path)

    def save_server(self):
        alias = self.alias.text().strip()
        if not alias:
            raise ValueError("请填写项目名称。")
        entry = dict(self.loaded_entry or {"protocol": "sftp"})
        host = self.address.text().strip()
        username = self.username.text().strip()
        if not host or not username or not self.root.text().strip():
            raise ValueError("请填写服务器地址、账号和日志目录。")
        identity_changed = (entry.get("host"), entry.get("port") or 22, entry.get("username")) != (host, self.port.value(), username)
        if identity_changed:
            for field in ("password_env", "password_credential", "key_passphrase_env", "key_passphrase_credential"):
                entry.pop(field, None)
        entry.update(host=host, port=self.port.value(), username=username, root=self.root.text().strip())
        value = self.password.text()
        target = None
        if self.auth.currentIndex() == 0:
            for field in ("private_key", "key_passphrase_env", "key_passphrase_credential"):
                entry.pop(field, None)
            if not entry.get("password_credential") and not entry.get("password_env"):
                entry["password_credential"] = credential_target(entry)
            if value:
                target = entry.get("password_credential") or credential_target(entry)
                entry["password_credential"] = target
        else:
            key = Path(self.key.text())
            if not key.is_absolute() or not key.is_file():
                raise ValueError("请选择本机已存在的私钥文件。")
            entry["private_key"] = str(key)
            entry.pop("password_env", None)
            entry.pop("password_credential", None)
            if value:
                target = entry.get("key_passphrase_credential") or credential_target(entry, kind="passphrase")
                entry["key_passphrase_credential"] = target
        entry["discovery"] = self.discovery_values()
        ServerConfig.model_validate(entry)
        self.settings.save_project(alias, entry, previous_alias=self.editing_alias,
                                   expected_entry=self.loaded_entry,
                                   save_credentials=(lambda: write_credential(target, value)) if target else None)
        self.password.clear()
        self.refresh(selected_alias=alias)
        self.status.setText(f"已保存项目 {alias}。更换服务器或账号后请填写对应凭据；已运行的 MCP 客户端需重启。")

    def background(self, operation):
        if self.worker and self.worker.isRunning():
            raise ValueError("当前测试尚未结束，请稍候。")
        self.worker = Worker(operation)
        self.worker.completed.connect(self.show_result)
        self.worker.failed.connect(self.show_result)
        self.worker.start()
        self.status.setText("正在连接或扫描，请稍候……")

    def show_result(self, result):
        if isinstance(result, dict) and result.get("fragment_listing"):
            if result["server"] != self.fragment_server.currentText():
                return
            self.fragment_tree.clear()
            for file in result["files"]:
                if not file["name"].endswith((".log", ".log.gz")):
                    continue
                item = QTreeWidgetItem(self.fragment_tree, [file["relative_path"], str(file["size"]), file["modified_at"] or "未知"])
                item.setData(0, Qt.ItemDataRole.UserRole, file["remote_path"])
                item.setCheckState(0, Qt.CheckState.Unchecked)
            self.fragment_result.setPlainText(json.dumps({"files": self.fragment_tree.topLevelItemCount(),
                "incomplete": result["incomplete"], "warnings": result["warnings"], "skipped": result["skipped_directories"]}, ensure_ascii=False, indent=2))
            self.status.setText("候选文件已列出；请勾选目标日期/分卷，检查扫描遗漏提示。")
            return
        if isinstance(result, dict) and result.get("fragment_collection"):
            self.last_fragment_result, self.last_fragment_alias = result, result["server"]
            self.fragment_result.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
            state = result["collection"]
            self.status.setText(f"片段已保存：网络读取 {state['network_bytes']} 字节，本地保存 {state['saved_bytes']} 字节。" + ("扫描未完整覆盖，请查看提示或继续扫描。" if state['incomplete'] else "已完成本次选定范围。"))
            return
        if isinstance(result, dict) and result.get("preview"):
            self.show_preview(result)
            return
        self.output.setPlainText(str(result))
        if self.tabs.currentIndex() == 3:
            self.fragment_result.setPlainText(str(result))
        self.status.setText(str(result).splitlines()[0])

    def start_remote(self):
        self.save_server()
        alias = self.alias.text()
        self.background(lambda: test_remote(self.settings.path, alias))

    def start_preview(self):
        self.save_server()
        alias = self.alias.text()
        self.background(lambda: preview_logs(self.settings.path, alias))

    def show_preview(self, result):
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(f"日志文件预览 · {result['server']}")
        dialog.resize(900, 650)
        layout = QVBoxLayout(dialog)
        state = "扫描未完整完成，请查看遗漏提示" if result["incomplete"] else "已完成配置范围内的扫描"
        summary = f"{state}。发现 {result['total_files']} 个日志文件，检查 {result['scanned_directories']} 个目录。未下载日志正文。"
        label = QLabel(summary)
        label.setWordWrap(True)
        layout.addWidget(label)
        tree = QTreeWidget()
        tree.setHeaderLabels(["目录 / 文件", "大小（字节）", "修改时间（UTC）"])
        nodes = {}
        for row in result["files"]:
            parts = row["relative_path"].split("/")
            parent = tree.invisibleRootItem()
            for index, name in enumerate(parts[:-1]):
                key = "/".join(parts[:index + 1])
                if key not in nodes:
                    nodes[key] = QTreeWidgetItem(parent, [name])
                parent = nodes[key]
            QTreeWidgetItem(parent, [parts[-1], str(row["size"]), row["modified_at"] or "未知"])
        tree.expandToDepth(0)
        tree.setColumnWidth(0, 430)
        tree.setColumnWidth(1, 130)
        layout.addWidget(tree, 1)
        details = QTextEdit()
        details.setReadOnly(True)
        details.setMaximumHeight(150)
        reasons = {"excluded": "按设置排除", "recursive_disabled": "未启用递归", "outside_history_dirs": "不在历史目录列表中",
                   "depth_limit": "达到最大扫描深度", "entry_limit": "达到目录项数量上限", "time_limit": "达到扫描时间上限",
                   "symlink_or_unsupported_type": "符号链接或不支持的类型", "invalid_child_name": "无效文件名"}
        lines = ["扫描根目录：" + result["directory"]]
        for item in result["warnings"] + result["skipped_directories"]:
            reason = item["reason"]
            message = "目录无法读取（权限或网络错误）" if reason.startswith("directory_unreadable:") else reasons.get(reason, reason)
            lines.append(f"{item['path']}：{message}")
        if result["diagnostics_truncated"]:
            lines.append("提示数量较多，以上只显示部分；请缩小扫描范围。")
        details.setPlainText("\n".join(lines))
        layout.addWidget(details)
        self.preview_dialog = dialog
        dialog.show()
        self.output.setPlainText(summary)
        self.status.setText(summary)

    def choose_cache(self):
        path = QFileDialog.getExistingDirectory(self, "选择日志缓存目录")
        if path:
            self.settings.set_cache(path)
            self.cache.setText(path)
            self.status.setText("已更新缓存位置，旧日志未搬迁或删除。请重启 MCP 客户端。")

    def export_client(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出客户端配置", "mcp-client.local.json", "JSON (*.json)")
        if path:
            entry = mcp_entry(self.settings.path)
            with Path(path).open("x", encoding="utf-8") as stream:
                json.dump({"mcpServers": {"log-tools": {k: entry[k] for k in ("command", "args")}}}, stream, ensure_ascii=False, indent=2)
            self.status.setText("已导出本机客户端配置，安装路径改变时需要重新生成。")

    def codex(self):
        path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        if QMessageBox.question(self, "接入 Codex", f"将更新以下配置中的 log-tools 条目，并保留其他设置：\n{path}\n继续吗？") != QMessageBox.StandardButton.Yes:
            return
        register_codex(self.settings.path, path, replace=True)
        self.status.setText("已接入 Codex，原配置已备份。请重启 Codex 后在新对话中测试。")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "测试进行中", "请等待连接测试完成后关闭窗口。")
            event.ignore()
        else:
            event.accept()


STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; color: #1d3046; }
QMainWindow { background: #f3f6fa; }
QLabel#heading { font-size: 30px; font-weight: 700; color: #143e66; }
QTabWidget::pane { background: white; border: 1px solid #dae2ec; border-radius: 8px; }
QTabBar::tab { padding: 13px 23px; margin-top: 12px; background: #e6edf5; }
QTabBar::tab:selected { background: white; color: #125f9e; }
QPushButton { padding: 9px 14px; background: #edf3f9; border: 1px solid #cbd9e8; border-radius: 6px; }
QPushButton:hover { background: #dceaf7; }
QPushButton#primary { background: #1769aa; color: white; border: 1px solid #1769aa; }
QLineEdit, QSpinBox, QComboBox { padding: 7px; border: 1px solid #cbd6e2; border-radius: 4px; background: white; }
QLineEdit:read-only { background: #f4f7fa; }
QTableWidget, QTextEdit { border: 1px solid #dce4ed; background: white; selection-background-color: #e2effb; selection-color: #163855; }
QHeaderView::section { background: #edf3f9; padding: 10px; border: none; font-weight: 600; }
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path)
    parser.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--screenshot-tab", choices=["servers", "fragments"], default="servers", help=argparse.SUPPRESS)
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    if args.screenshot and os.name == "nt":
        # Qt's offscreen plugin does not enumerate Windows fonts automatically.
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLE)
    window = Window(args.home)
    window.show()
    if args.screenshot_tab == "fragments":
        window.tabs.setCurrentIndex(3)
    if args.screenshot:
        app.processEvents()
        window.grab().save(str(args.screenshot))
        return
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
