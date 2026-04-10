import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk


ctk.set_appearance_mode("system")
ctk.set_default_color_theme("green")


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

    def run(self):
        self.root = ctk.CTk()
        self.root.title(self.title)
        self.root.geometry("920x660")
        self.root.minsize(820, 560)
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
        container.grid(row=0, column=0, sticky="nsew", padx=18, pady=18)
        container.grid_columnconfigure(0, weight=1)
        # row 0: header
        # row 1: actions
        # row 2: hint_label
        # row 3: peer_sections  ← expandable
        # row 4: pairing        ← expandable
        # row 5: error_frame
        container.grid_rowconfigure(3, weight=1)
        container.grid_rowconfigure(4, weight=1)

        # ── Header ────────────────────────────────────────────────────────────
        header = ctk.CTkFrame(container, corner_radius=18)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="UniPaste",
            font=ctk.CTkFont(size=24, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 4))

        self.device_label = ctk.CTkLabel(
            header,
            text="设备: -",
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        self.device_label.grid(row=1, column=0, sticky="w", padx=18)

        self.detail_label = ctk.CTkLabel(
            header,
            text="后台服务: 启动中",
            text_color=("gray35", "gray70"),
            font=ctk.CTkFont(size=12),
        )
        self.detail_label.grid(row=2, column=0, sticky="w", padx=18, pady=(4, 14))

        ctk.CTkButton(
            header,
            text="复制 ID",
            width=72,
            height=26,
            corner_radius=8,
            fg_color=("gray84", "gray24"),
            text_color=("gray12", "white"),
            hover_color=("gray78", "gray30"),
            font=ctk.CTkFont(size=11),
            command=self._copy_device_id,
        ).grid(row=2, column=1, sticky="e", padx=(0, 18), pady=(4, 14))

        self.status_badge = ctk.CTkLabel(
            header,
            text="启动中",
            corner_radius=999,
            padx=12,
            pady=6,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.status_badge.grid(row=0, column=1, rowspan=2, sticky="e", padx=18, pady=16)

        # ── Actions ───────────────────────────────────────────────────────────
        actions = ctk.CTkFrame(container, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        actions.grid_columnconfigure(0, weight=1)

        self.autostart_button = ctk.CTkButton(
            actions,
            text="自动启动",
            command=self._toggle_autostart,
            height=36,
            width=160,
            corner_radius=12,
        )
        self.autostart_button.grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            actions,
            text="刷新",
            command=self._refresh,
            height=36,
            width=84,
            corner_radius=12,
            fg_color=("gray84", "gray24"),
            text_color=("gray12", "white"),
            hover_color=("gray78", "gray30"),
        ).grid(row=0, column=1, sticky="e", padx=(0, 10))

        ctk.CTkButton(
            actions,
            text="退出",
            command=self._quit_service,
            height=36,
            width=84,
            corner_radius=12,
            fg_color="#B42318",
            hover_color="#912018",
        ).grid(row=0, column=2, sticky="e")

        # ── Hint ──────────────────────────────────────────────────────────────
        self.hint_label = ctk.CTkLabel(
            container,
            text="首次连接时，由收到连接请求的设备确认配对。",
            justify="left",
            anchor="w",
            text_color=("gray35", "gray70"),
            font=ctk.CTkFont(size=12),
        )
        self.hint_label.grid(row=2, column=0, sticky="ew", pady=(10, 0))

        # ── Peer sections (Connected + Discovered) ────────────────────────────
        peer_sections = ctk.CTkFrame(container, fg_color="transparent")
        peer_sections.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        peer_sections.grid_columnconfigure(0, weight=1)
        peer_sections.grid_columnconfigure(1, weight=1)
        peer_sections.grid_rowconfigure(0, weight=1)

        self.connected_title, self.connected_body = self._build_section(
            peer_sections,
            row=0,
            column=0,
            title="已连接设备",
            subtitle="已建立同步",
        )
        self.discovered_title, self.discovered_body = self._build_section(
            peer_sections,
            row=0,
            column=1,
            title="已发现设备",
            subtitle="连接方向由设备 ID 哈希决定",
        )

        # ── Pending pairings ──────────────────────────────────────────────────
        self.pairing_title, self.pairing_body = self._build_section(
            container,
            row=4,
            column=0,
            title="待处理配对",
            subtitle="只有首次入站连接会出现在这里",
        )

        # ── Error banner ──────────────────────────────────────────────────────
        self.error_frame = ctk.CTkFrame(container, corner_radius=14, fg_color=("#FEE4E2", "#49211D"))
        self.error_frame.grid(row=5, column=0, sticky="ew", pady=(14, 0))
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
        self.error_label.grid(row=0, column=0, sticky="ew", padx=14, pady=12)
        self.error_frame.grid_remove()

    def _build_section(self, parent, row, column, title, subtitle):
        padx = (0, 6) if column == 0 else (6, 0)
        frame = ctk.CTkFrame(parent, corner_radius=18)
        frame.grid(row=row, column=column, sticky="nsew", padx=padx, pady=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 0))
        header.grid_columnconfigure(0, weight=1)

        title_label = ctk.CTkLabel(
            header,
            text=title,
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        title_label.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            header,
            text=subtitle,
            text_color=("gray35", "gray70"),
            font=ctk.CTkFont(size=11),
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))

        body = ctk.CTkScrollableFrame(frame, corner_radius=14, fg_color="transparent")
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

        platform = snapshot.get("platform", "unknown")
        device_name = snapshot.get("device_name", "Unknown Device")
        device_id = snapshot.get("device_id", "unknown")
        status_text = snapshot.get("status_text", "未知状态")
        thread_alive = snapshot.get("thread_alive", True)

        self.device_label.configure(text=f"{device_name} ({platform})")
        self.detail_label.configure(
            text=f"设备 ID: {device_id}  |  后台服务: {'运行中' if thread_alive else '未运行'}"
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
        self._render_error(snapshot.get("startup_error") or snapshot.get("last_error"))
        self._maybe_focus_from_snapshot(snapshot)

        self._refresh_job = self.root.after(1000, self._refresh)

    def _update_autostart_button(self):
        if not self._autostart_supported:
            self.autostart_button.configure(
                text="自动启动: 不支持",
                state="disabled",
                fg_color=("gray84", "gray24"),
                text_color=("gray35", "gray74"),
            )
            return

        self.autostart_button.configure(state="normal")
        if self._autostart_enabled:
            self.autostart_button.configure(
                text="自动启动: 已开启",
                fg_color="#027A48",
                hover_color="#05603A",
                text_color="white",
            )
        else:
            self.autostart_button.configure(
                text="自动启动: 已关闭",
                fg_color=("gray84", "gray24"),
                hover_color=("gray78", "gray30"),
                text_color=("gray12", "white"),
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
                subtitle=f"{peer.get('platform', 'unknown')}  |  {direction}",
                trailing=subtitle,
                trailing_color=("gray38", "gray68"),
            )

    def _render_discovered_peers(self, peers):
        self._clear_children(self.discovered_body)
        if not peers:
            self._render_empty_state(self.discovered_body, "尚未发现其他设备")
            return

        for peer in peers:
            if peer.get("connected"):
                state_text = "已连接"
                state_color = "#027A48"
            elif peer.get("retry_in"):
                state_text = f"{peer['retry_in']:.0f}s 后重试"
                state_color = "#B54708"
            elif peer.get("will_initiate"):
                state_text = "本机发起"
                state_color = "#0B6E99"
            else:
                state_text = "等待对端"
                state_color = "#475467"

            self._render_row(
                self.discovered_body,
                title=peer["peer_id"],
                subtitle=f"{peer.get('platform', 'unknown')}  |  {peer.get('url') or '地址未知'}",
                trailing=state_text,
                trailing_color=state_color,
            )

    def _render_pairings(self, pending):
        self._clear_children(self.pairing_body)
        if not pending:
            self._render_empty_state(self.pairing_body, "当前没有待处理的首次连接请求")
            return

        for request in pending:
            card = ctk.CTkFrame(self.pairing_body, corner_radius=14)
            card.pack(fill="x", pady=5)
            card.grid_columnconfigure(0, weight=1)

            body = ctk.CTkFrame(card, fg_color="transparent")
            body.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
            body.grid_columnconfigure(0, weight=1)

            platform = request.get("platform", "")
            platform_prefix = "🍎 " if platform == "macos" else "🪟 " if platform == "windows" else ""
            ctk.CTkLabel(
                body,
                text=f"{platform_prefix}{request['device_name']}",
                font=ctk.CTkFont(size=14, weight="bold"),
            ).grid(row=0, column=0, sticky="w")

            ctk.CTkLabel(
                body,
                text=f"{request['platform']}  |  {request['ip_address']}  |  {request['device_id']}",
                text_color=("gray35", "gray70"),
                font=ctk.CTkFont(size=12),
            ).grid(row=1, column=0, sticky="w", pady=(4, 0))

            actions = ctk.CTkFrame(body, fg_color="transparent")
            actions.grid(row=0, column=1, rowspan=2, sticky="e", padx=(10, 0))

            ctk.CTkButton(
                actions,
                text="同意",
                width=76,
                height=32,
                command=lambda rid=request["device_id"]: self._accept_pairing(rid),
            ).pack(side="left")
            ctk.CTkButton(
                actions,
                text="拒绝",
                width=76,
                height=32,
                command=lambda rid=request["device_id"]: self._reject_pairing(rid),
                fg_color=("gray78", "gray28"),
                hover_color=("gray70", "gray34"),
            ).pack(side="left", padx=(8, 0))

    def _render_row(self, parent, title, subtitle, trailing, trailing_color):
        row = ctk.CTkFrame(parent, corner_radius=12)
        row.pack(fill="x", pady=5)
        row.grid_columnconfigure(0, weight=1)

        display_title = title if len(title) <= 24 else title[:21] + "…"
        ctk.CTkLabel(
            row,
            text=display_title,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 2))

        ctk.CTkLabel(
            row,
            text=subtitle,
            justify="left",
            anchor="w",
            text_color=("gray35", "gray70"),
            font=ctk.CTkFont(size=11),
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 10))

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
            text_color=("gray40", "gray68"),
            font=ctk.CTkFont(size=12),
        ).pack(fill="x", pady=8)

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
            return "#027A48"
        if "连接" in status_text or "发现" in status_text:
            return "#0B6E99"
        if "已停止" in status_text or "停止" in status_text or "错误" in status_text:
            return "#B42318"
        return "#475467"  # gray: 正在后台监听, unknown, etc.