import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk


ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

# Palette (light, dark)
_BG    = ("gray94", "gray14")
_CARD  = ("white",  "gray20")
_ROW   = ("gray95", "gray17")
_MUTED = ("gray45", "gray62")


class ControlPanel:
    """Shared CustomTkinter control panel for macOS and Windows."""

    def __init__(self, controller, title="UniPaste", on_quit_callback=None):
        self.controller = controller
        self.title = title
        self.on_quit_callback = on_quit_callback
        self.root = None
        self._refresh_job = None
        self._last_focus_token = None
        self._autostart_enabled = None
        self._autostart_supported = False
        self._dismissed_transfers: set = set()

    def run(self):
        self.root = ctk.CTk()
        self.root.title(self.title)
        self.root.geometry("920x660")
        self.root.minsize(820, 560)
        self.root.configure(fg_color=_BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._configure_window_icon()
        self._build_ui()
        self._refresh()
        self.root.mainloop()

    def focus(self):
        if not self.root:
            return
        self.root.after(0, self._focus_window)

    def _build_ui(self):
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        container = ctk.CTkFrame(self.root, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)
        container.grid_columnconfigure(0, weight=1)
        # row 0: header
        # row 1: actions
        # row 2: hint_label
        # row 3: peer_sections  ← expandable
        # row 4: pairing        ← expandable
        # row 5: transfer_frame
        # row 6: error_frame
        container.grid_rowconfigure(3, weight=1)
        container.grid_rowconfigure(4, weight=1)

        # ── Header ────────────────────────────────────────────────────────────
        header = ctk.CTkFrame(container, corner_radius=14, fg_color=_CARD)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="UniPaste",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 3))

        self.device_label = ctk.CTkLabel(
            header,
            text="设备: -",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.device_label.grid(row=1, column=0, sticky="w", padx=18)

        detail_row = ctk.CTkFrame(header, fg_color="transparent")
        detail_row.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(3, 14))
        detail_row.grid_columnconfigure(0, weight=1)

        self.detail_label = ctk.CTkLabel(
            detail_row,
            text="后台服务: 启动中",
            text_color=_MUTED,
            font=ctk.CTkFont(size=12),
            anchor="w",
        )
        self.detail_label.grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            detail_row,
            text="复制 ID",
            width=68,
            height=24,
            corner_radius=6,
            fg_color=_ROW,
            text_color=("gray20", "gray85"),
            hover_color=("gray88", "gray28"),
            border_width=1,
            border_color=("gray80", "gray32"),
            font=ctk.CTkFont(size=11),
            command=self._copy_device_id,
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.status_badge = ctk.CTkLabel(
            header,
            text="启动中",
            corner_radius=999,
            padx=12,
            pady=5,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.status_badge.grid(row=0, column=1, rowspan=2, sticky="e", padx=18, pady=16)

        # ── Actions ───────────────────────────────────────────────────────────
        actions = ctk.CTkFrame(container, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        actions.grid_columnconfigure(0, weight=1)

        self.autostart_button = ctk.CTkButton(
            actions,
            text="自动启动",
            command=self._toggle_autostart,
            height=34,
            width=155,
            corner_radius=10,
        )
        self.autostart_button.grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            actions,
            text="刷新",
            command=self._refresh,
            height=34,
            width=80,
            corner_radius=10,
            fg_color=_CARD,
            text_color=("gray15", "gray90"),
            hover_color=("gray88", "gray28"),
            border_width=1,
            border_color=("gray80", "gray32"),
        ).grid(row=0, column=1, sticky="e", padx=(0, 8))

        ctk.CTkButton(
            actions,
            text="退出",
            command=self._quit_service,
            height=34,
            width=80,
            corner_radius=10,
            fg_color="#C0392B",
            hover_color="#96281B",
        ).grid(row=0, column=2, sticky="e")

        # ── Hint ──────────────────────────────────────────────────────────────
        self.hint_label = ctk.CTkLabel(
            container,
            text="首次连接时，由收到连接请求的设备确认配对。",
            justify="left",
            anchor="w",
            text_color=_MUTED,
            font=ctk.CTkFont(size=11),
        )
        self.hint_label.grid(row=2, column=0, sticky="ew", pady=(10, 0))

        # ── Peer sections (Connected + Discovered) ────────────────────────────
        peer_sections = ctk.CTkFrame(container, fg_color="transparent")
        peer_sections.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        peer_sections.grid_columnconfigure(0, weight=1)
        peer_sections.grid_columnconfigure(1, weight=1)
        peer_sections.grid_rowconfigure(0, weight=1)

        self.connected_title, self.connected_body = self._build_section(
            peer_sections, row=0, column=0,
            title="已连接设备",
            subtitle="已建立同步",
        )
        self.discovered_title, self.discovered_body = self._build_section(
            peer_sections, row=0, column=1,
            title="已发现设备",
            subtitle="连接方向由设备 ID 哈希决定",
        )

        # ── Pending pairings ──────────────────────────────────────────────────
        self.pairing_title, self.pairing_body = self._build_section(
            container, row=4, column=0,
            title="待处理配对",
            subtitle="只有首次入站连接会出现在这里",
        )

        # ── Transfer toasts ───────────────────────────────────────────────────
        self.transfer_frame = ctk.CTkFrame(container, corner_radius=14, fg_color=_CARD)
        self.transfer_frame.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.transfer_frame.grid_columnconfigure(0, weight=1)
        self.transfer_frame.grid_remove()

        transfer_header = ctk.CTkFrame(self.transfer_frame, fg_color="transparent")
        transfer_header.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 0))
        transfer_header.grid_columnconfigure(0, weight=1)

        self.transfer_title = ctk.CTkLabel(
            transfer_header,
            text="文件传输",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.transfer_title.grid(row=0, column=0, sticky="w")

        self.transfer_body = ctk.CTkFrame(self.transfer_frame, fg_color="transparent")
        self.transfer_body.grid(row=1, column=0, sticky="ew", padx=10, pady=(6, 10))
        self.transfer_body.grid_columnconfigure(0, weight=1)

        # ── Error banner ──────────────────────────────────────────────────────
        self.error_frame = ctk.CTkFrame(
            container, corner_radius=12,
            fg_color=("#FEE4E2", "#49211D"),
        )
        self.error_frame.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        self.error_frame.grid_columnconfigure(0, weight=1)
        self.error_label = ctk.CTkLabel(
            self.error_frame,
            text="",
            justify="left",
            anchor="w",
            wraplength=840,
            text_color=("#7A271A", "#FFD7D3"),
            font=ctk.CTkFont(size=12),
        )
        self.error_label.grid(row=0, column=0, sticky="ew", padx=14, pady=10)
        self.error_frame.grid_remove()

    def _build_section(self, parent, row, column, title, subtitle):
        padx = (0, 5) if column == 0 else (5, 0)
        frame = ctk.CTkFrame(parent, corner_radius=14, fg_color=_CARD)
        frame.grid(row=row, column=column, sticky="nsew", padx=padx, pady=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 0))
        header.grid_columnconfigure(0, weight=1)

        title_label = ctk.CTkLabel(
            header,
            text=title,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        title_label.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            header,
            text=subtitle,
            text_color=_MUTED,
            font=ctk.CTkFont(size=11),
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        body = ctk.CTkScrollableFrame(frame, corner_radius=10, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        body.grid_columnconfigure(0, weight=1)
        return title_label, body

    def _configure_window_icon(self):
        if not self.root:
            return

        root_dir = Path(__file__).resolve().parents[1]
        assets_dir = root_dir / "assets"
        png_icon = assets_dir / "unipaste.png"
        ico_icon = assets_dir / "unipaste.ico"

        try:
            if ico_icon.exists():
                self.root.iconbitmap(default=str(ico_icon))
        except Exception:
            pass

        try:
            if png_icon.exists():
                icon = tk.PhotoImage(file=str(png_icon))
                self.root.iconphoto(True, icon)
                self.root._icon_image = icon
        except Exception:
            pass

    def _focus_window(self):
        if not self.root:
            return

        self.root.deiconify()
        self.root.lift()

        if sys.platform == "darwin":
            try:
                self.root.update_idletasks()
            except Exception:
                pass
            return

        try:
            self.root.attributes("-topmost", True)
            self.root.after(120, lambda: self.root and self.root.attributes("-topmost", False))
        except Exception:
            pass

        try:
            self.root.focus_force()
        except Exception:
            pass

    def _refresh(self):
        if not self.root:
            return

        if self._refresh_job:
            try:
                self.root.after_cancel(self._refresh_job)
            except Exception:
                pass
            finally:
                self._refresh_job = None

        snapshot = self.controller.get_ui_snapshot() or {}
        connected = snapshot.get("connected_peers", [])
        discovered = snapshot.get("discovered_peers", [])
        pending = snapshot.get("pending_pairings", [])
        active_transfers = snapshot.get("active_transfers", [])

        platform = snapshot.get("platform", "unknown")
        device_name = snapshot.get("device_name", "Unknown Device")
        device_id = snapshot.get("device_id", "unknown")
        status_text = snapshot.get("status_text", "未知状态")
        thread_alive = snapshot.get("thread_alive", True)

        self.device_label.configure(text=f"{device_name} ({platform})")
        self.detail_label.configure(
            text=f"设备 ID: {device_id}  ·  后台服务: {'运行中' if thread_alive else '未运行'}"
        )
        self.status_badge.configure(
            text=status_text,
            fg_color=self._status_color(status_text),
            text_color="white",
        )

        self.connected_title.configure(text=f"已连接设备 ({len(connected)})")
        self.discovered_title.configure(text=f"已发现设备 ({len(discovered)})")
        self.pairing_title.configure(text=f"待处理配对 ({len(pending)})")

        self._autostart_enabled = snapshot.get("autostart_enabled")
        self._autostart_supported = self._autostart_enabled is not None
        self._update_autostart_button()

        self.hint_label.configure(text=self._pairing_hint(platform))

        self._render_connected_peers(connected)
        self._render_discovered_peers(discovered)
        self._render_pairings(pending)
        self._render_transfer_toasts(active_transfers)
        self._render_error(snapshot.get("startup_error") or snapshot.get("last_error"))
        self._maybe_focus_from_snapshot(snapshot)

        self._refresh_job = self.root.after(1000, self._refresh)

    def _update_autostart_button(self):
        if not self._autostart_supported:
            self.autostart_button.configure(
                text="自动启动: 不支持",
                state="disabled",
                fg_color=_ROW,
                text_color=_MUTED,
            )
            return

        self.autostart_button.configure(state="normal")
        if self._autostart_enabled:
            self.autostart_button.configure(
                text="自动启动: 已开启",
                fg_color="#27AE60",
                hover_color="#1E8449",
                text_color="white",
            )
        else:
            self.autostart_button.configure(
                text="自动启动: 已关闭",
                fg_color=_CARD,
                hover_color=("gray88", "gray28"),
                text_color=("gray15", "gray90"),
            )

    def _maybe_focus_from_snapshot(self, snapshot):
        focus_token = snapshot.get("panel_focus_token")
        if focus_token is None or focus_token == self._last_focus_token:
            return
        self._last_focus_token = focus_token
        self.focus()

    def _render_connected_peers(self, peers):
        self._clear_children(self.connected_body)
        if not peers:
            self._render_empty_state(self.connected_body, "当前没有活动连接")
            return

        for peer in peers:
            direction = "本机发起" if peer.get("will_initiate") else "对端发起"
            subtitle = peer.get("url") or "地址未知"
            self._render_row(
                self.connected_body,
                title=peer["peer_id"],
                subtitle=f"{peer.get('platform', 'unknown')}  ·  {direction}",
                trailing=subtitle,
                trailing_color=_MUTED,
            )

    def _render_discovered_peers(self, peers):
        self._clear_children(self.discovered_body)
        if not peers:
            self._render_empty_state(self.discovered_body, "尚未发现其他设备")
            return

        for peer in peers:
            if peer.get("connected"):
                state_text = "已连接"
                state_color = "#27AE60"
            elif peer.get("retry_in"):
                state_text = f"{peer['retry_in']:.0f}s 后重试"
                state_color = "#E67E22"
            elif peer.get("will_initiate"):
                state_text = "本机发起"
                state_color = "#2980B9"
            else:
                state_text = "等待对端"
                state_color = _MUTED[0]

            self._render_row(
                self.discovered_body,
                title=peer["peer_id"],
                subtitle=f"{peer.get('platform', 'unknown')}  ·  {peer.get('url') or '地址未知'}",
                trailing=state_text,
                trailing_color=state_color,
            )

    def _render_pairings(self, pending):
        self._clear_children(self.pairing_body)
        if not pending:
            self._render_empty_state(self.pairing_body, "当前没有待处理的首次连接请求")
            return

        for request in pending:
            card = ctk.CTkFrame(self.pairing_body, corner_radius=10, fg_color=_ROW)
            card.pack(fill="x", pady=4)
            card.grid_columnconfigure(0, weight=1)

            body = ctk.CTkFrame(card, fg_color="transparent")
            body.grid(row=0, column=0, sticky="ew", padx=12, pady=10)
            body.grid_columnconfigure(0, weight=1)

            platform = request.get("platform", "")
            platform_prefix = "🍎 " if platform == "macos" else "🪟 " if platform == "windows" else ""
            ctk.CTkLabel(
                body,
                text=f"{platform_prefix}{request['device_name']}",
                font=ctk.CTkFont(size=13, weight="bold"),
            ).grid(row=0, column=0, sticky="w")

            ctk.CTkLabel(
                body,
                text=f"{request['platform']}  ·  {request['ip_address']}  ·  {request['device_id']}",
                text_color=_MUTED,
                font=ctk.CTkFont(size=11),
            ).grid(row=1, column=0, sticky="w", pady=(3, 0))

            btn_row = ctk.CTkFrame(body, fg_color="transparent")
            btn_row.grid(row=0, column=1, rowspan=2, sticky="e", padx=(10, 0))

            ctk.CTkButton(
                btn_row,
                text="同意",
                width=72,
                height=30,
                corner_radius=8,
                fg_color="#27AE60",
                hover_color="#1E8449",
                command=lambda rid=request["device_id"]: self._accept_pairing(rid),
            ).pack(side="left")
            ctk.CTkButton(
                btn_row,
                text="拒绝",
                width=72,
                height=30,
                corner_radius=8,
                fg_color=_CARD,
                text_color=("gray20", "gray85"),
                hover_color=("gray88", "gray28"),
                border_width=1,
                border_color=("gray75", "gray38"),
                command=lambda rid=request["device_id"]: self._reject_pairing(rid),
            ).pack(side="left", padx=(6, 0))

    def _render_transfer_toasts(self, transfers):
        # Clean dismissed set — remove IDs no longer active
        active_ids = {t["transfer_id"] for t in transfers}
        self._dismissed_transfers &= active_ids

        visible = [t for t in transfers if t["transfer_id"] not in self._dismissed_transfers]

        if not visible:
            self.transfer_frame.grid_remove()
            return

        self.transfer_title.configure(text=f"文件传输 ({len(visible)})")
        self.transfer_frame.grid()
        self._clear_children(self.transfer_body)

        for t in visible:
            self._render_transfer_row(self.transfer_body, t)

    def _render_transfer_row(self, parent, transfer):
        tid = transfer["transfer_id"]
        direction = transfer.get("direction", "receive")
        icon = "📥" if direction == "receive" else "📤"
        pct = transfer.get("percent", 0)
        filename = transfer.get("filename", "")
        if len(filename) > 34:
            filename = filename[:31] + "…"
        file_size = transfer.get("file_size", 0)
        received = transfer.get("received_bytes", 0)
        peer_id = transfer.get("peer_id") or ""

        def _fmt_bytes(n):
            if n >= 1024 * 1024:
                return f"{n / 1024 / 1024:.1f} MB"
            if n >= 1024:
                return f"{n / 1024:.0f} KB"
            return f"{n} B"

        row = ctk.CTkFrame(parent, corner_radius=10, fg_color=_ROW)
        row.pack(fill="x", pady=3)
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            row, text=icon, font=ctk.CTkFont(size=18),
        ).grid(row=0, column=0, rowspan=3, padx=(12, 8), pady=10, sticky="w")

        ctk.CTkLabel(
            row, text=filename,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="ew", pady=(10, 0))

        size_str = f"{_fmt_bytes(received)} / {_fmt_bytes(file_size)}" if file_size else ""
        peer_str = f"  ·  {peer_id}" if peer_id else ""
        ctk.CTkLabel(
            row, text=f"{size_str}{peer_str}",
            text_color=_MUTED,
            font=ctk.CTkFont(size=10),
            anchor="w",
        ).grid(row=1, column=1, sticky="ew")

        progress_bar = ctk.CTkProgressBar(row, height=5, corner_radius=3)
        progress_bar.set(pct / 100)
        progress_bar.grid(row=2, column=1, sticky="ew", pady=(3, 10))

        ctk.CTkLabel(
            row, text=f"{pct}%",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=_MUTED,
            width=36,
            anchor="e",
        ).grid(row=0, column=2, rowspan=3, padx=(4, 4), sticky="e")

        ctk.CTkButton(
            row,
            text="×",
            width=26, height=26,
            corner_radius=13,
            fg_color="transparent",
            text_color=_MUTED,
            hover_color=("gray85", "gray30"),
            command=lambda t=tid: self._dismiss_transfer(t),
        ).grid(row=0, column=3, rowspan=3, padx=(0, 8), sticky="e")

    def _dismiss_transfer(self, transfer_id: str):
        self._dismissed_transfers.add(transfer_id)
        self._refresh()

    def _render_row(self, parent, title, subtitle, trailing, trailing_color):
        row = ctk.CTkFrame(parent, corner_radius=10, fg_color=_ROW)
        row.pack(fill="x", pady=4)
        row.grid_columnconfigure(0, weight=1)

        display_title = title if len(title) <= 26 else title[:23] + "…"
        ctk.CTkLabel(
            row,
            text=display_title,
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(9, 2))

        ctk.CTkLabel(
            row,
            text=subtitle,
            justify="left",
            anchor="w",
            text_color=_MUTED,
            font=ctk.CTkFont(size=11),
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 9))

        ctk.CTkLabel(
            row,
            text=trailing,
            text_color=trailing_color,
            font=ctk.CTkFont(size=11, weight="bold"),
        ).grid(row=0, column=1, rowspan=2, sticky="e", padx=12)

    def _render_empty_state(self, parent, text):
        ctk.CTkLabel(
            parent,
            text=text,
            justify="left",
            anchor="w",
            text_color=_MUTED,
            font=ctk.CTkFont(size=12),
        ).pack(fill="x", pady=8, padx=2)

    def _render_error(self, message):
        if message:
            self.error_label.configure(text=f"最近错误: {message}")
            self.error_frame.grid()
        else:
            self.error_frame.grid_remove()

    def _pairing_hint(self, platform: str) -> str:
        base = "发现设备不会弹提醒；只有首次连接且收到对方主动接入的一端需要确认配对。"
        if platform == "windows":
            return f"{base} Windows 收到配对请求时会自动打开控制面板。"
        if platform == "macos":
            return f"{base} macOS 收到配对请求时会发系统通知，并自动拉起控制面板。"
        return base

    def _clear_children(self, widget):
        for child in widget.winfo_children():
            child.destroy()

    def _copy_device_id(self):
        snapshot = self.controller.get_ui_snapshot() or {}
        device_id = snapshot.get("device_id", "")
        if device_id and self.root:
            self.root.clipboard_clear()
            self.root.clipboard_append(device_id)

    def _toggle_autostart(self):
        if not self._autostart_supported:
            return
        if self._autostart_enabled:
            ok, message = self.controller.remove_autostart()
        else:
            ok, message = self.controller.install_autostart()
        if ok:
            messagebox.showinfo("UniPaste", message)
        else:
            messagebox.showerror("UniPaste", message)
        self._refresh()

    def _accept_pairing(self, device_id):
        if self.controller.accept_pairing_request(device_id):
            self._refresh()

    def _reject_pairing(self, device_id):
        if self.controller.reject_pairing_request(device_id):
            self._refresh()

    def _quit_service(self):
        self.controller.stop()
        if self.on_quit_callback:
            self.on_quit_callback()
        self._on_close()

    def _on_close(self):
        if self.root and self._refresh_job:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None
        if self.root:
            self.root.destroy()
            self.root = None

    @staticmethod
    def _status_color(status_text: str) -> str:
        if "已连接" in status_text:
            return "#27AE60"
        if "连接" in status_text or "发现" in status_text:
            return "#2980B9"
        if "已停止" in status_text or "停止" in status_text or "错误" in status_text:
            return "#C0392B"
        return "#7F8C8D"
