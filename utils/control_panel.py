import tkinter as tk
from tkinter import messagebox, ttk


class ControlPanel:
    """Lightweight Tk control panel for status, pairing and autostart."""

    def __init__(self, controller, title="UniPaste", on_quit_callback=None):
        self.controller = controller
        self.title = title
        self.on_quit_callback = on_quit_callback
        self.root = None
        self._pairing_rows = {}
        self._refresh_job = None

    def run(self):
        self.root = tk.Tk()
        self.root.title(self.title)
        self.root.geometry("760x560")
        self.root.minsize(680, 500)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.status_var = tk.StringVar(value="启动中")
        self.device_var = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value="")
        self.autostart_var = tk.StringVar(value="自动启动: 不支持")

        container = ttk.Frame(self.root, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(container)
        header.pack(fill=tk.X)
        ttk.Label(header, text="UniPaste", font=("SF Pro Text", 20, "bold")).pack(anchor=tk.W)
        ttk.Label(header, textvariable=self.status_var).pack(anchor=tk.W, pady=(6, 0))
        ttk.Label(header, textvariable=self.device_var).pack(anchor=tk.W, pady=(2, 0))
        ttk.Label(header, textvariable=self.mode_var).pack(anchor=tk.W, pady=(2, 12))

        actions = ttk.Frame(container)
        actions.pack(fill=tk.X, pady=(0, 12))
        ttk.Button(actions, text="启用自动启动", command=self._install_autostart).pack(side=tk.LEFT)
        ttk.Button(actions, text="关闭自动启动", command=self._remove_autostart).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="刷新", command=self._refresh).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="退出 UniPaste", command=self._quit_service).pack(side=tk.RIGHT)

        ttk.Label(container, textvariable=self.autostart_var).pack(anchor=tk.W, pady=(0, 12))

        peers_frame = ttk.Frame(container)
        peers_frame.pack(fill=tk.BOTH, expand=True)
        peers_frame.columnconfigure(0, weight=1)
        peers_frame.columnconfigure(1, weight=1)
        peers_frame.rowconfigure(0, weight=1)

        connected_frame = ttk.LabelFrame(peers_frame, text="已连接设备")
        connected_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.connected_list = tk.Listbox(connected_frame, height=12)
        self.connected_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        discovered_frame = ttk.LabelFrame(peers_frame, text="已发现设备")
        discovered_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.discovered_list = tk.Listbox(discovered_frame, height=12)
        self.discovered_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        pairing_frame = ttk.LabelFrame(container, text="待处理配对请求")
        pairing_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        self.pairing_container = ttk.Frame(pairing_frame)
        self.pairing_container.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self._refresh()
        self.root.mainloop()

    def focus(self):
        if not self.root:
            return
        self.root.after(0, self._focus_window)

    def _focus_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _refresh(self):
        if not self.root:
            return

        snapshot = self.controller.get_ui_snapshot()
        platform = snapshot.get("platform", "unknown")
        device_name = snapshot.get("device_name", "Unknown Device")
        device_id = snapshot.get("device_id", "unknown")
        connected = snapshot.get("connected_peers", [])
        discovered = snapshot.get("discovered_peers", [])
        pending = snapshot.get("pending_pairings", [])

        self.status_var.set(f"状态: {snapshot.get('status_text', '未知')}")
        self.device_var.set(f"设备: {device_name} ({platform})")
        self.mode_var.set(f"设备 ID: {device_id}")

        autostart = snapshot.get("autostart_enabled")
        if autostart is None:
            self.autostart_var.set("自动启动: 当前平台不支持")
        else:
            self.autostart_var.set(f"自动启动: {'已启用' if autostart else '未启用'}")

        self.connected_list.delete(0, tk.END)
        if connected:
            for peer in connected:
                self.connected_list.insert(
                    tk.END,
                    f"{peer['peer_id']} ({peer['platform']})",
                )
        else:
            self.connected_list.insert(tk.END, "当前没有活动连接")

        self.discovered_list.delete(0, tk.END)
        if discovered:
            for peer in discovered:
                retry_suffix = ""
                if peer.get("retry_in"):
                    retry_suffix = f" | 重试 {peer['retry_in']:.0f}s"
                self.discovered_list.insert(
                    tk.END,
                    f"{peer['peer_id']} ({peer['platform']}){' | 已连接' if peer['connected'] else ''}{retry_suffix}",
                )
        else:
            self.discovered_list.insert(tk.END, "尚未发现其他设备")

        self._render_pairings(pending)
        self._refresh_job = self.root.after(1000, self._refresh)

    def _render_pairings(self, pending):
        for widget in self.pairing_container.winfo_children():
            widget.destroy()

        if not pending:
            ttk.Label(self.pairing_container, text="当前没有待处理的配对请求").pack(anchor=tk.W)
            return

        for request in pending:
            row = ttk.Frame(self.pairing_container)
            row.pack(fill=tk.X, pady=4)
            desc = f"{request['device_name']} ({request['platform']}) | {request['ip_address']}"
            ttk.Label(row, text=desc).pack(side=tk.LEFT, fill=tk.X, expand=True)
            ttk.Button(
                row,
                text="接受",
                command=lambda rid=request["device_id"]: self._accept_pairing(rid),
            ).pack(side=tk.RIGHT)
            ttk.Button(
                row,
                text="拒绝",
                command=lambda rid=request["device_id"]: self._reject_pairing(rid),
            ).pack(side=tk.RIGHT, padx=(0, 8))

    def _accept_pairing(self, device_id):
        if self.controller.accept_pairing_request(device_id):
            self._refresh()

    def _reject_pairing(self, device_id):
        if self.controller.reject_pairing_request(device_id):
            self._refresh()

    def _install_autostart(self):
        ok, message = self.controller.install_autostart()
        if ok:
            messagebox.showinfo("UniPaste", message)
        else:
            messagebox.showerror("UniPaste", message)
        self._refresh()

    def _remove_autostart(self):
        ok, message = self.controller.remove_autostart()
        if ok:
            messagebox.showinfo("UniPaste", message)
        else:
            messagebox.showerror("UniPaste", message)
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
