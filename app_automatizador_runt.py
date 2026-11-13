# -*- coding: utf-8 -*-
"""
Panel de Control Gráfico (GUI): Automatizador RUNT Ciudadano + Modo Ráfaga Multi-Pestaña (4-6 Canales Simultáneos)
+ Sistema de Autenticación SQLite (Token 60 Minutos) + Generador de Reportes Excel desde Base de Datos.

Características clave:
1. Seguridad SQLite: Login obligatorio al abrir/cerrar, contraseñas PBKDF2-HMAC-SHA256 y Token de 60 minutos.
2. Modo Ráfaga Multi-Pestaña (Paralelo): Abre hasta 6 pestañas aisladas del RUNT al mismo tiempo, precarga
   las placas y sus respectivos CAPTCHAs simultáneamente, y salta automáticamente el foco de un Captcha al
   siguiente al presionar Enter mientras los demás canales toman las 5 capturas en segundo plano.
3. Actualización instantánea en memoria + SQLite sin bloquear la tabla.
4. Generador de Reportes Excel (.xlsx) de 4 hojas conectado directamente a la base de datos SQLite.
"""

import os
import io
import queue
import asyncio
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk

import auth_manager
import scanner_patios
from bot_playwright_runt import RuntParallelCluster


class AutomatizadorRuntGUI:
    MAX_CHANNELS = 4  # Tarjetas simultáneas visibles en el panel derecho (soporta 1 a 4 en vista directa)

    def __init__(self, root):
        self.root = root
        self.root.title("Automatizador Oficial de Expedientes RUNT Ciudadano - Acceso Seguro")
        self.root.geometry("1360x840")
        self.root.configure(bg="#0f172a")

        # Al abrir el programa, invalidar todas las sesiones previas para exigir siempre login nuevo
        auth_manager.init_auth_db()
        auth_manager.invalidate_all_active_sessions("INICIO_APLICACION_NUEVA_SESION_REQUERIDA")

        # Estado de sesión actual
        self.session = None
        self.timer_job = None

        # Datos del escáner y lote
        self.vehicles = []
        self.stats = {}
        self.current_index = None
        self.is_running_batch = False
        self.is_paused = False

        # Contenedores de vistas
        self.login_container = None
        self.dashboard_container = None

        # Widgets de los canales paralelos
        self.slot_widgets = {}
        self.slot_photos = {}
        self.slot_ready_flags = {}

        # Cola de comandos hacia el hilo asíncrono de Playwright
        self.cmd_queue = queue.Queue()
        self.cluster = None
        self.worker_thread = threading.Thread(target=self._async_worker_thread_entry, daemon=True)
        self.worker_thread.start()

        self._setup_styles()

        # Al cerrar la ventana con la X, revocar el token activo en SQLite
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_window)

        # Mostrar pantalla de inicio de sesión
        self._show_login_view()

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview",
                        background="#1e293b",
                        foreground="#f8fafc",
                        rowheight=28,
                        fieldbackground="#1e293b",
                        font=("Segoe UI", 10))
        style.configure("Treeview.Heading",
                        background="#0f172a",
                        foreground="#38bdf8",
                        font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#2563eb")])

    # =========================================================================
    # 1. PANTALLA DE INICIO DE SESIÓN (LOGIN) Y GESTIÓN DE TOKEN (60 MINUTOS)
    # =========================================================================
    def _show_login_view(self, info_msg=None):
        if self.timer_job:
            self.root.after_cancel(self.timer_job)
            self.timer_job = None

        if self.dashboard_container:
            self.dashboard_container.destroy()
            self.dashboard_container = None

        if self.login_container:
            self.login_container.destroy()

        self.root.title("Iniciar Sesión - Automatizador Oficial RUNT Ciudadano")

        self.login_container = tk.Frame(self.root, bg="#0f172a")
        self.login_container.pack(fill=tk.BOTH, expand=True)

        card = tk.Frame(
            self.login_container, bg="#1e293b",
            padx=38, pady=34,
            highlightbackground="#38bdf8", highlightthickness=1
        )
        card.place(relx=0.5, rely=0.5, anchor="center", width=460)

        tk.Label(
            card, text="🔐 ACCESO AL SISTEMA RUNT",
            bg="#1e293b", fg="#38bdf8",
            font=("Segoe UI", 16, "bold")
        ).pack(pady=(0, 4))

        tk.Label(
            card,
            text="Validación y Captura Automatizada de Expedientes Vehiculares\nSesión protegida con Token SQLite (Vigencia: 60 minutos)",
            bg="#1e293b", fg="#94a3b8",
            font=("Segoe UI", 9), justify="center"
        ).pack(pady=(0, 18))

        if info_msg:
            lbl_alert = tk.Label(
                card, text=info_msg,
                bg="#451a1c", fg="#fca5a5",
                font=("Segoe UI", 9, "bold"),
                padx=10, pady=8, wraplength=360, justify="center"
            )
            lbl_alert.pack(fill=tk.X, pady=(0, 14))

        tk.Label(
            card, text="Usuario:", bg="#1e293b", fg="#e2e8f0",
            font=("Segoe UI", 10, "bold")
        ).pack(anchor="w")
        self.ent_login_user = tk.Entry(
            card, font=("Segoe UI", 12),
            bg="#0f172a", fg="#f8fafc", insertbackground="#ffffff",
            relief=tk.FLAT, highlightthickness=1, highlightbackground="#334155"
        )
        self.ent_login_user.pack(fill=tk.X, ipady=6, pady=(4, 14))
        self.ent_login_user.bind("<Return>", lambda e: self.ent_login_pwd.focus_set())

        tk.Label(
            card, text="Contraseña:", bg="#1e293b", fg="#e2e8f0",
            font=("Segoe UI", 10, "bold")
        ).pack(anchor="w")

        pwd_frame = tk.Frame(card, bg="#0f172a", highlightthickness=1, highlightbackground="#334155")
        pwd_frame.pack(fill=tk.X, pady=(4, 8))

        self.show_pwd_var = tk.BooleanVar(value=False)
        self.ent_login_pwd = tk.Entry(
            pwd_frame, font=("Segoe UI", 12), show="•",
            bg="#0f172a", fg="#f8fafc", insertbackground="#ffffff",
            relief=tk.FLAT
        )
        self.ent_login_pwd.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=6)
        self.ent_login_pwd.bind("<Return>", lambda e: self._attempt_login())

        btn_eye = tk.Checkbutton(
            pwd_frame, text="Ver", variable=self.show_pwd_var,
            bg="#0f172a", fg="#94a3b8", selectcolor="#0f172a",
            activebackground="#0f172a", activeforeground="#38bdf8",
            font=("Segoe UI", 8),
            command=lambda: self.ent_login_pwd.configure(show="" if self.show_pwd_var.get() else "•")
        )
        btn_eye.pack(side=tk.RIGHT, padx=6)

        self.lbl_login_status = tk.Label(
            card, text="", bg="#1e293b", fg="#f87171",
            font=("Segoe UI", 9, "bold"), wraplength=360
        )
        self.lbl_login_status.pack(fill=tk.X, pady=(4, 8))

        self.btn_login = tk.Button(
            card, text="INGRESAR AL SISTEMA ➔",
            bg="#0284c7", fg="#ffffff",
            font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT, pady=10, cursor="hand2",
            command=self._attempt_login
        )
        self.btn_login.pack(fill=tk.X, pady=(6, 4))
        self._login_in_progress = False

        self.ent_login_user.focus_set()

    def _attempt_login(self):
        if getattr(self, "_login_in_progress", False):
            return
        user = self.ent_login_user.get().strip()
        pwd = self.ent_login_pwd.get()

        self._login_in_progress = True
        self.lbl_login_status.configure(
            text="⏳ Verificando credenciales e ingresando al sistema...",
            fg="#38bdf8"
        )
        self.btn_login.configure(text="⏳ INGRESANDO...", state="disabled", bg="#0369a1")
        self.root.update_idletasks()

        def _bg_auth():
            ok, msg, session_data = auth_manager.authenticate_user(user, pwd)

            def _on_auth_done():
                self._login_in_progress = False
                if not ok:
                    if hasattr(self, "lbl_login_status") and self.lbl_login_status.winfo_exists():
                        self.lbl_login_status.configure(text=f"⚠ {msg}", fg="#f87171")
                    if hasattr(self, "btn_login") and self.btn_login.winfo_exists():
                        self.btn_login.configure(text="INGRESAR AL SISTEMA ➔", state="normal", bg="#0284c7")
                    return

                if hasattr(self, "lbl_login_status") and self.lbl_login_status.winfo_exists():
                    self.lbl_login_status.configure(text="✅ Acceso autorizado. Cargando panel...", fg="#4ade80")
                self.root.update_idletasks()
                self.session = session_data
                self._show_dashboard_view()

            self.root.after(0, _on_auth_done)

        threading.Thread(target=_bg_auth, daemon=True).start()

    def _start_session_countdown(self):
        if not self.session:
            return

        token = self.session.get("token")
        valid, rem_secs, msg = auth_manager.validate_session_token(token)

        if not valid or rem_secs <= 0:
            self._force_logout_due_to_expiration(msg)
            return

        mins = rem_secs // 60
        secs = rem_secs % 60
        token_short = token[:10] + "..."

        color_timer = "#4ade80" if mins >= 10 else ("#facc15" if mins >= 3 else "#f87171")
        if hasattr(self, "lbl_session_timer") and self.lbl_session_timer.winfo_exists():
            self.lbl_session_timer.configure(
                text=f"🔑 Token: {token_short}  |  ⏱ Expira en: {mins:02d}:{secs:02d} min",
                fg=color_timer
            )

        self.timer_job = self.root.after(1000, self._start_session_countdown)

    def _force_logout_due_to_expiration(self, reason_msg):
        self.is_paused = True
        self.is_running_batch = False
        if self.cluster:
            self.cluster.is_paused = True
        if self.session:
            auth_manager.logout_session(self.session.get("token"), "EXPIRACION_AUTOMATICA_60_MIN")
        self.session = None
        messagebox.showwarning(
            "Sesión Expirada (60 Minutos)",
            f"{reason_msg}\n\nEl proceso en curso se ha pausado de forma segura."
        )
        self._show_login_view(info_msg="Tu sesión anterior cumplió los 60 minutos de validez. Inicia sesión nuevamente para continuar.")

    def _manual_logout(self):
        if not messagebox.askyesno("Cerrar Sesión", "¿Deseas cerrar tu sesión actual y bloquear el sistema?"):
            return
        self.is_paused = True
        self.is_running_batch = False
        if self.cluster:
            self.cluster.is_paused = True
        if self.session:
            auth_manager.logout_session(self.session.get("token"), "CIERRE_MANUAL_BOTON_SALIR")
        self.session = None
        self._show_login_view(info_msg="Has cerrado sesión correctamente. Tu token fue invalidado en la base de datos.")

    def _open_user_manager_dialog(self):
        if not self.session:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Gestión de Usuarios y Contraseñas (SQLite)")
        dlg.geometry("420x350")
        dlg.configure(bg="#1e293b")
        dlg.transient(self.root)
        dlg.grab_set()

        frame = tk.Frame(dlg, bg="#1e293b", padx=20, pady=18)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(frame, text="Crear Usuario o Cambiar Contraseña", bg="#1e293b", fg="#38bdf8", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 12))

        tk.Label(frame, text="Nombre de usuario (login):", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ent_u = tk.Entry(frame, font=("Segoe UI", 10), bg="#0f172a", fg="#ffffff", insertbackground="#ffffff")
        ent_u.pack(fill=tk.X, ipady=4, pady=(2, 8))
        ent_u.insert(0, self.session["username"])

        tk.Label(frame, text="Nombre completo:", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ent_n = tk.Entry(frame, font=("Segoe UI", 10), bg="#0f172a", fg="#ffffff", insertbackground="#ffffff")
        ent_n.pack(fill=tk.X, ipady=4, pady=(2, 8))
        ent_n.insert(0, self.session["nombre_completo"])

        tk.Label(frame, text="Nueva contraseña (mínimo 6 caracteres):", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ent_p = tk.Entry(frame, font=("Segoe UI", 10), show="•", bg="#0f172a", fg="#ffffff", insertbackground="#ffffff")
        ent_p.pack(fill=tk.X, ipady=4, pady=(2, 14))

        def _save():
            ok, msg = auth_manager.create_or_update_user(ent_u.get(), ent_n.get(), ent_p.get(), "ADMIN")
            if ok:
                messagebox.showinfo("Usuario Guardado", msg, parent=dlg)
                dlg.destroy()
            else:
                messagebox.showerror("Error de Validación", msg, parent=dlg)

        tk.Button(
            frame, text="💾 Guardar en SQLite", bg="#16a34a", fg="#ffffff",
            font=("Segoe UI", 10, "bold"), relief=tk.FLAT, pady=8, cursor="hand2",
            command=_save
        ).pack(fill=tk.X)

    # =========================================================================
    # 2. PANEL PRINCIPAL DE CONTROL DE PATIOS + MODO RÁFAGA MULTI-PESTAÑA
    # =========================================================================
    def _show_dashboard_view(self):
        if self.login_container:
            self.login_container.destroy()
            self.login_container = None

        self.root.title(
            f"Automatizador Oficial RUNT Ciudadano [Modo Ráfaga Multi-Pestaña] - Sesión: {self.session['nombre_completo']} ({self.session['username']})"
        )

        self.dashboard_container = tk.Frame(self.root, bg="#0f172a")
        self.dashboard_container.pack(fill=tk.BOTH, expand=True)

        # Barra superior de Sesión y Seguridad
        session_bar = tk.Frame(self.dashboard_container, bg="#090d16", padx=14, pady=6)
        session_bar.pack(fill=tk.X)

        tk.Label(
            session_bar,
            text=f"👤 Usuario Activo: {self.session['nombre_completo']} ({self.session['username']})",
            bg="#090d16", fg="#e2e8f0", font=("Segoe UI", 9, "bold")
        ).pack(side=tk.LEFT)

        btn_logout = tk.Button(
            session_bar, text="🚪 Cerrar Sesión",
            bg="#dc2626", fg="#ffffff", font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, padx=10, pady=2, cursor="hand2",
            command=self._manual_logout
        )
        btn_logout.pack(side=tk.RIGHT, padx=(8, 0))

        btn_users = tk.Button(
            session_bar, text="⚙ Usuarios / Clave",
            bg="#334155", fg="#f8fafc", font=("Segoe UI", 9),
            relief=tk.FLAT, padx=10, pady=2, cursor="hand2",
            command=self._open_user_manager_dialog
        )
        btn_users.pack(side=tk.RIGHT, padx=6)

        self.lbl_session_timer = tk.Label(
            session_bar,
            text="🔑 Token activo | ⏱ Expira en: 60:00 min",
            bg="#090d16", fg="#4ade80", font=("Consolas", 10, "bold")
        )
        self.lbl_session_timer.pack(side=tk.RIGHT, padx=14)

        # 1. Barra de título y selectores de Patio / Año / Reportes
        top_frame = tk.Frame(self.dashboard_container, bg="#1e293b", padx=14, pady=8)
        top_frame.pack(fill=tk.X, padx=12, pady=(6, 4))

        tk.Label(
            top_frame,
            text="AUTOMATIZADOR RUNT CIUDADANO",
            bg="#1e293b", fg="#f8fafc",
            font=("Segoe UI", 13, "bold")
        ).pack(side=tk.LEFT, padx=(0, 16))

        tk.Label(top_frame, text="Patio:", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.cmb_patio = ttk.Combobox(top_frame, state="readonly", width=26, font=("Segoe UI", 10))
        self.cmb_patio.pack(side=tk.LEFT, padx=(6, 12))
        self.cmb_patio.bind("<<ComboboxSelected>>", self._on_patio_selected)

        tk.Label(top_frame, text="Año Retención:", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.cmb_anio = ttk.Combobox(top_frame, state="readonly", width=12, font=("Segoe UI", 10))
        self.cmb_anio.pack(side=tk.LEFT, padx=(6, 12))
        self.cmb_anio.bind("<<ComboboxSelected>>", self._on_anio_selected)

        btn_refresh = tk.Button(
            top_frame, text="🔄 Actualizar Escaneo",
            bg="#334155", fg="#f8fafc", font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, padx=10, pady=4, cursor="hand2",
            command=self._scan_current_selection
        )
        btn_refresh.pack(side=tk.LEFT, padx=4)

        btn_report_top = tk.Button(
            top_frame, text="📊 Generar Reporte BD (Excel)",
            bg="#0284c7", fg="#ffffff", font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, padx=12, pady=4, cursor="hand2",
            command=self._open_report_generator_dialog
        )
        btn_report_top.pack(side=tk.LEFT, padx=4)

        # 2. Panel de tarjetas de indicadores (KPIs)
        kpi_frame = tk.Frame(self.dashboard_container, bg="#0f172a")
        kpi_frame.pack(fill=tk.X, padx=12, pady=3)

        self.kpi_vars = {
            "total": tk.StringVar(value="0"),
            "pendientes": tk.StringVar(value="0"),
            "completados": tk.StringVar(value="0"),
            "embargos": tk.StringVar(value="0"),
            "inconsistencias": tk.StringVar(value="0"),
            "sin_prop": tk.StringVar(value="0")
        }

        kpi_specs = [
            ("Total en Año", "total", "#38bdf8"),
            ("Pendientes por Consultar", "pendientes", "#facc15"),
            ("Completados RUNT Ciudadano", "completados", "#4ade80"),
            ("Con Embargo / Fiscalía", "embargos", "#f87171"),
            ("Inconsistencia Propietario", "inconsistencias", "#fb923c"),
            ("Cerrados Sin Propietario (Ley 1730)", "sin_prop", "#94a3b8")
        ]

        for label_txt, key, color in kpi_specs:
            card = tk.Frame(kpi_frame, bg="#1e293b", padx=12, pady=6, highlightbackground="#334155", highlightthickness=1)
            card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=3)
            tk.Label(card, textvariable=self.kpi_vars[key], bg="#1e293b", fg=color, font=("Segoe UI", 17, "bold")).pack(anchor="w")
            tk.Label(card, text=label_txt, bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 8)).pack(anchor="w")

        # 3. Panel Central Dividido: Izquierda (Tabla) | Derecha (4 Canales Simultáneos de Captcha Ráfaga)
        mid_frame = tk.Frame(self.dashboard_container, bg="#0f172a")
        mid_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=5)

        left_frame = tk.Frame(mid_frame, bg="#1e293b", padx=8, pady=8)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        filter_bar = tk.Frame(left_frame, bg="#1e293b")
        filter_bar.pack(fill=tk.X, pady=(0, 6))
        tk.Label(filter_bar, text="Filtrar vista:", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)

        self.filter_var = tk.StringVar(value="PENDIENTES_Y_RECIENTES")
        for txt, val in [
            ("Pendientes + Consultados", "PENDIENTES_Y_RECIENTES"),
            ("Solo Pendientes", "SOLO_PENDIENTES"),
            ("Solo Con Embargo / Alerta", "SOLO_EMBARGOS"),
            ("Todos (Incluye Sin Propietario)", "TODOS")
        ]:
            tk.Radiobutton(
                filter_bar, text=txt, variable=self.filter_var, value=val,
                bg="#1e293b", fg="#f8fafc", selectcolor="#0f172a",
                activebackground="#1e293b", activeforeground="#38bdf8",
                font=("Segoe UI", 9), command=self._populate_tree
            ).pack(side=tk.LEFT, padx=6)

        cols = ("placa", "tipo_doc", "nro_doc", "propietario", "estado", "embargo", "imgs")
        self.tree = ttk.Treeview(left_frame, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("placa", text="PLACA")
        self.tree.heading("tipo_doc", text="TIPO DOC")
        self.tree.heading("nro_doc", text="DOCUMENTO")
        self.tree.heading("propietario", text="PROPIETARIO REGISTRADO")
        self.tree.heading("estado", text="ESTADO EXPEDIENTE")
        self.tree.heading("embargo", text="EMBARGO / LIMITACIÓN")
        self.tree.heading("imgs", text="CAPTURAS")

        self.tree.column("placa", width=75, anchor="center")
        self.tree.column("tipo_doc", width=115, anchor="center")
        self.tree.column("nro_doc", width=105, anchor="center")
        self.tree.column("propietario", width=215, anchor="w")
        self.tree.column("estado", width=150, anchor="center")
        self.tree.column("embargo", width=155, anchor="w")
        self.tree.column("imgs", width=70, anchor="center")

        self.tree.tag_configure("PENDIENTE", foreground="#fde047")
        self.tree.tag_configure("COMPLETADO_CIUDADANO", foreground="#4ade80")
        self.tree.tag_configure("EMBARGO", foreground="#f87171", background="#3b181e")
        self.tree.tag_configure("INCONSISTENCIA_PROPIETARIO", foreground="#fb923c")
        self.tree.tag_configure("SIN_PROPIETARIO_CONSTANCIA", foreground="#94a3b8")

        vsb = ttk.Scrollbar(left_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # =====================================================================
        # PANEL DERECHO: MODO RÁFAGA MULTI-PESTAÑA (4 CANALES SIMULTÁNEOS)
        # =====================================================================
        right_frame = tk.Frame(mid_frame, bg="#1e293b", width=460, padx=12, pady=10)
        right_frame.pack(side=tk.RIGHT, fill=tk.Y)
        right_frame.pack_propagate(False)

        header_r = tk.Frame(right_frame, bg="#1e293b")
        header_r.pack(fill=tk.X, pady=(0, 6))

        tk.Label(
            header_r, text="⚡ MODO RÁFAGA MULTI-PESTAÑA",
            bg="#1e293b", fg="#38bdf8", font=("Segoe UI", 11, "bold")
        ).pack(side=tk.LEFT)

        tk.Label(header_r, text="Canales:", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9, "bold")).pack(side=tk.RIGHT, padx=(6, 4))
        self.cmb_channels = ttk.Combobox(
            header_r, state="readonly", width=14, font=("Segoe UI", 9),
            values=["4 Pestañas (Turbo)", "3 Pestañas", "2 Pestañas", "1 Pestaña"]
        )
        self.cmb_channels.set("4 Pestañas (Turbo)")
        self.cmb_channels.pack(side=tk.RIGHT)

        # Botonera de control de Lote Ráfaga
        btn_grid = tk.Frame(right_frame, bg="#1e293b")
        btn_grid.pack(fill=tk.X, pady=(0, 6))

        self.btn_start = tk.Button(
            btn_grid, text="⚡ INICIAR RÁFAGA MULTI-PESTAÑA (DESDE SELECCIÓN)",
            bg="#16a34a", fg="#ffffff", font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, pady=7, cursor="hand2",
            command=self._start_batch_process
        )
        self.btn_start.pack(fill=tk.X, pady=2)

        row_actions = tk.Frame(btn_grid, bg="#1e293b")
        row_actions.pack(fill=tk.X, pady=2)

        self.btn_single = tk.Button(
            row_actions, text="🎯 Solo Placa Seleccionada",
            bg="#2563eb", fg="#ffffff", font=("Segoe UI", 8, "bold"),
            relief=tk.FLAT, pady=5, cursor="hand2",
            command=self._run_selected_single
        )
        self.btn_single.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))

        self.btn_pause = tk.Button(
            row_actions, text="⏸ Pausar Ráfaga",
            bg="#d97706", fg="#ffffff", font=("Segoe UI", 8, "bold"),
            relief=tk.FLAT, pady=5, cursor="hand2",
            command=self._toggle_pause
        )
        self.btn_pause.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=3)

        self.btn_open_folder = tk.Button(
            row_actions, text="📂 Expediente",
            bg="#475569", fg="#ffffff", font=("Segoe UI", 8, "bold"),
            relief=tk.FLAT, pady=5, cursor="hand2",
            command=self._open_selected_expediente
        )
        self.btn_open_folder.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(3, 0))

        # Instrucción breve de uso de Ráfaga
        tk.Label(
            right_frame,
            text="Escribe el Captcha y presiona Enter ↵: el cursor salta al instante al siguiente\ncanal listo mientras los demás guardan las 5 capturas en segundo plano.",
            bg="#1e293b", fg="#94a3b8", font=("Segoe UI", 8), justify="left"
        ).pack(anchor="w", pady=(0, 4))

        # Contenedor de las 4 Tarjetas de Canales Simultáneos
        slots_container = tk.Frame(right_frame, bg="#1e293b")
        slots_container.pack(fill=tk.X, pady=(0, 6))

        self.slot_widgets = {}
        self.slot_photos = {}
        self.slot_ready_flags = {}

        for s_id in range(self.MAX_CHANNELS):
            card_s = tk.Frame(
                slots_container, bg="#0f172a", padx=8, pady=5,
                highlightbackground="#334155", highlightthickness=1
            )
            card_s.pack(fill=tk.X, pady=3)

            top_s = tk.Frame(card_s, bg="#0f172a")
            top_s.pack(fill=tk.X)

            lbl_title = tk.Label(
                top_s, text=f"CANAL #{s_id + 1}: En espera",
                bg="#0f172a", fg="#e2e8f0", font=("Segoe UI", 9, "bold")
            )
            lbl_title.pack(side=tk.LEFT)

            lbl_state = tk.Label(
                top_s, text="Inactivo",
                bg="#0f172a", fg="#94a3b8", font=("Segoe UI", 8, "bold")
            )
            lbl_state.pack(side=tk.RIGHT)

            body_s = tk.Frame(card_s, bg="#0f172a")
            body_s.pack(fill=tk.X, pady=(3, 0))

            lbl_img = tk.Label(
                body_s, text="[Captcha Canal #" + str(s_id + 1) + "]",
                bg="#ffffff", fg="#64748b", width=24, height=2, font=("Segoe UI", 8)
            )
            lbl_img.pack(side=tk.LEFT, padx=(0, 8))

            ent_cap = tk.Entry(
                body_s, font=("Consolas", 14, "bold"),
                bg="#1e293b", fg="#38bdf8", insertbackground="#ffffff",
                width=9, justify="center", relief=tk.FLAT,
                highlightthickness=1, highlightbackground="#475569"
            )
            ent_cap.pack(side=tk.LEFT, ipady=4, padx=(0, 5))
            ent_cap.bind("<Return>", lambda e, sid=s_id: self._submit_slot_captcha(sid))
            ent_cap.bind("<Down>", lambda e, sid=s_id: self._focus_next_ready_slot(sid))
            ent_cap.bind("<Up>", lambda e, sid=s_id: self._focus_prev_slot(sid))

            btn_send = tk.Button(
                body_s, text="Enviar ↵",
                bg="#0284c7", fg="#ffffff", font=("Segoe UI", 8, "bold"),
                relief=tk.FLAT, padx=8, pady=4, cursor="hand2",
                command=lambda sid=s_id: self._submit_slot_captcha(sid)
            )
            btn_send.pack(side=tk.LEFT, padx=(0, 4))

            btn_reload = tk.Button(
                body_s, text="🔄",
                bg="#334155", fg="#e2e8f0", font=("Segoe UI", 8),
                relief=tk.FLAT, padx=5, pady=4, cursor="hand2",
                command=lambda sid=s_id: self._refresh_slot_captcha(sid)
            )
            btn_reload.pack(side=tk.LEFT)

            self.slot_widgets[s_id] = {
                "card": card_s,
                "lbl_title": lbl_title,
                "lbl_state": lbl_state,
                "lbl_img": lbl_img,
                "ent_cap": ent_cap
            }
            self.slot_ready_flags[s_id] = False

        # Barra rápida para cambiar tipo de documento y reintentar placa seleccionada
        retry_box = tk.Frame(right_frame, bg="#1e293b")
        retry_box.pack(fill=tk.X, pady=(2, 4))
        tk.Label(retry_box, text="Probar otro Doc en placa seleccionada:", bg="#1e293b", fg="#94a3b8", font=("Segoe UI", 8)).pack(side=tk.LEFT)
        for doc_opt, short_lbl in [("Cédula de Extranjería", "C.E."), ("NIT", "NIT"), ("Pasaporte", "PA")]:
            tk.Button(
                retry_box, text=short_lbl, bg="#334155", fg="#e2e8f0",
                font=("Segoe UI", 8), relief=tk.FLAT, padx=6, pady=1,
                command=lambda d=doc_opt: self._retry_with_doc_type(d)
            ).pack(side=tk.LEFT, padx=2)

        tk.Label(right_frame, text="Bitácora de Ejecución Multi-Canal:", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.txt_log = tk.Text(
            right_frame, bg="#0f172a", fg="#e2e8f0",
            font=("Consolas", 8), height=6, state="disabled", wrap="word"
        )
        self.txt_log.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

        # Iniciar contador regresivo del Token de 60 minutos y cargar los patios sin congelar la interfaz
        self._start_session_countdown()
        self.log_message("⏳ Cargando patios y sincronizando expedientes...")
        self.root.update_idletasks()
        self.root.after(40, self._load_patios)

    # =========================================================================
    # 3. COORDINACIÓN MULTI-CANAL DE CAPTCHAS Y FOCO INSTANTÁNEO
    # =========================================================================
    def _get_selected_num_channels(self) -> int:
        if not hasattr(self, "cmb_channels"):
            return 4
        val = self.cmb_channels.get()
        for n in (4, 3, 2, 1):
            if str(n) in val:
                return n
        return 4

    def _on_slot_state_update(self, slot_id: int, state: dict):
        """
        Actualiza visualmente la tarjeta del Canal #slot_id en la GUI (imagen de captcha, placa, estado)
        y si ningún otro canal listo tiene el foco, enfoca automáticamente este canal.
        """
        def _draw():
            if slot_id not in self.slot_widgets:
                return
            w = self.slot_widgets[slot_id]
            if not w["card"].winfo_exists():
                return

            placa = state.get("placa", "-")
            doc_info = state.get("doc_info", "-")
            status_text = state.get("status_text", "")
            status_color = state.get("status_color", "#94a3b8")
            cap_bytes = state.get("captcha_bytes")
            ready = bool(state.get("ready_for_captcha", False))

            self.slot_ready_flags[slot_id] = ready

            if placa and placa != "-":
                w["lbl_title"].configure(text=f"CANAL #{slot_id + 1}: {placa}  ({doc_info})", fg="#38bdf8" if ready else "#e2e8f0")
            else:
                w["lbl_title"].configure(text=f"CANAL #{slot_id + 1}: Libre", fg="#94a3b8")

            w["lbl_state"].configure(text=status_text, fg=status_color)
            w["card"].configure(highlightbackground="#4ade80" if ready else "#334155")

            if cap_bytes:
                try:
                    pil_img = Image.open(io.BytesIO(cap_bytes))
                    pil_img = pil_img.resize((175, 44), Image.Resampling.LANCZOS)
                    photo = ImageTk.PhotoImage(pil_img)
                    self.slot_photos[slot_id] = photo
                    w["lbl_img"].configure(image=photo, text="", width=175, height=44)
                except Exception:
                    w["lbl_img"].configure(image="", text="[Ver en Chrome]", width=24, height=2)
            elif ready:
                w["lbl_img"].configure(image="", text="[Ver Captcha en Chrome]", width=24, height=2)
            else:
                w["lbl_img"].configure(image="", text=f"[{status_text[:22]}...]", width=24, height=2)

            if ready:
                w["ent_cap"].delete(0, tk.END)
                # Si el usuario no está escribiendo ya en otro canal que esté listo, darle foco inmediato a este
                focused_widget = self.root.focus_get()
                other_ready_focused = False
                for other_id, other_w in self.slot_widgets.items():
                    if other_id != slot_id and focused_widget == other_w["ent_cap"] and self.slot_ready_flags.get(other_id):
                        other_ready_focused = True
                        break
                if not other_ready_focused:
                    w["ent_cap"].focus_set()

        self.root.after(0, _draw)

    def _submit_slot_captcha(self, slot_id: int):
        if not self._ensure_authenticated():
            return
        if slot_id not in self.slot_widgets:
            return
        w = self.slot_widgets[slot_id]
        txt = w["ent_cap"].get().strip()
        if not txt:
            return

        w["ent_cap"].delete(0, tk.END)
        self.slot_ready_flags[slot_id] = False
        w["card"].configure(highlightbackground="#38bdf8")
        w["lbl_state"].configure(text="⏳ Consultando y capturando...", fg="#38bdf8")

        if self.cluster:
            self.cluster.route_captcha_to_slot(slot_id, txt)

        # Saltar instantáneamente al siguiente canal que ya tenga su Captcha listo
        self._focus_next_ready_slot(slot_id)

    def _refresh_slot_captcha(self, slot_id: int):
        if not self._ensure_authenticated():
            return
        if self.cluster:
            self.cluster.route_captcha_to_slot(slot_id, "__REFRESH_CAPTCHA__")

    def _focus_next_ready_slot(self, current_slot_id: int):
        # 1. Buscar primero el siguiente canal con captcha listo (ready_for_captcha == True)
        for offset in range(1, self.MAX_CHANNELS + 1):
            cand = (current_slot_id + offset) % self.MAX_CHANNELS
            if self.slot_ready_flags.get(cand):
                self.slot_widgets[cand]["ent_cap"].focus_set()
                return
        # 2. Si ninguno está marcado como listo aún, pasar al siguiente canal en orden
        nxt = (current_slot_id + 1) % self.MAX_CHANNELS
        self.slot_widgets[nxt]["ent_cap"].focus_set()

    def _focus_prev_slot(self, current_slot_id: int):
        prv = (current_slot_id - 1) % self.MAX_CHANNELS
        self.slot_widgets[prv]["ent_cap"].focus_set()

    # =========================================================================
    # 4. ACTUALIZACIÓN EN VIVO SIN BLOQUEAR LA TABLA Y ESCANEO DE PATIOS
    # =========================================================================
    def _ensure_authenticated(self) -> bool:
        if not self.session:
            return False
        valid, rem, msg = auth_manager.validate_session_token(self.session.get("token"))
        if not valid or rem <= 0:
            self.root.after(0, lambda: self._force_logout_due_to_expiration(msg))
            return False
        return True

    def log_message(self, msg):
        def _append():
            if hasattr(self, "txt_log") and self.txt_log.winfo_exists():
                self.txt_log.configure(state="normal")
                self.txt_log.insert(tk.END, f"• {msg}\n")
                self.txt_log.see(tk.END)
                self.txt_log.configure(state="disabled")
        self.root.after(0, _append)

    def _on_vehicle_completed_from_cluster(self, idx: int, v: dict, nuevo_estado: str, num_imgs: int, analysis: dict):
        """
        Se invoca cuando cualquiera de los canales paralelos termina un vehículo.
        Guarda en SQLite y actualiza únicamente esa fila y los KPIs en memoria (0ms de espera).
        """
        placa = v["placa"]
        tiene_emb = analysis.get("tiene_embargo", "NO")
        det_lim = analysis.get("detalle_limitacion", "NINGUNO")
        soat_est = analysis.get("soat_estado", "-")
        rtm_est = analysis.get("rtm_estado", "-")

        scanner_patios.save_consulta_result(
            v["patio"], v["anio"], placa, v["propietario"],
            v["tipo_doc_runt"], v["nro_doc"], nuevo_estado,
            tiene_emb, det_lim, soat_est, rtm_est, num_imgs, v["exp_dir"]
        )

        if tiene_emb != "NO":
            self.log_message(f"🚨 ALERTA {placa}: {det_lim}")
        elif nuevo_estado == "COMPLETADO_CIUDADANO":
            self.log_message(f"✅ {placa} completado sin limitaciones ({num_imgs} capturas).")

        def _update_ui_fast():
            if idx < len(self.vehicles):
                prev_est = self.vehicles[idx]["estado"]
                self.vehicles[idx]["estado"] = nuevo_estado
                self.vehicles[idx]["num_imgs"] = num_imgs
                self.vehicles[idx]["tiene_embargo"] = tiene_emb
                self.vehicles[idx]["detalle_limitacion"] = det_lim
                self.vehicles[idx]["soat_estado"] = soat_est
                self.vehicles[idx]["rtm_estado"] = rtm_est

                if prev_est == "PENDIENTE" and self.stats.get("pendientes_con_propietario", 0) > 0:
                    self.stats["pendientes_con_propietario"] -= 1
                if nuevo_estado == "COMPLETADO_CIUDADANO" and prev_est != "COMPLETADO_CIUDADANO":
                    self.stats["completados_ciudadano"] = self.stats.get("completados_ciudadano", 0) + 1
                elif nuevo_estado == "INCONSISTENCIA_PROPIETARIO" and prev_est != "INCONSISTENCIA_PROPIETARIO":
                    self.stats["inconsistencias"] = self.stats.get("inconsistencias", 0) + 1
                if tiene_emb != "NO":
                    self.stats["con_embargo_alerta"] = self.stats.get("con_embargo_alerta", 0) + 1

                self.kpi_vars["pendientes"].set(str(self.stats.get("pendientes_con_propietario", 0)))
                self.kpi_vars["completados"].set(str(self.stats.get("completados_ciudadano", 0)))
                self.kpi_vars["embargos"].set(str(self.stats.get("con_embargo_alerta", 0)))
                self.kpi_vars["inconsistencias"].set(str(self.stats.get("inconsistencias", 0)))

                if hasattr(self, "tree") and self.tree.winfo_exists() and self.tree.exists(str(idx)):
                    tag = nuevo_estado
                    if "SI" in str(tiene_emb).upper() or "EMBARGO" in str(det_lim).upper() or "HURTO" in str(det_lim).upper():
                        tag = "EMBARGO"
                    emb_display = det_lim if det_lim and det_lim != "NINGUNO" else tiene_emb
                    self.tree.item(
                        str(idx),
                        values=(
                            v["placa"],
                            v["tipo_doc_runt"],
                            v["nro_doc"],
                            v["propietario"],
                            nuevo_estado,
                            emb_display,
                            f"{num_imgs} img"
                        ),
                        tags=(tag,)
                    )

        self.root.after(0, _update_ui_fast)

    def _load_patios(self):
        if not self._ensure_authenticated():
            return
        patios = scanner_patios.list_patios()
        self.cmb_patio["values"] = patios
        if patios:
            default_p = next((p for p in patios if "FABRICATO" in p.upper()), patios[0])
            self.cmb_patio.set(default_p)
            self._on_patio_selected()

    def _on_patio_selected(self, event=None):
        if not self._ensure_authenticated():
            return
        patio = self.cmb_patio.get()
        anios = scanner_patios.list_anios(patio)
        self.cmb_anio["values"] = anios
        if anios:
            default_a = next((a for a in anios if "2024" in a), anios[0])
            self.cmb_anio.set(default_a)
            self._scan_current_selection()

    def _on_anio_selected(self, event=None):
        if not self._ensure_authenticated():
            return
        self._scan_current_selection()

    def _scan_current_selection(self):
        if not self._ensure_authenticated():
            return
        if not hasattr(self, "cmb_patio") or not self.cmb_patio.winfo_exists():
            return
        patio = self.cmb_patio.get()
        anio = self.cmb_anio.get()
        if not patio or not anio:
            return
        self.log_message(f"⏳ Escaneando {patio} -> {anio}...")

        def _bg_scan():
            res = scanner_patios.scan_patio_anio(patio, anio)

            def _on_scan_done():
                if not hasattr(self, "tree") or not self.tree.winfo_exists():
                    return
                self.vehicles = res["vehicles"]
                self.stats = res["stats"]

                self.kpi_vars["total"].set(str(self.stats.get("total", 0)))
                self.kpi_vars["pendientes"].set(str(self.stats.get("pendientes_con_propietario", 0)))
                self.kpi_vars["completados"].set(str(self.stats.get("completados_ciudadano", 0)))
                self.kpi_vars["embargos"].set(str(self.stats.get("con_embargo_alerta", 0)))
                self.kpi_vars["inconsistencias"].set(str(self.stats.get("inconsistencias", 0)))
                self.kpi_vars["sin_prop"].set(str(self.stats.get("sin_propietario_constancia", 0)))

                self._populate_tree()
                self.log_message(
                    f"Escaneo listo: {self.stats.get('pendientes_con_propietario', 0)} pendientes | "
                    f"{self.stats.get('completados_ciudadano', 0)} completados | "
                    f"{self.stats.get('sin_propietario_constancia', 0)} sin propietario."
                )

            self.root.after(0, _on_scan_done)

        threading.Thread(target=_bg_scan, daemon=True).start()

    def _populate_tree(self):
        if not hasattr(self, "tree") or not self.tree.winfo_exists():
            return
        for item in self.tree.get_children():
            self.tree.delete(item)

        mode = self.filter_var.get()
        for idx, v in enumerate(self.vehicles):
            est = v["estado"]
            emb = str(v.get("tiene_embargo") or "NO")
            det = str(v.get("detalle_limitacion") or "")

            if mode == "SOLO_PENDIENTES" and est != "PENDIENTE":
                continue
            if mode == "PENDIENTES_Y_RECIENTES" and est == "SIN_PROPIETARIO_CONSTANCIA":
                continue
            if mode == "SOLO_EMBARGOS" and ("SI" not in emb.upper() and "PRENDA" not in emb.upper()):
                continue

            tag = est
            if "SI" in emb.upper() or "EMBARGO" in det.upper() or "HURTO" in det.upper():
                tag = "EMBARGO"

            emb_display = det if det and det != "NINGUNO" else emb
            self.tree.insert(
                "", tk.END, iid=str(idx),
                values=(
                    v["placa"],
                    v["tipo_doc_runt"],
                    v["nro_doc"],
                    v["propietario"],
                    est,
                    emb_display,
                    f"{v['num_imgs']} img"
                ),
                tags=(tag,)
            )

    def _on_tree_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        self.current_index = int(sel[0])

    def _open_selected_expediente(self):
        if not self._ensure_authenticated():
            return
        sel = self.tree.selection()
        idx = int(sel[0]) if sel else self.current_index
        if idx is None or idx >= len(self.vehicles):
            messagebox.showinfo("Abrir Carpeta", "Selecciona primero un vehículo en la tabla.")
            return
        exp_dir = self.vehicles[idx]["exp_dir"]
        os.makedirs(exp_dir, exist_ok=True)
        subprocess.Popen(["explorer", exp_dir])

    def _open_report_generator_dialog(self):
        if not self._ensure_authenticated():
            return

        patio_actual = self.cmb_patio.get() if hasattr(self, "cmb_patio") else ""
        anio_actual = self.cmb_anio.get() if hasattr(self, "cmb_anio") else ""

        dlg = tk.Toplevel(self.root)
        dlg.title("Generar Reporte Consolidado desde Base de Datos SQLite")
        dlg.geometry("540x430")
        dlg.configure(bg="#1e293b")
        dlg.transient(self.root)
        dlg.grab_set()

        frame = tk.Frame(dlg, bg="#1e293b", padx=24, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            frame,
            text="📊 GENERADOR DE REPORTES DESDE SQLITE",
            bg="#1e293b", fg="#38bdf8",
            font=("Segoe UI", 13, "bold")
        ).pack(anchor="w", pady=(0, 6))

        tk.Label(
            frame,
            text="Sincroniza los expedientes y extrae toda la información de la base de datos\n(progreso_consultas.db) en un libro Excel con 4 hojas detalladas:",
            bg="#1e293b", fg="#cbd5e1",
            font=("Segoe UI", 9), justify="left"
        ).pack(anchor="w", pady=(0, 12))

        bullets = (
            "1. RESUMEN EJECUTIVO BD (Totales y % de avance por Patio y Año)\n"
            "2. BASE DE DATOS VEHÍCULOS (Todas las placas, propietarios, estados y capturas)\n"
            "3. ALERTAS Y EMBARGOS (Solo vehículos con Embargo, Fiscalía, Prenda o Inconsistencia)\n"
            "4. AUDITORÍA SESIONES SQLITE (Historial de accesos y tokens de 60 minutos)"
        )
        lbl_info = tk.Label(
            frame, text=bullets,
            bg="#0f172a", fg="#e2e8f0",
            font=("Consolas", 8), justify="left", padx=12, pady=10
        )
        lbl_info.pack(fill=tk.X, pady=(0, 14))

        tk.Label(
            frame, text="Selecciona el alcance del reporte a consultar en SQLite:",
            bg="#1e293b", fg="#f8fafc", font=("Segoe UI", 10, "bold")
        ).pack(anchor="w", pady=(0, 6))

        scope_var = tk.StringVar(value="PATIO")

        opts = [
            (f"Todos los Años del Patio Actual ({patio_actual})", "PATIO"),
            (f"Solo el Año Seleccionado ({patio_actual} - {anio_actual})", "ANIO"),
            ("Toda la Base de Datos Completa (Todos los Patios y Años)", "TODOS")
        ]
        for txt, val in opts:
            tk.Radiobutton(
                frame, text=txt, variable=scope_var, value=val,
                bg="#1e293b", fg="#f8fafc", selectcolor="#0f172a",
                activebackground="#1e293b", activeforeground="#38bdf8",
                font=("Segoe UI", 9)
            ).pack(anchor="w", pady=2)

        lbl_status = tk.Label(
            frame, text="Listo para exportar desde SQLite.",
            bg="#1e293b", fg="#4ade80", font=("Segoe UI", 9, "bold")
        )
        lbl_status.pack(anchor="w", pady=(10, 8))

        btn_row = tk.Frame(frame, bg="#1e293b")
        btn_row.pack(fill=tk.X, pady=(4, 0))

        def _run_export(choose_custom_path=False):
            if not self._ensure_authenticated():
                dlg.destroy()
                return

            sc = scope_var.get()
            custom_out = None
            if choose_custom_path:
                custom_out = filedialog.asksaveasfilename(
                    parent=dlg,
                    title="Guardar Reporte Excel de Base de Datos SQLite",
                    defaultextension=".xlsx",
                    filetypes=[("Libro de Excel (*.xlsx)", "*.xlsx")],
                    initialfile=f"REPORTE_RUNT_SQLITE_{sc}.xlsx"
                )
                if not custom_out:
                    return

            lbl_status.configure(text="⏳ Sincronizando y consultando tablas SQLite, por favor espera...", fg="#facc15")
            dlg.update_idletasks()

            def _bg_task():
                try:
                    out_file, summary = scanner_patios.generate_sqlite_excel_report(
                        output_path=custom_out,
                        scope=sc,
                        patio=patio_actual,
                        anio=anio_actual,
                        log_cb=self.log_message
                    )
                    if self.session:
                        auth_manager.registrar_evento(
                            self.session.get("username", "usuario"),
                            "REPORTE_GENERADO",
                            f"Alcance: {sc} | Total BD: {summary['total_vehiculos']} | Archivo: {os.path.basename(out_file)}"
                        )

                    def _on_done():
                        self.log_message(f"📊 Reporte BD generado exitosamente: {out_file}")
                        if dlg.winfo_exists():
                            lbl_status.configure(
                                text=f"✅ ¡Reporte creado! ({summary['total_vehiculos']} vehículos en BD)",
                                fg="#4ade80"
                            )
                        msg_res = (
                            f"El reporte consolidado fue generado directamente desde la Base de Datos SQLite:\n\n"
                            f"• Archivo: {out_file}\n\n"
                            f"RESUMEN EN BASE DE DATOS:\n"
                            f"• Total vehículos: {summary['total_vehiculos']}\n"
                            f"• Completados RUNT Ciudadano: {summary['completados']}\n"
                            f"• Con Embargo / Limitación: {summary['embargos']}\n"
                            f"• Inconsistencia Propietario: {summary['inconsistencias']}\n"
                            f"• Sin Propietario (Ley 1730): {summary['sin_propietario']}\n"
                            f"• Pendientes por consultar: {summary['pendientes']}\n"
                            f"• Capturas registradas: {summary['total_capturas']}\n\n"
                            f"¿Deseas abrir el archivo Excel ahora mismo?"
                        )
                        parent_win = dlg if dlg.winfo_exists() else self.root
                        if messagebox.askyesno("Reporte SQLite Generado", msg_res, parent=parent_win):
                            try:
                                os.startfile(out_file)
                            except Exception:
                                subprocess.Popen(["explorer", os.path.dirname(out_file)])
                        if dlg.winfo_exists():
                            dlg.destroy()

                    self.root.after(0, _on_done)
                except Exception as ex:
                    def _on_err():
                        if dlg.winfo_exists():
                            lbl_status.configure(text=f"❌ Error: {ex}", fg="#f87171")
                        messagebox.showerror("Error al Generar Reporte", str(ex), parent=dlg if dlg.winfo_exists() else self.root)
                    self.root.after(0, _on_err)

            threading.Thread(target=_bg_task, daemon=True).start()

        btn_gen_auto = tk.Button(
            btn_row, text="📥 Generar y Abrir Excel Ahora",
            bg="#16a34a", fg="#ffffff", font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=14, pady=8, cursor="hand2",
            command=lambda: _run_export(choose_custom_path=False)
        )
        btn_gen_auto.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        btn_gen_saveas = tk.Button(
            btn_row, text="💾 Guardar Como...",
            bg="#0284c7", fg="#ffffff", font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=12, pady=8, cursor="hand2",
            command=lambda: _run_export(choose_custom_path=True)
        )
        btn_gen_saveas.pack(side=tk.LEFT, padx=(0, 6))

        def _open_reports_folder():
            os.makedirs(scanner_patios.REPORTS_DIR, exist_ok=True)
            subprocess.Popen(["explorer", scanner_patios.REPORTS_DIR])

        btn_open_rep_dir = tk.Button(
            btn_row, text="📂 Carpeta Reportes",
            bg="#475569", fg="#ffffff", font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, padx=10, pady=8, cursor="hand2",
            command=_open_reports_folder
        )
        btn_open_rep_dir.pack(side=tk.RIGHT)

    # =========================================================================
    # 5. CONTROLES DE EJECUCIÓN EN MODO RÁFAGA PARALELO E INDIVIDUAL
    # =========================================================================
    def _retry_with_doc_type(self, new_doc_type):
        if not self._ensure_authenticated():
            return
        sel = self.tree.selection()
        idx = int(sel[0]) if sel else self.current_index
        if idx is None or idx >= len(self.vehicles):
            return
        self.vehicles[idx]["tipo_doc_runt"] = new_doc_type
        self._populate_tree()
        self.tree.selection_set(str(idx))
        self.cmd_queue.put(("SINGLE", idx))

    def _run_selected_single(self):
        if not self._ensure_authenticated():
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Consultar Placa", "Selecciona un vehículo de la tabla para consultarlo.")
            return
        idx = int(sel[0])
        self.is_paused = False
        if self.cluster:
            self.cluster.is_paused = False
        self.cmd_queue.put(("SINGLE", idx))

    def _start_batch_process(self):
        if not self._ensure_authenticated():
            return
        num_ch = self._get_selected_num_channels()
        sel = self.tree.selection()
        start_idx = int(sel[0]) if sel else 0

        self.is_running_batch = True
        self.is_paused = False
        if self.cluster:
            self.cluster.is_paused = False
        self.btn_pause.configure(text="⏸ Pausar Ráfaga", bg="#d97706")
        self.cmd_queue.put(("START_PARALLEL_BATCH", (start_idx, num_ch)))

    def _toggle_pause(self):
        if not self._ensure_authenticated():
            return
        self.is_paused = not self.is_paused
        if self.cluster:
            self.cluster.is_paused = self.is_paused
        if self.is_paused:
            self.log_message("⏸ Ráfaga en pausa. Deteniendo canales activos...")
            self.btn_pause.configure(text="▶ Reanudar Ráfaga", bg="#16a34a")
            for sid in range(self.MAX_CHANNELS):
                if self.cluster:
                    self.cluster.route_captcha_to_slot(sid, "__CANCEL__")
        else:
            self.log_message("▶ Reanudando Modo Ráfaga Multi-Pestaña...")
            self.btn_pause.configure(text="⏸ Pausar Ráfaga", bg="#d97706")
            num_ch = self._get_selected_num_channels()
            self.cmd_queue.put(("START_PARALLEL_BATCH", (0, num_ch)))

    def _async_worker_thread_entry(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._async_worker_loop())

    async def _async_worker_loop(self):
        self.cluster = RuntParallelCluster(
            log_cb=self.log_message,
            ui_slot_cb=self._on_slot_state_update,
            on_vehicle_done_cb=self._on_vehicle_completed_from_cluster,
            check_auth_fn=self._ensure_authenticated
        )

        while True:
            try:
                cmd, payload = await asyncio.get_event_loop().run_in_executor(None, self.cmd_queue.get)
                if cmd == "CLOSE_BROWSER":
                    await self.cluster.stop_cluster()
                    break
                if not self._ensure_authenticated():
                    continue

                if cmd == "SINGLE":
                    idx = payload
                    if 0 <= idx < len(self.vehicles):
                        await self.cluster.start_cluster(1)
                        await self.cluster.process_vehicle_on_slot(self.cluster.slots[0], idx, self.vehicles[idx])

                elif cmd == "START_PARALLEL_BATCH":
                    start_idx, num_ch = payload
                    # Tomar primero desde el vehículo seleccionado hacia adelante, y luego los pendientes anteriores si quedaron
                    pending_list = [
                        (i, v) for i, v in enumerate(self.vehicles)
                        if i >= start_idx and v["estado"] == "PENDIENTE"
                    ]
                    if not pending_list:
                        pending_list = [
                            (i, v) for i, v in enumerate(self.vehicles)
                            if v["estado"] == "PENDIENTE"
                        ]
                    if not pending_list:
                        self.log_message("ℹ No hay vehículos pendientes por consultar en este año.")
                        continue

                    await self.cluster.run_batch_parallel(pending_list, num_channels=num_ch)
                    self.is_running_batch = False

            except Exception as e:
                self.log_message(f"Error en orquestador paralelo: {e}")
                self.is_running_batch = False

    def _on_close_window(self):
        self.is_paused = True
        self.is_running_batch = False
        if self.cluster:
            self.cluster.is_paused = True
            self.cluster.stop_requested = True
        if self.session and self.session.get("token"):
            auth_manager.logout_session(self.session["token"], "CIERRE_VENTANA_APLICACION")
        auth_manager.invalidate_all_active_sessions("CIERRE_TOTAL_APLICACION")
        try:
            self.cmd_queue.put_nowait(("CLOSE_BROWSER", None))
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = AutomatizadorRuntGUI(root)
    root.mainloop()
